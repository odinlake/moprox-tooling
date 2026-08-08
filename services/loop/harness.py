#!/usr/bin/env python3
"""loop harness — one bounded cycle per wake. The model proposes; the harness disposes.

  harness.py <agent> [--dry-run] [--once]

NOT A HOT LOOP. A systemd timer fires one cycle and the process exits, so we inherit journald,
restart backoff, Persistent=true and `systemctl stop` as a kill switch for free. A `while True:`
would own all of that badly.

Each failure mode of a naive loop maps to one mechanism here:

  grades its own homework  -> the model emits a machine-checkable `verify`; THIS runs it
  drifts / re-derives      -> durable ledger of open/tried/failed, seeded into every prompt
  context rot              -> each wake is a fresh bounded call, not a growing conversation
  plausible-but-wrong      -> adversarial pass, separate call, defaults to reject
  busywork / spinning      -> N dry cycles in a row -> back off
  runaway cost             -> budget gate BEFORE the call, from the real usage ledger
  unsafe outward action    -> DENY patterns on the verifier + propose-don't-dispose

THE VERIFIER IS THE WHOLE POINT, AND THE SHARP EDGE
  An agent that scores its own claim will always find itself persuasive. So the contract is
  {claim, verify, expect}: `verify` is a command the HARNESS runs, and the claim survives only if
  its output matches `expect`. That means executing model-authored commands, which is exactly the
  thing to be careful about. Defence is layered, and none of these alone is sufficient:
    - the loop guest is contained (Squid egress, no data credentials, no SSH out, git-reversible)
    - DENY blocks the classes git cannot undo (outward sends, pushes, host ops, history rewrites)
    - a timeout and an output cap
  This is defence in depth, NOT a sandbox. Do not run this harness on a box holding credentials.
"""
import argparse
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agents"))
import errlog  # noqa: E402

HOME = pathlib.Path.home()
STATE = HOME / ".local/share/moprox/loop"
USAGE = HOME / ".local/share/moprox/agent-usage.jsonl"
LOCK = STATE / "loop.lock"

VERIFY_TIMEOUT = int(os.environ.get("LOOP_VERIFY_TIMEOUT", "120"))
VERIFY_MAX_OUT = int(os.environ.get("LOOP_VERIFY_MAX_OUT", "20000"))
BUDGET_TOKENS = int(os.environ.get("LOOP_BUDGET_TOKENS", "500000"))    # rolling 24h, THIS agent only
DRY_BACKOFF_AT = int(os.environ.get("LOOP_DRY_BACKOFF", "3"))

# Classes git cannot undo. Matched against the verifier command before it is ever executed.
DENY = [
    (r"\bgit\s+push\b", "pushes"),
    (r"\bgit\s+.*--force\b|\bgit\s+.*-f\b(?!ile)", "force-push / forced ops"),
    (r"\bgit\s+(rebase|filter-branch|reset\s+--hard)\b", "history rewrites"),
    (r"\b(pct|qm|virsh)\b", "host / hypervisor ops"),
    (r"\bssh\b|\bscp\b|\brsync\b.*::", "reaching other boxes"),
    (r"\bcurl\b.*-(d|F|-data)|\bwget\b.*--post", "outward sends"),
    (r"\bsudo\b", "privilege escalation"),
    (r"\brm\s+-rf?\s+/", "destructive removal"),
    (r"\bsystemctl\s+(start|stop|restart|disable|enable)\b", "changing services"),
    (r">\s*/dev/(sd|nvme)", "raw device writes"),
]


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def agent_dir(agent):
    d = STATE / agent
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path(agent):
    return agent_dir(agent) / "ledger.jsonl"


def load_ledger(agent, limit=200):
    """Open/tried/failed claims. Seeded into the prompt so the agent does not re-derive what it
    already tried — the single biggest cause of a loop that looks busy and achieves nothing."""
    p = ledger_path(agent)
    if not p.exists():
        return []
    rows = []
    for ln in p.read_text(errors="ignore").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError as e:
            errlog.skip("harness: ledger line", e)
    return rows[-limit:]


