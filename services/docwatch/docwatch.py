#!/usr/bin/env python3
"""docwatch — notice a document dropped in Drive ROOT, index it, file a renamed copy, say what it is.

The operator's habit is to drop scans and downloads straight into Drive root and deal with them
later; the 2026-07 reorg then sorted 3k of them in one batch (~/docindex). This is the incremental
half of that job: the batch pass is what happens once, this is what happens from now on.

WHAT IT DOES, per new file parented in root:
  1. extracts text (reusing ~/docindex/pipeline.py — same extractors, same download cache)
  2. one `claude -p` call returns BOTH the index fields (summary/tags/date/category/entities) and
     the filing decision (proposed name + destination folder)
  3. copies the file, under its proposed name, into that folder
  4. upserts it into docindex.db so mo.lan/docs finds it
  5. Telegrams a digest of what was filed and what it turned out to be

THE ORIGINAL IS LEFT ALONE, BY DESIGN. The operator's workflow is: read the digest, check the filed
copy, delete the original from root himself. So the copy is the renamed artefact and root stays his
inbox until he clears it — which is also why the digest links to both. Deleting an original after
filing is safe: dedup is on the source drive_id in filed.jsonl, which outlives the file.

(Incidentally the service account could not rename it anyway — measured 2026-08-26, the full `drive`
scope returns `unauthorized_client` and `drive.file` covers only files the app created. Noted so
nobody re-tests it, NOT as a gap to close: nothing here wants that scope.)

Drive's changes feed is the trigger — a saved startPageToken, polled. Not a webhook: the estate has
no inbound path from Google, which is the same reason private-web polls GitHub.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/docindex"))
import pipeline                                   # extract/download/con — one source of truth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

KEYF = "/home/mikael/.config/claude-dev/google-sa.json"
SUB = "mikael@odinlake.net"
# drive.file is what lets us CREATE the copy; readonly is what lets us read the source. Full `drive`
# is not authorised for this client — see the module docstring.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/drive.file"]
STATE = Path(os.environ.get("DOCWATCH_STATE", Path.home() / ".local/state/docwatch"))
# ABSOLUTE path, not "claude". systemd gives a unit the default PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin) and the CLI lives in
# ~/.local/bin — so subprocess.run(["claude", ...]) raised FileNotFoundError on every attempt from
# the day this was installed, and decide() swallowed it. Same resolution the loop harness uses.
CLAUDE = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
LOG = STATE / "filed.jsonl"
# A skip consumed the file's CHANGE. Drive will not report it again, so without this the document is
# unfiled for ever and nothing says so — which is exactly what happened to the operator's 42-page
# scan on 2026-08-27: seen once, skipped once, then "nothing new in root" for ever after.
PENDING = STATE / "pending.jsonl"
RETRY_MAX = int(os.environ.get("DOCWATCH_RETRY_MAX", "5"))
FOLDER_MIME = "application/vnd.google-apps.folder"
MAXTXT = 8000
# Credential material must never reach the index, the digest, or a model prompt. This is not
# hypothetical: the July batch pass indexed github-recovery-codes.txt, accounts.txt and two others
# with their FULL TEXT stored in docindex.db and in the FTS table — so the codes are searchable at
# mo.lan/docs and through the docsearch MCP that every agent can call. Same pipeline, same database.
#
# Matching is on the FILENAME only. Reading the content to decide whether content is secret is the
# mistake itself, and a name is what the operator controls when they drop the file.
SECRET_NAME = re.compile(r"recovery[-_ ]?code|backup[-_ ]?code|2fa|otp|seed[-_ ]?phrase|mnemonic|"
                         r"password|passwd|credential|secret|api[-_ ]?key|private[-_ ]?key|"
                         r"id_(rsa|ed25519)|\.pem$|\.p12$|\.pfx$|keystore|vault", re.I)
SECRET_FOLDER = os.environ.get("DOCWATCH_SECRET_FOLDER", "Secrets")
# A run that suddenly wants to file hundreds of files is a signal that something is wrong (a restore,
# a sync loop, a bad page token), not a busy day. Stop and say so rather than reorganising the drive.
SANE_MAX = int(os.environ.get("DOCWATCH_MAX_PER_RUN", "25"))

_svc = None


def drive():
    global _svc
    if _svc is None:
        c = service_account.Credentials.from_service_account_file(KEYF, scopes=SCOPES, subject=SUB)
        _svc = build("drive", "v3", credentials=c, cache_discovery=False)
    return _svc


def state_load():
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def state_save(s):
    (STATE / "state.json").write_text(json.dumps(s, indent=1))


def bucket_of(mime, name):
    """Which extractor pipeline.extract() should use. Mirrors the buckets the batch pass assigned."""
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("application/vnd.google-apps"):
        return "gdoc"
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    if re.search(r"\.(docx?|xlsx?|xlsm|pptx?|odt|ods|odp)$", name or "", re.I):
        return "office"
    return "other"


def taxonomy(root_id):
    """Existing folders, two levels under root -> {"People/Mikael": id}. Read fresh each run: the
    operator adds folders by hand and a stale cache would silently file into the wrong tree."""
    out = {}
    tops = drive().files().list(
        q=f"'{root_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false",
        fields="files(id,name)", pageSize=100).execute().get("files", [])
    for t in tops:
        out[t["name"]] = t["id"]
        subs = drive().files().list(
            q=f"'{t['id']}' in parents and mimeType='{FOLDER_MIME}' and trashed=false",
            fields="files(id,name)", pageSize=200).execute().get("files", [])
        for s in subs:
            out[f"{t['name']}/{s['name']}"] = s["id"]
    return out


# The naming convention is NOT re-derived here — it is the one from ~/docindex/rename2.py, which was
# tuned against operator corrections over 3k files. Changing it here would silently fork the archive
# into two conventions, so it is quoted rather than paraphrased.
RULES = """You normalise personal-archive filenames to the convention:
  <date>_<subject>_<doctype-and-descriptors>.<ext>
