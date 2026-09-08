#!/usr/bin/env bash
# What moprox-dev-launch actually invokes, per instance, without spawning a real session.
#
#   bash services/sessions/tests/test_launch.sh
#
# A fake `claude` records its argv instead of registering anything, so this is safe to run on the
# live box: nothing touches the account's bridge registration pool, and MOPROX_DEV_STATE_DIR keeps
# the pointers out of the running sessions' state dir.
#
# It exists because the launcher grew a second kind of agent. The identity block decides a session's
# NAME and its whole system prompt, and both failure modes are quiet ones: a wrong name registers a
# stranger in the app, a wrong project dir makes every start silently "fresh" and drops the thread.
set -u
cd "$(dirname "$0")/../../.." || exit 1
LAUNCH=services/sessions/dev-launch.sh
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
pass=0; fail=0
ok() { if [ "$1" = 1 ]; then echo "PASS  $2"; pass=$((pass+1)); else echo "FAIL  $2"; fail=$((fail+1)); fi; }

cat > "$tmp/claude" <<'FAKE'
#!/usr/bin/env bash
printf '%s\0' "$@" > "$FAKE_ARGV"
FAKE
chmod +x "$tmp/claude"

run() {   # run <instance> <cwd>; leaves argv in $tmp/argv (NUL-separated)
  ( cd "$2" && FAKE_ARGV="$tmp/argv" MOPROX_DEV_CLAUDE="$tmp/claude" \
      MOPROX_DEV_STATE_DIR="$tmp/state" MOPROX_DEV_SUMMARY_PROMPT='' \
      bash "$OLDPWD/$LAUNCH" "$1" >"$tmp/out" 2>&1 )
  echo $?
}
argv() { tr '\0' '\n' < "$tmp/argv"; }

rc=$(run coach /home/mikael/projects/private-data/agents/coach)
ok "$([ "$rc" = 0 ] && echo 1 || echo 0)" "coach launches (rc=$rc)"
ok "$(argv | grep -qx 'moprox coach' && echo 1 || echo 0)" "coach registers as 'moprox coach'"
ok "$(argv | grep -q 'odinlake-ai-coach' && echo 1 || echo 0)" "coach gets the coach system prompt"
ok "$(argv | grep -q 'hints.jsonl' && echo 1 || echo 0)" "...including the health-hints directive"
ok "$(argv | grep -q 'FRESHNESS' && echo 1 || echo 0)" "...and the freshness rule (2026-09-08 stale-lane call)"
ok "$(argv | grep -qx -- '--session-id' && echo 1 || echo 0)" "no pointer yet, so a fresh session id"
ok "$(grep -q 'fresh session' "$tmp/out" && echo 1 || echo 0)" "and it says so"

rm -rf "$tmp/state"
rc=$(run one /home/mikael)
ok "$(argv | grep -qx 'moprox dev one' && echo 1 || echo 0)" "dev one is unchanged: 'moprox dev one'"
ok "$(argv | grep -q 'AGENT_ID=one' && echo 1 || echo 0)" "dev one still gets the memory protocol"
ok "$(argv | grep -q 'odinlake-ai-coach' && echo 0 || echo 1)" "and not the coach prompt"

rc=$(run nosuchagent /home/mikael)
ok "$([ "$rc" = 64 ] && echo 1 || echo 0)" "an unknown instance exits 64 rather than registering (rc=$rc)"
ok "$(grep -q 'unknown instance' "$tmp/out" && echo 1 || echo 0)" "and names itself in the journal"

# The resume path, which is where a wrong project dir would hurt: it would find no transcript, fall
# back to "fresh", and drop the thread without a word. Uses a real transcript already in coach's
# project dir (read-only) so the lookup is the real one.
COACH_PROJ="$HOME/.claude/projects/-home-mikael-projects-private-data-agents-coach"
real=$(ls -t "$COACH_PROJ"/*.jsonl 2>/dev/null | head -1)
if [ -n "$real" ]; then
  sid=$(basename "$real" .jsonl)
  size=$(stat -c%s "$real")
  mkdir -p "$tmp/state"; printf '%s\n' "$sid" > "$tmp/state/coach.session"
  if [ "$size" -le 5242880 ]; then
    rc=$(run coach /home/mikael/projects/private-data/agents/coach)
    ok "$(argv | grep -qx -- '--resume' && echo 1 || echo 0)" "an existing transcript RESUMES (project dir resolved)"
    ok "$(argv | grep -qx "$sid" && echo 1 || echo 0)" "and resumes the recorded id, not the latest"
  else
    echo "SKIP  resume case: newest coach transcript is $((size/1024)) KiB, over the 5 MiB cap"
  fi
  rm -rf "$tmp/state"
else
  echo "SKIP  resume case: no transcript in $COACH_PROJ"
fi

# The project dir is derived, and a wrong one is silent: it just looks like "no transcript".
slug() { ( cd "$1" && printf %s "$PWD" | sed 's#[/.]#-#g' ); }
ok "$([ "$(slug /home/mikael)" = '-home-mikael' ] && echo 1 || echo 0)" "slug: /home/mikael"
want=-home-mikael-projects-private-data-agents-coach
ok "$([ "$(slug /home/mikael/projects/private-data/agents/coach)" = "$want" ] && echo 1 || echo 0)" "slug: coach dir"
ok "$([ -d "$HOME/.claude/projects/$want" ] && echo 1 || echo 0)" "and that project dir exists on disk"

echo; echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
