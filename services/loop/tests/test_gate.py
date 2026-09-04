#!/usr/bin/env python3
"""The change gate, exercised end to end against a throwaway repo with a real bare origin.

    python3 services/loop/tests/test_gate.py

No agent is spawned (LOOP_ADVERSARIAL=0) and nothing touches the estate's repos: REPO_DIRS is
pointed at a temp clone whose origin is a bare repo in the same temp dir, so the push path is
exercised for real and lands nowhere. Both of the gate's own bugs so far were found here rather
than by a live cycle: a patch that ADDS a file read as "changed nothing" (and its new file was
invisible to the deny check), and a __pycache__ that a check dropped into the tree got swept into
the commit.
"""
import os, subprocess, sys, tempfile, shutil, json
from pathlib import Path

os.environ["LOOP_ADVERSARIAL"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

def sh(*a, cwd=None):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"{a}: {r.stderr}"
    return r.stdout.strip()

tmp = Path(tempfile.mkdtemp(prefix="gate-"))
origin, repo = tmp / "origin.git", tmp / "testrepo"
sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
sh("git", "clone", "-q", str(origin), str(repo))
sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "config", "user.name", "t", cwd=repo)
sh("git", "config", "user.email", "t@t", cwd=repo)

# The "bug": divide() returns the wrong thing on a zero denominator instead of raising.
(repo / "svc").mkdir()
(repo / "svc" / "calc.py").write_text(
    "def divide(a, b):\n"
    "    if b == 0:\n"
    "        return 0          # BUG: a silent wrong answer\n"
    "    return a / b\n")
sh("git", "add", "-A", cwd=repo)
sh("git", "commit", "-qm", "seed", cwd=repo)
sh("git", "push", "-q", "origin", "main", cwd=repo)

state = tmp / "state"
state.mkdir()
loop.REPO_DIRS = {"testrepo": repo}
loop.STATE = state
loop.PATCHES = state / "patches"
loop.DENY_PATHS = {"testrepo": [("svc/forbidden.py", "test deny rule")]}

CHECK = (
    "import os, sys, importlib.util\n"
    "tree = os.environ['TREE']\n"
    "spec = importlib.util.spec_from_file_location('calc', tree + '/svc/calc.py')\n"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "try:\n"
    "    m.divide(1, 0)\n"
    "    print('divide(1,0) returned instead of raising')\n"
    "except ZeroDivisionError:\n"
    "    print('RAISES as it should')\n")

def patch_for(new_text, path="svc/calc.py"):
    wt = tmp / "mk"
    shutil.rmtree(wt, ignore_errors=True)
    sh("git", "-C", str(repo), "fetch", "-q", "origin")
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   capture_output=True)
    sh("git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), "origin/main")
    (wt / path).parent.mkdir(parents=True, exist_ok=True)
    (wt / path).write_text(new_text)
    sh("git", "-C", str(wt), "add", "-A")
    d = sh("git", "-C", str(wt), "diff", "--cached")
    sh("git", "-C", str(repo), "worktree", "remove", "--force", str(wt))
    return d

FIXED = ("def divide(a, b):\n"
         "    if b == 0:\n"
         "        raise ZeroDivisionError('divide by zero')\n"
         "    return a / b\n")

def run(name, prop, want_status, want_in=""):
    st, detail, extra = loop.change_gate(prop, 1, "burndown")
    ok = st == want_status and want_in.lower() in detail.lower()
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {st} [{detail[:110]}]")
    return ok

base = dict(repo="testrepo", item="divide() returns 0 instead of raising", subject="calc: raise",
            source="test", why="a silent wrong answer", check=CHECK, expect="RAISES as it should")

results = []
results.append(run("the real fix lands", {**base, "patch": patch_for(FIXED)}, "accepted", "passes with the patch"))
results.append(run("no patch", {**base, "patch": ""}, "rejected", "no patch"))
results.append(run("unknown repo", {**base, "repo": "nope", "patch": "x"}, "rejected", "unknown repo"))

# After the first landed, origin/main already raises, so the same check now passes on the base tree.
results.append(run("check already passes at origin/main",
                   {**base, "patch": patch_for(FIXED + "# an unrelated edit\n")},
                   "rejected", "already passes"))

# Reset origin to the buggy state for the remaining cases.
sh("git", "-C", str(repo), "fetch", "-q", "origin")
first = sh("git", "-C", str(repo), "rev-list", "--max-parents=0", "origin/main")
sh("git", "-C", str(repo), "push", "-q", "--force", "origin", f"{first}:main")
sh("git", "-C", str(repo), "fetch", "-q", "origin")

results.append(run("stale patch", {**base, "patch": patch_for(FIXED).replace("if b == 0:", "if b == 99:")},
                   "rejected", "does not apply"))
results.append(run("change does not fix it",
                   {**base, "patch": patch_for("def divide(a, b):\n    return 0  # still wrong\n")},
                   "rejected", "does not make its own check pass"))
results.append(run("syntax error", {**base, "patch": patch_for("def divide(a, b)\n    raise ZeroDivisionError\n")},
                   "rejected", "does not parse"))
results.append(run("denied path",
                   {**base, "patch": patch_for("x = 1\n", path="svc/forbidden.py")},
                   "rejected", "off limits"))
results.append(run("check that reads the live tree, not TREE",
                   {**base, "patch": patch_for(FIXED),
                    "check": "import os\nprint('RAISES as it should' if os.path.exists('/etc/hostname') else 'no')\n"},
                   "rejected", "already passes"))

print("\n%d/%d passed" % (sum(results), len(results)))
print("origin log:", sh("git", "-C", str(repo), "log", "--oneline", "origin/main"))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
