#!/usr/bin/env python3
"""The shared conversation log — one append-only transcript of every message in and out, across all
agents. This is the "shared state store" of the supervisor pattern: the sender is always recorded,
the steward marks each inbound message with the agent it routed to, and the WHOLE conversation is
available to any agent for context (so nobody is blind to what was just said).

  ~/.local/share/moprox/conversation.jsonl
  in :  {"ts","dir":"in","from":"operator","to":<agent|null>,"msg_id","reply_to","text"}
  out:  {"ts","dir":"out","from":<agent>,"msg_id","text"}

Outbound msg_ids are logged so a Telegram reply-to can be mapped back to the agent that spoke
(deterministic handoff). Volume is tiny (one operator), so we just scan the file.
"""
import json, time
from pathlib import Path

LOG = Path.home() / ".local/share/moprox/conversation.jsonl"
AGENTS = ("coach", "dev", "steward")

def _append(rec):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", int(time.time()))
    with open(LOG, "a") as f: f.write(json.dumps(rec) + "\n")

def log_in(text, msg_id=None, reply_to=None, to=None):
    _append({"dir": "in", "from": "operator", "to": to, "msg_id": msg_id,
             "reply_to": reply_to, "text": text})

def log_out(agent, text, msg_id=None):
    _append({"dir": "out", "from": agent, "msg_id": msg_id, "text": text})

def _read(n=None):
    if not LOG.exists(): return []
    lines = LOG.read_text().splitlines()
    out = []
    for ln in (lines[-n:] if n else lines):
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

def agent_for_msg(msg_id):
    """Which agent sent the outbound message the operator replied to (deterministic handoff)."""
    if not msg_id: return None
    for r in reversed(_read()):
        if r.get("dir") == "out" and r.get("msg_id") == msg_id and r.get("from") in AGENTS:
            return r.get("from")
    return None

def last_agent():
    """The agent that spoke most recently (the recency / last-speaker default)."""
    for r in reversed(_read()):
        if r.get("dir") == "out" and r.get("from") in AGENTS:
            return r.get("from")
    return None

def transcript(n=14):
    """The last n turns as a plain-text transcript for an agent's context."""
    out = []
    for r in _read(n):
        if r.get("dir") == "in":
            who = "operator" + ((" -> %s" % r["to"]) if r.get("to") else "")
        else:
            who = r.get("from") or "?"
        out.append("[%s] %s" % (who, (r.get("text") or "").strip()))
    return "\n".join(out)
