#!/usr/bin/env python3
"""Dispatch captured Telegram messages to the right agent, with a per-agent input queue and
single-flight execution: an agent is never invoked while a previous invocation of itself is still
running — further messages queue and run in order. Different agents run concurrently, so a long
coach session never blocks triage/pickup of the next message.

  inbox.jsonl --(tail)--> [router] --decide(signals)--> [coach|dev|steward queue] --worker--> reply
                                     logs the inbound to the shared transcript, marked with its target

telegram_poll only *captures* to the inbox (fast, never blocks); this service does all the work.
Only messages from the operator's own chat_id are acted on.
"""
import json, queue, threading, time
from pathlib import Path
import route, tg, convo

INBOX  = Path.home() / ".local/share/moprox/telegram-inbox.jsonl"
OFFSET = Path.home() / ".local/share/moprox/dispatcher-offset"     # byte offset into the inbox

Q = {"router": queue.Queue(), "coach": queue.Queue(), "dev": queue.Queue(), "steward": queue.Queue()}

def agent_worker(name):
    """Single-flight worker for one agent: one invocation at a time, the rest queue (in order)."""
    while True:
        rec = Q[name].get()
        try:
            print("%s <-" % name, (rec.get("text") or "")[:50]); route.handle(name, rec)
        except Exception as e:
            print("%s error:" % name, e); tg.send("(%s error: %s)" % (name, str(e)[:150]), agent=name)
        finally:
            Q[name].task_done()

def router_worker():
    """Decide the target (deterministic signals, else steward judgement), record the inbound message
    in the shared transcript marked with its target, then hand to that agent's single-flight queue."""
    while True:
        rec = Q["router"].get()
        try:
            agent, reason = route.decide(rec)
            print("route:", agent, "(%s) |" % reason, (rec.get("text") or "")[:50])
            convo.log_in((rec.get("text") or "").strip(), rec.get("msg_id"), rec.get("reply_to"), to=agent)
            Q[agent].put(rec)
        except Exception as e:
            print("router error:", e); tg.send("(routing error: %s)" % str(e)[:150], agent="steward")
        finally:
            Q["router"].task_done()

def our_chat(rec):
    _, chat = tg.creds()
    return str(rec.get("chat_id")) == str(chat)

def main():
    INBOX.parent.mkdir(parents=True, exist_ok=True); INBOX.touch(exist_ok=True)
    # resume from saved offset; first ever run starts at EOF so we don't replay captured history
    off = int(OFFSET.read_text()) if OFFSET.exists() else INBOX.stat().st_size
    threading.Thread(target=router_worker, daemon=True).start()
    for name in ("coach", "dev", "steward"):
        threading.Thread(target=agent_worker, args=(name,), daemon=True).start()
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
                        Q["router"].put(rec)
                off += nl + 1
                OFFSET.write_text(str(off))
        except Exception as e:
            print("tail error:", e)
        time.sleep(2)

if __name__ == "__main__":
    main()
