#!/usr/bin/env python3
"""land() when main moves during a cycle, against a real bare origin in a temp dir.

    python3 services/loop/tests/test_land.py

Cycle 174 (2026-10-26) verified a patch and lost it to "rejected (fetch first)" because a PR merged
while it ran. land() now rebases and retries, but only when the newcomers touch none of its files.
"""
import os, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def setup(tmp):
    origin, wt, other = (os.path.join(tmp, n) for n in ("origin.git", "wt", "other"))
    git(tmp, "init", "-q", "--bare", "-b", "main", origin)
    git(tmp, "clone", "-q", origin, other)
    for n in ("a.py", "b.py"):
        Path(other, n).write_text("x = 1\n")
    git(other, "add", "."); git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    git(other, "push", "-q", "origin", "HEAD:main")
    git(tmp, "clone", "-q", origin, wt)
    return origin, wt, other


def race(other, name):
    """Someone else pushes to main while the cycle is verifying."""
    Path(other, name).write_text("x = 2\n")
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "theirs")
    git(other, "push", "-q", "origin", "HEAD:main")


def run(touched_by_them):
    with tempfile.TemporaryDirectory() as tmp:
        origin, wt, other = setup(tmp)
        loop.REPO_DIRS["t"] = wt
        Path(wt, "a.py").write_text("x = 3\n")
        race(other, touched_by_them)
        sha, why = loop.land({"repo": "t", "subject": "fix a"}, wt, ["a.py"], "0" * 12, 999, "test")
        log = git(origin, "log", "--format=%s", "main")
        return sha, why, log


sha, why, log = run("b.py")
assert sha and why is None, why
assert log.split("\n")[:2] == ["fix a", "theirs"], log
print("ok: main moved on another file -> rebased and landed")

sha, why, log = run("a.py")
assert sha is None and "a.py" in why, why
assert "fix a" not in log, log
print("ok: main moved on the same file -> refused:", why)
