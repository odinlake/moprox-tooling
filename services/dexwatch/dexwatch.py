#!/usr/bin/env python3
"""dexwatch — keep the shared calendar in step with Dexters' viewing emails for 10 Croft Road.

Asked for 2026-11-05: "a service that checks my inbox frequently for new emails - anything from
dexters should be passed to an agent. Agent should determine if it's viewing related and keep my
calendar synchronized. Add entries for new viewings or modify when they change. Don't delete but
prefix (canc) in title if canceled. Use Onsjoe shared calendar and assume it takes 30 min. Post
brief notes on telegram. If not viewing related, use judgement and post notes about anything
nontrivial non-advertising to telegram too."

Each run (dexwatch.timer, every 5 min):
  1. lists Gmail for `from:dexters` (this also catches "Dexters Wimbledon Sales via Docusign") and
     takes the messages it has not handled yet, oldest first;
  2. hands each one, with the viewings already in the calendar, to one `claude -p` (Max
     subscription, never the API). The agent decides whether it is about a viewing and which
     calendar change, if any, follows, and writes a short Telegram note;
  3. applies the change ITSELF. The agent never touches the calendar, so the operator's rules hold
     in code: events are never deleted, a cancelled viewing gets "(canc) " prefixed to its title,
     every viewing is 30 min, and an event the agent names must be one this run showed it;
  4. posts the agent's note plus one line per calendar change, as #valet (so a swipe-reply reaches
     valet, which has the mail and calendar tools).

A message is marked handled only once the calendar change succeeded. A calendar failure retries next
run; creating twice is prevented by the same-start guard in apply(). Neither retry is unbounded: a
run re-asks the agent from scratch, so if the agent cannot answer, OR the calendar change cannot be
made, MAX_FAILS times running, the message is posted raw and marked handled rather than re-spending
one `claude -p` every run forever. Both give-up paths share the one per-message failure counter.

Auth: the service account at ~/.config/claude-dev/google-sa.json with domain-wide delegation,
impersonating the operator: gmail.readonly and calendar.events (both already authorized in DWD).

    dexwatch.py                 one pass (what the timer runs)
    dexwatch.py --dry-run [--days N]
                                judge Dexters mail from the last N days (default 3), print the
                                verdicts and planned changes; write nothing, send nothing
"""
import argparse, base64, datetime, html, json, os, re, subprocess, sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
import errlog  # noqa: E402

KEYF = os.path.expanduser("~/.config/claude-dev/google-sa.json")
SUBJECT = "mikael@odinlake.net"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CAL_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
# "Onsjö shared" (owner: the operator).
CAL = os.environ.get("DEXWATCH_CAL",
                     "c_bd007d3df8d01e1e73e3f0c3a5a9a16bbdeebba0a411a8be9636f5f8caa6f515@group.calendar.google.com")
QUERY = "from:dexters"
STATE = Path(os.environ.get("DEXWATCH_STATE", Path.home() / ".local/state/dexwatch/state.json"))
CLAUDE = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
TZ = ZoneInfo("Europe/London")
DURATION = datetime.timedelta(minutes=30)
ADDRESS = "10 Croft Road, Wimbledon, London SW19 2NF"
CANC = "(canc) "
COLOR = "7"                      # Peacock (cyan): the least-used colour across the operator's calendars
TAG = "dexwatch"                     # extendedProperties.private key on every event we create
AGENT = "valet"
MAX_FAILS = 3
FIRST_RUN_WINDOW = datetime.timedelta(hours=24)   # with no state, older mail counts as already seen
KEEP_SEEN = datetime.timedelta(days=60)


# ---------- Google ----------

def _svc(api, ver, scopes):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(KEYF, scopes=scopes, subject=SUBJECT)
    return build(api, ver, credentials=creds, cache_discovery=False)


def gmail():
    return _svc("gmail", "v1", GMAIL_SCOPES)


def calendar():
    return _svc("calendar", "v3", CAL_SCOPES)


def list_mail(g, days):
    out, tok = [], None
    while True:
        r = g.users().messages().list(userId="me", q="%s newer_than:%dd" % (QUERY, days),
                                      maxResults=100, pageToken=tok).execute()
        out += [m["id"] for m in r.get("messages", [])]
        tok = r.get("nextPageToken")
        if not tok:
            return out


def _walk(p):
    if p.get("parts"):
        for q in p["parts"]:
            yield from _walk(q)
    else:
        yield p


def to_text(payload):
    parts = list(_walk(payload))
    for want in ("text/plain", "text/html"):
        for p in parts:
            if p.get("mimeType") == want and p.get("body", {}).get("data"):
                t = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
                if want == "text/html":
                    t = re.sub(r"(?is)<(style|script).*?</\1>", "", t)
                    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
                    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
                t = re.sub(r"[ \t\xa0]+", " ", t)
                return re.sub(r"\s*\n\s*(\n\s*)+", "\n", t).strip()
    return ""


