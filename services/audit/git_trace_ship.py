#!/usr/bin/env python3
"""Ship git's own trace2 records of every state-changing git command into the fleet journal.

/etc/gitconfig points trace2.eventTarget at TRACE_DIR, so EVERY git process on the box (agents,
loop.py, tooling-pull, private-data-sync, sudo git) writes one JSON-lines file there. This runs each
minute as root, turns each finished process into one journal line (ident `git-trace`), and
deletes the file. Read-only commands (status, log, diff, rev-parse ...) are dropped: Claude Code
alone runs `git status` dozens of times an hour.

Who ran it: trace2.envVars records MOPROX_AGENT, AGENT_ID and INVOCATION_ID. INVOCATION_ID is set
by systemd for every service, so even a process started before the 50-moprox-agent drop-in existed
resolves to its unit through the journal.
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402

TRACE_DIR = Path(os.environ.get("GIT_TRACE_DIR", "/var/log/git-trace2"))
STALE = 3600            # a file with no exit event after this long: ship it as "no exit seen"
MUTATING = {"commit", "push", "pull", "fetch", "merge", "rebase", "reset", "checkout", "switch",
            "restore", "worktree", "stash", "branch", "tag", "cherry-pick", "revert", "clone", "am",
            "apply", "rm", "mv", "clean", "init", "remote", "gc", "prune", "update-ref", "notes",
            "submodule", "filter-branch", "replace", "config"}
_units = {}


def unit_of(inv):
    if not inv:
        return ""
    if inv not in _units:
        r = subprocess.run(["journalctl", f"_SYSTEMD_INVOCATION_ID={inv}", "-n1", "-o", "json",
                            "--output-fields=_SYSTEMD_UNIT"], capture_output=True, text=True, timeout=20)
        try:
            _units[inv] = json.loads(r.stdout.splitlines()[-1]).get("_SYSTEMD_UNIT", "").removesuffix(".service")
        except (ValueError, IndexError):
            _units[inv] = ""          # expected for a short-lived unit with no journal line yet
    return _units[inv]


def subcommand(argv):
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return ""


def read_only(sub, rest):
    """The listing forms of subcommands that can also write."""
    flags = set(rest)
    if sub == "config":
        return any(a.startswith("--get") or a in ("-l", "--list", "--show-origin") for a in rest) or len(rest) < 2
    if sub in ("branch", "tag", "remote", "stash", "worktree", "notes", "submodule"):
        return not rest or rest[0] in ("list", "show", "-v", "-vv", "-a", "-r", "-l", "--list",
                                       "--show-current", "--contains", "--merged", "status", "get-url")
    return bool(flags & {"--dry-run", "-n"}) and sub in ("clean", "rm", "push")


def parse(path):
    rec = {"env": {}}
    for line in path.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue                   # a torn last line from a process still writing
        ev = e.get("event")
        if ev == "start" and "argv" not in rec:
            rec.update(argv=e.get("argv", []), time=e.get("time", ""))
        elif ev == "def_param":
            rec["env"][e.get("param")] = e.get("value")
        elif ev == "def_repo" and "repo" not in rec:
            rec["repo"] = e.get("worktree", "")
        elif ev == "exit":
            rec.update(code=e.get("code"), secs=e.get("t_abs"))
        elif ev == "cmd_ancestry" and "ancestry" not in rec:
            rec["ancestry"] = e.get("ancestry", [])
    return rec


def ship(rec, path):
    argv = rec.get("argv") or ["git"]
    sub = subcommand(argv)
    if sub not in MUTATING or read_only(sub, argv[argv.index(sub) + 1:]):
        return
    env = rec["env"]
    who = env.get("MOPROX_AGENT") or unit_of(env.get("INVOCATION_ID")) or env.get("AGENT_ID") \
        or ">".join(rec.get("ancestry", [])[:3]) or "unknown"
    if env.get("AGENT_ID") and env["AGENT_ID"] not in who:
        who += "/" + env["AGENT_ID"]
    code = rec.get("code")
    tail = f"exit {code} ({rec.get('secs', 0):.1f}s)" if code is not None else "no exit seen"
    msg = f"{who} git {' '.join(argv[1:])[:1200]} -> {tail} in {rec.get('repo') or '?'}"
    body = "".join(f"{k}={str(v).replace(chr(10), ' ')}\n" for k, v in {
        "SYSLOG_IDENTIFIER": "git-trace", "PRIORITY": "6" if code in (0, None) else "5", "MESSAGE": msg,
        "MOPROX_AGENT": who, "GIT_SUBCOMMAND": sub, "GIT_EXIT": "" if code is None else code,
        "GIT_REPO": rec.get("repo", ""), "GIT_STARTED": rec.get("time", "")}.items())
    subprocess.run(["logger", "--journald"], input=body, text=True, check=True, timeout=10)


def main():
    now, shipped = time.time(), 0
    for p in sorted(TRACE_DIR.iterdir()) if TRACE_DIR.is_dir() else []:
        try:
            age = now - p.stat().st_mtime
            rec = parse(p)
            if "code" not in rec and age < STALE:
                continue                # still running (a long push); next minute
            ship(rec, p)
            shipped += 1
            p.unlink()
        except FileNotFoundError:
            continue                    # benign: git's own trace2 sentinel handling removed it
        except Exception as e:
            errlog.skip(f"git_trace_ship.py: {p.name}", e)
    print(f"git-trace: {shipped} file(s) processed")


if __name__ == "__main__":
    main()
