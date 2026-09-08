#!/bin/bash
# moprox-dev-launch <one|two|three|coach> — pick resume-vs-fresh, then run a Claude Code
# remote-control session.
#
# WHY THIS EXISTS
#   A restart used to drop the session's whole thread. It doesn't have to: Claude Code can reattach to a
#   prior conversation with `--resume <session-id>`, and (verified on 2.1.220) that composes with
#   `--remote-control <name>` — resume is validated BEFORE any bridge registration, so a bad id fails
#   cheaply without clogging the account's registration pool.
#
# THE FORK FLAG — DO NOT REMOVE, IT PROTECTS THE ZOMBIE FIX
#   Measured, not assumed: a plain `--resume` reattaches to the SAME bridge registration. The transcript
#   shows one unchanged bridgeSessionId across the restart with lastSequenceNum advancing 0 -> 6, and the
#   claude.ai URL is unchanged. That is exactly what we want for the app thread — but it means a plain
#   resume would NEUTER the weekly recycle, whose entire purpose is to rotate the registration so it never
#   lives long enough to become a server-side zombie (CC #57715).
#   So the weekly recycle (dev-cycle.sh) drops a `<id>.fork` flag, and this script consumes it to launch
#   `--resume <old> --fork-session --session-id <new>`: history is carried into a NEW session id, which
#   rotates the registration. Crash/credential/reboot restarts take the plain-resume path and keep the
#   same bridge. Net: unplanned restarts are free, and the deliberate weekly one still rotates.
#
# WHY A STATE FILE AND NOT A PINNED UUID
#   `--session-id <uuid>` refuses a second use ("Session ID … is already in use"), so an agent cannot own
#   one stable UUID forever. Instead: mint a UUID on a fresh start, record it, and `--resume` it after
#   that — resume PRESERVES the session id, so the recorded pointer stays valid indefinitely.
#
# WHY NOT `--continue`
#   All three instances run with WorkingDirectory=/home/mikael, so they share ONE project transcript dir.
#   `-c/--continue` takes the most-recently-active conversation in the cwd, which is whichever sibling
#   spoke last — restarting `two` would silently hijack `three`'s thread. The pointer must be per-agent.
#
# FAILURE POSTURE (same as the unit's ExecStartPre gates: never be the reason a session can't start)
#   Every check falls back to a FRESH session rather than blocking. A resume that dies within
#   RESUME_FAIL_WINDOW seconds clears the pointer, so an unresumable transcript self-heals on the next
#   restart instead of pinning the instance in a restart loop.
set -u

ID="${1:?usage: moprox-dev-launch <one|two|three|coach>}"

CLAUDE="${MOPROX_DEV_CLAUDE:-/home/mikael/.local/bin/claude}"
# Overridable for the same reason as $CLAUDE below: so the tests can exercise this script
# without writing pointers into the live sessions' state dir.
STATE_DIR="${MOPROX_DEV_STATE_DIR:-/home/mikael/.local/state/moprox-dev}"
# Derived from the CWD, not hardcoded, because instances no longer share one working directory:
# coach runs in its own agent dir so that its CLAUDE.md persona auto-loads. Claude Code names a
# project dir after the path with `/` and `.` replaced by `-`, so /home/mikael is "-home-mikael" and
# the coach dir is "-home-mikael-projects-private-data-agents-coach". Getting this wrong would not
# fail loudly: the size/age checks below would find no transcript and every start would be "fresh",
# silently dropping the thread this whole script exists to keep.
PROJECT_DIR="/home/mikael/.claude/projects/$(printf %s "$PWD" | sed 's#[/.]#-#g')"
STATE="$STATE_DIR/$ID.session"
FORK_FLAG="$STATE_DIR/$ID.fork"   # dropped by dev-cycle.sh; see "THE FORK FLAG" above
SUMMARY_STAMP="$STATE_DIR/$ID.recap"

# Estate MCP servers (google / agent-write / webscout). Passed WITHOUT --strict-mcp-config, so these
# ADD to whatever is configured at user scope rather than replacing it — the claude.ai connectors stay.
# Scoped to the dev sessions here rather than registered at user scope on purpose: only valet and
# theming pass --strict-mcp-config, so a user-scope entry would also land in coach, steward and every
# other `claude -p` run from $HOME, which have no use for 56 Google/Drive/webscout tools.
# Cost: measured 2026-08-11, the three servers' full tool schemas are ~17,700 tok (google 30 tools
# ~14,255, webscout 21 ~2,755, agent-write 5 ~697) against ~242 tok for names alone. Claude Code DEFERS
# tool schemas — names are announced, ToolSearch loads a schema on demand — which is how the hosted
# claude.ai connectors behave in these sessions. Deferral for locally-configured HTTP servers is
# expected but NOT directly confirmed; what IS confirmed is that the servers connect and work
# (`claude -p --mcp-config <this> --allowedTools mcp__google__search_gmail_messages` returned real
# Gmail results). If context ever looks bloated, measure before blaming this.
MCP_CONFIG="${MOPROX_DEV_MCP_CONFIG:-/home/mikael/projects/moprox-tooling/services/sessions/mcp.json}"
MCP_ARGS=()
[ -r "$MCP_CONFIG" ] && MCP_ARGS=(--mcp-config "$MCP_CONFIG")

