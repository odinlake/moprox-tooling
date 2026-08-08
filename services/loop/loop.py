#!/usr/bin/env python3
"""The continuous-agent harness: one bounded cycle per invocation, run as a systemd oneshot.

Not a `while True`. Each wake is a fresh `claude -p`, seeded from a durable ledger, wrapped in four
things the agent cannot opt out of because they happen *around* it:

  1. BUDGET gate      — spend in the last window vs a cap, before anything is spawned.
  2. LIVENESS         — we consume `--output-format stream-json` and reset an idle timer on every
                        event. A long task that keeps emitting is fine; SILENCE is what gets killed.
                        Measuring silence rather than duration is what lets slow work and stuck work
                        be told apart at all.
  3. PROOF            — the agent writes proposals as files; the harness RUNS their verifier scripts.
                        The model never grades its own homework.
  4. LEDGER           — what is in flight is checkpointed, so killing a cycle costs minutes, not work.
                        That is what makes the watchdog safe to be blunt.

Published narration: we re-emit the model's own `thinking` / `text` / `tool_use` as short readable
lines on stdout, which systemd puts in the journal and the fleet lane ships to logview (mo.lan/logs).
`tool_result` is deliberately DROPPED — it is the only block that reaches megabytes, and the UI
collapses it too. Full fidelity stays in ~/.claude/projects/<slug>/*.jsonl on the box for 90 days.
"""
import fcntl, json, os, queue, re, signal, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from errlog import err, warn, Skips        # repo hard rule: no silent errors

HOME     = Path.home()
STATE    = HOME / ".local/share/moprox/loop"
LEDGER   = STATE / "ledger.json"
PROPOSALS= STATE / "proposals"          # the agent drops {claim, verify, expect} files here
STOP     = STATE / "STOP"               # touch to halt every loop agent; checked first
USAGE    = HOME / ".local/share/moprox/agent-usage.jsonl"   # shared with run.py
AGENTS   = HOME / "projects/private-data/agents"
MEMORY   = HOME / "projects/moprox-memory"
CLAUDE   = os.environ.get("CLAUDE_BIN") or str(HOME / ".local/bin/claude")

IDLE_MAX_S   = int(os.environ.get("LOOP_IDLE_MAX_S", 600))    # silence before we call it stuck
HARD_MAX_S   = int(os.environ.get("LOOP_HARD_MAX_S", 5400))   # backstop; systemd caps this too
BUDGET_H     = float(os.environ.get("LOOP_BUDGET_WINDOW_H", 5))   # Max plan resets on ~5 h windows
BUDGET_CAP   = float(os.environ.get("LOOP_BUDGET_CAP_USD", 8.0))  # loop's own share of that window
MAX_STRIKES  = int(os.environ.get("LOOP_MAX_STRIKES", 3))     # consecutive bad cycles before halting
VERIFY_MAX_S = int(os.environ.get("LOOP_VERIFY_MAX_S", 300))

TOOLS = "Bash,Read,Write,Edit,Grep,Glob,WebSearch,WebFetch,Task,TodoWrite,NotebookEdit"


# --- journal-friendly output ------------------------------------------------
# systemd parses a leading <N> severity prefix on stdout, so logview gets priority colouring and
# filtering for free — no structured-field plumbing needed.
def say(text, pri=6, agent=""):
    tag = f"loop[{agent}] " if agent else "loop "
    for line in str(text).splitlines() or [""]:
        print(f"<{pri}>{tag}{line}", flush=True)


def _clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _wrap(s, width=96):
    words, out, cur = str(s).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


# --- ledger -----------------------------------------------------------------
def load_ledger():
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception as exc:
            # Falling back to a blank ledger would silently discard every tried/accepted item and
            # the loop would cheerfully re-derive months of dead ends. Preserve and refuse instead.
            bad = LEDGER.with_suffix(".corrupt")
            LEDGER.rename(bad)
            err(f"ledger unreadable, preserved at {bad} — REFUSING to start with a blank one", exc)
            raise SystemExit(1)
    return {"cycle": 0, "strikes": 0, "open": [], "tried": [], "accepted": [], "inflight": None}


def save_ledger(l):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(l, indent=1))
    tmp.replace(LEDGER)


# --- budget -----------------------------------------------------------------
def spent_recently(hours):
    if not USAGE.exists():
        return 0.0
    cut, tot = time.time() - hours * 3600, 0.0
    lines = USAGE.read_text().splitlines()
    skips = Skips("parsing agent-usage.jsonl")
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception as exc:
            skips.add(exc); continue
        if str(r.get("agent", "")).startswith("loop") and (r.get("ts") or 0) >= cut:
            tot += float(r.get("cost_usd") or 0)
    skips.report(total=len(lines))
    return tot