def fetch(g, mid):
    x = g.users().messages().get(userId="me", id=mid, format="full").execute()
    h = {i["name"].lower(): i["value"] for i in x["payload"].get("headers", [])}
    att = [p.get("filename") for p in _walk(x["payload"]) if p.get("filename")]
    return {"id": mid, "thread": x.get("threadId"), "ts": int(x.get("internalDate", 0)) / 1000,
            "from": h.get("from", ""), "to": h.get("to", ""), "subject": h.get("subject", ""),
            "date": h.get("date", ""), "body": to_text(x["payload"]), "attachments": att,
            "link": "https://mail.google.com/mail/u/0/#all/%s" % x.get("threadId")}


def viewings(cal, now):
    """Viewing events on the shared calendar: ours (tagged) plus any hand-made one titled viewing."""
    t0 = (now - datetime.timedelta(days=30)).isoformat()
    t1 = (now + datetime.timedelta(days=365)).isoformat()
    seen = {}
    for kw in ({"privateExtendedProperty": "%s=viewing" % TAG}, {"q": "viewing"}):
        tok = None
        while True:
            r = cal.events().list(calendarId=CAL, timeMin=t0, timeMax=t1, singleEvents=True,
                                  orderBy="startTime", maxResults=250, pageToken=tok, **kw).execute()
            for ev in r.get("items", []):
                if ev.get("status") != "cancelled" and ev.get("start", {}).get("dateTime"):
                    seen[ev["id"]] = ev
            tok = r.get("nextPageToken")
            if not tok:
                break
    return sorted(seen.values(), key=lambda e: e["start"]["dateTime"])


# ---------- the agent ----------

PROMPT = """You keep a calendar of house viewings in sync with estate-agent emails. Mikael Onsjoe (the
operator) is selling his house, 10 Croft Road, Wimbledon SW19 2NF, through Dexters Wimbledon. Buyers
view it while Mikael is not there; Dexters email him to confirm, move or cancel viewings, and also
about offers, feedback, paperwork and so on.

Now (Europe/London): {now}

Viewings already in the calendar (id | start, Europe/London | title):
{existing}

The email, received {received}:
From: {sender}
Subject: {subject}
Attachments: {attachments}
---
{body}
---

Decide:
1. viewing_related: is this email about a viewing of the house (booking, confirmation, time change,
   cancellation, or feedback after one)?
2. actions: the calendar changes it calls for, compared with the viewings listed above.
   - "create" a viewing that is not in the calendar yet.
   - "update" when an existing viewing (same viewer) moves to a new time, or when a viewing marked
     (canc) is booked again. Give the event id and the new start.
   - "cancel" an existing viewing the email cancels. Give its event id.
   - Nothing (an empty list) for a re-confirmation of a viewing already listed at that time, for
     feedback, and for anything that is not a viewing.
   Dates in these emails are UK style (07/11 = 7 November). With no year, take the first such date
   on or after the date the email was received. Times are UK local time.
3. telegram: a brief note to Mikael, one or two short lines, plain words, no em dashes. For a viewing:
   who, when and what changed. For viewing feedback: the gist of the feedback. For anything else from
   Dexters, use judgement: note it only if it is nontrivial and not advertising or a newsletter
   (an offer, a price question, a request for a decision or document, a problem). A DocuSign mail
   whose subject starts "Completed:" means everyone has signed: say so in a few words, and do not
   ask Mikael to sign it.
   Use "" when nothing is worth posting. Do not mention the calendar; that is added separately.

Reply with ONLY minified JSON, no prose or code fences:
{{"viewing_related":true|false,"actions":[{{"op":"create","start":"YYYY-MM-DDTHH:MM","viewer":"<name as written>","agent":"<Dexters person who sent it>"}},{{"op":"update","event_id":"<id>","start":"YYYY-MM-DDTHH:MM","viewer":"<name>"}},{{"op":"cancel","event_id":"<id>"}}],"telegram":"<note or empty>"}}"""


def fmt_existing(evs):
    if not evs:
        return "(none)"
    return "\n".join("%s | %s | %s" % (e["id"], local(e["start"]["dateTime"]).strftime("%a %d %b %Y %H:%M"),
                                       e.get("summary", "")) for e in evs)