# Startup recap: submitted automatically when a thread is RESUMED (never on a fresh session — there is
# nothing to recap). Set MOPROX_DEV_SUMMARY_PROMPT='' to turn it off.
SUMMARY_PROMPT="${MOPROX_DEV_SUMMARY_PROMPT-This session just reconnected after a restart. Without running any tools, give a 3-6 bullet recap of what we were working on and exactly where we left off, ending with the single most useful next step. Be terse. If the thread has no substantive history, reply only: (reconnected, no prior work in this thread).}"
SUMMARY_MIN_GAP="${MOPROX_DEV_SUMMARY_MIN_GAP:-1800}"   # seconds; suppresses recaps during a restart loop

# Resume caps. THE SIZE CAP IS A HARD PROTOCOL LIMIT, NOT A COST TUNABLE — do not raise it.
# Remote-control session creation POSTs the ENTIRE transcript in the request body (`events:`), so large
# transcripts fail registration outright with the generic "Session creation failed — see debug log"
# (upstream #78825, with measurements: 9.9 MB and 10.9 MB fail; 0.86 MB and below succeed; every >5 MB
# live session failed). That failure is indistinguishable in the app from the zombie bridge we recycle to
# avoid — so an over-generous cap would MANUFACTURE the exact symptom this whole setup exists to prevent.
# 5 MiB keeps us inside the measured-good band with margin. Disk and context cost are irrelevant here;
# only the registration payload matters.
MAX_BYTES="${MOPROX_DEV_MAX_TRANSCRIPT_BYTES:-5242880}"    # 5 MiB — see #78825, do not raise
MAX_DAYS="${MOPROX_DEV_MAX_TRANSCRIPT_DAYS:-90}"           # untouched for this long -> start fresh
RESUME_FAIL_WINDOW="${MOPROX_DEV_RESUME_FAIL_WINDOW:-60}"  # seconds; faster than this = resume is broken

mkdir -p "$STATE_DIR"

say() { echo "moprox-dev-launch $ID: $*"; }

reason=""
sid=""
resume=0

if [ -s "$STATE" ]; then
  sid=$(tr -d '[:space:]' < "$STATE")
  tx="$PROJECT_DIR/$sid.jsonl"
  if ! [[ "$sid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    reason="recorded session id is not a UUID"
  elif [ ! -f "$tx" ]; then
    reason="no transcript on disk for $sid"
  elif [ "$(stat -c%s "$tx" 2>/dev/null || echo 0)" -gt "$MAX_BYTES" ]; then
    # KiB, not MiB: integer division renders anything under a gigabyte-scale cap as "0 MiB exceeds 0 MiB",
    # and this line is triage output.
    reason="transcript $(( $(stat -c%s "$tx") / 1024 )) KiB exceeds $(( MAX_BYTES / 1024 )) KiB cap"
  elif [ -n "$(find "$tx" -mtime "+$MAX_DAYS" -print -quit 2>/dev/null)" ]; then
    reason="transcript idle more than $MAX_DAYS days"
  else
    resume=1
  fi
else
  reason="no recorded session"
fi

# --- who this instance is -----------------------------------------------------------------------
# The only per-agent part of this script. Everything above and below is shared, which is the point:
# the resume/fork/fallback machinery was expensive to get right and must not exist in two copies.
#
# The dev trio share one working directory, one context and the memory protocol. coach is a
# different agent: it runs in its own directory so that its persona, physiology reference and
# durable notebook auto-load from the CLAUDE.md there, and it needs none of the fact-store protocol
# because it maintains its own notebook instead.
case "$ID" in
  one|two|three)
    RC_NAME="moprox dev $ID"
    PROMPT="You are moprox dev $ID (AGENT_ID=$ID), one of three Claude Code dev sessions that SHARE one memory dir at /home/mikael/projects/moprox-memory. Read /home/mikael/projects/moprox-tooling/services/memory/PROTOCOL.md once at session start and follow it. In short: at task start read CHANGES.md to see what the other two learned; when you save a durable fact, set metadata scope (global or project:NAME), salience, and agents: [$ID], and append one line to journals/$ID.jsonl describing it. Never hand-edit MEMORY.md, CHANGES.md, CONFLICTS.md or .reconcile-state.json — a 10-min reconciler rebuilds those from the fact files + journals."
    ;;
  coach)
    RC_NAME="moprox coach"
    PROMPT="You are odinlake-ai-coach, running as a Claude Code session Mikael talks to from the Claude app. Your persona, physiology reference and durable notebook auto-load from the CLAUDE.md in this directory and are binding. Two things differ from your Telegram lane, where you are invoked one-shot per session post. FIRST, this is a CONVERSATION, not a session post: the one-post-with-its-caption rule governs Telegram, and here you answer at whatever length the question deserves. You can still send a chart to Telegram with lib.send_read when a chart is the answer, and say when you have. SECOND, you persist: this thread resumes across restarts, so continue it rather than reintroducing yourself. Your remit here is Mikael's health and training as a whole, wider than one session read: the training lanes (private-data/polar/incoming, private-data/technogym/cardio, the classified history at ~/.cache/moprox-dashboard-data/training/sessions.json), the health lanes under private-data (ultrahuman, apple-health, fitbit), and the physiology in athlete.json. Compute rather than assert, cite what you read, and keep the no-claptrap rule: if a number matters, calculate it from the data, and if you do not know, say so and go and find out. Write durable learnings to /home/mikael/projects/moprox-memory/agents/coach-memory.md by that REAL path, because the copy in this directory is a symlink and the harness refuses to write through one. Your one-shot Telegram invocations write that same file, so re-read it immediately before editing rather than trusting the copy loaded at startup. Standing directive: when Mikael lets a health remark slip in passing (sleep, a niggle, illness, fuelling, stress, travel), append it to /home/mikael/projects/private-data/health/hints.jsonl, because those hints are what later explain the ring and the training data. FRESHNESS, which is the one way this lane is systematically worse than your Telegram one: a one-shot invocation re-reads the world every time it speaks, and you do not. An observation you made earlier in this thread may be many hours and several timer runs old. So re-check, in the same turn you say it, ANY claim about what data exists, when a lane last updated, or what the newest session is. On 2026-09-08 you told Mikael the Ultrahuman lane had not ingested since Sunday, on a listing you had run eleven hours and two fetches earlier, and used it to qualify a training recommendation. Concretely for that lane: ultrahuman-fetch runs at 10:30 and 19:30, re-fetches a ten-day window, and the CURRENT day's raw file is legitimately tiny until the ring syncs to the API, so a small or absent file for today is not evidence of a broken feed. Check the newest row in ultrahuman/daily.jsonl, not file sizes."
    ;;
  *)
    # Loud and non-zero: an unknown instance would otherwise register a nameless session against the
    # account's bridge pool, which is the one resource this whole design is careful with.
    echo "moprox-dev-launch: unknown instance '$ID' (expected one|two|three|coach)" >&2
    exit 64
    ;;
