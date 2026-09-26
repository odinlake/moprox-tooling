#!/usr/bin/env python3
"""tokenlock's properties, plus the repo-wide guard that keeps new refreshers inside it.

    python3 services/lib/tests/test_tokenlock.py

1. Many processes racing a stale token spend each refresh token exactly once (no replay).
2. A holder that is killed -9 does not leave the lock stuck.
3. A holder that hangs is bypassed after `wait`, loudly, instead of wedging everyone.
4. Every file in the repo that spends a refresh token does so under token_lock.
"""
import json, multiprocessing as mp, os, re, signal, subprocess, sys, tempfile, time
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
REPO = LIB.parents[1]
sys.path.insert(0, str(LIB))
from tokenlock import token_lock, write_atomic

tmp = Path(tempfile.mkdtemp(prefix="tokenlock-"))
ok = []


def check(name, good, detail=""):
    print(f"{'PASS' if good else 'FAIL'}  {name}{': ' + detail if detail else ''}")
    ok.append(bool(good))


# -- 1. no replay under contention --------------------------------------------------------------
CRED, SPENT = tmp / "cred.json", tmp / "spent.log"


def consumer(_):
    """The canonical pattern from tokenlock's docstring, against a fake rotating provider."""
    tok = json.loads(CRED.read_text())
    if tok["exp"] > time.time():
        return
    with token_lock(CRED):
        tok = json.loads(CRED.read_text())
        if tok["exp"] > time.time():
            return
        with open(SPENT, "a") as fh:              # the provider records every refresh token spent
            fh.write(tok["refresh"] + "\n")
        time.sleep(0.05)                          # the HTTP round trip: the race window
        write_atomic(CRED, json.dumps({"refresh": tok["refresh"] + "+", "exp": time.time() + 0.3}))


write_atomic(CRED, json.dumps({"refresh": "r", "exp": 0}))
with mp.Pool(12) as pool:
    for _ in range(5):                            # five expiry waves, twelve racers each
        pool.map(consumer, range(12))
        time.sleep(0.35)
spent = SPENT.read_text().split()
check("each refresh token spent once under 12-way contention",
      len(spent) == len(set(spent)) and len(spent) >= 5, f"{len(spent)} refreshes, {len(set(spent))} distinct")

# -- 2. a killed holder releases ---------------------------------------------------------------
HOLD = f"""
import sys, time; sys.path.insert(0, {str(LIB)!r})
from tokenlock import token_lock
with token_lock({str(tmp / 'k.json')!r}):
    print('held', flush=True); time.sleep(60)
"""
p = subprocess.Popen([sys.executable, "-c", HOLD], stdout=subprocess.PIPE, text=True)
p.stdout.readline()
p.send_signal(signal.SIGKILL); p.wait()
t0 = time.monotonic()
with token_lock(tmp / "k.json", wait=5) as held:
    pass
check("lock free immediately after holder is killed -9", held and time.monotonic() - t0 < 1)

# -- 3. a hung holder is bypassed, and says who ------------------------------------------------
p = subprocess.Popen([sys.executable, "-c", HOLD.replace("k.json", "h.json")], stdout=subprocess.PIPE, text=True)
p.stdout.readline()
r = subprocess.run([sys.executable, "-c", f"""
import sys; sys.path.insert(0, {str(LIB)!r})
from tokenlock import token_lock
with token_lock({str(tmp / 'h.json')!r}, wait=1) as held:
    print('held' if held else 'bypassed')
"""], capture_output=True, text=True, timeout=20)
p.kill(); p.wait()
check("hung holder bypassed after wait", r.stdout.strip() == "bypassed", r.stdout.strip())
check("bypass logged at err level naming the holder",
      r.stderr.startswith("<3>") and f"pid {p.pid}" in r.stderr, r.stderr.strip()[:120])

# -- 4. nothing in the repo refreshes outside the lock -----------------------------------------
# A refresh is: an OAuth refresh_token grant, or a client library's refresh call. Any file doing one
# must also use token_lock. If this fails, wrap the new refresher -- do not add it to an allowlist.
SPEND = re.compile(r"""["']grant_type["']\s*[:=,]\s*["']refresh_token["']|refresh_access_token\(""")
bad = []
for f in REPO.rglob("*.py"):
    if ".git" in f.parts or "tests" in f.parts or f.name == "tokenlock.py":
        continue
    s = f.read_text(errors="replace")
    if SPEND.search(s) and "token_lock(" not in s:
        bad.append(str(f.relative_to(REPO)))
check("every refresh-token spender in the repo uses token_lock", not bad, ", ".join(bad))

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
