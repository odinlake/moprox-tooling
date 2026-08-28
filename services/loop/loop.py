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
import json, os, queue, signal, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from errlog import err, warn, Skips        # repo hard rule: no silent errors
import notify                              # the loop's one outward voice (firehose group)

HOME     = Path.home()
STATE    = HOME / ".local/share/moprox/loop"
LEDGER   = STATE / "ledger.json"
PROPOSALS= STATE / "proposals"          # the agent drops {claim, verify, expect} files here
VERIFIERS= STATE / "verifiers"          # every verifier ever run, verbatim, one file per cycle
OBJECTIONS= STATE / "objections"        # every audit objection ever raised, verbatim, one per lens
STOP     = STATE / "STOP"               # touch to halt every loop agent; checked first
USAGE    = HOME / ".local/share/moprox/agent-usage.jsonl"   # shared with run.py
AGENTS   = HOME / "projects/private-data/agents"
MEMORY   = HOME / "projects/moprox-memory"
CLAUDE   = os.environ.get("CLAUDE_BIN") or str(HOME / ".local/bin/claude")

IDLE_MAX_S   = int(os.environ.get("LOOP_IDLE_MAX_S", 600))    # silence before we call it stuck
HARD_MAX_S   = int(os.environ.get("LOOP_HARD_MAX_S", 5400))   # backstop; systemd caps this too
BUDGET_H     = float(os.environ.get("LOOP_BUDGET_WINDOW_H", 5))   # Max plan resets on ~5 h windows
BUDGET_CAP   = float(os.environ.get("LOOP_BUDGET_CAP_USD", 8.0))  # loop's own share of that window
# NB: spent_recently() sums EVERY loop-* ledger row, refuters included since 9eddaf9c98. The
# deployed cap lives in loop@.service (16) — this default is for a hand-run loop, not production.
MAX_STRIKES  = int(os.environ.get("LOOP_MAX_STRIKES", 3))     # consecutive bad cycles before halting
VERIFY_MAX_S = int(os.environ.get("LOOP_VERIFY_MAX_S", 300))
ADVERSARIAL  = os.environ.get("LOOP_ADVERSARIAL", "1") != "0"
REFUTE_MAX_S = int(os.environ.get("LOOP_REFUTE_MAX_S", 420))
# Verifier source, as shown to a refuter and as archived in the ledger. Both were bounded well
# under what the agent actually writes (the longest so far is 10934 chars), so both were cutting.
# Generous on purpose: the ledger digest never carries this field, so the only cost is disk.
SRC_MAX      = int(os.environ.get("LOOP_SRC_MAX", 40000))
# An audit objection, as captured from the refuter. This was _clip(..., 300) at the point of
# capture, so the ledger never held more: 32 of the 34 objections ever raised came back at exactly
# the cap, 36.6% of their text discarded, and for 15 of them the consequence clause — the part that
# says what the defect MEANS for the claim — fell entirely in the discarded tail. The agent is told
# answering an objection is a full increment while being shown two thirds of one.
OBJ_MAX      = int(os.environ.get("LOOP_OBJ_MAX", 4000))

# Server-level MCP grants: `mcp__<server>` allows every tool that server exposes. Naming tools
# individually is how the analyst ended up with 2 of the estate's 11 servers and a job
# description citing lanes it could not open — an allowlist quietly became a capability policy
# nobody had reviewed. If a server should be off limits, remove it here deliberately.
TOOLS = ("Bash,Read,Write,Edit,Grep,Glob,WebSearch,WebFetch,Task,TodoWrite,NotebookEdit,"
         "mcp__logview,mcp__corpus-search,mcp__mcp-google,mcp__agent-write,"
         "mcp__email-search,mcp__docsearch,mcp__notif-search,mcp__finance-search,"
         "mcp__news-search,mcp__webscout,mcp__totolo")


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


