#!/usr/bin/env python3
"""dev-handoff.py <transcript.jsonl> <out.md> -- distil a rolled-over thread into a handoff.

A moprox-dev session that starts FRESH (transcript over the size cap, idle too long, pointer gone)
used to begin with nothing: the resume path gets a "where we left off" recap because the model can
read its own context, and the fresh path had no context to read. This closes that gap. It pulls the
conversation OUT of the old transcript (what the operator typed and what the agent said back; tool
calls, tool output and thinking are dropped, which is why a 5 MiB transcript distils to ~90 KB of
prose), asks a headless `claude -p` for a handoff, and writes it where the launcher can hand it to
the new session as its first prompt.

Falls back to the raw tail of the conversation if the summariser fails or returns nothing, so the
new session always gets SOMETHING true rather than a blank start. Never raises: a broken handoff
must not block a restart.
"""
import json, os, subprocess, sys

MAX_CHARS = int(os.environ.get("MOPROX_HANDOFF_MAX_CHARS", "160000"))   # ~40k tokens of prose
CLAUDE = os.environ.get("CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude")
TIMEOUT = int(os.environ.get("MOPROX_HANDOFF_TIMEOUT_S", "300"))

ASK = """You are writing a HANDOFF for yourself. You are a long-running Claude Code dev session and
your transcript has just hit the size cap, so you are about to continue in a fresh session with no
memory of this thread. Below is the conversation from the old thread (operator turns and your own
replies; tool output omitted). Do NOT use any tools and do not inspect the machine: answer from the
transcript alone. Write the handoff in markdown, terse, no em dashes:

## What was being worked on
5-10 bullets, most recent first. Concrete identifiers: paths, commit hashes, unit names, ids, dates,
numbers. State what is DONE and what is not.
## Rules and decisions the operator gave
Only ones that still apply.
## Open items
Each with who or what it waits on.
## Next step
The single most useful next action, one line.

Do not invent anything not in the transcript. If the transcript is empty or unreadable, say so."""


def conversation(path):
    out = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        t = j.get("type"); m = j.get("message") or {}; c = m.get("content")
        if t == "user" and isinstance(c, str) and c.strip():
            out.append("USER: " + c.strip())
        elif t == "assistant" and isinstance(c, list):
            txt = "\n".join(b.get("text", "") for b in c if b.get("type") == "text").strip()
            if txt:
                out.append("ASSISTANT: " + txt)
    text = "\n\n".join(out)
    if len(text) > MAX_CHARS:
        text = "[... earlier turns omitted ...]\n\n" + text[-MAX_CHARS:]
    return text


def summarise(text):
    r = subprocess.run([CLAUDE, "-p", ASK + "\n\n---- TRANSCRIPT ----\n" + text, "--output-format", "json",
                        "--disallowedTools", "Bash,Read,Edit,Write,Grep,Glob,WebSearch,WebFetch,Agent,NotebookEdit"],
                       capture_output=True, text=True, timeout=TIMEOUT,
                       env={k: v for k, v in os.environ.items()
                            if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")})
    j = json.loads(r.stdout)
    res = (j.get("result") or "").strip()
    if not res or j.get("is_error"):
        raise RuntimeError("empty or error result")
    return res


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    try:
        text = conversation(src)
    except Exception as e:
        text = ""
        print(f"dev-handoff: could not read {src}: {e}", file=sys.stderr)
    body, how = "", "summarised"
    if text:
        try:
            body = summarise(text)
        except Exception as e:
            print(f"dev-handoff: summariser failed ({type(e).__name__}: {str(e)[:120]}); using raw tail", file=sys.stderr)
            body, how = "## Raw tail of the previous thread (summariser unavailable)\n\n" + text[-8000:], "raw tail"
    else:
        body, how = "(the previous transcript held no readable conversation)", "empty"
    with open(dst, "w") as fh:
        fh.write(body.rstrip() + "\n")
    print(f"dev-handoff: {how}, {len(body)} chars -> {dst}")


if __name__ == "__main__":
    main()
