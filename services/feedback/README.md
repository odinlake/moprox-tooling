# anthropic-feedback: send feedback to Anthropic from an agent session

When the operator says "send my feedback to Anthropic", or an agent hits a bug in Claude Code or the
Claude apps worth reporting, run this. Do not tell the operator to paste the text somewhere himself.

```bash
anthropic-feedback "Claude app on iPhone: tapping a suggestion in a doc sometimes opens the editor..."
anthropic-feedback --dry-run "..."      # fill in the form, print the review screen, send nothing
some-command | anthropic-feedback -     # text from stdin
```

On success it prints `Feedback sent. Reference ID: <uuid>`. Pass the reference ID on to the operator:
it is what Anthropic support asks for. On any failure it exits 1 with `NOT SENT` and the screen it
was looking at, and nothing has been sent.

## Why it works this way

Agent sessions have no feedback tool, and `claude -p "/feedback ..."` answers "/feedback isn't
available in this environment". The command exists only in the interactive TUI. `send.py` therefore
starts `claude` in a detached tmux session and fills in the form by keystroke:

1. trusts the working directory (an empty scratch directory it created itself);
2. runs `/feedback`, and when a "Feedback drafts" list appears, skips it via "Write new feedback"
   (drafts listed there belong to other sessions: never send or discard them);
3. types the text, picks "This session only", then "Yes, send it";
4. reads the reference ID, kills the tmux session and removes the scratch directory.

Each step waits for the text the form is expected to show. If a Claude Code release changes the
form, the script stops and prints the screen instead of guessing. Fix the expected strings in
`send.py` then; `--dry-run` exercises everything up to the final confirmation.

## What is sent

- The text, as one line (newlines are collapsed: the form's field submits on Enter).
- Environment info (OS, terminal, Claude Code version) and recent error messages of the scratch
  session.
- The scratch session's transcript, which is empty. Nothing from the calling agent's conversation
  goes along, so put everything the reader needs into the text itself.

The form belongs to Claude Code. For feedback about another product (the iOS app, claude.ai, Docs),
say so in the first words of the text.

Keep secrets, tokens and third parties' personal details out of the text.

## Install (on claude-dev)

```bash
ln -sf /opt/moprox-tooling/services/feedback/anthropic-feedback /home/mikael/.local/bin/anthropic-feedback
```

Needs `tmux` and a logged-in Claude Code at `~/.local/bin/claude`. First used 2026-11-20 with
Claude Code v2.1.329.
