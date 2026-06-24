#!/usr/bin/env python3
"""Steward routing logic: triage one inbound message, then act on the decision.

Split in two so the dispatcher can triage cheaply on the main thread and run the (slow) target
agent on that agent's own single-flight worker:
  triage(text)        -> {"route": coach|dev|chat|ignore, "reason", "context"}   (runs the steward)
  handle(decision, rec)-> coach reply | dev task log + ack | steward chat reply  (runs the target)

All agents run on the Max plan via run_agent; all output goes out through tg with an #agent tag.
"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
from run import run_agent
import tg

DEV_INBOX = Path.home() / ".local/share/moprox/dev-requests.jsonl"

def _json(s):
    m = re.search(r"\{.*\}", s or "", re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def triage(text):
    prompt = ("Triage this inbound Telegram message from the athlete and output ONLY the routing "
              "JSON object (no prose, no code fence).\n\nMESSAGE: %s" % text)
    return _json(run_agent("steward", prompt, timeout=120)) or {"route": "ignore", "reason": "unparsed"}

def handle(decision, rec):
    """Act on a triage decision. Returns the chosen route (for logging)."""
    text = (rec.get("text") or "").strip()
    route = decision.get("route", "ignore"); ctx = decision.get("context", "")
    if route == "coach":
        reply = run_agent("coach",
            "The athlete sent this via Telegram: %r\nContext: %s\nReply concisely, in your voice." % (text, ctx),
            timeout=420)
        tg.send(reply, agent="coach", reply_to=rec.get("reply_to") or None)
    elif route == "dev":
        DEV_INBOX.parent.mkdir(parents=True, exist_ok=True)
        with open(DEV_INBOX, "a") as f:
            f.write(json.dumps({"ts": rec.get("ts"), "text": text, "context": ctx}) + "\n")
        tg.send("🛠️ Logged as a dev task — I'll pick it up in the next dev session.\n• " + (ctx or text),
                agent="dev")
    elif route == "chat":
        tg.send(ctx or "👍", agent="steward")
    # ignore -> nothing
    return route

def process_message(rec):
    """Convenience: triage + handle inline (used by the CLI; the dispatcher calls the two halves)."""
    text = (rec.get("text") or "").strip()
    if not text: return "empty"
    try:
        return handle(triage(text), rec)
    except Exception as e:
        tg.send("(steward error: %s)" % str(e)[:150], agent="steward"); return "error"

if __name__ == "__main__":
    print("route:", process_message({"text": " ".join(sys.argv[1:])}))
