#!/usr/bin/env python3
"""Dashboard updater — timer-driven (every 2 min on claude-dev).

Two publish targets, deliberately split so fast-changing data never triggers a slow GitHub Pages
rebuild:

  * app shell (index.html, per-tab pages, manifest, icons) -> the `gh-pages` branch. GitHub Pages
    redeploys the WHOLE site on every push (~10 min, all-or-nothing), so we push here ONLY when the
    shell/assets actually change (i.e. a dashboard code change) — rarely.
  * data feeds (system, dns, training, stats) -> the `data` branch, served to the public dashboard
    via the jsDelivr CDN (cdn.jsdelivr.net/gh/odinlake/moprox-tooling@data/...). A push to a normal
    branch runs no Actions and no Pages build; after pushing we purge the changed files from jsDelivr
    so the CDN serves them within one cadence. This is what lets hypervisor stats be fresh every
    couple of minutes with zero Pages churn.

Both branches publish as a single parentless commit (force-pushed) so history stays flat. Efficiency:
training is only rebuilt when its raw Polar input changed; a branch whose tree is byte-identical to
its last publish is skipped entirely (fast idle tick).

Env: DASH_WORKTREE, DATA_WORKTREE, POLAR_RAW, DNS_EXPORTER, PVE_ENV (defaults below).
"""
import fcntl, hashlib, json, os, shutil, subprocess as sp, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WT   = Path(os.environ.get("DASH_WORKTREE", Path.home() / ".cache/moprox-dashboard-ghpages"))
DWT  = Path(os.environ.get("DATA_WORKTREE", Path.home() / ".cache/moprox-dashboard-data"))
DASH = WT / "dashboard"
DATA = DWT                                 # data feeds live at the ROOT of the `data` branch
SRC  = REPO / "components/dashboard/web"   # shell + PWA assets (source of truth)
STATE = Path.home() / ".cache/moprox-update-state.json"
LOCK  = Path.home() / ".cache/moprox-update.lock"
POLAR_RAW = Path(os.environ.get("POLAR_RAW", Path.home() / "projects/private-data/polar/raw"))
DNS_EXPORTER = os.environ.get("DNS_EXPORTER", "http://10.10.10.3:9153/")
PVE_ENV = Path(os.environ.get("PVE_ENV", Path.home() / ".config/proxmox/pve-metrics.env"))
GH = "odinlake/moprox-tooling"                                  # jsDelivr purge namespace
NAV = ("system", "training")                                   # real nav tabs (SPA only links these)
ASSETS = ("manifest.json", "apple-touch-icon.png", "icon-512.png", "icon.svg")

PAGES_YML = (
    "name: deploy-pages\n"
    "on: { push: { branches: [gh-pages] } }\n"
    "permissions: { contents: read, pages: write, id-token: write }\n"
    "concurrency: { group: pages, cancel-in-progress: true }\n"
    "jobs:\n"
    "  deploy:\n"
    "    runs-on: ubuntu-latest\n"
    "    environment: { name: github-pages, url: \"${{ steps.dep.outputs.page_url }}\" }\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: actions/configure-pages@v5\n"
    "      - uses: actions/upload-pages-artifact@v3\n"
    "        with: { path: \".\" }\n"
    "      - id: dep\n"
    "        uses: actions/deploy-pages@v4\n")


def git(wt, *a):
    return sp.run(["git", *a], cwd=str(wt), check=True, text=True, capture_output=True).stdout.strip()

def load_env(p):
    if p.exists():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ.setdefault(k, v)

def ensure_wt(wt, branch):
    if (wt / ".git").exists():
        git(wt, "reset", "--hard", "-q"); return
    wt.parent.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "-C", str(REPO), "worktree", "prune"], check=False, capture_output=True)
    sp.run(["git", "-C", str(REPO), "worktree", "add", "--force", str(wt), branch],
           check=True, capture_output=True)

def dir_size(p):
    p = Path(p)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0

def training_fp():
    files = sorted(POLAR_RAW.glob("*.zip"))
    inc = POLAR_RAW.parent / "incoming"
    if inc.exists():
        files += sorted(f for f in inc.rglob("*") if f.is_file())
    ah = POLAR_RAW.parent.parent / "apple-health"            # Apple Health CSVs (device-gap fill)
    if ah.exists():
        files += sorted(f for f in ah.glob("*.csv") if f.is_file())
    return hashlib.sha1("|".join(f"{f}:{f.stat().st_size}:{f.stat().st_mtime_ns}" for f in files).encode()).hexdigest()

def staged_changes(wt):
    """Stage everything and return the list of paths differing from the branch's last publish."""
    git(wt, "add", "-A")
    return git(wt, "diff", "--cached", "--name-only").splitlines()

def flat_publish(wt, branch):
    """Force-push a single parentless commit iff the (already-staged) tree changed. -> bool published."""
    tree = git(wt, "write-tree")
    if tree == git(wt, "rev-parse", "HEAD^{tree}"):
        git(wt, "reset", "-q"); return False
    env = {**os.environ, "GIT_AUTHOR_NAME": "dashboard-bot", "GIT_AUTHOR_EMAIL": "noreply@odinlake.net",
           "GIT_COMMITTER_NAME": "dashboard-bot", "GIT_COMMITTER_EMAIL": "noreply@odinlake.net"}
    msg = f"publish {branch} " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new = sp.run(["git", "commit-tree", tree, "-m", msg], cwd=str(wt), env=env,
                 check=True, text=True, capture_output=True).stdout.strip()
    git(wt, "reset", "--hard", "-q", new)
    sp.run(["git", "push", "-qf", "origin", branch], cwd=str(wt), check=True, capture_output=True)
    return True

