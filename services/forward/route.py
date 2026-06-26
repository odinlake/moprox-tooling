#!/usr/bin/env python3
"""Routing: decide which agent handles an inbound message, then run it with full conversation context.

Signal priority (deterministic first — the lesson from production multi-agent systems is to only fall
back to an LLM when there's genuine ambiguity):
  1. explicit address      — message starts with #steward / coach: / @dev ...        -> that agent
  2. telegram reply-to      — operator swipe-replied to an agent's message            -> that agent
  3. steward judgement       — no signal: steward picks, told the last speaker so it can default to
                              the recency/last-speaker continuation unless content says otherwise.

Every agent is invoked with the recent shared transcript (convo.transcript), so none of them is ever
blind to "my previous message". The steward can also be the destination — it answers the operator
directly about routing / the agent setup.
"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
from run import run_agent
import tg, convo

DEV_INBOX = Path.home() / ".local/share/moprox/dev-requests.jsonl"
ADDR = re.compile(r"^\s*[@#]?(steward|coach|dev|valet|theming)\b[\s:,>\-]*", re.I)   # explicit address at the start

def _json(s):
    m = re.search(r"\{.*\}", s or "", re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def decide(rec):
    """Return (agent, reason). Deterministic signals win; steward LLM only for true ambiguity."""
    text = (rec.get("text") or "").strip()
    m = ADDR.match(text)
    if m: return m.group(1).lower(), "explicit address"
    a = convo.agent_for_msg(rec.get("reply_to"))
    if a: return a, "reply-to %s" % rec.get("reply_to")
    last = convo.last_agent() or "coach"
    prompt = ("Route the operator's new Telegram message to ONE agent. Output ONLY "
              '{"route":"coach|dev|steward|valet|theming","reason":"<short>"}.\n'
              "The most recent agent to speak was '%s' — if the new message reads as a continuation, "
              "affirmation, thanks, or short follow-up, route it THERE. Otherwise route by content: "
              "training / workouts / the plan / how a session went -> coach; homelab / dashboard / "
              "infra / code / 'the bot is broken' -> dev; the morning brief / news / markets / "
              "geopolitics / weather / 'more|less of X in the brief' -> valet; literary themes / the "
              "theme ontology / themeontology.org / stories & their themes / the theming repo -> "
              "theming; questions about message routing or the agent setup itself -> steward."
              "\n\nRecent conversation:\n%s\n\nNEW MESSAGE: %s"
              % (last, convo.transcript(12), text))
    d = _json(run_agent("steward", prompt, timeout=120)) or {}
    route = d.get("route") if d.get("route") in ("coach", "dev", "steward", "valet", "theming") else last
    return route, d.get("reason", "steward judgement")

# How an agent pulls conversation history on demand (it is NOT force-fed the transcript).
HISTORY_NOTE = ("You are given only this latest message. You usually need no more — rely on your own "
                "context. If THIS message refers to earlier conversation, run `convo tail [N]` (recent "
                "turns, default 12) or `convo search <regex>` to pull just what you need.")

def handle(agent, rec):
    """Run the chosen agent on the routed message. History is optional — the agent reads it on demand."""
    text = (rec.get("text") or "").strip()
    reply_to = rec.get("msg_id") or None        # thread our reply under the operator's message
    if agent == "coach":
        reply = run_agent("coach",
            "The athlete sent you (#coach) this on Telegram:\n%r\n\n%s\n\nReply concisely, in your voice."
            % (text, HISTORY_NOTE), timeout=600)
        tg.send(reply, agent="coach", reply_to=reply_to)
    elif agent == "dev":
        DEV_INBOX.parent.mkdir(parents=True, exist_ok=True)
        reply = run_agent("dev",
            "The operator sent you (#dev) this on Telegram:\n%r\n\n%s\n\nAct per your autonomy rules: if "
            "it's simple and reversible, DO it now (edit / build / local commit) and reply describing "
            "exactly what you changed; if it's risky / irreversible / outward-facing / complex, do NOT do "
            "it — append a one-line JSON entry to the book of works at %s and say you've queued it. Reply "
            "concisely for Telegram, starting with #dev." % (text, HISTORY_NOTE, DEV_INBOX), timeout=900)
        tg.send(reply, agent="dev", reply_to=reply_to)
    elif agent == "steward":
        reply = run_agent("steward",
            "The operator is talking to YOU (#steward) — usually about how messages are routed or the "
            "agent setup. Their message:\n%r\n\n%s\n\nAnswer directly and briefly (normal prose, NOT "
            "routing JSON), starting with #steward." % (text, HISTORY_NOTE), timeout=120)
        tg.send(reply, agent="steward", reply_to=reply_to)
    elif agent == "valet":
        reply = run_agent("valet",
            "The operator sent you (#valet) this about the morning brief / news / their interests:\n%r\n\n"
            "%s\n\nReply briefly. If they're telling you what to surface more/less of, UPDATE your "
            "valet-memory.md so future briefs reflect it, and confirm. Start with #valet."
            % (text, HISTORY_NOTE), timeout=300)
        tg.send(reply, agent="valet", reply_to=reply_to)
    elif agent == "theming":
        reply = run_agent("theming",
            "The operator sent you (#theming) this on Telegram:\n%r\n\n%s\n\nIf it's a question about "
            "the theme ontology's data, answer it using the totolo MCP (search / get_document). If it "
            "asks you to add or revise a theme/story, prepare the change on a BRANCH in ~/projects/"
            "theming (never master, never force) and say what you changed + that it awaits review. "
            "Reply concisely for Telegram, starting with #theming." % (text, HISTORY_NOTE), timeout=600)
        tg.send(reply, agent="theming", reply_to=reply_to)
    return agent

def summarize_for_digest(old_text):
    """Compact archived conversation turns into a terse digest (the LLM 'compaction' step)."""
    return run_agent("steward",
        "These older Telegram conversation turns are being archived to keep context manageable. "
        "Summarise them into 5-12 terse bullets capturing durable facts, decisions made, open threads, "
        "and anything an agent might need to recall later. No fluff — just the bullets.\n\n%s" % old_text,
        timeout=300)

def process_message(rec):
    """Convenience for the CLI: decide + log + handle inline (the dispatcher splits these)."""
    text = (rec.get("text") or "").strip()
    if not text: return "empty"
    try:
        agent, reason = decide(rec)
        convo.log_in(text, rec.get("msg_id"), rec.get("reply_to"), to=agent)
        return handle(agent, rec)
    except Exception as e:
        tg.send("(routing error: %s)" % str(e)[:150], agent="steward"); return "error"

if __name__ == "__main__":
    print("route:", process_message({"text": " ".join(sys.argv[1:])}))
