#!/usr/bin/env python3
"""Long-poll Telegram getUpdates and capture inbound messages into a durable inbox (JSONL) for a
router/steward to consume. ~30s long-poll loop (returns instantly on a message), so it's cheap and
near-real-time without webhooks. Creds from ~/.config/claude-dev/telegram.env.

This is just the *capture* layer — routing replies to the right agent is layered on top (see the
agent-architecture decision in docs/roadmap.md). Run as a Restart=always systemd service.
"""
import json, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import tg_files          # documents/photos/voice -> a real file on disk (see tg_files.py)
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

TG_ENV = Path(os.environ.get("TELEGRAM_ENV", Path.home() / ".config/claude-dev/telegram.env"))
INBOX  = Path(os.environ.get("TELEGRAM_INBOX", Path.home() / ".local/share/moprox/telegram-inbox.jsonl"))
STATE  = Path.home() / ".local/share/moprox/telegram-offset"
LOCATION = Path.home() / ".local/share/moprox/location.json"   # freshest fix from a shared/live location
# capture-only: the dispatcher service tails this inbox and does triage + routing (single-flight
# per agent), so a long agent run never blocks message pickup here. Location pins / Live Location
# updates are captured separately to LOCATION (latest wins) for the valet's "where am I" check.

# When this loop cannot reach Telegram, nothing in the estate can tell. It catches every exception
# and continues, so the unit stays `active (running)` and unit-failed never raises; the only trace is
# the info-level `poll error:` lines below, which a priority query does not return. There is no
# freshness lane to fall back on either — an inbox with no new messages in it is the normal state of
# an inbox, so silence there proves nothing. claude-dev's 2026-09-14 19:08-20:44 inbound outage
# reached the operator only because logscan happened to shape-match three of those info lines.
#
# The predicate is EPISODE DURATION, not consecutive failures. Measured over the 150 poll errors in
# the fleet journal's 30-day window (2026-08-17..09-14): that outage was a FLAP, 27 errors across
# 5774 s with successful polls in between — 22 of its 26 inter-error gaps exceed the ~50 s one
# back-to-back failing cycle takes — so an "N failures in a row" gate would have sat at zero
# throughout it. What separates it cleanly is how long the channel stayed unhealthy AT ALL. An
# episode starts at the first error and ends only after CLEAR_S with no error whatsoever; on that
# definition the whole archive holds exactly one episode over 216 s: 4226 s from 2026-09-14 19:34.
# The runner-up is the recurring ~01:11 nightly blip, 2-8 errors inside four minutes, which is
# weather and must stay quiet. STALL_S sits 8x above it, so this speaks once per real outage.
STALL_S = 1800   # unhealthy this long without a clear window => news, say so at err
CLEAR_S = 600    # this long with no error at all => the episode is over

def capture_location(loc):
    rec = {"lat": loc["latitude"], "lon": loc["longitude"], "ts": int(time.time()),
           "accuracy": loc.get("horizontal_accuracy"), "live_period": loc.get("live_period"),
           "heading": loc.get("heading")}
    if loc.get("live_period"): rec["until"] = rec["ts"] + loc["live_period"]
    LOCATION.parent.mkdir(parents=True, exist_ok=True)
    LOCATION.write_text(json.dumps(rec))
    print("location <-", rec["lat"], rec["lon"], "(live)" if loc.get("live_period") else "(pin)")

def creds():
    tok = chat = None
    for ln in TG_ENV.read_text().splitlines():
        if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
        if ln.startswith("TELEGRAM_CHAT_ID="):  chat = ln.split("=", 1)[1].strip()
    if not tok: raise SystemExit("missing TELEGRAM_BOT_TOKEN")
    return tok, chat

def api(tok, method, params):
    url = "https://api.telegram.org/bot%s/%s?%s" % (tok, method, urllib.parse.urlencode(params))
    return json.load(urllib.request.urlopen(url, timeout=45))

def main():
    tok, _ = creds()
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    offset = int(STATE.read_text()) if STATE.exists() else 0
    print(f"telegram-poll up; offset={offset}; inbox={INBOX}")
    ep_start = ep_last = None      # current unhealthy episode: first error, most recent error
    ep_n, stalled = 0, False
    while True:
        try:
            r = api(tok, "getUpdates", {"offset": offset, "timeout": 30,
                                        "allowed_updates": json.dumps(["message", "edited_message"])})
        except Exception as e:
            print("poll error:", e)    # one blip is weather; the retry below covers it
            now = time.monotonic()
            if ep_start is None or now - ep_last > CLEAR_S:
                ep_start, ep_n, stalled = now, 0, False      # a fresh episode
            ep_last, ep_n = now, ep_n + 1
            if not stalled and now - ep_start >= STALL_S:
                stalled = True
                errlog.err(f"telegram inbound down: {ep_n} poll errors over "
                           f"{(now - ep_start) / 60:.0f} min with no {CLEAR_S // 60}-minute clear "
                           f"window; still retrying every 5s; last error", e)
            time.sleep(5); continue
        if ep_start is not None and time.monotonic() - ep_last > CLEAR_S:
            if stalled:
                print(f"telegram inbound recovered; episode ran {(ep_last - ep_start) / 60:.0f} min, "
                      f"{ep_n} poll errors")
            ep_start = ep_last = None
            ep_n, stalled = 0, False
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message")    # live location streams as edited_message
            if not m: continue
            if m.get("location"):                              # a pin or Live Location update
                capture_location(m["location"]); continue
            if "message" not in u: continue                    # ignore edits to text; only new messages route
            rec = {"ts": int(time.time()), "update_id": u["update_id"], "chat_id": m["chat"]["id"],
                   "msg_id": m.get("message_id"),
                   "from": (m.get("from") or {}).get("username") or (m.get("from") or {}).get("first_name"),
                   "text": m.get("text", "") or m.get("caption", "") or "",
                   "reply_to": (m.get("reply_to_message") or {}).get("message_id")}
            # Attachments: download them HERE, while the file_id is still valid. Telegram never
            # replays an update once the offset advances, so a file not fetched now is a file the
            # operator has to send again (tg_files.recover can re-forward, but only if we notice).
            # A caption is the message's text, so "here's the letter — file it" routes normally.
            files = tg_files.capture(tok, m)
            if files:
                rec["files"] = files
                if m.get("media_group_id"): rec["media_group"] = m["media_group_id"]
            # Keep where a forward CAME FROM. The flattened record dropped it, so "forward me this and
            # act on it" silently lost its origin - and identifying a group by forwarding one of its
            # messages here produced nothing usable. Bot API 7.x sends forward_origin; older payloads
            # send forward_from_chat. Record whichever arrived.
            _fo = m.get("forward_origin") or {}
            _src = _fo.get("chat") or _fo.get("sender_chat") or m.get("forward_from_chat")
            if isinstance(_src, dict):
                rec["fwd_from_chat_id"] = _src.get("id")
                rec["fwd_from_title"] = _src.get("title") or _src.get("username")
                rec["fwd_from_type"] = _src.get("type")
            else:
                _u2 = _fo.get("sender_user") or m.get("forward_from") or {}
                if _u2: rec["fwd_from_user"] = _u2.get("username") or _u2.get("first_name")
            with open(INBOX, "a") as f: f.write(json.dumps(rec) + "\n")
            print("inbox <-", rec["from"], repr(rec["text"][:80]),
                  "+%d file(s)" % len(files) if files else "")
        if r.get("result"): STATE.write_text(str(offset))

if __name__ == "__main__":
    main()
