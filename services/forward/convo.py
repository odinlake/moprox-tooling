#!/usr/bin/env python3
"""The shared conversation log — one append-only transcript of every message in and out, across all
agents. The "shared state store" of the supervisor pattern: the sender is always recorded, the router
marks each inbound message with the agent it routed to, and the whole history is searchable.

Files (all under ~/.local/share/moprox/):
  conversation.jsonl          the live, recent log (kept bounded by compaction)
  conversation-archive.jsonl  older raw turns, rolled off the live log (lossless, still searchable)
  conversation-digest.md      rolling LLM summary of archived turns (the "compacted conversation")

Agents are NOT force-fed the transcript — they have standing context (CLAUDE.md + memory) and are
handed the routed message. Reading more is OPTIONAL, via the `convo` helper:
  convo tail [N=12]            recent turns (windowed)
  convo search REGEX [LIM=20]  matching turns across live + archive, plus digest hits
  convo digest                 the compacted summary of older conversation

Context-size management (the high-water-mark pattern): when the live log passes MAX_LINES, the
oldest turns beyond KEEP_TAIL are appended to the archive, summarised into the digest, and dropped
from the live log. Recent turns stay verbatim; nothing is lost.
"""
import json, os, re, sys, time
from pathlib import Path

D = Path.home() / ".local/share/moprox"
LOG     = D / "conversation.jsonl"
ARCHIVE = D / "conversation-archive.jsonl"
DIGEST  = D / "conversation-digest.md"
AGENTS = ("coach", "dev", "steward")
MAX_LINES = int(os.environ.get("CONVO_MAX_LINES", "1200"))   # high-water mark for the live log
KEEP_TAIL = int(os.environ.get("CONVO_KEEP_TAIL", "400"))    # recent turns kept verbatim after compaction

def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", int(time.time()))
    with open(path, "a") as f: f.write(json.dumps(rec) + "\n")

def log_in(text, msg_id=None, reply_to=None, to=None):
    _append(LOG, {"dir": "in", "from": "operator", "to": to, "msg_id": msg_id,
                  "reply_to": reply_to, "text": text})

def log_out(agent, text, msg_id=None):
    _append(LOG, {"dir": "out", "from": agent, "msg_id": msg_id, "text": text})

def _load(path, n=None):
    if not path.exists(): return []
    lines = path.read_text().splitlines()
    out = []
    for ln in (lines[-n:] if n else lines):
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

def agent_for_msg(msg_id):
    """Which agent sent the message the operator replied to (live first, then archive)."""
    if not msg_id: return None
    for path in (LOG, ARCHIVE):
        for r in reversed(_load(path)):
            if r.get("dir") == "out" and r.get("msg_id") == msg_id and r.get("from") in AGENTS:
                return r.get("from")
    return None

def last_agent():
    """The agent that spoke most recently (the recency / last-speaker default)."""
    for r in reversed(_load(LOG)):
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
    """The last n turns of the LIVE log as plain text (windowed read, sensible default)."""
    return _fmt(_load(LOG, n))

def search(pattern, limit=20, ignore_case=True):
    """Turns matching `pattern` (regex) across archive + live, plus any digest lines, most recent."""
    try: rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e: return "bad regex: %s" % e
    hits = [r for r in (_load(ARCHIVE) + _load(LOG)) if rx.search(r.get("text") or "")]
    res = _fmt(hits[-limit:]) if hits else "(no matches for %r)" % pattern
    if DIGEST.exists():
        dl = [l for l in DIGEST.read_text().splitlines() if rx.search(l)]
        if dl: res += "\n\n[digest]\n" + "\n".join(dl[-limit:])
    return res

# --- context-size management (high-water-mark compaction) -----------------------------------------

def over_highwater():
    if not LOG.exists(): return False
    with open(LOG) as f:
        return sum(1 for _ in f) > MAX_LINES

def harvest():
    """Roll everything older than KEEP_TAIL off the live log into the archive (lossless) and return
    those harvested turns as text, for the caller to summarise into the digest. Mechanical, no LLM."""
    lines = LOG.read_text().splitlines() if LOG.exists() else []
    if len(lines) <= KEEP_TAIL: return ""
    old, tail = lines[:-KEEP_TAIL], lines[-KEEP_TAIL:]
    with open(ARCHIVE, "a") as f:
        for ln in old: f.write(ln + "\n")
    LOG.write_text("\n".join(tail) + "\n")
    rows = []
    for ln in old:
        try: rows.append(json.loads(ln))
        except Exception: pass
    return _fmt(rows)

def add_digest(summary):
    DIGEST.parent.mkdir(parents=True, exist_ok=True)
    with open(DIGEST, "a") as f:
        f.write("\n## compacted %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M"), (summary or "").strip()))

def read_digest():
    return DIGEST.read_text() if DIGEST.exists() else "(no digest yet)"

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tail"
    if cmd == "tail":
        print(transcript(int(sys.argv[2]) if len(sys.argv) > 2 else 12))
    elif cmd == "search" and len(sys.argv) > 2:
        print(search(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20))
    elif cmd == "digest":
        print(read_digest())
    else:
        print("usage: convo [tail N | search REGEX [LIMIT] | digest]")
