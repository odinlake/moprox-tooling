# moprox dev sessions — three shared-context remote-control agents

`moprox-dev@.service` is a templated systemd unit that runs three long-lived Claude Code
remote-control sessions — **moprox dev one / two / three** — driven from the Claude app. They share
one memory dir (the shared context, see [`../memory/`](../memory/)) and each runs in a restart loop
with exponential backoff, so a wedged session self-heals **without losing its thread** — restarts resume
the conversation (see below); a fresh one is available on request.

**Status: LIVE on claude-dev since 2026-06-29.** All three are enabled and connected.

## Install / enable (on claude-dev, as root)
```bash
install -m 0755 services/sessions/ensure-folder-trust.py /usr/local/bin/moprox-ensure-folder-trust
install -m 0755 services/sessions/creds-check.py         /usr/local/bin/moprox-creds-check
install -m 0755 services/sessions/dev-launch.sh           /usr/local/bin/moprox-dev-launch
install -m 0755 services/sessions/dev-cycle.sh           /usr/local/bin/moprox-dev-cycle
cp services/sessions/moprox-dev@.service services/sessions/moprox-dev-cycle@* \
   services/sessions/moprox-creds-warn.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now moprox-dev@one moprox-dev@two moprox-dev@three
systemctl enable --now moprox-dev-cycle@one.timer moprox-dev-cycle@two.timer moprox-dev-cycle@three.timer
systemctl enable --now moprox-creds-warn.timer
```
They'll appear in the Claude app as **moprox dev one/two/three**. (Both helpers are required — the
unit's `ExecStartPre` calls them; see "Boot & trust robustness" below.)

## Restart keeps the thread; ask explicitly for a fresh one
```bash
systemctl restart moprox-dev@two          # same thread, reattached
rm ~/.local/state/moprox-dev/two.session && systemctl restart moprox-dev@two   # deliberately fresh
```
`/usr/local/bin/moprox-dev-launch <id>` (from `dev-launch.sh`) sits between the unit and `claude`: it
records the instance's session id in `~/.local/state/moprox-dev/<id>.session` and reattaches with
`--resume` on the next start. Continuity across a *deliberate* reset still lives in the shared memory +
git, not the conversation.

Verified on 2.1.220 — these are the three facts the design rests on:

| | |
|---|---|
| `--remote-control <name> --resume <id>` | **compose.** Resume is validated *before* bridge registration, so a stale id fails cheaply without clogging the registration pool |
| `--session-id <uuid>` twice | **refused** (`Session ID … is already in use`) — an agent can't own one pinned UUID forever, hence the recorded pointer. `--resume` preserves the id, so the pointer stays valid indefinitely |
| `-c/--continue` | **wrong tool here.** All three instances share `WorkingDirectory=/home/mikael` → one project transcript dir; `--continue` takes whichever *sibling* spoke last, so restarting `two` would hijack `three`'s thread |

