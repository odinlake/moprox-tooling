#!/usr/bin/env python3
"""Claude Code PreToolUse hook: one journal line per agent action, in the fleet log.

Installed globally (~/.claude/settings.json on claude-dev and claude-loop), so it covers the dev
sessions, the Telegram agents and every `claude -p` the timers and the loop start. Searchable at
mo.lan/logs as ident `agent-action`.

Why PRE-tool: the record must exist even when the action is what kills the box (an OOM, a reboot,
a hung push). Why this exists: on 2026-10-26 no one could say which agent raised claude-loop's RAM
or merged into main under a running loop cycle; every actor looked identical.

Must never get in the way: always exit 0, never print to stdout, ignore read-only tools.
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import who  # noqa: E402

QUIET = {"Read", "Grep", "Glob", "LS", "ToolSearch", "TodoWrite", "TaskList", "TaskGet",
         "WebSearch", "ListMcpResourcesTool", "ReadMcpResourceTool"}
# Over-redacts on purpose ("the token file" loses "file"): a lost word is cheap, a leaked key is not.
SECRET = re.compile(r"(?i)((?:token|secret|passw(?:or)?d|api[_-]?key|authorization)[\"']?(?:\s*[=:]\s*|\s+)[\"']?"
                    r"(?:bearer\s+|PVEAPIToken=)?)[^\s\"']+|(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")


def summary(tool, inp):
    if tool == "Bash":
        return inp.get("command", "")
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return inp.get("file_path") or inp.get("notebook_path") or ""
    if tool in ("Agent", "Task"):
        return f"{inp.get('subagent_type', '')} {inp.get('description', '')}"
    return json.dumps(inp, ensure_ascii=False, sort_keys=True)


def main():
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    tool = ev.get("tool_name") or "?"
    if tool in QUIET or re.match(r"mcp__.*__(search|get|list|read|query|content|fetch|links)", tool):
        return
    text = SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", summary(tool, ev.get("tool_input") or {}))
    text = " ⏎ ".join(text.splitlines())[:1500]
    agent = who.agent()
    fields = {"SYSLOG_IDENTIFIER": "agent-action", "PRIORITY": "6",
              "MESSAGE": f"{agent} {tool}: {text}", "MOPROX_AGENT": agent, "MOPROX_TOOL": tool,
              "CLAUDE_SESSION": ev.get("session_id", ""), "MOPROX_CWD": ev.get("cwd", "")}
    body = "".join(f"{k}={str(v).replace(chr(10), ' ')}\n" for k, v in fields.items())
    subprocess.run(["logger", "--journald"], input=body, text=True, timeout=3,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    # An audit hook must never block the action it audits, so nothing propagates; but a broken hook
    # is exactly the silent gap this exists to close, so it reports at err. Not via errlog: a hook's
    # stderr goes to Claude Code, not the journal.
    try:
        main()
    except Exception as e:
        subprocess.run(["logger", "-t", "agent-action", "-p", "user.err",
                        f"agent_action.py: hook failed, this action is NOT recorded: {type(e).__name__}: {e}"])
    sys.exit(0)
