#!/usr/bin/env python3
"""work.sh, run for real against a throwaway repo under a fake HOME.

    python3 services/loop/tests/test_work.py

This test exists because the first live burndown cycle died on `git worktree prune -q`, which is
not a switch (prune takes only -n/-v/--expire) and which `set -e` turns into an immediate exit. The
gate's own tests did not catch it: they call git through loop.py, which ignores prune's exit status,
and nothing executed the script the agent is told to use. A helper that is only ever read is not
tested.
"""
import os, subprocess, sys, tempfile, shutil
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "work.sh"


def sh(*a, **kw):
    r = subprocess.run(a, capture_output=True, text=True, **kw)
    assert r.returncode == 0, f"{a}: {r.stderr}"
    return r.stdout.strip()


tmp = Path(tempfile.mkdtemp(prefix="work-"))
home = tmp / "home"
(home / "projects").mkdir(parents=True)
origin = tmp / "origin.git"
repo = home / "projects" / "testrepo"
sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
sh("git", "clone", "-q", str(origin), str(repo))
sh("git", "-C", str(repo), "config", "user.name", "t")
sh("git", "-C", str(repo), "config", "user.email", "t@t")
(repo / "seed").write_text("1\n")
sh("git", "-C", str(repo), "add", "-A")
sh("git", "-C", str(repo), "commit", "-qm", "seed")
sh("git", "-C", str(repo), "push", "-q", "origin", "main")
head = sh("git", "-C", str(repo), "rev-parse", "HEAD")

env = {**os.environ, "HOME": str(home), "LOOP_AGENT": "burndown",
       "LOOP_STATE": str(tmp / "state/burndown")}
ok = []


def run(name, expect_rc=0):
    r = subprocess.run(["bash", str(WORK), "testrepo"], capture_output=True, text=True, env=env)
    good = r.returncode == expect_rc
    print(f"{'PASS' if good else 'FAIL'}  {name}"
          + ("" if good else f" (rc={r.returncode} err={r.stderr.strip()[:200]})"))
    ok.append(good)
    return r.stdout.strip()


out = run("work.sh exits 0 and prints a path")
wt = Path(out) if out else None
ok.append(bool(wt and wt.is_dir()))
print(f"{'PASS' if ok[-1] else 'FAIL'}  the path it printed exists: {out}")
if wt and wt.is_dir():
    at = sh("git", "-C", str(wt), "rev-parse", "HEAD")
    ok.append(at == head)
    print(f"{'PASS' if ok[-1] else 'FAIL'}  it is at origin/main")
    # a dirty worktree, then re-run: the point of the helper is that it is idempotent
    (wt / "scratch").write_text("uncommitted\n")
    out2 = run("re-running over a dirty worktree still exits 0")
    ok.append(out2 == out and not (Path(out2) / "scratch").exists())
    print(f"{'PASS' if ok[-1] else 'FAIL'}  and it came back clean")
    # the case that actually bit: stale metadata left by a killed cycle
    shutil.rmtree(wt)
    out3 = run("a worktree deleted behind git's back is recovered")
    ok.append(out3 == out and Path(out3).is_dir())
    print(f"{'PASS' if ok[-1] else 'FAIL'}  and the path is usable again")

print("\n%d/%d passed" % (sum(ok), len(ok)))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(ok) else 1)
