#!/usr/bin/env python3
"""incident_digest.py — one Telegram message a day about the estate's open incidents, or silence.

The incident queue was a pull surface with nothing pulling: no timer, service or digest anywhere
read it, and the analyst asked for it six times in 72 cycles. Detections were landing in a room with
nobody in it — unit failures, stale data lanes, and (since 2026-08-13) services logging errors below
err level. This is the reader.

THE BAR IS DELIBERATELY HIGH. A daily message that always arrives becomes wallpaper, and wallpaper
is how a six-day outage goes unread. Since 2026-10-14 (operator: "rather not see anything unless
there's something new") the daily message carries only:

  - incidents that are NEW (never seen before, and recent), and
  - the watchdog going overdue, once per outage.

Everything known, chronic or growing goes into ONE weekly roundup of what is still recurring and
unaddressed, plus the issues waiting on the operator. That roundup also keeps silence meaningful:
a week with no message at all means the digest itself is dead.

    incident_digest.py          evaluate and post if material
    incident_digest.py --dry    print what it would post (or why not); post nothing
    incident_digest.py --force  post regardless of the bar (for testing the path)
    incident_digest.py --weekly include the weekly roundup now
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog
import tg

URL = os.environ.get("INCIDENT_URL", "http://logview.lan:8016/api/incidents?window=14d&limit=40")
ISSUES_URL = os.environ.get("ISSUES_URL", "http://logview.lan:8016/api/issues?status=open&limit=50")
STATE = Path(os.environ.get("INCIDENT_DIGEST_STATE",
                            Path.home() / ".local/state/incident-digest.json"))
TOP_N = int(os.environ.get("INCIDENT_TOP_N", "5"))
FRESH_H = 24
# A never-reported incident whose last occurrence is days old is not urgent by definition, and
# reporting it as "new" would make the FIRST digest a dump of the whole 14-day window. It stays
# visible in mo.lan/logs; it just does not earn a message. 48 h, so a single failure three days ago
# stays out of the daily ping.
NEW_MAX_AGE_H = float(os.environ.get("INCIDENT_NEW_MAX_AGE_H", "48"))
# How long an incident key is remembered after it was last seen. `seen` used to be rebuilt from the
# 14-day queue window alone, so anything quiet for two weeks was forgotten and its next occurrence
# announced as "new": 34 of the 64 distinct keys posted 2026-08-14..10-28 were repeats, one of them
# seven times (operator, 2026-10-28: "too much noise"). A recurrence of something known is not news;
# while it keeps recurring the weekly roundup carries it.
FORGET_DAYS = float(os.environ.get("INCIDENT_FORGET_DAYS", "90"))
# The weekly roundup's period.
LIVENESS_DAYS = float(os.environ.get("INCIDENT_LIVENESS_DAYS", "7"))


def send(text):
    """Post through the estate's ONE transport, tagged #watchdog.

    An earlier version of this file rolled its own urllib post — which is how the first digest went
    out with no #handle and no entry in the conversation log, so the operator could not tell which
    agent had spoken. tg.send() is the single place that enforces the handle convention, converts
    markdown, falls back to plain text on a MarkdownV2 error, and records the message in convo.
    Duplicating a transport duplicates every one of those decisions, badly."""
    try:
        return bool(tg.send(text, agent="watchdog"))
    except Exception as exc:
        errlog.err("incident-digest: telegram post failed — this digest reached nobody", exc)
        return False


def fetch():
    with urllib.request.urlopen(URL, timeout=20) as r:
        return json.loads(r.read().decode()).get("incidents") or []


def open_issues():
    """Open issues are context, never a reason to post: an issue waiting on the operator is not
    news every morning. Counted in a digest that goes out anyway, so the two halves of the board
    are never read apart."""
    try:
        with urllib.request.urlopen(ISSUES_URL, timeout=15) as r:
            return json.loads(r.read().decode()).get("issues") or []
    except Exception as exc:
        errlog.err("incident-digest: could not read the issue list", exc)
        return []


def watchdog_age():
    """Hours since the watchdog last ran, from the fleet journal. None if it has not run at all.

    Nothing else can report this: the watchdog is what notices a dead checker, so its own death is
    invisible to every other check. This digest is the one thing that speaks unprompted, which makes
    it the only place the answer can land."""
    try:
        u = ("http://logview.lan:8016/api/search?hosts=monitoring&units=watchdog.service"
             "&since=2026-01-01&limit=1")
        with urllib.request.urlopen(u, timeout=15) as r:
            rows = json.loads(r.read().decode()).get("rows") or []
        if not rows:
            return None
        from datetime import datetime
        ts = datetime.fromisoformat(rows[0]["ts"]).timestamp()
        return (time.time() - ts) / 3600.0
    except Exception as exc:
        errlog.warn(f"incident-digest: could not read the watchdog's last run ({exc})")
        return None


def key(i):
    """A lane is one thing, whichever box noticed it. The freshness checker runs on claude-dev and
    the analyst's loop box runs it too, so keying lanes by host reported the same stale lane twice
    (2026-10-11: claude-loop/lane-statements-amex 'new' four days after claude-dev's copy)."""
    unit = i.get("unit", "?")
    return unit if unit.startswith("lane-") else f"{i.get('host', '?')}/{unit}"


def merge(rows):
    """Collapse rows that share a key: worst score, latest occurrence, biggest count."""
    out = {}
    for i in rows:
        k = key(i)
        o = out.get(k)
        if o is None:
            out[k] = dict(i)
            continue
        if int(i.get("score") or 0) > int(o.get("score") or 0):
            o["score"] = i.get("score")
        if age_h(i) < age_h(o):
            o["age_hours"], o["last_message"] = i.get("age_hours"), i.get("last_message")
        o["count"] = max(int(o.get("count") or 0), int(i.get("count") or 0))
        o["first_seen"] = min(int(o.get("first_seen") or 0) or 10**20, int(i.get("first_seen") or 0) or 10**20)
    return list(out.values())


def ago(i):
    h = age_h(i)
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def open_days(i, now):
    """Days since the incident first appeared (logview stamps first_seen in microseconds)."""
    try:
        return max(0.0, (now - int(i.get("first_seen")) / 1e6) / 86400)
    except (TypeError, ValueError):
        return 0.0


def age_h(i):
    """Hours since last seen; the API computes it, so trust its clock rather than ours."""
    try:
        return float(i.get("age_hours"))
    except (TypeError, ValueError):
        return 1e9


def line(i):
    """One incident, one line plus a short gist (operator, 2026-10-28: concise)."""
    # A log-errors message is boilerplate ("N error-shaped lines matching ... between ..."): no gist.
    detail = "" if i.get("kind") == "log-errors" else " ".join(str(i.get("last_message") or "").split())
    out = f"• {key(i)} ({i.get('kind', '?')}), {ago(i)}"
    return out + (f"\n  {detail[:100]}{'…' if len(detail) > 100 else ''}" if detail else "")


TITLE_MAX = 240


def issue_title(i):
    """An issue's whole title, and visibly cut if it is ever absurd.

    This used to be `title[:80]` (and `[:70]` in the daily digest before that), which cut EVERY title
    the board has ever held: lengths 77/88/89. All 33 deliveries of i-20260814-154101 went out as
    "...since 2026-08-06 — is it", dropping the question's object ("the app or the pad?"), and 0 of 33
    carried the whole line (moprox-memory/incident-digest-title-slice-cuts-every-issue). A title is one
    line an agent wrote to be read; the slice saved nothing and cost the sentence. The bound here is a
    sanity limit well above anything the board has carried, not a layout width, and it says when it
    bites — a silent cut is how this went unnoticed for two months."""
    t = (i.get("title") or i.get("id") or "?").strip().replace("\n", " ")
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX] + "… (title cut at %d chars)" % TITLE_MAX


