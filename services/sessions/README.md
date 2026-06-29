# moprox dev sessions — three shared-context remote-control agents

`moprox-dev@.service` is a templated systemd unit that runs three long-lived Claude Code
remote-control sessions — **moprox dev one / two / three** — driven from the Claude app. They share
one memory dir (the shared context, see [`../memory/`](../memory/)) and each runs in a restart loop
with exponential backoff, so a wedged session self-heals and you can ask for a fresh one any time.

**Status: LIVE on claude-dev since 2026-06-29.** All three are enabled and connected.

## Install / enable (on claude-dev, as root)
```bash
install -m 0755 services/sessions/ensure-folder-trust.py /usr/local/bin/moprox-ensure-folder-trust
cp services/sessions/moprox-dev@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now moprox-dev@one moprox-dev@two moprox-dev@three
```
They'll appear in the Claude app as **moprox dev one/two/three**. (The helper is required — the unit's
`ExecStartPre` calls it; see "Boot & trust robustness" below.)

## "Kill that one, give me a fresh session"
```bash
systemctl restart moprox-dev@two
```
≤5 s later a brand-new bypass session is up. A restart **drops that thread's history by design** —
continuity lives in the shared memory + git, not the conversation.

## Permissions — allow all, but sudo prompts
Bypass (auto-allow all edits/commands) comes from **`defaultMode: bypassPermissions` in
`~/.claude/settings.json`** — *not* from a `--dangerously-skip-permissions` flag on the unit. The one
exception is `sudo`, kept as a human-approval gate via an **`ask` rule** honoured even under bypass.
Ensure `~/.claude/settings.json` contains:
```json
{ "permissions": { "defaultMode": "bypassPermissions", "ask": ["Bash(sudo *)"] },
  "skipDangerousModePermissionPrompt": true }
```
(For an *un-bypassable* sudo gate, replace the `ask` rule with a `PreToolUse` hook on `Bash(sudo *)`
returning `permissionDecision: "ask"` — hooks fire before the permission-mode check. The `ask` rule is
simpler and sufficient here, since `sudo` is passwordless on this box and the gate is about
deliberate human approval, not OS auth.)

> **Why no `--dangerously-skip-permissions` flag?** That flag refuses to run in an *untrusted* folder
> and forces an interactive "do you trust this folder?" dialog at startup. A headless `script` PTY
> can't answer it, so every session would wedge **alive but unconnected** (invisible in the app) before
> reaching the relay. `defaultMode: bypassPermissions` gives identical auto-allow behaviour with no
> trust guard. This bit us hard on 2026-06-29 — see below.

## Boot & trust robustness
Two `ExecStartPre` gates on the unit, both learned from a 2026-06-29 reboot that left the old
single-session `claude-remote.service` running-but-unreachable:

1. **Trust self-heal** — `flock … /usr/local/bin/moprox-ensure-folder-trust` re-asserts
   `projects["/home/mikael"].hasTrustDialogAccepted = true` in `~/.claude.json` before each launch.
   Trust lives *only* there (no env/managed-settings knob), it gets **re-armed to false by Claude
   version bumps**, and concurrent sessions can clobber it — so we repair it on every start. Without
   this, an auto-update silently re-wedges all three on their next restart/reboot.
2. **Egress race gate** — polls a real HTTPS request to `api.anthropic.com` before launching.
   `network-online.target` only means interfaces are up, not that DNS/egress works; starting too early
   makes the client fail to register and wedge alive (systemd sees a healthy process → `Restart` never
   fires). Any HTTP response (incl. 404) proves the path. **NB:** when the Squid cutover
   (`infra/vms/claude-dev/use-squid.sh`) is applied, this probe must inherit `https_proxy` or it will
   false-negative and block startup.

## Retiring the old ad-hoc sessions
This replaces the previous setup (one tmux `claude-remote`/`remote-spawn` session + the single
`claude-remote.service` running `claude-dev`). After the three are up and confirmed in the app:
```bash
systemctl disable --now claude-remote.service          # the old single managed session
tmux kill-session -t claude-remote 2>/dev/null || true  # the old ad-hoc tmux session
```
Note: rolling this out from inside one of the old sessions will end that session when you cut over —
expected.