def log_usage(agent, res, outcome):
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    u = (res or {}).get("usage") or {}
    rec = {"ts": int(time.time()), "agent": f"loop-{agent}", "outcome": outcome,
           "cost_usd": (res or {}).get("total_cost_usd") or 0,
           "turns": (res or {}).get("num_turns"), "ms": (res or {}).get("duration_ms"),
           "in": u.get("input_tokens"), "out": u.get("output_tokens"),
           "error": (res or {}).get("is_error")}
    with open(USAGE, "a") as f:
        f.write(json.dumps(rec) + "\n")


# --- the model's narration → readable lines ---------------------------------
def emit_event(e, agent, state):
    """Re-emit one stream event as human-readable lines. Returns True if it was worth printing."""
    t = e.get("type")
    if t == "assistant":
        printed = False
        for c in (e.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            k = c.get("type")
            if k == "thinking":
                for ln in _wrap(_clip(c.get("thinking", ""), 300)):
                    say(f"~ {ln}", 6, agent); printed = True
            elif k == "text" and c.get("text", "").strip():
                for ln in _wrap(_clip(c["text"], 400)):
                    say(f"> {ln}", 6, agent); printed = True
            elif k == "tool_use":
                inp = c.get("input") or {}
                # one-line target, never the payload
                target = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                         or inp.get("url") or inp.get("description") or ""
                say(f"· {c.get('name','?'):<6} {_clip(target, 80)}", 6, agent); printed = True
                state["tools"] += 1
        return printed
    if t == "result":
        return False          # summarised by the caller
    return False              # system/user(tool_result)/rate_limit — deliberately not published


# --- spawning ---------------------------------------------------------------
def run_cycle_agent(agent, prompt):
    """Spawn claude, stream it, enforce idle + hard timeouts. -> (result_dict|None, outcome)."""
    cwd = AGENTS / agent
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env.update({
        "PATH": f"{HOME}/.local/bin:" + env.get("PATH", "/usr/bin:/bin"),
        # Nothing may wait on a human: no pagers, no prompts, no interactive apt/ssh.
        "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
        "DEBIAN_FRONTEND": "noninteractive", "CI": "1",
    })
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions", "--allowedTools", TOOLS,
           "--add-dir", str(HOME / "projects/moprox-tooling"),
           str(HOME / "projects/private-data"), str(HOME / "projects/moprox-memory"),
           str(STATE)]
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1, start_new_session=True)

    q, done = queue.Queue(), object()

    def pump():
        # iter(readline) NOT `for ln in p.stdout` — file-object iteration uses an internal read-ahead
        # buffer that blocks until it fills, so events arrive in clumps or not at all. That silence
        # looks exactly like a hung agent to the watchdog below, and cost one 9-minute false "stuck".
        try:
            for ln in iter(p.stdout.readline, ""):
                q.put(ln)
        finally:
            q.put(done)

    def pump_err():
        # NEVER discard stderr: a claude that fails to start is otherwise indistinguishable from one
        # that is merely quiet, and you wait out the whole idle timeout to learn nothing.
        try:
            for ln in iter(p.stderr.readline, ""):
                if ln.strip():
                    say(f"! {_clip(ln, 200)}", 4, agent)
        except Exception as exc:
            err("stderr pump died — claude errors will be invisible for this cycle", exc)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()

    state = {"tools": 0}
    stream_skips = Skips("parsing stream-json events")
    result, outcome, started, last = None, "ok", time.time(), time.time()
    while True:
        budget_left = HARD_MAX_S - (time.time() - started)
        if budget_left <= 0:
            outcome = "hardcap"; break
        try:
            item = q.get(timeout=min(15, max(1, IDLE_MAX_S)))
        except queue.Empty:
            idle = time.time() - last
            if idle > IDLE_MAX_S:
                outcome = "stuck"; break
            # Heartbeat so a quiet agent is visibly distinct from a dead harness.
            if int(idle) and int(idle) % 60 < 16:
                say(f"… waiting {int(idle)}s (idle limit {IDLE_MAX_S}s)", 6, agent)
            continue
        if item is done:
            break
        last = time.time()
        try:
            e = json.loads(item)
        except Exception as exc:
            stream_skips.add(exc); continue
        if e.get("type") == "result":
            result = e
        else:
            emit_event(e, agent, state)

    stream_skips.report()
    if outcome != "ok":
        say(f"KILLED ({outcome}) after {int(time.time()-started)}s, {state['tools']} tool calls "
            f"— last activity {int(time.time()-last)}s ago", 3, agent)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM); time.sleep(3)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass          # benign: it already exited between the SIGTERM and the SIGKILL
    p.wait(timeout=30)
    return result, outcome