def weekly(rows, now):
    """The once-a-week roundup of what is still open and unaddressed (operator, 2026-10-14: he will
    not act on the Amex lanes now, and is happy to be reminded weekly, but not daily). Lists what is
    still RECURRING and has been open a while; a one-off that stopped days ago is not a reminder."""
    chronic = [i for i in rows if age_h(i) <= 48 and not i.get("acked")]
    chronic.sort(key=lambda i: (-int(i.get("score") or 0), -open_days(i, now)))
    lines = ["🔧 Weekly: outstanding"]
    for i in chronic[:8]:
        lines.append(f"• {key(i)} ({i.get('kind', '?')}), {open_days(i, now):.0f} d")
    if len(chronic) > 8:
        lines.append(f"…+{len(chronic) - 8} more")
    if not chronic:
        lines.append("Nothing recurring. (Sent so a silent week stays meaningful.)")
    iss = open_issues()
    mine = [i for i in iss if (i.get("owner") or "") == "operator"]
    if mine:
        lines.append(f"\nOn you ({len(mine)}):")
        for i in mine[:5]:
            lines.append(f"• {issue_title(i)}")
        if len(mine) > 5:
            lines.append(f"…+{len(mine) - 5} more")
    if len(iss) > len(mine):
        lines.append(f"\n{len(iss) - len(mine)} other open issue(s), with agents: mo.lan/issues")
    return lines