- date: the DOCUMENT'S OWN date (issue date for ID docs, written date for letters, statement period,
  etc). Prefer the content's stated date over the filename. Granularity: YYYY-MM is enough for ID
  documents/certificates; use YYYY-MM-DD when the exact day is integral (dated letters, receipts);
  YYYY if that's all that is known. OMIT the field entirely if no date is known — NEVER invent one.
- subject: the person (mikael/rie/akiko/yuko), institution (hsbc/cassini/hmrc/skatteverket...), or
  property the record belongs to; lowercase.
- doctype+descriptors: what the document IS, then qualifiers. PRESERVE every meaning-bearing token
  from the original name (photo, cover-letter, translation, scale, front/back, page1, lowres, draft,
  application, form, receipt...). Dropping such a token CHANGES THE MEANING and is the worst error:
  a "passport photo" is not a "passport". You may rephrase/normalise wording and drop true
  redundancies, but never drop distinguishing content. All lowercase; hyphens inside fields;
  underscores between the three fields; keep the original extension; ASCII only."""


def decide(name, mime, context, is_file, folders):
    """One claude -p call: index fields AND filing decision. One call rather than three because they
    read the same document and disagreeing about what it is would be worse than either answer."""
    listing = "\n".join("  " + f for f in sorted(folders))
    body = (f"[document text follows]\n{context}" if not is_file
            else f"[the document is at {context} — Read it, it is a scan or image]")
    prompt = f"""{RULES}

You are also cataloguing this document for a personal archive index.

Original filename: {name}
MIME: {mime}

Choose a destination folder from EXACTLY this list — never invent one:
{listing}
If nothing fits, or you are not confident what the document is, use "00 Inbox/_needs-review".
Prefer the most specific folder that clearly fits. A document about one person goes under
People/<Name>; something concerning the family or the children jointly goes under Family/...;
anything tied to an address goes under Properties/<address>.

{body}