# --- proof ------------------------------------------------------------------
# Classes git cannot undo, matched against the verifier BEFORE it runs. The verifier is Python
# executed via `python -c`, so these are python idioms rather than shell ones. Defence in depth
# alongside guest containment — emphatically not a sandbox.
DENY = [
    (r"subprocess|os\.system|os\.popen|pty\.|shutil\.rmtree", "spawning processes or bulk deletion"),
    (r"requests\.(post|put|patch|delete)|urlopen\([^)]*data=|smtplib|\.sendall\(", "outward sends"),
    (r"\bgit\b.*\b(push|reset --hard|rebase|filter-branch)\b|--force", "pushes / history rewrites"),
    (r"\bpct\b|\bqm\b|systemctl\s+(start|stop|restart|enable|disable)", "host or service changes"),
    # Must require a MODE ARGUMENT. Matching a quote followed by w/a/x also matched the
    # FILENAME: open('x.json') tripped it because the name starts with "x".
    (r"open\([^)]*,\s*(mode\s*=\s*)?['\"][^'\"]*[wax+]", "opening a file for writing"),
]


def deny_reason(script):
    for pat, why in DENY:
        if re.search(pat, script, re.I):
            return why
    return None


def verify(prop, agent):
    """Run the proposal's own check. The harness executes it; the agent only wrote it."""
    script = prop.get("verify")
    if not script:
        return False, "no verify script"
    why = deny_reason(script)
    if why:
        # A verifier is meant to OBSERVE. One that mutates is not proving a claim, it is taking an
        # action the loop was never authorised to take.
        say(f"REFUSED verifier: attempts {why}", 3, agent)
        return False, f"REFUSED: verifier attempts {why}"
    # A verifier that touches nothing cannot fail, so it proves nothing: `print("ok")` with
    # expect="ok" passes trivially. Require evidence that it actually consults the world. This is a
    # HEURISTIC, not a proof — a determined tautology still gets through, which is why the digest
    # reports evidence alongside every accepted claim for you to eyeball.
    if not any(tok in script for tok in (
            "open(", "glob", "json.load", "read_text", "subprocess", "Path(", "os.listdir",
            "os.walk", "os.scandir", "sqlite3", "requests", "urllib", "csv.", "pandas")):
        return False, "verifier performs no I/O — it cannot fail, so it proves nothing"
    try:
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           timeout=VERIFY_MAX_S, stdin=subprocess.DEVNULL,
                           cwd=str(HOME), env={**os.environ, "PAGER": "cat"})
    except subprocess.TimeoutExpired:
        return False, f"verifier timed out after {VERIFY_MAX_S}s"
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return False, f"verifier exit {r.returncode}: {_clip(r.stderr or out, 200)}"
    expect = str(prop.get("expect", "")).strip()
    ok = (expect.lower() in out.lower()) if expect else bool(out)
    return ok, _clip(out, 200)


