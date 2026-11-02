#!/usr/bin/env python3
"""axis-title.py: UserPromptSubmit hook that titles each MX Axis Mundi chat "MX <topic>".

Operator, 2026-11-02: the app titles a new chat from its first message, which is good, but he wants
the "MX" prefix on it so an estate chat is recognisable in a session list that also holds ordinary
claude.ai sessions. Claude Code's own auto-title cannot take a prefix, and an explicit title (--name,
/rename, a hook's sessionTitle) replaces it outright, so the hook has to supply the topic as well.

Runs on the first prompt only: Claude Code passes `session_title` once a custom title is set, and a
title the operator chose later in the app is never overwritten. The topic comes from a short Haiku
call; if that fails, the first words of the prompt stand in, so a chat is never left untitled.
"""
import json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402

CLAUDE = os.environ.get("AXIS_TITLE_CLAUDE", "/home/mikael/.local/bin/claude")
ASK = ("Give a 2-5 word title for a chat that opens with the message below. Title case, no quotes, "
       "no punctuation at the end, no em dashes. Reply with the title only.\n\n")


def first_words(text, n=6):
    words = " ".join(text.split()).split(" ")
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def topic(prompt):
    try:
        # cwd outside the axis dir, so this one-shot run does not load axis's settings and fire this
        # very hook on its own prompt.
        r = subprocess.run([CLAUDE, "-p", ASK + prompt[:2000], "--model", "haiku",
                            "--strict-mcp-config", "--tools", ""],
                           cwd="/tmp", stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25)
        t = " ".join((r.stdout or "").split()).strip(" \"'.")
        if r.returncode == 0 and 0 < len(t) <= 60:
            return t
        errlog.warn("axis-title: haiku gave no usable title (rc=%d, %r), using the first words"
                    % (r.returncode, (r.stdout or r.stderr or "")[:120]))
    except Exception as exc:
        errlog.warn("axis-title: haiku title call failed (%s), using the first words" % exc)
    return first_words(prompt)


def main():
    try:
        ev = json.load(sys.stdin)
    except ValueError as exc:
        errlog.err("axis-title: unreadable hook input, chat left untitled", exc)
        return 0
    if ev.get("session_title") or not (ev.get("prompt") or "").strip():
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "sessionTitle": "MX " + topic(ev["prompt"])}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