Reply with ONLY minified JSON, no prose or fences:
{{"summary":"<=240 chars, factual, include key names/dates/numbers/amounts",
 "tags":["5-12 lowercase tags"],
 "entities":["people/orgs named"],
 "doc_date":"YYYY-MM-DD or YYYY-MM or YYYY or empty",
 "category":"{pipeline.CATS}",
 "proposed":"<the convention filename>",
 "folder":"<one path from the list above>",
 "confidence":"high|medium|low"}}"""
    last = "no attempt made"
    for attempt in range(3):
        try:
            r = subprocess.run([CLAUDE, "-p", "--dangerously-skip-permissions", prompt],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
            out = r.stdout or ""
            i, j = out.find("{"), out.rfind("}")
            if i >= 0 and j > i:
                return json.loads(out[i:j + 1])
            last = (f"exit {r.returncode}, no JSON in {len(out)} chars of stdout; "
                    f"stderr: {(r.stderr or '')[:200]}")
        except subprocess.TimeoutExpired:
            last = "timed out after 300 s"
        except Exception as exc:                    # FileNotFoundError, bad JSON, anything
            last = f"{type(exc).__name__}: {exc}"
        errlog.warn(f"docwatch: decide() attempt {attempt + 1}/3 failed for {name!r} — {last}")
        time.sleep(5 * (attempt + 1))
    # Three failures is not a quiet outcome: it means nothing can be filed, for any document. The
    # original code returned None here silently and the only trace was "skipped 1" in the journal
    # with the reason visible nowhere but a Telegram line.
    errlog.err(f"docwatch: decide() gave up on {name!r} after 3 attempts — last: {last}")
    return None


def upsert(f, d, copy_id, dest):
    """Put it in docindex.db so mo.lan/docs can find it. Same columns the batch pass writes; the
    embedding and FTS row are left to the next pipeline run rather than duplicated here."""
    c = pipeline.con()
    c.execute("""INSERT OR REPLACE INTO files
                 (key,drive_id,path,name,mime,bucket,summary,tags,entities,doc_date,category,
                  extracted_text,sampled,status,proposed,error,updated)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'enriched',?,'',?)""",
              (f["key"], f["drive_id"], dest, d["proposed"], f["mime"], f["bucket"],
               d.get("summary", ""), json.dumps(d.get("tags", [])), json.dumps(d.get("entities", [])),
               d.get("doc_date", ""), d.get("category", "other"), (f.get("text") or "")[:MAXTXT], 0,
               f"{dest}/{d['proposed']}", time.strftime("%Y-%m-%dT%H:%M:%S")))
    c.commit()
    c.close()


def notify(filed, skipped):
    if not filed and not skipped:
        return
    lines = [f"**{len(filed)} document(s) filed from Drive root**"]
    for r in filed:
        flag = "" if r["confidence"] == "high" else f" _({r['confidence']} confidence)_"
        # Both links on purpose: the operator's workflow is to check the filed copy and then delete
        # the original himself, so the message has to make both reachable in one tap each.
        lines.append(
            f"\n**{r['proposed']}**{flag}\n{r['folder']}\n{r['summary']}"
            f"\n[filed copy](https://drive.google.com/file/d/{r['copy_id']}/view)"
            f" · [original: {r['old_name']}](https://drive.google.com/file/d/{r['drive_id']}/view)")
    for r in skipped:
        lines.append(f"\n⚠️ **{r['old_name']}** — not filed: {r['why']}")
    lines.append("\n_Originals left in root for you to delete once you've checked the copy._")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
        import tg
        tg.send("\n".join(lines), agent="docwatch")
    except BaseException as e:                      # tg.creds() raises SystemExit without telegram.env
        errlog.err(f"docwatch: notify failed: {type(e).__name__}: {e}")


def pending_load():
    """Files seen before and not yet filed, with an attempt count. Bounded: a document that fails
    RETRY_MAX times is dropped from the retry set rather than retried until the heat death."""
    out = {}
    if PENDING.exists():
        for line in PENDING.read_text().splitlines():
            try:
                r = json.loads(line)
                out[r["id"]] = r
            except Exception as exc:
                errlog.skip("docwatch: parsing pending.jsonl", exc)
    return out


def pending_save(p):
    PENDING.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in p.values()))


def main():
    st = state_load()
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--file="):
            only = a.split("=", 1)[1]
    root_id = drive().files().get(fileId="root", fields="id").execute()["id"]

    if not st.get("page_token"):
        # First run establishes the baseline and files nothing: everything already in root predates
        # the watcher and is the batch pass's business, not ours.
        st["page_token"] = drive().changes().getStartPageToken().execute()["startPageToken"]
        st["root_id"] = root_id
        state_save(st)
        print(f"docwatch: baseline set at page token {st['page_token']}; nothing filed on first run")
        return 0

    seen = set()
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            try:
                seen.add(json.loads(line)["drive_id"])
            except Exception:
                pass

    new, token = [], st["page_token"]
    while token:
        resp = drive().changes().list(
            pageToken=token, pageSize=200, restrictToMyDrive=True,
            fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,parents,trashed))"
        ).execute()
        for ch in resp.get("changes", []):
            f = ch.get("file") or {}
            if ch.get("removed") or f.get("trashed") or f.get("mimeType") == FOLDER_MIME:
                continue
            if root_id not in (f.get("parents") or []):
                continue                            # only root — the operator's drop spot
            if f["id"] in seen:
                continue
            new.append(f)
        token = resp.get("nextPageToken")
        if not token:
            st["page_token"] = resp.get("newStartPageToken", st["page_token"])

    # Re-offer anything seen before and never filed. Their change is long gone from the feed, so
    # this list is the only route back for them.
    pend = pending_load()
    for fid, rec in list(pend.items()):
        if fid in seen or rec.get("tries", 0) >= RETRY_MAX:
            continue
        try:
            new.append(drive().files().get(
                fileId=fid, fields="id,name,mimeType,parents,trashed").execute())
        except HttpError as exc:
            errlog.warn(f"docwatch: pending {fid} is no longer readable ({exc}) — dropping it")
            pend.pop(fid, None)

    if only:                                        # --file=<id>: one document, on demand
        new = [drive().files().get(
            fileId=only, fields="id,name,mimeType,parents,trashed").execute()]

    new = {f["id"]: f for f in new}.values()        # a file edited twice is still one file
    if not new:
        state_save(st)
        print("docwatch: nothing new in root")
        return 0
    if len(new) > SANE_MAX:
        state_save(st)
        errlog.err(f"docwatch: {len(new)} new files in root exceeds the {SANE_MAX} sane-run cap — "
                   f"filing NOTHING. This looks like a restore or a sync loop, not a normal drop. "
                   f"Raise DOCWATCH_MAX_PER_RUN deliberately if it really is a bulk import.")
        return 0

    folders = taxonomy(root_id)
    filed, skipped = [], []
    for f in new:
        fid, name, mime = f["id"], f["name"], f["mimeType"]

        if SECRET_NAME.search(name):
            # Filed, never read. No extract, no model call, no text in the index, nothing quoted in
            # the digest — the whole point is that this document's CONTENT never leaves Drive.
            if SECRET_FOLDER not in folders:
                skipped.append({"drive_id": fid, "old_name": name,
                                "why": f"looks like credential material and there is no "
                                       f"{SECRET_FOLDER!r} folder to file it in — create one"})
                continue
            try:
                cp = drive().files().copy(fileId=fid, body={"name": name,
                                          "parents": [folders[SECRET_FOLDER]]},
                                          fields="id").execute()
            except HttpError as e:
                skipped.append({"drive_id": fid, "old_name": name,
                                "why": f"Drive refused the copy ({getattr(e.resp,'status','?')})"})
                continue
            rec = {"key": f"docwatch:{fid}", "drive_id": fid, "name": name, "mime": mime,
                   "bucket": bucket_of(mime, name), "text": ""}
            upsert(rec, {"proposed": name, "summary": "Credential material — deliberately not read "
                         "or indexed. Filename only.", "tags": ["secret", "not-indexed"],
                         "entities": [], "doc_date": "", "category": "reference",
                         "confidence": "high"}, cp["id"], SECRET_FOLDER)
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "drive_id": fid, "copy_id": cp["id"],
                   "old_name": name, "proposed": name, "folder": SECRET_FOLDER,
                   "summary": "credential material — filed unread", "confidence": "high"}
            with open(LOG, "a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            filed.append(row)
            errlog.warn(f"docwatch: {name!r} filed to {SECRET_FOLDER} UNREAD (name matched the "
                        f"credential pattern). Drive is not a secret store — consider Vaultwarden.")
            continue

        rec = {"key": f"docwatch:{fid}", "drive_id": fid, "name": name, "mime": mime,
               "bucket": bucket_of(mime, name)}
        try:
            context, is_file, _ = pipeline.extract(rec)
        except Exception as e:
            skipped.append({"drive_id": fid, "old_name": name,
                            "why": f"could not extract text ({type(e).__name__})"})
            continue
        rec["text"] = "" if is_file else context
        d = decide(name, mime, context, is_file, folders)
        if not d or not d.get("proposed") or d.get("folder") not in folders:
            skipped.append({"drive_id": fid, "old_name": name,
                            "why": "no usable filing decision returned"})
            continue
        dest = d["folder"]
        try:
            cp = drive().files().copy(fileId=fid,
                                      body={"name": d["proposed"], "parents": [folders[dest]]},
                                      fields="id").execute()
        except HttpError as e:
            skipped.append({"drive_id": fid, "old_name": name,
                            "why": f"Drive refused the copy ({getattr(e.resp,'status','?')})"})
            continue
        upsert(rec, d, cp["id"], dest)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "drive_id": fid, "copy_id": cp["id"],
               "old_name": name, "proposed": d["proposed"], "folder": dest,
               "summary": d.get("summary", ""), "confidence": d.get("confidence", "medium")}
        with open(LOG, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        filed.append(row)

    for r in filed:                                 # filed at last — stop offering it
        pend.pop(r["drive_id"], None)
    for s in skipped:
        fid = s.get("drive_id")
        if fid:
            e = pend.setdefault(fid, {"id": fid, "name": s["old_name"], "tries": 0})
            e["tries"] += 1
            e["last_why"] = s["why"]
    pending_save(pend)

    state_save(st)
    notify(filed, skipped)
    for s in skipped:                               # the digest is not a log; the journal is
        errlog.warn(f"docwatch: skipped {s['old_name']!r} — {s['why']}")
    print(f"docwatch: filed {len(filed)}, skipped {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