def main():
    dry = "--dry" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    want_weekly = "--weekly" in sys.argv[1:]
    now = time.time()

    try:
        rows = merge(fetch())
    except Exception as exc:
        errlog.err(f"incident-digest: could not read the queue at {URL}", exc)
        return 1

    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    # key -> last time the queue showed it (epoch s). Before 2026-10-28 the value was a count; a
    # count carries no date, so treat those as seen now and let them age out from here.
    seen = {k: (v if isinstance(v, float) else now) for k, v in state.get("seen", {}).items()}
    # Keys used to carry the host for lanes too; carry those forward so the switch to host-free
    # lane keys does not announce every chronic lane as "new" on the first run.
    for k in list(seen):
        host, _, unit = k.partition("/")
        if unit.startswith("lane-"):
            seen[unit] = max(seen.get(unit, 0.0), seen.pop(k))
    reported = state.get("reported", {})    # key -> when it last earned a message

    # THE DAILY BAR IS "NEW", AND NOTHING ELSE (operator, 2026-10-14: "rather not see anything
    # unless there's something new"). The old bar also posted an incident whose count had doubled
    # and a high-score one weekly; for a stale lane the count grows every single check, so the
    # same lanes came back as "grew 20->44" day after day. Known, unchanged-or-growing items
    # belong to the weekly roundup.
    material, quiet = [], []
    for i in rows:
        k = key(i)
        if k not in seen and age_h(i) <= NEW_MAX_AGE_H:
            material.append((i, "new"))
        else:
            quiet.append(i)

    material.sort(key=lambda t: (-int(t[0].get("score") or 0), -int(t[0].get("count") or 0)))
    top, overflow = material[:TOP_N], material[TOP_N:]
    # What the cap drops must not also be swallowed: rebuilding `seen` from every row would retire
    # a truncated incident's "new" status without it ever having earned a message. Hold it over.
    held = {key(i) for i, _ in overflow}
    next_seen = {k: t for k, t in seen.items() if now - t < FORGET_DAYS * 86400}
    next_seen.update({key(i): float(now) for i in rows if key(i) not in held})

    # The watchdog's own death is the one thing nothing else can report, so it is news, but only
    # once: flag it when it goes overdue, and again only after it has recovered and died again.
    wd = watchdog_age()
    wd_down = wd is None or wd > 2
    wd_news = wd_down and not state.get("wd_alerted")

    # Weekly on Monday's run. A missed Monday (box down) catches up the next day; the very first run
    # only starts the clock, so deploying this does not post a roundup mid-week.
    last_weekly = float(state.get("last_weekly") or 0)
    if not last_weekly:
        state["last_weekly"] = last_weekly = now
    since_weekly = (now - last_weekly) / 86400.0
    weekly_due = want_weekly or (time.localtime(now).tm_wday == 0 and since_weekly > 3) \
        or since_weekly >= LIVENESS_DAYS + 1

    lines = []
    if top or force:
        lines.append(f"🔧 New: {len(top)} incident(s)")
        for i, _ in top:
            lines.append(line(i))
        if overflow:
            lines.append(f"+{len(overflow)} more new, tomorrow")
    if wd_news:
        lines.append("\nwatchdog: " + ("no run found in the last 7 days, the checker-checker is DOWN"
                                         if wd is None else f"last ran {wd:.0f} h ago, it should run every 30 min"))
    if weekly_due:
        if lines:
            lines.append("")
        lines.extend(weekly(rows, now))

    state.update({"last_run": now, "reported": reported, "seen": next_seen,
                  "wd_alerted": bool(wd_down)})
    if not lines:
        # Deliberate silence. Recorded, so a long quiet streak is visible in the state file rather
        # than being indistinguishable from a broken timer.
        state["quiet_runs"] = int(state.get("quiet_runs", 0)) + 1
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        print(f"incident-digest: silent, {len(rows)} open, nothing new "
              f"(quiet runs: {state['quiet_runs']})")
        return 0

    lines.append("\nhttps://mo.lan/incidents/")
    text = "\n".join(lines)[:3900]
    if dry:
        print(text)
        return 0
    if not send(text):
        return 1
    reported.update({key(i): now for i, _ in top})
    state.update({"last_posted": now, "quiet_runs": 0})
    if weekly_due:
        state["last_weekly"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(f"incident-digest: posted {len(top)} new, {len(overflow)} held over, "
          f"weekly={'yes' if weekly_due else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
