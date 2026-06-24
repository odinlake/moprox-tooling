#!/usr/bin/env python3
"""Dispatch captured Telegram messages to the right agent, with a per-agent input queue and
single-flight execution: an agent is never invoked while a previous invocation of itself is still
running — further messages queue and run in order. Different agents run concurrently, so a long
coach session never blocks triage/pickup of the next message.

  inbox.jsonl --(tail)--> [steward queue] --steward worker: triage--> [coach queue] | dev ack | chat
                                                            [coach queue] --coach worker--> reply

telegram_poll only *captures* to the inbox (fast, never blocks); this service does all the work.
Only messages from the operator's own chat_id are acted on.
"""
import json, queue, threading, time
from pathlib import Path
import route, tg

INBOX  = Path.home() / ".local/share/moprox/telegram-inbox.jsonl"
OFFSET = Path.home() / ".local/share/moprox/dispatcher-offset"     # byte offset into the inbox

Q = {"steward": queue.Queue(), "coach": queue.Queue()}

def coach_worker():
    while True:
        decision, rec = Q["coach"].get()
        try:
            print("coach <-", (rec.get("text") or "")[:50]); route.handle(decision, rec)
        except Exception as e:
            print("coach error:", e); tg.send("(coach error: %s)" % str(e)[:150], agent="coach")
        finally:
            Q["coach"].task_done()

def steward_worker():
    while True:
        rec = Q["steward"].get()
        try:
            d = route.triage((rec.get("text") or "").strip())
            r = d.get("route", "ignore")
            print("route:", r, "|", (rec.get("text") or "")[:50])
            if r == "coach":
                Q["coach"].put((d, rec))          # hand the slow work to coach's single-flight worker
            else:
                route.handle(d, rec)              # dev ack / chat reply / ignore — all cheap
        except Exception as e:
            print("steward error:", e); tg.send("(steward error: %s)" % str(e)[:150], agent="steward")
        finally:
            Q["steward"].task_done()

def our_chat(rec):
    _, chat = tg.creds()
    return str(rec.get("chat_id")) == str(chat)

def main():
    INBOX.parent.mkdir(parents=True, exist_ok=True); INBOX.touch(exist_ok=True)
    # resume from saved offset; first ever run starts at EOF so we don't replay captured history
    off = int(OFFSET.read_text()) if OFFSET.exists() else INBOX.stat().st_size
    threading.Thread(target=steward_worker, daemon=True).start()
    threading.Thread(target=coach_worker, daemon=True).start()
    print("dispatcher up; offset=%d" % off)
    while True:
        try:
            with open(INBOX, "rb") as f:
                f.seek(off); chunk = f.read()
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                for raw in chunk[:nl + 1].split(b"\n"):
                    if not raw.strip(): continue
                    try: rec = json.loads(raw.decode())
                    except Exception: continue
                    if (rec.get("text") or "").strip() and our_chat(rec):
                        Q["steward"].put(rec)
                off += nl + 1
                OFFSET.write_text(str(off))
        except Exception as e:
            print("tail error:", e)
        time.sleep(2)

if __name__ == "__main__":
    main()
