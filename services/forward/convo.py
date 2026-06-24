#!/usr/bin/env python3
"""The shared conversation log — one append-only transcript of every message in and out, across all
agents. The "shared state store" of the supervisor pattern: the sender is always recorded, the router
marks each inbound message with the agent it routed to, and the whole history is searchable.

  ~/.local/share/moprox/conversation.jsonl
  in :  {"ts","dir":"in","from":"operator","to":<agent|null>,"msg_id","reply_to","text"}
  out:  {"ts","dir":"out","from":<agent>,"msg_id","text"}

Agents do NOT get the transcript force-fed — they have their own standing context (CLAUDE.md +
memory) and are handed the routed message. Reading more is OPTIONAL and on demand, via this module's
helpers / CLI:

  convo.py tail [N=12]            # the last N turns (windowed read, sensible default)
  convo.py search REGEX [LIM=20]  # every turn whose text matches the regex (last LIM)

Volume is tiny (one operator), so we just scan the file.
"""
import json, re, sys, time
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

def _fmt(rows):
    out = []
    for r in rows:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("ts", 0)))
        if r.get("dir") == "in":
            who = "operator" + ((" -> %s" % r["to"]) if r.get("to") else "")
        else:
            who = r.get("from") or "?"
        out.append("%s [%s] %s" % (t, who, (r.get("text") or "").strip()))
    return "\n".join(out)

def transcript(n=12):
    """The last n turns as a plain-text transcript (windowed read, sensible default)."""
    return _fmt(_read(n))

def search(pattern, limit=20, ignore_case=True):
    """Every turn whose text matches `pattern` (regex), most recent `limit`."""
    try: rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e: return "bad regex: %s" % e
    hits = [r for r in _read() if rx.search(r.get("text") or "")]
    return _fmt(hits[-limit:]) if hits else "(no matches for %r)" % pattern

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tail"
    if cmd == "tail":
        print(transcript(int(sys.argv[2]) if len(sys.argv) > 2 else 12))
    elif cmd == "search" and len(sys.argv) > 2:
        print(search(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20))
    else:
        print("usage: convo.py [tail N | search REGEX [LIMIT]]")