def local(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def ask(msg, evs, now):
    prompt = PROMPT.format(now=now.astimezone(TZ).strftime("%a %d %b %Y %H:%M"), existing=fmt_existing(evs),
                           received=datetime.datetime.fromtimestamp(msg["ts"], TZ).strftime("%a %d %b %Y %H:%M"),
                           sender=msg["from"], subject=msg["subject"],
                           attachments=", ".join(msg["attachments"]) or "-", body=msg["body"][:6000])
    r = subprocess.run([CLAUDE, "-p", "--model", "sonnet", prompt], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=300, cwd=str(STATE.parent))
    v = parse(r.stdout or "")
    if v is None:
        raise RuntimeError("exit %s, no verdict in %r; stderr: %s"
                           % (r.returncode, (r.stdout or "")[:200], (r.stderr or "")[:200]))
    return v


def parse(out):
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(out[i:j + 1])
    except ValueError:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("viewing_related"), bool):
        return None
    if not isinstance(v.get("actions", []), list) or not isinstance(v.get("telegram", ""), str):
        return None
    v.setdefault("actions", [])
    v.setdefault("telegram", "")
    return v


# ---------- applying the verdict (the operator's rules live here) ----------

def when(ev):
    return local(ev["start"]["dateTime"]).strftime("%a %-d %b %H:%M")


def parse_start(s, now):
    """A naive 'YYYY-MM-DDTHH:MM' in UK time -> aware datetime, or None if absurd."""
    try:
        d = datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    d = d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
    if not (now - datetime.timedelta(days=2) <= d <= now + datetime.timedelta(days=365)):
        return None
    return d


def title_for(viewer):
    """The operator's own style for these entries: "House viewing: Mr Stewart (Hawes & Co)"."""
    return "House viewing: %s (Dexters)" % viewer if viewer else "House viewing (Dexters)"


def body_for(a, msg):
    lines = ["Dexters viewing at %s." % ADDRESS]
    if a.get("viewer"):
        lines.append("Viewer: %s" % a["viewer"])
    if a.get("agent"):
        lines.append("Dexters: %s" % a["agent"])
    lines += ["", "From the email \"%s\" (%s)" % (msg["subject"], msg["date"]), msg["link"],
              "", "Kept in sync by dexwatch; 30 min is an assumed length."]
    return "\n".join(lines)


def apply(cal, verdict, evs, msg, now, dry=False):
    """Carry out the agent's actions. Returns human lines for Telegram. Raises on a calendar error."""
    known = {e["id"]: e for e in evs}
    done = []
    for a in verdict.get("actions", []):
        op = a.get("op")
        if op == "create":
            st = parse_start(a.get("start"), now)
            if not st:
                errlog.warn("dexwatch: ignoring create with bad start %r (msg %s)" % (a.get("start"), msg["id"]))
                continue
            dup = next((e for e in known.values() if local(e["start"]["dateTime"]) == st), None)
            if dup:
                if dup.get("summary", "").startswith(CANC.strip()):
                    a = dict(a, op="update", event_id=dup["id"])     # rebooked into a cancelled slot
                else:
                    continue                                         # already there: never twice
            else:
                viewer = (a.get("viewer") or "").strip()
                ev = {"summary": title_for(viewer),
                      "location": ADDRESS, "description": body_for(a, msg),
                      "start": {"dateTime": st.isoformat(), "timeZone": "Europe/London"},
                      "end": {"dateTime": (st + DURATION).isoformat(), "timeZone": "Europe/London"},
                      "colorId": COLOR,
                      "extendedProperties": {"private": {TAG: "viewing", "msg": msg["id"]}}}
                if not dry:
                    ev = cal.events().insert(calendarId=CAL, body=ev).execute()
                else:
                    ev["id"] = "dry-%d" % len(known)
                known[ev["id"]] = ev
                done.append("Calendar: added %s, %s" % (when(ev), ev["summary"]))
                continue
        if a.get("op") == "update":
            ev = known.get(a.get("event_id"))
            st = parse_start(a.get("start"), now)
            if not ev or not st:
                errlog.warn("dexwatch: ignoring update %r (unknown event or bad start; msg %s)" % (a, msg["id"]))
                continue
            title = ev.get("summary") or title_for("")
            if title.startswith(CANC):
                title = title[len(CANC):]
            if a.get("viewer") and a["viewer"] not in title:
                title = title_for(a["viewer"].strip())
            if local(ev["start"]["dateTime"]) == st and title == ev.get("summary"):
                continue
            old = when(ev)
            patch = {"summary": title, "colorId": COLOR,
                     "start": {"dateTime": st.isoformat(), "timeZone": "Europe/London"},
                     "end": {"dateTime": (st + DURATION).isoformat(), "timeZone": "Europe/London"},
                     "description": (ev.get("description") or "") +
                                    "\n\nChanged from %s per \"%s\" (%s)\n%s" % (old, msg["subject"], msg["date"], msg["link"])}
            if not dry:
                ev = dict(ev, **cal.events().patch(calendarId=CAL, eventId=ev["id"], body=patch).execute())
            else:
                ev = dict(ev, **patch)
            known[ev["id"]] = ev
            done.append("Calendar: moved %s to %s, %s" % (old, when(ev), title) if old != when(ev)
                        else "Calendar: rebooked %s, %s" % (when(ev), title))
        elif a.get("op") == "cancel":
            ev = known.get(a.get("event_id"))
            if not ev:
                errlog.warn("dexwatch: ignoring cancel of unknown event %r (msg %s)" % (a.get("event_id"), msg["id"]))
                continue
            title = ev.get("summary", "")
            if title.startswith(CANC):
                continue
            patch = {"summary": CANC + title,
                     "description": (ev.get("description") or "") +
                                    "\n\nCancelled per \"%s\" (%s)\n%s" % (msg["subject"], msg["date"], msg["link"])}
            if not dry:
                ev = dict(ev, **cal.events().patch(calendarId=CAL, eventId=ev["id"], body=patch).execute())
            else:
                ev = dict(ev, **patch)
            known[ev["id"]] = ev
            done.append("Calendar: cancelled %s, %s" % (when(ev), title))
    return done


