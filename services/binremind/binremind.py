#!/usr/bin/env python3
"""binremind: at 19:30 UK time, tell the operator on Telegram which bins go out tomorrow.

Source: the "Bin calendar" in his Google Calendar, an ICS subscription of the Croft Road council
collection schedule. Google refreshes the feed itself; we re-read it every run, so a changed date
(bank holidays) is picked up without any state.

Operator ask (2026-11-15): monitor until the end of 2026, notify tersely the evening before.
After END the run does nothing (the timer can then be removed).
"""
import datetime, os, sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
import errlog  # noqa: E402

KEYF = os.path.expanduser("~/.config/claude-dev/google-sa.json")
SUBJECT = "mikael@odinlake.net"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CAL = "0ul2ok3mvc9de3v25l7ufeu7ehepss2d@import.calendar.google.com"   # "Bin calendar"
TZ = ZoneInfo("Europe/London")
END = datetime.date(2026, 12, 31)        # last collection day we remind for
AGENT = "valet"
STALE_DAYS = 21                          # no collection at all this far ahead => the feed has stalled


def calendar():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(KEYF, scopes=SCOPES, subject=SUBJECT)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def collections(cal, start, days):
    """{date: [summary, ...]} for all-day events in [start, start+days)."""
    r = cal.events().list(calendarId=CAL, singleEvents=True, orderBy="startTime",
                          timeMin=datetime.datetime.combine(start, datetime.time(), TZ).isoformat(),
                          timeMax=datetime.datetime.combine(start + datetime.timedelta(days=days),
                                                            datetime.time(), TZ).isoformat()).execute()
    out = {}
    for e in r.get("items", []):
        s = e["start"].get("date") or e["start"]["dateTime"][:10]
        d = datetime.date.fromisoformat(s)
        if start <= d < start + datetime.timedelta(days=days):
            out.setdefault(d, []).append(e.get("summary", "?"))
    return out


def short(summary):
    """'Non-recyclable waste collection' -> 'non-recyclable'; 'Paper and card collection' -> 'paper & card'."""
    s = summary.lower().replace(" collection", "").replace(" waste", "").replace(" and ", " & ").strip()
    return s or summary


def message(day, items):
    return "Bins tomorrow (%s): %s" % (day.strftime("%a %-d %b"), ", ".join(short(i) for i in items))


def main(argv):
    dry = "--dry-run" in argv
    today = datetime.datetime.now(TZ).date()
    tomorrow = today + datetime.timedelta(days=1)
    if tomorrow > END:
        print("binremind: past %s, nothing to do" % END)
        return 0
    try:
        ahead = collections(calendar(), tomorrow, STALE_DAYS)
    except Exception as e:
        errlog.err("binremind: reading the Bin calendar", e)
        raise
    if not ahead:
        errlog.warn("binremind: no collections in the next %d days; the Bin calendar feed may have stalled"
                    % STALE_DAYS)
    items = ahead.get(tomorrow)
    if not items:
        print("binremind: no collection %s" % tomorrow)
        return 0
    text = message(tomorrow, items)
    print("binremind: " + text)
    if not dry:
        import tg
        tg.send(text, agent=AGENT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