def append_ledger(agent, rec):
    rec.setdefault("ts", _now())
    with open(ledger_path(agent), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def tokens_last_24h(agent):
    """Budget from the REAL ledger run.py already writes, not a counter this harness keeps —
    a self-reported budget is the same mistake as a self-graded claim.

    Scoped to THIS agent. Summing the whole estate (the first cut) meant a busy interactive day
    starved a loop agent that had spent nothing — the gate fired on 2.97M tokens none of which
    were the loop's. A budget must meter the thing it is meant to restrain."""
    if not USAGE.exists():
        return 0
    cut, tot = time.time() - 86400, 0
    for ln in USAGE.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if (r.get("ts") or 0) < cut or r.get("agent") != agent:
            continue
        for k in ("in", "out", "cache_read", "cache_write"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                tot += v
    return int(tot)


def deny_reason(cmd):
    for pat, why in DENY:
        if re.search(pat, cmd, re.I):
            return why
    return None


def run_verifier(cmd):
    """Execute a model-authored check. Returns (ok, output, note)."""
    why = deny_reason(cmd)
    if why:
        return False, "", f"REFUSED: verifier attempts {why}"
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=VERIFY_TIMEOUT, cwd=str(HOME))
    except subprocess.TimeoutExpired:
        return False, "", f"verifier timed out after {VERIFY_TIMEOUT}s"
    out = ((p.stdout or "") + (p.stderr or ""))[:VERIFY_MAX_OUT]
    return p.returncode == 0, out, f"exit={p.returncode}"


def call_agent(agent, prompt, dry_run=False):
    """One bounded call via run.py, so usage lands in agent-usage.jsonl like every other agent."""
    if dry_run:
        return json.dumps({"claim": "DRY RUN — no model call was made", "lane": "dry",
                           "verify": "true", "expect": "", "novelty_key": "dry-run"})
    from run import run_agent            # imported late: it is heavy and unused in --dry-run
    return run_agent(agent, prompt)


def parse_proposal(text):
    """The contract is JSON. Models fence it, prefix it, and occasionally apologise around it."""
    if not text:
        raise ValueError("empty response")
    s = text[text.find("{"):text.rfind("}") + 1]
    if not s:
        raise ValueError("no JSON object in response")
    return json.loads(s)


def cycle(agent, dry_run=False):
    stop = agent_dir(agent) / "STOP"
    if stop.exists():
        print(f"  {agent}: STOP flag present ({stop}) — standing down", flush=True)
        return 0

    used = tokens_last_24h(agent)
    if used >= BUDGET_TOKENS:
        errlog.warn(f"harness {agent}: budget spent ({used:,} >= {BUDGET_TOKENS:,} tok/24h for {agent}) — skipping cycle")
        return 0

    ledger = load_ledger(agent)
    tried = [r.get("novelty_key") for r in ledger if r.get("novelty_key")]
    dry_streak = 0
    for r in reversed(ledger):
        if r.get("status") == "dry":
            dry_streak += 1
        else:
            break
    if dry_streak >= DRY_BACKOFF_AT:
        print(f"  {agent}: {dry_streak} dry cycles — backing off this wake", flush=True)
        append_ledger(agent, {"status": "backoff", "note": f"{dry_streak} dry cycles"})
        return 0

    prompt = (
        "Propose ONE finding about this estate that you can prove.\n\n"
        "Reply ONLY with a JSON object:\n"
        '{"claim": "...", "lane": "...", "verify": "<shell command>", '
        '"expect": "<substring the command output must contain>", "novelty_key": "..."}\n\n'
        "`verify` is run BY THE HARNESS, not by you, and must be READ-ONLY: no writes, no pushes, "
        "no network sends, no service changes. The claim is accepted only if the command's output "
        "contains `expect`. If you have nothing worth proving, reply {\"claim\": null}.\n\n"
        f"Already tried (do not repeat these novelty_keys): {json.dumps(tried[-40:])}\n"
    )

    raw = call_agent(agent, prompt, dry_run=dry_run)
    try:
        prop = parse_proposal(raw)
    except (ValueError, TypeError) as e:
        errlog.err(f"harness {agent}: unparseable proposal", e)
        append_ledger(agent, {"status": "error", "note": f"unparseable: {e}", "raw": (raw or "")[:400]})
        return 1

    if not prop.get("claim"):
        append_ledger(agent, {"status": "dry", "note": "agent proposed nothing"})
        print(f"  {agent}: nothing proposed (dry cycle)", flush=True)
        return 0

    key = prop.get("novelty_key") or prop["claim"][:60]
    if key in tried:
        append_ledger(agent, {"status": "dry", "novelty_key": key, "note": "repeat of a tried key"})
        print(f"  {agent}: repeat proposal, skipped", flush=True)
        return 0

    cmd = (prop.get("verify") or "").strip()
    expect = (prop.get("expect") or "").strip()
    if not cmd:
        append_ledger(agent, {"status": "failed", "novelty_key": key, "claim": prop["claim"],
                              "note": "no verifier offered — unfalsifiable, rejected"})
        return 0

    ok, out, note = run_verifier(cmd)
    matched = ok and (expect in out if expect else True)
    rec = {"novelty_key": key, "claim": prop["claim"], "lane": prop.get("lane", ""),
           "verify": cmd, "expect": expect, "verifier_note": note,
           "evidence": out[-1200:], "status": "verified" if matched else "failed"}
    append_ledger(agent, rec)
    print(f"  {agent}: {'VERIFIED' if matched else 'rejected'} — {prop['claim'][:90]}", flush=True)
    if not matched and note.startswith("REFUSED"):
        errlog.err(f"harness {agent}: {note} :: {cmd[:160]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agent")
    ap.add_argument("--dry-run", action="store_true", help="exercise the harness without a model call")
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    # Mutually exclusive across ALL loop agents: they are independently scheduled, nothing waits on
    # them, so concurrency buys no latency and costs quota bursts and unattributable interleaving.
    with open(LOCK, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("  another loop agent holds the lock — skipping this wake", flush=True)
            return 0
        return cycle(args.agent, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
