#!/usr/bin/env python3
"""incident_digest.py — one Telegram message a day about the estate's open incidents, or silence.

The incident queue was a pull surface with nothing pulling: no timer, service or digest anywhere
read it, and the analyst asked for it six times in 72 cycles. Detections were landing in a room with
nobody in it — unit failures, stale data lanes, and (since 2026-08-13) services logging errors below
err level. This is the reader.

THE BAR IS DELIBERATELY HIGH. A daily message that always arrives becomes wallpaper, and wallpaper
is how a six-day outage goes unread. So this posts only when at least one incident is MATERIAL:

  - new since the last digest, or
  - escalated (its count grew by ESCALATION_FACTOR or more), or
  - high-scoring (>= HIGH_SCORE), seen in the last day, and not already reported this week.

Anything else — a queue of known, unchanged, low-score items — is silence. Silence therefore means
"nothing changed", not "nothing is wrong", and the message says how many quiet items are still open
so the difference stays visible.

    incident_digest.py          evaluate and post if material
    incident_digest.py --dry    print what it would post (or why not); post nothing
    incident_digest.py --force  post regardless of the bar (for testing the path)
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
HIGH_SCORE = int(os.environ.get("INCIDENT_HIGH_SCORE", "8"))
ESCALATION_FACTOR = float(os.environ.get("INCIDENT_ESCALATION", "2.0"))
TOP_N = int(os.environ.get("INCIDENT_TOP_N", "5"))
FRESH_H = 24
# A never-reported incident whose last occurrence is days old is not urgent by definition, and
# reporting it as "new" would make the FIRST digest a dump of the whole 14-day window. It stays
# visible in mo.lan/logs; it just does not earn a message. 48 h, so a single failure three days ago
# stays out of the daily ping.
NEW_MAX_AGE_H = float(os.environ.get("INCIDENT_NEW_MAX_AGE_H", "48"))
# A chronic high-score incident would otherwise qualify as "material" every single day and become
# the wallpaper this whole design is trying to avoid. Serious-but-known resurfaces weekly; serious-
# and-CHANGING still comes through immediately, because escalation is checked before this.
HIGH_REPEAT_DAYS = float(os.environ.get("INCIDENT_HIGH_REPEAT_DAYS", "7"))
# Silence from this digest is ambiguous by construction: a quiet estate and a dead timer look
# identical from the outside. Once a week, say something regardless — that one line is what makes
# total silence meaningful, and it is the only thing watching the watchdog that watches everything
# else. Do not remove it as noise; it is the opposite.
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
    return f"{i.get('host', '?')}/{i.get('unit', '?')}"


def ago(i):
    h = age_h(i)
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def age_h(i):
    """Hours since last seen — the API computes it, so trust its clock rather than ours."""
    try:
        return float(i.get("age_hours"))
    except (TypeError, ValueError):
        return 1e9


def main():
    dry = "--dry" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    now = time.time()

    try:
        rows = fetch()
    except Exception as exc:
        errlog.err(f"incident-digest: could not read the queue at {URL}", exc)
        return 1

    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    seen = state.get("seen", {})            # key -> count at last digest
    reported = state.get("reported", {})    # key -> when it last EARNED a message

    material, quiet = [], []
    for i in rows:
        k, n = key(i), int(i.get("count") or 0)
        was = seen.get(k)
        fresh = age_h(i) <= FRESH_H
        if was is None:
            # Never reported. Worth a message only if it is also recent — otherwise the first run
            # would dump the whole window, and a fortnight-old one-off is not news.
            if age_h(i) <= NEW_MAX_AGE_H:
                why = "new"
            else:
                quiet.append(i)
                continue
        elif n >= max(was * ESCALATION_FACTOR, was + 1) and fresh:
            why = f"grew {was}->{n}"
        elif (int(i.get("score") or 0) >= HIGH_SCORE and fresh
              and now - float(reported.get(k) or 0) >= HIGH_REPEAT_DAYS * 86400):
            why = "still open"
        else:
            quiet.append(i)
            continue
        material.append((i, why))

    material.sort(key=lambda t: (-int(t[0].get("score") or 0), -int(t[0].get("count") or 0)))
    top = material[:TOP_N]

    last_posted = state.get("last_posted") or 0
    overdue = (now - last_posted) / 86400.0 >= LIVENESS_DAYS if last_posted else False

    if not top and not force and not overdue:
        # Deliberate silence. Recorded, so a long quiet streak is visible in the state file rather
        # than being indistinguishable from a broken timer.
        state.update({"last_run": now, "last_posted": state.get("last_posted"),
                      "quiet_runs": int(state.get("quiet_runs", 0)) + 1, "reported": reported,
                      "seen": {key(i): int(i.get("count") or 0) for i in rows}})
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        print(f"incident-digest: silent — {len(rows)} open, none material "
              f"(quiet runs: {state['quiet_runs']})")
        return 0

    last = state.get("last_posted")
    since = f" (last digest {int((now - last) / 86400)}d ago)" if last else ""
    if not top and overdue:
        lines = [f"🔧 Estate quiet for {int((now - last_posted) / 86400)} days — nothing material. "
                 f"This line exists so silence stays meaningful."]
    else:
        lines = [f"🔧 Estate incidents — {len(top)} worth a look{since}"]
    for i, why in top:
        detail = str(i.get("last_message") or "").strip().replace("\n", " ")
        lines.append(f"\n• [{i.get('score')}] {key(i)} ({i.get('kind', '?')}) — {why}, "
                         f"x{i.get('count')}, last {ago(i)}")
        if detail:
            lines.append(f"  {detail[:200]}")
    if quiet:
        lines.append(f"\n{len(quiet)} other open incident(s) unchanged and not repeated here.")
    iss = open_issues()
    if iss:
        mine = sum(1 for i in iss if (i.get("owner") or "") == "operator")
        lines.append(f"{len(iss)} open issue(s){f', {mine} waiting on you' if mine else ''}: "
                     + "; ".join((i.get("title") or i.get("id", ""))[:70] for i in iss[:3]))
    wd = watchdog_age()
    if wd is not None:
        lines.append(f"\nwatchdog last ran {wd:.0f} h ago"
                     + (" — OVERDUE, it should run every 30 min" if wd > 2 else ""))
    elif wd is None:
        lines.append("\nwatchdog: no run found in the last 7 days — the checker-checker is DOWN")
    lines.append("\nhttps://mo.lan/incidents/")
    text = "\n".join(lines)[:3900]

    if dry:
        print(text)
        return 0
    if not send(text):
        return 1
    reported.update({key(i): now for i, _ in top})
    state.update({"last_run": now, "last_posted": now, "quiet_runs": 0, "reported": reported,
                  "seen": {key(i): int(i.get("count") or 0) for i in rows}})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(f"incident-digest: posted {len(top)} incident(s), {len(quiet)} quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