esac

if [ "$resume" = 1 ]; then
  kib=$(( $(stat -c%s "$PROJECT_DIR/$sid.jsonl") / 1024 ))
  # Consume the recycle's fork flag (one-shot: removed whether or not the launch succeeds, so a crash
  # loop can't fork on every retry and spray session ids).
  if [ -e "$FORK_FLAG" ]; then
    rm -f "$FORK_FLAG"
    new=$(uuidgen)
    say "forking $sid -> $new (weekly recycle: carries history into a new bridge registration, ${kib} KiB)"
    printf '%s\n' "$new" > "$STATE"
    set -- --resume "$sid" --fork-session --session-id "$new"
    sid="$new"
  else
    say "resuming $sid (${kib} KiB transcript)"
    set -- --resume "$sid"
  fi
  # Unprompted "where we left off" line on reconnect. Passed as the positional prompt, so it is submitted
  # automatically on start and the operator sees the recap in the app without asking. Rate-limited by a
  # stamp file: a crash/backoff loop must not spend a turn (and a Telegram-visible message) every 5s.
  if [ -n "$SUMMARY_PROMPT" ] && [ -z "$(find "$SUMMARY_STAMP" -newermt "-$SUMMARY_MIN_GAP seconds" -print -quit 2>/dev/null)" ]; then
    touch "$SUMMARY_STAMP"
    set -- "$@" "$SUMMARY_PROMPT"
  else
    say "skipping startup recap (one was issued less than $SUMMARY_MIN_GAP s ago)"
  fi
  started=$SECONDS
  "$CLAUDE" --remote-control "$RC_NAME" "${MCP_ARGS[@]}" "$@" --append-system-prompt "$PROMPT"
  rc=$?
  # Only treat a FAST exit as "this transcript can't be resumed" — a long-lived session that later dies is
  # an ordinary restart and must keep its pointer.
  if [ "$rc" -ne 0 ] && [ $(( SECONDS - started )) -lt "$RESUME_FAIL_WINDOW" ]; then
    say "resume of $sid exited $rc after $(( SECONDS - started ))s — clearing pointer, next start will be fresh"
    rm -f "$STATE"
  fi
  exit "$rc"
fi

sid=$(uuidgen)
say "fresh session $sid ($reason)"
printf '%s\n' "$sid" > "$STATE"
exec "$CLAUDE" --remote-control "$RC_NAME" "${MCP_ARGS[@]}" --session-id "$sid" --append-system-prompt "$PROMPT"
