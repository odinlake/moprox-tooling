#!/usr/bin/env python3
"""Telegram attachment capture — turn an inbound document/photo/voice/… into a file on disk that
the agents can actually open.

Why this exists: the poller recorded message TEXT only. A document's `file_id` was dropped on
arrival and Telegram will not replay an update once the offset advances, so "here's the clinical
letter, put it on Drive" reached the agent as an EMPTY message — the agent looked negligent while
the file had in fact been thrown away one layer below it (2026-08-13). Anything the operator hands
the bot has to survive the poll loop.

  message ──> attachments()  ──> getFile ──> download to FILES/<YYYY>/<MM>/<msg_id>-<name>
                                              └─ path recorded in the inbox record's "files"

`recover()` is the escape hatch for anything that arrived while this was blind (or while the poller
was down): a bot may re-forward an old message to itself, and the forward RESPONSE carries a fresh
file_id for the same file — so nothing sent to the chat is truly lost. The forwarded copy is
deleted again straight away so the operator's chat stays clean.

Limits: getFile caps bot downloads at 20 MB. That is a hard Bot-API limit, not something to retry —
it is recorded as an error on the record so the agent can say so instead of finding no file.
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

FILES = Path(os.environ.get("TELEGRAM_FILES", Path.home() / ".local/share/moprox/telegram-files"))
TG_ENV = Path(os.environ.get("TELEGRAM_ENV", Path.home() / ".config/claude-dev/telegram.env"))
API = "https://api.telegram.org/bot%s/%s"
FILE_API = "https://api.telegram.org/file/bot%s/%s"    # getFile gives a path under this, not the API host
MAX_BYTES = 20 * 1024 * 1024          # Bot API getFile ceiling; bigger files cannot be fetched at all

# Ordered: a message carries at most one of these, but `document` wins if Telegram ever sends two.
KINDS = ("document", "photo", "video", "audio", "voice", "video_note", "animation")


def creds():
    tok = chat = None
    for ln in TG_ENV.read_text().splitlines():
        if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
        if ln.startswith("TELEGRAM_CHAT_ID="):  chat = ln.split("=", 1)[1].strip()
    if not tok: raise SystemExit("missing TELEGRAM_BOT_TOKEN")
    return tok, chat


def _api(tok, method, **params):
    url = API % (tok, method) + "?" + urllib.parse.urlencode(params)
    try:
        return json.load(urllib.request.urlopen(url, timeout=45))
    except urllib.error.HTTPError as e:                 # Telegram puts the real reason in the body
        try: return json.load(e)
        except Exception: return {"ok": False, "description": "HTTP %s" % e.code}


def attachments(m):
    """Describe every downloadable file on a message (empty list for a plain text message)."""
    out = []
    for kind in KINDS:
        v = m.get(kind)
        if not v: continue
        if kind == "photo": v = max(v, key=lambda p: p.get("file_size") or 0)   # largest rendition
        out.append({"kind": kind, "file_id": v["file_id"], "uid": v.get("file_unique_id"),
                    "name": v.get("file_name"), "mime": v.get("mime_type"),
                    "size": v.get("file_size")})
    return out


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _dest(att, msg_id, remote_path, ts=None):
    """FILES/<year>/<month>/<msg_id>-<sanitised name>. Named so a human scanning the directory can
    tell what a file is and which chat message it came from."""
    name = att.get("name") or ""
    if not name:                                        # photos/voice have no file_name — build one
        ext = Path(remote_path or "").suffix or {"voice": ".ogg", "photo": ".jpg"}.get(att["kind"], "")
        name = "%s-%s%s" % (att["kind"], att.get("uid") or "file", ext)
    name = _SAFE.sub("-", name).strip("-") or "file"
    day = time.gmtime(ts or time.time())
    d = FILES / time.strftime("%Y/%m", day)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s-%s" % (msg_id, name))
    n = 1
    while p.exists():                                   # never clobber an earlier capture
        p = d / ("%s-%d-%s" % (msg_id, n, name)); n += 1
    return p


def download(tok, att, msg_id, ts=None):
    """Fetch one attachment to disk. Returns the record with "path" set, or "error" explaining why
    not — never a half-written file and never a silent drop."""
    rec = {k: att.get(k) for k in ("kind", "name", "mime", "size", "uid")}
    if (att.get("size") or 0) > MAX_BYTES:
        rec["error"] = "too big for the Bot API (%.1f MB > 20 MB)" % ((att["size"] or 0) / 1048576.0)
        errlog.err("tg_files: %s %r not fetchable" % (att["kind"], att.get("name")), None)
        return rec
    r = _api(tok, "getFile", file_id=att["file_id"])
    if not r.get("ok"):
        rec["error"] = "getFile: %s" % r.get("description")
        errlog.err("tg_files: getFile failed for msg %s" % msg_id, RuntimeError(r.get("description")))
        return rec
    remote = r["result"]["file_path"]
    dest = _dest(att, msg_id, remote, ts)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(FILE_API % (tok, remote), timeout=120) as fh, open(tmp, "wb") as out:
            while True:
                b = fh.read(65536)
                if not b: break
                out.write(b)
        tmp.rename(dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        rec["error"] = "download: %s" % e
        errlog.err("tg_files: downloading msg %s (%s)" % (msg_id, remote), e)
        return rec
    rec["name"] = rec.get("name") or dest.name
    rec["path"] = str(dest)
    rec["size"] = dest.stat().st_size
    return rec


def capture(tok, m):
    """Every attachment on a message, downloaded. [] when there is nothing to fetch."""
    atts = attachments(m)
    if not atts: return []
    return [download(tok, a, m.get("message_id"), m.get("date")) for a in atts]


def describe(files):
    """One-line-per-file summary for a prompt / the conversation log."""
    out = []
    for f in files or []:
        if f.get("error"): out.append("[%s %s — NOT retrieved: %s]" % (f.get("kind"), f.get("name") or "?", f["error"]))
        else: out.append("[%s: %s (%s, %s bytes) saved at %s]"
                         % (f.get("kind"), f.get("name"), f.get("mime") or "?", f.get("size"), f.get("path")))
    return "\n".join(out)


def recover(msg_id, chat_id=None):
    """Re-fetch an attachment from a message that was already polled (or missed entirely).

    A bot cannot read chat history, but it CAN forward a message it received — and the forward
    response is a full Message object carrying a live file_id. Forward to ourselves, capture, then
    delete the forwarded copy so the chat is left as it was."""
    tok, chat = creds()
    chat_id = chat_id or chat
    r = _api(tok, "forwardMessage", chat_id=chat_id, from_chat_id=chat_id,
             message_id=int(msg_id), disable_notification="true")
    if not r.get("ok"):
        raise RuntimeError("forward of msg %s failed: %s" % (msg_id, r.get("description")))
    fwd = r["result"]
    try:
        files = capture(tok, dict(fwd, message_id=int(msg_id)))     # name the file after the ORIGINAL msg
    finally:
        _api(tok, "deleteMessage", chat_id=chat_id, message_id=fwd["message_id"])
    return files


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "recover":
        raise SystemExit("usage: tg_files.py recover <message_id> [<message_id> ...]")
    for mid in sys.argv[2:]:
        print(json.dumps({"msg_id": int(mid), "files": recover(mid)}, indent=1))