def _src(s, n):
    """Bound a block of SOURCE CODE. Keeps newlines, and says out loud when it cut.

    _clip() is for log lines and prose: it folds all whitespace, newlines included, to single
    spaces. Run a Python program through it and what comes out is not a Python program — it is
    either a syntax error or, if the file opened with a `#!` line, one enormous comment that parses
    to an empty module. Both were happening: the refuter was shown an unmarked 6000-char slice
    (11 of 13 verifiers cut mid-statement) and the ledger kept a _clip'd copy (0 of 33 executable).
    Line structure is the payload here, not decoration.
    """
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + (f"\n\n# ---- TRUNCATED HERE: {len(s) - n} of {len(s)} chars were cut. "
                    f"What you are reading is NOT the whole program. ----")


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
           # All three input counters, because the sole reader (agents/usage.py) computes
           # ctx = in + cache_read + cache_write. Writing only `in` — the UNCACHED remainder —
           # reported a fraction of the real context and made the loop look far cheaper per turn
           # than it is. The estate's own telemetry was the least trustworthy data it held.
           "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
           "cache_read": u.get("cache_read_input_tokens", 0),
           "cache_write": u.get("cache_creation_input_tokens", 0),
           # agents/usage.py computes ctx = in + cache_read + cache_write, and run.py writes all
           # three. This writer used to emit only `in` (the UNCACHED remainder), so usage.py read
           # the missing keys as 0 and reported loop-analyst's standing context as 47 tokens at
           # 0% cache — 707 input tokens against 324,716 output over 457 turns. Keep all three.
           "cache_read": u.get("cache_read_input_tokens", 0),
           "cache_write": u.get("cache_creation_input_tokens", 0),
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
def verify(prop, cyc, agent):
    """Run the proposal's own check. The harness executes it; the agent only wrote it."""
    script = prop.get("verify")
    if not script:
        return False, "no verify script"
    # Keep the source before running it. The ledger's copy is bounded and lives inside a JSON
    # digest; this one is the program itself, on disk, forever. Cycles 26-59 were archived only in
    # the ledger, where _clip() folded every newline and cut 32 of 34 at 4000 chars — 46% of the
    # source gone — and the only reason those are recoverable at all is that /tmp had not been
    # swept. Nothing here should depend on the agent remembering to save its own work.
    try:
        VERIFIERS.mkdir(parents=True, exist_ok=True)
        # A cycle may drop more than one proposal; never let the second silently erase the first.
        dst, n = VERIFIERS / f"c{cyc}.py", 1
        while dst.exists() and dst.read_text() != str(script):
            n += 1
            dst = VERIFIERS / f"c{cyc}-{n}.py"
        dst.write_text(str(script))
    except OSError as exc:
        warn(f"could not archive verifier for cycle {cyc}: {exc}")
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


# Two lenses, deliberately different. A verifier the agent wrote can pass for two unrelated
# reasons: the CHECK is hollow (it does not actually test the claim), or the CHECK is fine but the
# CLAIM says more than the evidence supports. One skeptic asked twice catches the first kind twice
# and the second kind never, so the lenses are split along that seam.
LENSES = [
    ("check", "Does the verifier actually test the stated claim? Look for: an `expect` string that "
              "would appear whatever the data said; a script that recomputes something narrower "
              "than the claim; hard-coded values; a filter that quietly selects the confirming "
              "cases; arithmetic that cannot come out any other way."),
    ("claim", "Is the claim's inference sound given the evidence produced? Look for: correlation "
              "stated as mechanism; a changepoint that is one of many equally good ones; n too "
              "small for the confidence expressed; the non-matching cases never examined; a "
              "generalisation wider than the window actually measured."),
]


