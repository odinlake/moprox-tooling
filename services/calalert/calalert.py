#!/usr/bin/env python3
"""calalert — a loud Telegram alert 2 h before each important appointment in the operator's calendar.

Asked for 2026-10-01: "Calendar events that are mine personal and that have specific time (not all
day) and that aren't recurring and that look like important appointments (medical, PTA, ...) I'd like
a 2h advance notification on telegram with some sort of alert formatting to catch my eye".
Context: an appointment was missed on 2026-09-29, after which remind.py was built for one-off,
hand-added reminders. This is the automatic counterpart; the two can coexist.

Each run (calalert.timer, every 5 min):
  1. lists the operator's PRIMARY calendar for events starting in the next LEAD (2 h). Rie's
     calendar, the shared family one, Polar, holidays and bins are other calendars and never read;
  2. keeps only timed (not all-day), non-recurring, not-declined, ordinary events (no focus time /
     out-of-office / working-location);
  3. asks one `claude -p` (Max subscription, never the API) whether the event looks like an important
     appointment. The verdict is cached per event id + title + start, so an event is judged once;
  4. sends each important one ONCE through tg.py as #reminder, keyed by event id + start, so a
     rescheduled event alerts again for its new time.

Fail-open: if the classifier cannot answer, the alert is sent anyway, marked as unclassified. A spare
ping is cheap; a missed appointment is the failure this exists to stop. Persistent=true on the timer
means a run missed while claude-dev was down happens at boot, and anything still in the future gets
its alert late rather than never.

Auth: the service account at ~/.config/claude-dev/google-sa.json with domain-wide delegation,
impersonating the operator, scope calendar.readonly (already authorized in DWD for the google MCP).

    calalert.py              one pass (what the timer runs)
    calalert.py --dry-run [--days N]
                             judge every eligible event from N days ago to N days ahead (default 30),
                             print verdicts, send nothing, leave state alone
    calalert.py --sample     send one sample alert so the formatting can be seen
"""
import argparse, datetime, json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
import errlog  # noqa: E402

KEYF = os.path.expanduser("~/.config/claude-dev/google-sa.json")
SUBJECT = "mikael@odinlake.net"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
STATE = Path(os.environ.get("CALALERT_STATE", Path.home() / ".local/state/calalert/state.json"))
# Absolute: systemd's default PATH has no ~/.local/bin (see docwatch.py for the day that cost).
CLAUDE = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
LEAD = datetime.timedelta(hours=2)
KEEP = datetime.timedelta(days=30)           # forget sent/verdict entries for events older than this
AGENT = "reminder"                           # same handle as remind.py: one voice for appointments
SKIP_TYPES = {"focusTime", "outOfOffice", "workingLocation", "birthday", "fromGmail"}
ICON = {"medical": "🩺", "dental": "🦷", "school": "🏫", "legal": "⚖️", "finance": "💷",
        "government": "🏛️", "meeting": "🤝", "booking": "📋", "travel": "✈️", "child": "🧒"}


def service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(KEYF, scopes=SCOPES, subject=SUBJECT)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def events(svc, t0, t1):
    out, tok = [], None
    while True:
        r = svc.events().list(calendarId="primary", timeMin=t0.isoformat(), timeMax=t1.isoformat(),
                              singleEvents=True, orderBy="startTime", maxResults=250,
                              pageToken=tok).execute()
        out += r.get("items", [])
        tok = r.get("nextPageToken")
        if not tok:
            return out


def start_of(ev):
    s = ev.get("start", {}).get("dateTime")
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def eligible(ev):
    """Timed, one-off, not declined, an ordinary event. Returns None if eligible, else why not."""
    if ev.get("status") == "cancelled":
        return "cancelled"
    if not start_of(ev):
        return "all-day"
    if ev.get("recurringEventId") or ev.get("recurrence"):
        return "recurring"
    if ev.get("eventType", "default") in SKIP_TYPES:
        return ev["eventType"]
    for a in ev.get("attendees", []):
        if a.get("self") and a.get("responseStatus") == "declined":
            return "declined"
    return None


def key(ev):
    return "%s|%s" % (ev["id"], ev["start"]["dateTime"])


def vkey(ev):
    return "%s|%s" % (key(ev), ev.get("summary", ""))


PROMPT = """You decide whether one calendar event deserves a loud "2 hours to go" alert on the owner's
phone. The owner is a busy parent in London who missed an appointment recently and asked for alerts
on events that "look like important appointments (medical, PTA, ...)".

IMPORTANT (alert): something the owner must turn up to, or be on a call for, at that time, where missing it
costs something. Examples: GP / hospital / dentist / optician / therapy / medication review /
vaccination; school meetings, PTA, parents' evening, school events a parent must attend; legal,
financial, government, council or immigration appointments; bookings with a reference number or a
slot (tip/recycling-centre slot, MOT, repair visit, delivery window); a meeting or call with a named
person or organisation; children's appointments.

NOT IMPORTANT (no alert): shopping lists, to-dos and reminders-to-self ("Buy: ...", "call X sometime"),
optional public events (open days, fairs, concerts the owner might drop into), TV, sport results, training
targets, holidays, placeholders.

When it is genuinely unclear (e.g. just a first name), answer important=true: a spare alert is cheap,
a missed appointment is not.

Event:
  title: {title}
  when: {when} ({dur})
  location: {loc}
  description: {desc}

Reply with ONLY minified JSON, no prose or fences:
{{"important":true|false,"kind":"medical|dental|school|legal|finance|government|meeting|booking|travel|child|other","reason":"<=12 words"}}"""