def promote(prop, evidence, agent):
    """Land a VERIFIED finding as a memory fact + journal line, then push.

    Deliberately the harness's job, not the agent's: the agent writes its proposal before it knows
    whether the claim survives, so anything it wrote itself would be a finding published on its own
    say-so. This runs only after the verifier passed.
    """
    slug = str(prop.get("novelty_key") or "").strip() or None
    body = str(prop.get("fact") or "").strip()
    if not slug or not body:
        warn(f"accepted claim has no slug/fact body — not promoted to memory ({_clip(prop.get('claim'), 60)})")
        return False
    scope = str(prop.get("scope") or "global")
    doc = (f"---\nname: {slug}\n"
           f"description: \"{_clip(prop.get('claim',''), 180)}\"\n"
           f"metadata:\n  node_type: memory\n  type: project\n  scope: {scope}\n"
           f"  salience: normal\n  agents: [{agent}]\n---\n\n{body}\n\n"
           f"**Verified {datetime.now().date().isoformat()}** by the loop harness — the claim's own "
           f"check was executed, not taken on assertion: `{_clip(evidence, 300)}`\n")
    (MEMORY / f"{slug}.md").write_text(doc)
    jline = {"ts": datetime.now().date().isoformat(), "agent": agent, "slug": slug,
             "action": "add", "scope": scope, "salience": "normal",
             "note": _clip(prop.get("claim", ""), 90)}
    jf = MEMORY / "journals" / f"{agent}.jsonl"
    jf.parent.mkdir(parents=True, exist_ok=True)
    with open(jf, "a") as fh:
        fh.write(json.dumps(jline) + "\n")
    for cmd in (["git", "add", f"{slug}.md", f"journals/{agent}.jsonl"],
                ["git", "commit", "-q", "-m", f"{agent}: {_clip(prop.get('claim',''), 72)}"],
                ["git", "pull", "-q", "--rebase", "origin", "main"],
                ["git", "push", "-q", "origin", "main"]):
        r = subprocess.run(cmd, cwd=str(MEMORY), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            err(f"promote: `{' '.join(cmd[:2])}` failed for {slug} — fact is written but unpushed",
                RuntimeError(_clip(r.stderr or r.stdout, 200)))
            return False
    return True


def collect_proposals():
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(PROPOSALS.glob("*.json")):
        try:
            out.append((f, json.loads(f.read_text())))
        except Exception as exc:
            bad = f.with_suffix(".bad")
            f.rename(bad)
            err(f"proposal {f.name} is not valid JSON, kept at {bad.name}", exc)
    return out


def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else "analyst"
    STATE.mkdir(parents=True, exist_ok=True)

    # Mutually exclusive across ALL loop agents. They are independently scheduled and nothing waits
    # on them, so overlap buys no latency and costs quota bursts and unattributable interleaving.
    _lock = open(STATE / "loop.lock", "w")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("another loop agent holds the lock — skipping this wake", 5, agent)
        return 0

    if STOP.exists():
        say("STOP flag present — not running", 4, agent); return 0

    led = load_ledger()
    if led.get("strikes", 0) >= MAX_STRIKES:
        say(f"halted: {led['strikes']} consecutive bad cycles. Investigate, then "
            f"reset 'strikes' in {LEDGER}", 3, agent)
        return 0

    spent = spent_recently(BUDGET_H)
    if spent >= BUDGET_CAP:
        say(f"budget: ${spent:.2f} of ${BUDGET_CAP:.2f} in the last {BUDGET_H:g}h — skipping", 5, agent)
        return 0

    led["cycle"] = led.get("cycle", 0) + 1
    cyc = led["cycle"]
    inflight = led.get("inflight")
    save_ledger(led)

    say(f"▸ cycle {cyc} · budget ${spent:.2f}/${BUDGET_CAP:.2f}"
        + (f" · resuming {inflight}" if inflight else ""), 6, agent)

    # The ledger IS the seed: open items, what has already been tried, what was accepted.
    prompt = (
        f"Loop cycle {cyc}. Your standing instructions are in CLAUDE.md — follow them.\n\n"
        f"LEDGER (your durable state; do not re-derive what is already here):\n"
        f"{json.dumps({k: led.get(k) for k in ('open','tried','accepted','inflight')}, indent=1)[:6000]}\n\n"
        f"Do ONE increment this cycle. Write any proposal as a JSON file in {PROPOSALS} "
        f"(schema in CLAUDE.md). Finish by stating in one line what you did."
    )

    for f, _ in collect_proposals():      # stale proposals from a killed cycle
        f.unlink(missing_ok=True)

    res, outcome = run_cycle_agent(agent, prompt)
    log_usage(agent, res, outcome)

    if outcome != "ok":
        led["strikes"] = led.get("strikes", 0) + 1
        led["inflight"] = inflight or f"cycle {cyc} ({outcome})"
        save_ledger(led)
        say(f"✗ cycle {cyc} {outcome} — strike {led['strikes']}/{MAX_STRIKES}", 4, agent)
        return 1

    accepted = rejected = 0
    for f, prop in collect_proposals():
        ok, detail = verify(prop, agent)
        claim = _clip(prop.get("claim", "?"), 90)
        if ok:
            accepted += 1
            led.setdefault("accepted", []).append({"cycle": cyc, "claim": prop.get("claim"),
                                                   "evidence": detail})
            say(f"✓ verify PASS  {claim}  [{detail}]", 6, agent)
            if promote(prop, detail, agent):
                say(f"  → memory: {prop.get('novelty_key')}.md committed + pushed", 6, agent)
        else:
            rejected += 1
            led.setdefault("tried", []).append({"cycle": cyc, "claim": prop.get("claim"),
                                                "why": detail})
            say(f"✗ verify FAIL  {claim}  [{detail}]", 4, agent)
        f.unlink(missing_ok=True)

    # A cycle that produced nothing is not a failure, but a run of them means back off.
    led["strikes"] = 0 if (accepted or rejected) else led.get("strikes", 0)
    led["inflight"] = None
    led["tried"] = led.get("tried", [])[-200:]
    led["accepted"] = led.get("accepted", [])[-200:]
    save_ledger(led)

    cost = (res or {}).get("total_cost_usd") or 0
    turns = (res or {}).get("num_turns") or 0
    secs = int(((res or {}).get("duration_ms") or 0) / 1000)
    say(f"▸ cycle {cyc} done · {accepted} accepted, {rejected} rejected · "
        f"${cost:.2f} · {turns} turns · {secs//60}m{secs%60:02d}s", 6, agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