def note(verdict, done, msg):
    text = (verdict.get("telegram") or "").strip()
    if not text and not done:
        return None
    if not text:
        text = msg["subject"]
    return "Dexters: " + "\n".join([text] + done)


# ---------- state + main ----------

def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return None
    except ValueError as e:
        errlog.err("dexwatch: state unreadable, starting from the first-run window", e)
        return None


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True))
    tmp.replace(STATE)


def run_once():
    import tg
    now = datetime.datetime.now(datetime.timezone.utc)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    st = load()
    first = st is None
    st = st or {"seen": {}, "fails": {}}
    g, cal = gmail(), calendar()
    ids = [i for i in list_mail(g, 7) if i not in st["seen"]]
    msgs = sorted((fetch(g, i) for i in ids), key=lambda m: m["ts"])
    for m in msgs:
        if first and m["ts"] < (now - FIRST_RUN_WINDOW).timestamp():
            st["seen"][m["id"]] = m["ts"]
            continue
        evs = viewings(cal, now)
        try:
            v = ask(m, evs, now)
        except Exception as e:   # noqa: BLE001 — any agent failure is retried, then surfaced
            n = st["fails"][m["id"]] = st["fails"].get(m["id"], 0) + 1
            if n < MAX_FAILS:
                errlog.warn("dexwatch: agent failed (%d/%d) on %r" % (n, MAX_FAILS, m["subject"]), e)
                save(st)
                continue
            errlog.err("dexwatch: agent failed %d times on %r, posting it raw" % (n, m["subject"]), e)
            tg.send("Dexters: I could not read this one, please check it: \"%s\" from %s\n%s"
                    % (m["subject"], m["from"], m["link"]), agent=AGENT)
            st["seen"][m["id"]] = m["ts"]
            st["fails"].pop(m["id"], None)
            save(st)
            continue
        try:
            done = apply(cal, v, evs, m, now)
        except Exception as e:   # noqa: BLE001 — retried, then surfaced; the same-start guard makes a retry safe
            n = st["fails"][m["id"]] = st["fails"].get(m["id"], 0) + 1
            if n < MAX_FAILS:
                errlog.err("dexwatch: calendar change failed (%d/%d) for %r, will retry"
                           % (n, MAX_FAILS, m["subject"]), e)
                save(st)
                continue
            errlog.err("dexwatch: calendar change failed %d times for %r, posting it raw"
                       % (n, m["subject"]), e)
            st["seen"][m["id"]] = m["ts"]
            st["fails"].pop(m["id"], None)
            save(st)   # BEFORE the post: a Telegram failure must not re-spend the agent next run
            tg.send("Dexters: I could not put this in the calendar, please check it: \"%s\" from %s\n%s"
                    % (m["subject"], m["from"], m["link"]), agent=AGENT)
            continue
        text = note(v, done, m)
        print("dexwatch: %s | viewing=%s | %s | %s" % (m["subject"], v["viewing_related"], done, text))
        if text:
            tg.send(text, agent=AGENT)
        st["seen"][m["id"]] = m["ts"]
        st["fails"].pop(m["id"], None)
        save(st)
    cutoff = (now - KEEP_SEEN).timestamp()
    st["seen"] = {k: t for k, t in st["seen"].items() if t >= cutoff}
    save(st)


def dry_run(days):
    now = datetime.datetime.now(datetime.timezone.utc)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    g, cal = gmail(), calendar()
    evs = viewings(cal, now)
    print("existing viewings:\n" + fmt_existing(evs))
    for m in sorted((fetch(g, i) for i in list_mail(g, days)), key=lambda m: m["ts"]):
        v = ask(m, evs, now)
        done = apply(cal, v, evs, m, now, dry=True)
        print("\n== %s | %s\n   verdict: %s\n   would post: %r" % (m["subject"], m["from"], json.dumps(v), note(v, done, m)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    if a.dry_run:
        dry_run(a.days)
    else:
        run_once()


if __name__ == "__main__":
    main()