def judge(ev):
    st = start_of(ev)
    en = ev.get("end", {}).get("dateTime")
    dur = "%d min" % ((datetime.datetime.fromisoformat(en.replace("Z", "+00:00")) - st).total_seconds() // 60) \
        if en else "?"
    prompt = PROMPT.format(title=ev.get("summary", "(no title)"), when=st.strftime("%a %d %b %Y %H:%M"),
                           dur=dur, loc=ev.get("location") or "-",
                           desc=(ev.get("description") or "-")[:800].replace("\n", " "))
    last = ""
    for attempt in range(2):
        try:
            r = subprocess.run([CLAUDE, "-p", "--model", "haiku", prompt], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120)
            out = r.stdout or ""
            i, j = out.find("{"), out.rfind("}")
            if i >= 0 and j > i:
                v = json.loads(out[i:j + 1])
                if isinstance(v.get("important"), bool):
                    return v
            last = "exit %s, no verdict in %r; stderr: %s" % (r.returncode, out[:200], (r.stderr or "")[:200])
        except subprocess.TimeoutExpired:
            last = "timed out after 120 s"
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        errlog.warn("calalert: judge attempt %d/2 failed for %r: %s" % (attempt + 1, ev.get("summary"), last))
        time.sleep(5)
    errlog.err("calalert: could not classify %r, alerting anyway: %s" % (ev.get("summary"), last))
    return {"important": True, "kind": "other", "reason": "UNCLASSIFIED (classifier failed)", "failed": True}


def fmt(ev, verdict, now):
    st = start_of(ev).astimezone()
    now = now.astimezone()
    mins = max(0, round((st - now).total_seconds() / 60))
    left = "%dh %02dm" % divmod(mins, 60) if mins >= 60 else "%d min" % mins
    day = "today" if st.date() == now.date() else st.strftime("%a %-d %b")
    icon = ICON.get(verdict.get("kind"), "📌")
    lines = ["🚨🚨🚨 *APPOINTMENT IN %s* 🚨🚨🚨" % left.upper(),
             "",
             "%s *%s*" % (icon, ev.get("summary", "(no title)").strip()),
             "🕐 *%s* %s" % (st.strftime("%H:%M"), day)]
    if ev.get("location"):
        lines.append("📍 %s" % ev["location"].strip())
    if verdict.get("failed"):
        lines.append("_(couldn't check whether this matters; alerting to be safe)_")
    return "\n".join(lines)


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"sent": {}, "verdict": {}}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True))
    tmp.replace(STATE)


def prune(s, now):
    """Drop entries whose event started more than KEEP ago. The start is the key's second field."""
    def fresh(k):
        try:
            return datetime.datetime.fromisoformat(k.split("|")[1].replace("Z", "+00:00")) > now - KEEP
        except Exception:
            return False
    for part in ("sent", "verdict"):
        s[part] = {k: v for k, v in s.get(part, {}).items() if fresh(k)}


def run_once():
    import tg
    now = datetime.datetime.now(datetime.timezone.utc)
    s = load()
    for ev in events(service(), now, now + LEAD):
        if eligible(ev) or key(ev) in s["sent"]:
            continue
        st = start_of(ev)
        if not now < st <= now + LEAD:
            continue
        v = s["verdict"].get(vkey(ev))
        if v is None:
            v = judge(ev)
            if not v.get("failed"):
                s["verdict"][vkey(ev)] = v
                save(s)
        print("%s %-40s important=%s (%s)" % (st.isoformat(), ev.get("summary", "")[:40], v["important"],
                                              v.get("reason")))
        if not v["important"]:
            continue
        tg.send(fmt(ev, v, now), agent=AGENT)
        s["sent"][key(ev)] = now.isoformat()
        save(s)
    prune(s, now)
    save(s)


def dry_run(days):
    now = datetime.datetime.now(datetime.timezone.utc)
    d = datetime.timedelta(days=days)
    for ev in events(service(), now - d, now + d):
        why = eligible(ev)
        st = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        if why:
            print("skip %-9s %s  %s" % (why, st[:16], ev.get("summary")))
            continue
        v = judge(ev)
        print("%-14s %s  %s  -- %s" % ("ALERT " + v.get("kind", "") if v["important"] else "quiet",
                                       st[:16], ev.get("summary"), v.get("reason")))


def sample():
    import tg
    now = datetime.datetime.now(datetime.timezone.utc)
    ev = {"summary": "Sample: GP appointment (formatting test, not real)", "location": "The Surgery",
          "start": {"dateTime": (now + LEAD).isoformat()}}
    tg.send(fmt(ev, {"kind": "medical"}, now), agent=AGENT)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--sample", action="store_true")
    a = p.parse_args()
    if a.dry_run:
        return dry_run(a.days)
    if a.sample:
        return sample()
    run_once()


if __name__ == "__main__":
    main()
