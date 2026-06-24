#!/usr/bin/env python3
"""Dashboard updater — timer-driven (every 5 min on claude-dev).

For each data feed (system, training, dns) it rebuilds the data, and publishes to gh-pages ONLY
when something actually changed. Publishing uses a single parentless commit (force-pushed) so
gh-pages history stays flat no matter how often this runs. Per-feed stats (build time + payload
size, recorded only when that feed changed) go into data/stats.json for the dashboard's Stats tab.

Efficiency / "nothing to do -> exit fast":
- training is only rebuilt when its raw input (the Polar export/incoming files) actually changed;
- after building, if NOTHING changed (git tree identical), it exits without committing or touching
  stats.

Env: DASH_WORKTREE, POLAR_RAW, DNS_EXPORTER, PVE_ENV (defaults below). Reads PVE_* from PVE_ENV.
"""
import fcntl, hashlib, json, os, shutil, subprocess as sp, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WT   = Path(os.environ.get("DASH_WORKTREE", Path.home() / ".cache/moprox-dashboard-ghpages"))
DASH = WT / "dashboard"; DATA = DASH / "data"
STATE = Path.home() / ".cache/moprox-update-state.json"
LOCK  = Path.home() / ".cache/moprox-update.lock"
POLAR_RAW = Path(os.environ.get("POLAR_RAW", Path.home() / "projects/private-data/polar/raw"))
DNS_EXPORTER = os.environ.get("DNS_EXPORTER", "http://10.10.10.3:9153/")
PVE_ENV = Path(os.environ.get("PVE_ENV", Path.home() / ".config/proxmox/pve-metrics.env"))
TABS = ("system", "training", "dns", "stats")

def git(*a):  # run git in the worktree, return stdout
    return sp.run(["git", *a], cwd=str(WT), check=True, text=True, capture_output=True).stdout.strip()

def load_env(p):
    if p.exists():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ.setdefault(k, v)

def ensure_worktree():
    if (WT / ".git").exists():
        git("reset", "--hard", "-q"); return
    WT.parent.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "-C", str(REPO), "worktree", "prune"], check=False, capture_output=True)
    sp.run(["git", "-C", str(REPO), "worktree", "add", "--force", str(WT), "gh-pages"],
           check=True, capture_output=True)

def dir_size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())

def training_fp():
    files = sorted(POLAR_RAW.glob("*.zip"))
    inc = POLAR_RAW.parent / "incoming"
    if inc.exists():
        files += sorted(f for f in inc.rglob("*") if f.is_file())
    return hashlib.sha1("|".join(f"{f}:{f.stat().st_size}:{f.stat().st_mtime_ns}" for f in files).encode()).hexdigest()

def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # a run is already in progress
    load_env(PVE_ENV)
    ensure_worktree()
    DATA.mkdir(parents=True, exist_ok=True)

    # refresh the shell + per-tab endpoint pages from the repo (no-op in git if unchanged)
    shell = REPO / "components/dashboard/web/index.html"
    shutil.copy(shell, DASH / "index.html")
    for t in TABS:
        (DASH / t).mkdir(exist_ok=True); shutil.copy(shell, DASH / t / "index.html")

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    timings = {}  # feed -> (ms, bytes), only for feeds we (re)built this run

    # system — cheap, RRD advances every minute, so always rebuild
    t0 = time.monotonic()
    sp.run([sys.executable, str(REPO / "services/metrics/rrd_json.py"), "all"],
           env={**os.environ, "OUT": str(DATA)}, check=True, capture_output=True)
    timings["system"] = (round((time.monotonic() - t0) * 1000), dir_size(DATA / "system"))

    # dns — cheap fetch from the exporter (leave as-is if it's unreachable)
    t0 = time.monotonic()
    try:
        raw = sp.run(["curl", "-fsS", "--max-time", "10", DNS_EXPORTER], check=True, text=True, capture_output=True).stdout
        d = json.loads(raw)
        (DATA / "dns").mkdir(exist_ok=True)
        (DATA / "dns/blocked.json").write_text(json.dumps(
            {"generated": int(time.time()), "totals": d.get("totals", {}), "blocked_days": d.get("blocked_days", [])},
            separators=(",", ":")))
        timings["dns"] = (round((time.monotonic() - t0) * 1000), (DATA / "dns/blocked.json").stat().st_size)
    except sp.CalledProcessError:
        pass

    # training — only rebuild when the raw Polar input changed (avoids the expensive parse)
    fp = training_fp()
    if fp != state.get("training_fp"):
        t0 = time.monotonic()
        sp.run([sys.executable, str(REPO / "services/training/build.py")],
               env={**os.environ, "POLAR_RAW": str(POLAR_RAW), "POLAR_IN": str(POLAR_RAW.parent / "incoming"),
                    "OUT": str(DATA / "training/sessions.json")},
               check=True, capture_output=True)
        timings["training"] = (round((time.monotonic() - t0) * 1000), (DATA / "training/sessions.json").stat().st_size)
        state["training_fp"] = fp

    # what changed?
    git("add", "-A")
    changed = git("diff", "--cached", "--name-only").splitlines()
    if not changed:
        git("reset", "-q")
        return  # nothing to do — fast exit, no stats, no publish

    def feed_of(p):
        for f in ("system", "training", "dns"):
            if f"/data/{f}/" in p: return f
        return None
    changed_feeds = {feed_of(p) for p in changed} - {None}

    # stats — only for feeds that actually changed this run
    sp_path = DATA / "stats.json"
    stats = json.loads(sp_path.read_text()) if sp_path.exists() else {"sources": {}}
    now = int(time.time())
    for f in changed_feeds:
        if f in timings:
            ms, size = timings[f]; stats["sources"][f] = {"updated": now, "ms": ms, "bytes": size}
    if changed_feeds:
        stats["generated"] = now
        sp_path.write_text(json.dumps(stats, separators=(",", ":")))
        git("add", "-A")

    # flat-history publish: one parentless commit, force-pushed
    tree = git("write-tree")
    if tree == git("rev-parse", "HEAD^{tree}"):
        git("reset", "-q"); STATE.write_text(json.dumps(state)); return
    env = {**os.environ, "GIT_AUTHOR_NAME": "dashboard-bot", "GIT_AUTHOR_EMAIL": "noreply@odinlake.net",
           "GIT_COMMITTER_NAME": "dashboard-bot", "GIT_COMMITTER_EMAIL": "noreply@odinlake.net"}
    msg = "publish " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new = sp.run(["git", "commit-tree", tree, "-m", msg], cwd=str(WT), env=env, check=True, text=True, capture_output=True).stdout.strip()
    git("reset", "--hard", "-q", new)
    sp.run(["git", "push", "-qf", "origin", "gh-pages"], cwd=str(WT), check=True, capture_output=True)
    STATE.write_text(json.dumps(state))
    print(f"published: feeds={sorted(changed_feeds)} timings={timings}")

if __name__ == "__main__":
    main()