**A plain `--resume` reattaches to the SAME bridge registration** — measured, not assumed: across a
restart the transcript keeps one unchanged `bridgeSessionId` with `lastSequenceNum` advancing 0 → 6, and
the `claude.ai/code/session_…` URL is unchanged. Good for the app thread (the conversation continues at
the same link), but it means a plain resume would **neuter the weekly recycle**, whose whole job is to
rotate the registration before it can zombie (CC #57715).

Hence the **fork flag**: `moprox-dev-cycle` drops a one-shot `~/.local/state/moprox-dev/<id>.fork`, and
the launcher turns that start into `--resume <old> --fork-session --session-id <new>` — history carried
into a new session id, which *does* rotate the registration (verified live: `cse_0172…` → `cse_01J4A…`).
The flag is consumed whether or not the launch succeeds, so a crash loop can't spray session ids.

| restart kind | path | thread | registration |
|---|---|---|---|
| crash, credential gate, reboot, manual `systemctl restart` | plain `--resume` | kept | reused |
| weekly `moprox-dev-cycle` | `--fork-session` | kept | **rotated** |
| pointer cleared / cap tripped | `--session-id <new>` | dropped | new |

**Fresh-start triggers** (all logged to the PTY log as `moprox-dev-launch <id>: fresh session … (<reason>)`):
no pointer · pointer isn't a UUID · transcript missing · transcript > 200 MiB · transcript untouched > 90 d
· a resume that dies within 60 s (which also clears the pointer, so an unresumable transcript self-heals
instead of pinning the instance in a restart loop). Caps are overridable via
`MOPROX_DEV_MAX_TRANSCRIPT_{BYTES,DAYS}` / `MOPROX_DEV_RESUME_FAIL_WINDOW` in
`~/.config/claude-dev/agent.env`.

### The 5 MiB size cap is a protocol limit — do not raise it
Remote-control **session creation POSTs the entire transcript** in the request body (`events:`), so a
large transcript fails registration outright with the generic `Session creation failed — see debug log`
— upstream [#78825](https://github.com/anthropics/claude-code/issues/78825), measured: 9.9 MB and 10.9 MB
fail, 0.86 MB and below succeed, and *every* live session over 5 MB failed.

This matters more than it looks: that failure is **indistinguishable in the app from the zombie bridge**
the weekly recycle exists to avoid. A generous cap would therefore manufacture the exact symptom this
whole setup prevents — and it would do it on a schedule, since forking copies the transcript forward.
5 MiB sits inside the measured-good band with margin. Disk and context cost are irrelevant to this
number; only the registration payload is.

**Consequence: an in-app thread cannot be permanent.** It must roll over before the wall. For scale,
transcripts in this project dir have historically reached 40, 27 and 20 MiB, and an active session adds
roughly 1–2 MiB per working day — so expect a roll every few weeks, announced in the PTY log.

## Startup recap
On every **resumed** start (never a fresh one — there'd be nothing to recap) the launcher submits a
positional prompt asking for a short "here's where we left off" recap, so the app shows the state of play
without being asked. Verified live: the prompt auto-submits and the reply lands in the app thread.

Rate-limited by `~/.local/state/moprox-dev/<id>.recap` to one per 30 min, so a crash/backoff loop can't
spend a turn every 5 s. Tune with `MOPROX_DEV_SUMMARY_MIN_GAP`; disable with
`MOPROX_DEV_SUMMARY_PROMPT=''`; reword with the same variable.

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
Three `ExecStartPre` gates on the unit. All three exist because of the same failure shape — a session
that wedges **ALIVE**: the process is healthy, so `Restart=always` never fires, `systemctl --failed` is
clean, and the only evidence is on-screen in the PTY log. Every gate therefore turns an invisible
wedge into a visible restart loop, by refusing to launch at all:

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
3. **Credential gate** — `moprox-creds-check --gate` refuses to launch on an expired refresh token.
   Runs first (local and instant; no point waiting on the ≤60s egress probe to start a session that
   can't log in). See "Credential lifecycle" below.

## Credential lifecycle
These sessions authenticate **only** with the shared Claude Max OAuth in `~/.claude/.credentials.json`.
The unit's `UnsetEnvironment=ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN` means an
Anthropic **API key is never involved** — if the sessions are down, the API key is not the cause.

Two timers, ~4 weeks apart in effect:

- **access token** — ~8h, refreshed in-process, needs nothing from us.
- **refresh token** — ~4 weeks (`refreshTokenExpiresAt`), *rotated as a side effect* of that refresh.
  So a continuously-running session stays healthy indefinitely, and the failure only bites after
  **downtime spanning the expiry** — i.e. at a boot, hitting all three sessions at once.

**Outage 2026-07-25.** The box booted with a dead refresh token; all three came up at `Not logged in ·
Run /login` with healthy processes and zero restarts. A later `/login` fixed the file but could not
reach the already-running sessions — they read credentials **once at startup**. Two layers now:

| layer | what | effect |
|---|---|---|
| `moprox-creds-warn.timer` | daily 08:00 ±30m, `Persistent=true`; Telegrams `#dev` when < 5 days left | the expiry stops being a surprise |
| `--gate` `ExecStartPre` | blocks launch on a parsed, expired token | sessions self-heal ~2min after `/login`, no `systemctl` needed |

The gate **fails open** by design — unreadable file, missing file, wrong JSON shape, absent or
non-numeric timestamp all exit 0. It blocks only on a timestamp it actually parsed that is actually in
the past. This check must never itself be the reason all three sessions are down. Verified against all
six malformed variants, plus an end-to-end restart-backoff-then-self-heal test on a throwaway unit.

Human check any time: `moprox-creds-check --status`.

**Triage — which wedge is it?** Grep the PTY log (`~/.local/state/moprox-dev/<id>.log`, see below):

- `Not logged in` → credential wedge. A local `systemctl restart` cures it (once creds are fresh).
- `Session creation failed` / `Remote credentials fetch failed` → server-side **zombie bridge**
  (Claude Code #57715). A local restart does **not** cure it; it needs a remote-control registration
  from another machine to trigger eviction. Do not hammer throwaway `claude --remote-control` test
  sessions to diagnose — each dead one re-clogs the account's registration pool.

## Weekly recycle (zombie-bridge prevention)
`moprox-dev-cycle@{one,two,three}.timer` → `moprox-dev-cycle@.service` → `/usr/local/bin/moprox-dev-cycle
<id>`, one weekday each at 03:00 ±10m (one=Tue, two=Wed, three=Thu). Staggered so **only one session
recycles at a time** — never all three down together. Each run now also drops the `.fork` flag, so it
rotates the bridge registration **without** costing the thread; it is no longer a weekly context reset.
Each run archives the session's PTY log dated (keeps ~2 weeks) then restarts the instance, so a
registration never lives long enough to zombie. **Prevention only** — it cannot cure a formed zombie.

Each session records its PTY to `~/.local/state/moprox-dev/<id>.log` (was `/dev/null`, which hid the
failure text — both wedge signatures above only ever appear on-screen). `script` truncates it on every
restart; the weekly cycle archives it first. Unbounded intra-week — check `du -sh
~/.local/state/moprox-dev` occasionally.

## Retiring the old ad-hoc sessions
This replaces the previous setup (one tmux `claude-remote`/`remote-spawn` session + the single
`claude-remote.service` running `claude-dev`). After the three are up and confirmed in the app:
```bash
systemctl disable --now claude-remote.service          # the old single managed session
tmux kill-session -t claude-remote 2>/dev/null || true  # the old ad-hoc tmux session
```
Note: rolling this out from inside one of the old sessions will end that session when you cut over —
expected.