def purge_jsdelivr(paths):
    """Best-effort CDN purge of the files we just pushed, so the dashboard sees them within a cadence."""
    for p in paths:
        sp.run(["curl", "-fsS", "--max-time", "10", f"https://purge.jsdelivr.net/gh/{GH}@data/{p}"],
               check=False, capture_output=True)


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # a run is already in progress
    load_env(PVE_ENV)
    ensure_wt(WT, "gh-pages")
    ensure_wt(DWT, "data")
    DATA.mkdir(parents=True, exist_ok=True)

    # ---- app shell -> gh-pages (Pages deploys ONLY when this actually changes) ----
    (WT / ".nojekyll").write_text("")                     # serve static files as-is; skip Jekyll
    wf = WT / ".github/workflows/pages.yml"               # Pages source = GitHub Actions: this IS the deploy
    wf.parent.mkdir(parents=True, exist_ok=True); wf.write_text(PAGES_YML)
    shutil.rmtree(DASH / "data", ignore_errors=True)      # data no longer lives on gh-pages (moved to CDN)
    DASH.mkdir(parents=True, exist_ok=True)
    # stamp a build version (hash of the shell source) so the page hard-reloads when new CODE ships;
    # data-only updates never touch the shell, so they never bump this.
    shell_src = (SRC / "index.html").read_text()
    ver = hashlib.sha1(shell_src.encode()).hexdigest()[:12]
    shell = shell_src.replace("__BUILD__", ver)
    (DASH / "index.html").write_text(shell)
    for t in NAV:
        (DASH / t).mkdir(exist_ok=True); (DASH / t / "index.html").write_text(shell)
    (DASH / "version.txt").write_text(ver)                # same-origin reload signal (lives WITH the shell)
    for a in ASSETS:
        if (SRC / a).exists(): shutil.copy2(SRC / a, DASH / a)
    shell_changed = staged_changes(WT)
    if flat_publish(WT, "gh-pages"):
        print(f"published gh-pages (shell/assets): {shell_changed}")

    # ---- data feeds -> data branch (served via jsDelivr; NO Pages build) ----
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    timings = {}  # feed -> (ms, bytes), only for feeds (re)built this run

    # system — RRD advances every minute, so always rebuild (cheap)
    t0 = time.monotonic()
    sp.run([sys.executable, str(REPO / "services/metrics/rrd_json.py"), "all"],
           env={**os.environ, "OUT": str(DATA)}, check=True, capture_output=True)
    timings["system"] = (round((time.monotonic() - t0) * 1000), dir_size(DATA / "system"))

    # dns — cheap fetch from the exporter (leave the prior file if it's unreachable)
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

    # training — rebuild when the raw Polar input changed (avoids the expensive parse), or when the
    # published file is missing (e.g. a freshly-seeded data branch) so the feed is never absent.
    fp = training_fp()
    if fp != state.get("training_fp") or not (DATA / "training/sessions.json").exists():
        t0 = time.monotonic()
        sp.run([sys.executable, str(REPO / "services/training/build.py")],
               env={**os.environ, "POLAR_RAW": str(POLAR_RAW), "POLAR_IN": str(POLAR_RAW.parent / "incoming"),
                    "OUT": str(DATA / "training/sessions.json")}, check=True, capture_output=True)
        timings["training"] = (round((time.monotonic() - t0) * 1000), (DATA / "training/sessions.json").stat().st_size)
        state["training_fp"] = fp

    # agents / mcp usage for the Stats tab (cheap; publishes only when it changes)
    (DATA / "stats").mkdir(parents=True, exist_ok=True)
    sp.run([sys.executable, str(REPO / "services/agents/agent_stats.py")],
           env={**os.environ, "OUT": str(DATA / "stats/agents.json")}, check=False, capture_output=True)
    sp.run([sys.executable, str(REPO / "services/agents/mcp_stats.py")],
           env={**os.environ, "OUT": str(DATA / "stats/mcp.json")}, check=False, capture_output=True)

    # what data changed?
    changed = staged_changes(DWT)
    if not changed:
        git(DWT, "reset", "-q"); STATE.write_text(json.dumps(state)); return

    def feed_of(p):
        seg = p.split("/", 1)[0]
        if seg in ("system", "training", "dns"): return seg
        if seg in ("stats", "stats.json"): return "stats"
        return None
    changed_feeds = {feed_of(p) for p in changed} - {None}

    # per-feed stats (build time + payload size), recorded only for feeds that changed this run
    sp_path = DATA / "stats.json"
    stats = json.loads(sp_path.read_text()) if sp_path.exists() else {"sources": {}}
    now = int(time.time())
    for f in changed_feeds:
        if f in timings:
            ms, size = timings[f]; stats["sources"][f] = {"updated": now, "ms": ms, "bytes": size}
    stats["generated"] = now
    sp_path.write_text(json.dumps(stats, separators=(",", ":")))

    changed = staged_changes(DWT)      # restage to include stats.json
    if flat_publish(DWT, "data"):
        purge_jsdelivr(changed)
        print(f"published data: feeds={sorted(changed_feeds)} files={len(changed)} timings={timings}")
    STATE.write_text(json.dumps(state))

if __name__ == "__main__":
    main()
