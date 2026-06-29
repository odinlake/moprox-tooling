# moprox dev sessions — three shared-context remote-control agents

`moprox-dev@.service` is a templated systemd unit that runs three long-lived Claude Code
remote-control sessions — **moprox dev one / two / three** — driven from the Claude app. They share
one memory dir (the shared context, see [`../memory/`](../memory/)) and each runs in a restart loop
with exponential backoff, so a wedged session self-heals and you can ask for a fresh one any time.

## Install / enable (on claude-dev, as root)
```bash
cp services/sessions/moprox-dev@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now moprox-dev@one moprox-dev@two moprox-dev@three
```
They'll appear in the Claude app as **moprox dev one/two/three**.

## "Kill that one, give me a fresh session"
```bash
systemctl restart moprox-dev@two
```
≤5 s later a brand-new bypass session is up. A restart **drops that thread's history by design** —
continuity lives in the shared memory + git, not the conversation.

## Permissions — allow all, but sudo prompts
The unit launches with `--dangerously-skip-permissions` (auto-allow all edits/commands). The one
exception is `sudo`, kept as a human-approval gate via an **`ask` rule** that is honoured even under
bypass. Ensure `~/.claude/settings.json` contains:
```json
{ "permissions": { "defaultMode": "bypassPermissions", "ask": ["Bash(sudo *)"] },
  "skipDangerousModePermissionPrompt": true }
```
(For an *un-bypassable* sudo gate, replace the `ask` rule with a `PreToolUse` hook on `Bash(sudo *)`
returning `permissionDecision: "ask"` — hooks fire before the permission-mode check. The `ask` rule is
simpler and sufficient here, since `sudo` is passwordless on this box and the gate is about
deliberate human approval, not OS auth.)

## Retiring the old ad-hoc sessions
This replaces the previous setup (one tmux `claude-remote`/`remote-spawn` session + the single
`claude-remote.service` running `claude-dev`). After the three are up and confirmed in the app:
```bash
systemctl disable --now claude-remote.service          # the old single managed session
tmux kill-session -t claude-remote 2>/dev/null || true  # the old ad-hoc tmux session
```
Note: rolling this out from inside one of the old sessions will end that session when you cut over —
expected.