def refute(prop, evidence, lens, agent):
    """Spawn one independent skeptic. Returns a defect string, or None if it found nothing.

    Read-oriented tools only, and it is told plainly that vague doubt is not refutation — an
    adversary that can reject on a feeling rejects everything, which is as useless as accepting
    everything. It must name something specific and checkable.
    """
    name, focus = lens
    prompt = (
        f"You are auditing another agent's finding. Your job is to REFUTE it if it is refutable.\n\n"
        f"CLAIM: {prop.get('claim')}\n\nWHY IT MATTERS: {prop.get('why')}\n\n"
        f"EXPECTED SUBSTRING: {prop.get('expect')}\n\n"
        f"THE VERIFIER THAT PASSED:\n```python\n{_src(prop.get('verify'), SRC_MAX)}\n```\n\n"
        f"ITS OUTPUT:\n{_clip(evidence, 1500)}\n\n"
        f"YOUR LENS — {name}: {focus}\n\n"
        f"You may read files and run read-only commands to check. Change nothing.\n"
        f"Only refute if you can name a SPECIFIC, CHECKABLE defect — say what is wrong and how you "
        f"know. Vague doubt, 'more data would help', and style objections are NOT refutations; if "
        f"the finding survives your lens, say so.\n\n"
        f"Your last line must be exactly one JSON object and nothing else:\n"
        f'{{"refuted": true|false, "defect": "one sentence, empty if not refuted"}}'
    )
    # --output-format json for the usage envelope: two refuters run per cycle, each a full agent
    # doing real reading, and NEITHER was ledgered anywhere — log_usage was called once per cycle,
    # for the cycle agent only. Roughly 238 invocations to cycle 119 that no ledger on any host had
    # ever seen, which is also why the Stats page could look plausible while being wrong.
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           "--allowedTools", "Read,Grep,Glob,Bash,mcp__logview,mcp__corpus-search",
           "--add-dir", str(HOME / "projects/moprox-tooling"),
           str(HOME / "projects/private-data"), str(HOME / "projects/moprox-memory")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=REFUTE_MAX_S,
                           stdin=subprocess.DEVNULL, cwd=str(HOME))
    except subprocess.TimeoutExpired:
        warn(f"refuter[{name}] timed out after {REFUTE_MAX_S}s — claim not audited on this lens")
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        err(f"refuter[{name}] exited {r.returncode} — claim not audited on this lens",
            RuntimeError(_clip(r.stderr or out, 200)))
        return None
    # Unwrap the json envelope: {result, usage, total_cost_usd, ...}. The verdict is the last JSON
    # object inside `result`, exactly as before. If the format ever changes, fall back to reading
    # stdout raw — the audit still runs, it just goes unledgered, and that says so.
    try:
        envelope = json.loads(out)
    except Exception:
        envelope = None
    if isinstance(envelope, dict) and "result" in envelope:
        log_usage(agent, envelope, f"refute[{name}]")
        out = (envelope.get("result") or "").strip()
    else:
        warn(f"refuter[{name}] gave no json envelope — verdict read raw, tokens NOT ledgered")
    i, j = out.rfind("{"), out.rfind("}")
    if i < 0 or j <= i:
        warn(f"refuter[{name}] returned no JSON verdict — treating as no objection")
        return None
    try:
        v = json.loads(out[i:j + 1])
    except Exception as exc:
        warn(f"refuter[{name}] verdict was not valid JSON ({exc}) — treating as no objection")
        return None
    if v.get("refuted") and str(v.get("defect", "")).strip():
        return f"[{name}] {_clip(str(v['defect']).strip(), OBJ_MAX)}"
    return None


def adversarial(prop, evidence, agent, cyc=0):
    """Run every lens. Returns the objections raised, empty if the finding survived.

    Each objection is also written to disk verbatim, for the same reason verifiers are: the ledger
    is a bounded digest, and a disputed claim is published nowhere else, so the ledger's copy of the
    objection was the only copy — and it was cut at 300 chars.
    """
    if not ADVERSARIAL:
        return []
    objections = []
    for lens in LENSES:
        d = refute(prop, evidence, lens, agent)
        say(f"  {'✗' if d else '·'} refute[{lens[0]}]: {d or 'no objection'}", 6, agent)
        if d:
            objections.append(d)
            try:
                OBJECTIONS.mkdir(parents=True, exist_ok=True)
                dst, n = OBJECTIONS / f"c{cyc}-{lens[0]}.txt", 1
                while dst.exists() and dst.read_text() != d:
                    n += 1
                    dst = OBJECTIONS / f"c{cyc}-{lens[0]}-{n}.txt"
                dst.write_text(d)
            except OSError as exc:
                warn(f"could not archive objection for cycle {cyc}: {exc}")
    return objections


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


CAPS = Path(__file__).resolve().parent / "capabilities.json"


INCIDENTS_URL = os.environ.get("LOOP_INCIDENTS_URL",
                               "http://logview.lan:8016/api/incidents?window=7d&limit=8")


