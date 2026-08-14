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
  - high-scoring (>= HIGH_SCORE) and seen in the last day.

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
import errlog

URL = os.environ.get("INCIDENT_URL", "http://logview.lan:8016/api/incidents?window=14d&limit=40")
STATE = Path(os.environ.get("INCIDENT_DIGEST_STATE",
                            Path.home() / ".local/state/incident-digest.json"))
TG_ENV = Path(os.environ.get("TG_ENV", Path.home() / ".config/claude-dev/telegram.env"))
API = "https://api.telegram.org/bot%s/sendMessage"
HIGH_SCORE = int(os.environ.get("INCIDENT_HIGH_SCORE", "8"))
ESCALATION_FACTOR = float(os.environ.get("INCIDENT_ESCALATION", "2.0"))
TOP_N = int(os.environ.get("INCIDENT_TOP_N", "5"))
FRESH_H = 24
# A never-reported incident whose last occurrence is days old is not urgent by definition, and
# reporting it as "new" would make the FIRST digest a dump of the whole 14-day window. It stays
# visible in mo.lan/logs; it just does not earn a message. 48 h, so a single failure three days ago
# stays out of the daily ping.
NEW_MAX_AGE_H = float(os.environ.get("INCIDENT_NEW_MAX_AGE_H", "48"))


def env(path):
    out = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def send(text):
    """Post to the OPERATOR's chat — telegram.env only, never the firehose override. A failed post
    is an err: 'nothing was wrong' and 'could not say what was wrong' must not look alike."""
    d = env(TG_ENV)
    tok, chat = d.get("TELEGRAM_BOT_TOKEN"), d.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        errlog.err(f"incident-digest: no telegram credentials in {TG_ENV} — nothing was sent")
        return False
    body = json.dumps({"chat_id": chat, "text": text,
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(API % tok, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as exc:
        errlog.err("incident-digest: telegram post failed", exc)
        return False


def fetch():
    with urllib.request.urlopen(URL, timeout=20) as r:
        return json.loads(r.read().decode()).get("incidents") or []


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
        elif int(i.get("score") or 0) >= HIGH_SCORE and fresh:
            why = "high"
        else:
            quiet.append(i)
            continue
        material.append((i, why))

    material.sort(key=lambda t: (-int(t[0].get("score") or 0), -int(t[0].get("count") or 0)))
    top = material[:TOP_N]

    if not top and not force:
        # Deliberate silence. Recorded, so a long quiet streak is visible in the state file rather
        # than being indistinguishable from a broken timer.
        state.update({"last_run": now, "last_posted": state.get("last_posted"),
                      "quiet_runs": int(state.get("quiet_runs", 0)) + 1,
                      "seen": {key(i): int(i.get("count") or 0) for i in rows}})
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        print(f"incident-digest: silent — {len(rows)} open, none material "
              f"(quiet runs: {state['quiet_runs']})")
        return 0

    last = state.get("last_posted")
    since = f" (last digest {int((now - last) / 86400)}d ago)" if last else ""
    lines = [f"🔧 Estate incidents — {len(top)} worth a look{since}"]
    for i, why in top:
        detail = str(i.get("last_message") or "").strip().replace("\n", " ")
        lines.append(f"\n• [{i.get('score')}] {key(i)} ({i.get('kind', '?')}) — {why}, "
                         f"x{i.get('count')}, last {ago(i)}")
        if detail:
            lines.append(f"  {detail[:200]}")
    if quiet:
        lines.append(f"\n{len(quiet)} other open incident(s) unchanged and not repeated here.")
    lines.append("\nmo.lan/logs → Incidents")
    text = "\n".join(lines)[:3900]

    if dry:
        print(text)
        return 0
    if not send(text):
        return 1
    state.update({"last_run": now, "last_posted": now, "quiet_runs": 0,
                  "seen": {key(i): int(i.get("count") or 0) for i in rows}})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(f"incident-digest: posted {len(top)} incident(s), {len(quiet)} quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