def open_incidents():
    """The estate's open incident queue, as a prompt block. Empty string when there is nothing.

    The queue is a PULL surface and nothing was pulling: in 72 cycles the analyst called
    open_incidents() six times, and no timer, service or digest anywhere in the estate reads it at
    all. So detections landed in a room with nobody in it — the freshness checker's lane breaches
    and, since 2026-08-13, logscan's error-shaped-log findings. Injecting it makes the queue a
    standing input rather than something the agent has to remember to ask about, the same way the
    DEGRADED block turned capability gaps from invisible into unavoidable.

    Not fatal, and deliberately not a task list: the agent decides whether an incident is the best
    increment this cycle. It cannot ack them (that grant is withheld on purpose), so an incident it
    investigates stays open until a human closes it.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(INCIDENTS_URL, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:
        # NOT "": that is what a clean estate returns, so a dead queue read used to be
        # indistinguishable from nothing-is-wrong. On 2026-08-28 six of twelve cycles took this
        # path (TimeoutError, 10s) while the fleet's monitoring stack was itself collapsing —
        # logscan.service failing, logview.service oom-killed — and every one of those cycles was
        # told, in effect, that the queue was clear. Say it in the prompt instead.
        err("open_incidents: could not read the incident queue — it is UNCHECKED this cycle", exc)
        return ("\n\nOPEN INCIDENTS — UNAVAILABLE this cycle. The queue could not be read "
                f"({type(exc).__name__}: {_clip(str(exc), 120)}), so this is NOT a statement that "
                "the estate is healthy — it is the absence of one. The reader that failed is part "
                "of the same monitoring stack the queue reports on, so a failure here is itself "
                "weak evidence something is wrong. Treat the queue as unknown, not empty.")
    rows = d.get("incidents") or []
    if not rows:
        return ""
    lines = []
    for i in rows:
        # age_hours, not last_seen: the API's raw field is journald's MICROSECOND epoch, which
        # renders as an 16-digit integer nobody can read at a glance.
        try:
            age = float(i.get("age_hours"))
            when = f", last {age:.0f} h ago" if age < 48 else f", last {age / 24:.0f} d ago"
        except (TypeError, ValueError):
            when = ""
        lines.append(f"- [{i.get('score', '?')}] {i.get('host', '?')}/{i.get('unit', '?')} "
                     f"({i.get('kind', '?')}) x{i.get('count', '?')}{when}")
    return ("\n\nOPEN INCIDENTS (the estate's queue — unit failures, stale lanes, and services "
            "logging errors below err level; highest score first):\n" + "\n".join(lines)
            + "\nThese are NOT assignments and nobody has triaged them. Investigating one is a "
              "legitimate increment; so is ignoring them for something better. You cannot "
              "acknowledge them, so say plainly in your closing line if one looks urgent.")


def preflight():
    """Check declared capabilities against reality. Returns a list of human-readable failures.

    The agent cannot notice a tool it was never given — absence has no symptom from the inside. So
    the intent is declared in capabilities.json and checked from the outside, every cycle. An MCP
    server counts as reachable if it answers AT ALL: a streamable-http endpoint returns 406 to a
    bare GET, and 403 is what a Squid block looks like, so any HTTP status means the path is open
    and only a connection error is a real failure.
    """
    import urllib.request, urllib.error
    try:
        caps = json.loads(CAPS.read_text())
    except Exception as exc:
        err(f"preflight: cannot read {CAPS} — capabilities are unchecked this cycle", exc)
        return []

    bad = []
    try:
        registered = set(json.loads((HOME / ".claude.json").read_text()).get("mcpServers", {}))
    except Exception as exc:
        err("preflight: cannot read ~/.claude.json — MCP registration is unchecked", exc)
        registered = None

    for name, url in (caps.get("mcp") or {}).items():
        if registered is not None and name not in registered:
            bad.append(f"MCP {name}: declared but NOT REGISTERED in ~/.claude.json")
            continue
        try:
            urllib.request.urlopen(url, timeout=6)
        except urllib.error.HTTPError:
            pass                                    # any status means we reached it
        except Exception as exc:
            bad.append(f"MCP {name}: unreachable at {url} ({type(exc).__name__})")

    for raw in (caps.get("paths") or []):
        if not Path(raw.replace("~", str(HOME), 1)).exists():
            bad.append(f"path {raw}: declared as a lane but does not exist")

    for raw in (caps.get("creds") or []):
        f = Path(raw.replace("~", str(HOME), 1))
        if not f.exists() or f.stat().st_size == 0:
            bad.append(f"credential {raw}: missing or empty")

    return bad


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


REPOS = ("projects/moprox-tooling", "projects/private-data", "projects/moprox-memory")


def refresh_repos():
    """Bring the agent's repos to origin before the cycle reads its own instructions from them.

    There is no pull timer on this box, so for a while the harness and the agent's CLAUDE.md only
    changed when a human happened to pull by hand — a cycle could run against week-old instructions
    and nothing would say so. Doing it here rather than on a timer ties the refresh to the moment it
    matters and removes the race with a cycle already in flight.

    --rebase, never reset --hard: the agent legitimately commits and pushes to these trees, and
    discarding an unpushed finding to save a pull would be worse than running slightly stale.
    """
    for rel in REPOS:
        d = HOME / rel
        if not (d / ".git").exists():
            continue
        r = subprocess.run(["git", "pull", "-q", "--rebase"], cwd=str(d),
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            err(f"refresh: `git pull --rebase` failed in {rel} — this cycle runs on a stale tree",
                RuntimeError(_clip(r.stderr or r.stdout, 200)))


def ledger_digest(led, budget=26000):
    """The prompt's view of the ledger — clipped per section, not by one slice off the top.

    This used to be `json.dumps({...five keys...}, indent=1)[:7000]`. By cycle 49 the ledger was
    188 kB and `disputed` began at char 100602, so the agent was told "your objections come back in
    your ledger" while receiving none of them, and saw only the first 6 of 32 accepted claims.
    Cycles 47 and 48 both answered the same cycle-46 objection as a result.

    Accepted claims are deliberately the shortest: they are published, so their full text is in
    moprox-memory. Disputed claims are not published anywhere, so this is the agent's only copy.
    """
    def objection(cyc, o, olen):
        """Prefer the verbatim archive on disk over the ledger's bounded copy.

        Objections raised before the archive existed are in the ledger cut at 300 chars; the full
        text was recovered from the refuters' own transcripts and written to OBJECTIONS, so this
        lookup is what makes that repair visible. Only substitutes when the archived text really is
        the same objection, extended.
        """
        name = o.split("]", 1)[0].lstrip("[") if o.startswith("[") else ""
        try:
            full = (OBJECTIONS / f"c{cyc}-{name}.txt").read_text().strip()
        except OSError:
            return _clip(o, olen)
        stem = _clip(o, len(o)).rstrip("…")
        return _clip(full if full.startswith(stem) else o, olen)

    def entry(e, claim, objs=0, olen=1600):
        d = {"cycle": e.get("cycle")}
        if e.get("claim"):
            d["claim"] = _clip(e["claim"], claim)
        for k in ("why", "note", "operator_note"):
            if e.get(k):
                d[k] = _clip(e[k], 300)
        if objs:
            d["objections"] = [objection(e.get("cycle"), o, olen)
                               for o in (e.get("objections") or [])[:objs]]
        return d

    dis = list(led.get("disputed") or [])

    def build(olen, n_old, clip_old, clip_acc, clip_dis, omitted):
        # `disputed` sits ahead of the recoverable sections deliberately. It is the one thing in
        # here that exists nowhere else, and the last-resort slice below eats the tail — so the
        # tail must never be it. Until cycle 86 `disputed` was last, and cycle 86's own prompt was
        # cut 1649 chars short: it lost the whole newest dispute (c82) and never knew.
        old = dis[:-6]
        return {
            "_reading": "accepted claims are clipped to a line — the full fact is published as "
                        "moprox-memory/<slug>.md. disputed claims are published NOWHERE, so what "
                        "is here is all there is; disputed_older is the same list, one line each.",
            "_omitted": omitted or "nothing — this digest is complete",
            "open": led.get("open") or [],
            "inflight": led.get("inflight"),
            "disputed": [entry(e, clip_dis, objs=2, olen=olen) for e in dis[-6:]],
            "tried": [entry(e, 300) for e in (led.get("tried") or [])[-20:]],
            "accepted": [entry(e, clip_acc) for e in (led.get("accepted") or [])],
            "disputed_older": [entry(e, clip_old) for e in old[-n_old:] if n_old],
        }

    # Fit by discarding named material in a fixed order, cheapest loss first, and SAYING what went.
    # A digest that silently ends mid-sentence reads as a complete ledger; one that reports
    # "24 disputed_older entries dropped" is a cycle that knows what it is missing.
    # The rungs used to be a flat list in which `n_old` was swept 44->30->20->10->0 ONLY at
    # clip_acc=160, and every rung after that hard-wired n_old=0. So once the fit search was forced
    # to clip accepted claims it could never put a disputed entry back — the exact inversion of the
    # priority stated above, since accepted claims are recoverable from moprox-memory and these are
    # not. Measured at cycle 160 (50 disputed / 90 accepted): the digest landed at 22905 chars, 3095
    # UNDER the 26000 budget, while announcing "44 of 44 oldest entries dropped to fit the digest
    # budget". Sixteen of them fit at the very rung it settled on. A first-fit walk down a ladder
    # that cannot restore n_old discards material it has room for and blames the budget for it.
    #
    # So: degrade in named stages, and within each stage keep the MOST disputed_older that fits,
    # one at a time rather than in jumps of ten. First fit still wins across stages — the stages are
    # ordered by cost as before — but inside a stage the search is exact. ~n_old json.dumps of a
    # 26 kB structure per stage, once per cycle: microseconds, and the thing being bought is the
    # only copy of a rejected claim.
    n_old = len(dis[:-6])
    stages = [(1600, 140, 160, 450), (1600, 140, 120, 450), (1200, 140, 120, 450),
              (900, 140, 90, 450), (600, 140, 60, 300), (400, 100, 60, 200)]
    ladder = [(olen, n, clip_old, clip_acc, clip_dis)
              for olen, clip_old, clip_acc, clip_dis in stages
              for n in range(n_old, -1, -1)]
    for olen, n, clip_old, clip_acc, clip_dis in ladder:
        omitted = []
        if n < n_old:
            omitted.append(f"disputed_older: {n_old - n} of {n_old} oldest entries dropped to fit "
                           f"the digest budget ({budget} chars); the newest {n} are kept")
        if clip_acc < 160:
            omitted.append(f"accepted: claims clipped to {clip_acc} chars, not 160 — "
                           f"full text is moprox-memory/<slug>.md")
        if olen < 1600 or clip_dis < 450:
            omitted.append(f"disputed: claims clipped to {clip_dis} and objections to {olen} "
                           f"chars — objections are verbatim in "
                           f"{OBJECTIONS}/c<cycle>-<lens>.txt, the claims NOWHERE else")
        s = json.dumps(build(olen, n, clip_old, clip_acc, clip_dis, omitted), indent=1)
        if len(s) <= budget:
            return s
    return s[:budget] + (f"\n… LEDGER DIGEST TRUNCATED at {budget} of {len(s)} chars even after "
                         f"every reduction — say so. What was cut is the TAIL: disputed_older "
                         f"first, then accepted; `disputed` is emitted early and is intact.")


def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else "analyst"
    STATE.mkdir(parents=True, exist_ok=True)
    refresh_repos()

    if STOP.exists():
        say("STOP flag present — not running", 4, agent); return 0

    led = load_ledger()
    if led.get("strikes", 0) >= MAX_STRIKES:
        msg = (f"HALTED after {led['strikes']} consecutive bad cycles. Nothing will run until "
               f"'strikes' is reset in {LEDGER}.")
        say(msg, 3, agent)
        notify.send(msg, agent)
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
        f"{ledger_digest(led)}\n\n"
        f"Anything under 'disputed' passed its own verifier and was then refuted by an "
        f"independent audit; it is NOT published. Answering an objection — by fixing the check, "
        f"narrowing the claim, or showing the objection wrong — is a legitimate increment.\n\n"
        f"Do ONE increment this cycle. Write any proposal as a JSON file in {PROPOSALS} "
        f"(schema in CLAUDE.md). Finish by stating in one line what you did."
    )

    prompt += open_incidents()

    gaps = preflight()
    if gaps:
        for g in gaps:
            err(f"preflight: {g}", RuntimeError("declared capability unavailable"))
        say(f"preflight: {len(gaps)} declared capabilities unavailable", 4, agent)
        notify.send("PREFLIGHT — %d declared capabilities unavailable:\n%s"
                    % (len(gaps), "\n".join("- " + g for g in gaps[:20])), agent)
        prompt += ("\n\nDEGRADED — these were declared available to you and are NOT:\n"
                   + "\n".join("- " + g for g in gaps)
                   + "\nDo not work around them silently; if one blocks the increment you chose, "
                     "say so as your result and pick something else.")

    for f, _ in collect_proposals():      # stale proposals from a killed cycle
        f.unlink(missing_ok=True)

    res, outcome = run_cycle_agent(agent, prompt)
    # run_cycle_agent only reports what the HARNESS did to the process (hardcap/stuck). A run the
    # CLI itself flags as failed comes back outcome="ok" with is_error set — 2026-08-08T08:32:18 was
    # one: 27.8 min, 1 turn, 0 tokens, $0. That cost nothing, so the budget guard stays quiet too,
    # and it took no strike, so an unbroken run of them would never reach MAX_STRIKES. Count it.
    if outcome == "ok" and (res or {}).get("is_error"):
        outcome = "error"
    log_usage(agent, res, outcome)

    if outcome != "ok":
        led["strikes"] = led.get("strikes", 0) + 1
        led["inflight"] = inflight or f"cycle {cyc} ({outcome})"
        save_ledger(led)
        say(f"✗ cycle {cyc} {outcome} — strike {led['strikes']}/{MAX_STRIKES}", 4, agent)
        notify.send(f"cycle {cyc} {outcome} — strike {led['strikes']}/{MAX_STRIKES}", agent)
        return 1

    accepted = rejected = disputed = 0
    for f, prop in collect_proposals():
        ok, detail = verify(prop, cyc, agent)
        claim = _clip(prop.get("claim", "?"), 90)
        if ok:
            say(f"✓ verify PASS  {claim}  [{detail}]", 6, agent)
            objections = adversarial(prop, detail, agent, cyc)
            if objections:
                # DISPUTED, not rejected. A rejected claim goes to `tried`, which the agent is told
                # is finished work — so filing an unresolved objection there would bury a possibly
                # true finding behind a door marked "already answered". Disputed claims come back
                # in the prompt as an objection to ANSWER, which is the useful thing to do with one.
                disputed += 1
                led.setdefault("disputed", []).append(
                    {"cycle": cyc, "claim": prop.get("claim"), "evidence": detail,
                     "verify": _src(prop.get("verify") or "", SRC_MAX),
                     "objections": objections})
                say(f"⚑ DISPUTED  {claim}", 4, agent)
                notify.send("DISPUTED — the verifier passed but the audit objected\n%s\n\n%s"
                            % (prop.get("claim", "?"), "\n".join(objections)), agent)
                f.unlink(missing_ok=True)
                continue
            accepted += 1
            led.setdefault("accepted", []).append(
                {"cycle": cyc, "claim": prop.get("claim"), "evidence": detail,
                 # Kept so a later audit can see what was actually run. Without it a retro-audit
                 # has to reconstruct the check from the claim, which is exactly the guesswork the
                 # verifier exists to remove.
                 "verify": _src(prop.get("verify") or "", SRC_MAX)})
            if promote(prop, detail, agent):
                say(f"  → memory: {prop.get('novelty_key')}.md committed + pushed", 6, agent)
            notify.send("FINDING (verified)\n%s\n\nwhy: %s\nevidence: %s"
                        % (prop.get("claim", "?"), _clip(prop.get("why", ""), 400),
                           _clip(detail, 300)), agent)
        else:
            rejected += 1
            led.setdefault("tried", []).append({"cycle": cyc, "claim": prop.get("claim"),
                                                "why": detail})
            say(f"✗ verify FAIL  {claim}  [{detail}]", 4, agent)
        f.unlink(missing_ok=True)

    # A cycle that produced nothing is not a failure, but a run of them means back off.
    led["strikes"] = 0 if (accepted or rejected or disputed) else led.get("strikes", 0)
    led["inflight"] = None
    led["tried"] = led.get("tried", [])[-200:]
    led["accepted"] = led.get("accepted", [])[-200:]
    led["disputed"] = led.get("disputed", [])[-50:]
    save_ledger(led)

    cost = (res or {}).get("total_cost_usd") or 0
    turns = (res or {}).get("num_turns") or 0
    secs = int(((res or {}).get("duration_ms") or 0) / 1000)
    say(f"▸ cycle {cyc} done · {accepted} accepted, {disputed} disputed, {rejected} rejected · "
        f"${cost:.2f} · {turns} turns · {secs//60}m{secs%60:02d}s", 6, agent)

    # Report the cycle's own last line. A cycle that found nothing still reports: silence is what
    # made thirteen toolless cycles invisible, and "nothing worth doing" is a legitimate result the
    # operator is entitled to see. Only rejected-only cycles stay quiet-ish — they still say so.
    closing = _clip((res or {}).get("result") or "(no closing line)", 600)
    notify.send("cycle %d · %d accepted, %d disputed, %d rejected · $%.2f · %dm%02ds\n\n%s"
                % (cyc, accepted, disputed, rejected, cost, secs // 60, secs % 60, closing), agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
