#!/usr/bin/env python3
"""Read the theming repo's branch-sync status and tell #exocomps when a branch needs a human.

The workflow (theming .github/workflows/branch-sync.yml) rebases or merges master into every live
branch and writes ONE artifact describing all of them. It never fails on a conflict — a conflict is
a fact about the repo, not a broken job — so nothing in GitHub's UI goes red and nobody finds out.
This closes that loop: pull the newest status, and post when a branch is stuck.

WHAT IT POSTS, AND WHAT IT DOES NOT
  Only conflicts and push rejections, and only when the SET of them changes. The same two branches
  conflicting every night at 04:17 is one message, not a nightly drip — the estate already learned
  that lesson with the valet's repeated-bullet failure. A conflict clearing is worth saying once too,
  so the channel reflects the current state rather than a stream of alarms.

Auth: `gh` on claude-dev is odinlake-ai with team write on theme-ontology/theming, which is enough
to list runs and download artifacts. Discord goes through services/forward/discord_api.py — the same
bot and channel the theming agent uses, so a human can reply to it there.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "forward"))
import errlog                      # noqa: E402  — no silent swallows; see services/lib/errlog.py
import discord_api                 # noqa: E402  — the estate's one Discord transport

REPO = os.environ.get("THEMING_REPO", "theme-ontology/theming")
WORKFLOW = "branch-sync.yml"
STATE = Path.home() / ".local/share/moprox/branch-sync-seen.json"
STUCK = ("conflict", "push-rejected", "error")


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if check and r.returncode != 0:
        raise RuntimeError("gh %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def latest_status():
    """The status JSON from the most recent completed run, or None if there isn't one yet."""
    try:
        listing = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW,
                     "--status", "completed", "--limit", "1",
                     "--json", "databaseId,conclusion,createdAt,url")
    except RuntimeError as e:
        # The workflow not existing yet (PR not merged) is a state, not a fault. Anything else is.
        if "not found" in str(e).lower():
            return None, None
        raise
    runs = json.loads(listing)
    if not runs:
        return None, None
    run = runs[0]
    with tempfile.TemporaryDirectory() as td:
        gh("run", "download", str(run["databaseId"]), "--repo", REPO,
           "--name", "branch-sync-status", "--dir", td)
        files = list(Path(td).rglob("*.json"))
        if not files:
            raise RuntimeError("run %s has no status json in its artifact" % run["databaseId"])
        return json.loads(files[0].read_text()), run


def render(status, run, stuck, cleared):
    lines = []
    for b in stuck:
        files = ", ".join(b.get("conflict_files") or [])[:180]
        detail = files or (b.get("error") or "")
        lines.append("**%s** — %s (%s ahead, %s behind)%s"
                     % (b["branch"], b["action"], b["ahead"], b["behind"],
                        "\n  ↳ " + detail if detail else ""))
    if cleared:
        lines.append("Cleared since last check: " + ", ".join(sorted(cleared)))
    counts = ", ".join("%s %s" % (v, k) for k, v in sorted(status["summary"].items()))
    lines.append("_%s · base %s · %s_" % (counts, status.get("base_sha", "?"), run["url"]))
    return "\n".join(lines)


def main():
    try:
        status, run = latest_status()
    except Exception as e:
        errlog.err("branch_sync_watch: fetching latest status", e)
        return 1
    if not status:
        print("no completed branch-sync runs yet")
        return 0

    stuck = [b for b in status["branches"] if b.get("action") in STUCK]
    names = {b["branch"] for b in stuck}
    try:
        seen = set(json.loads(STATE.read_text()).get("stuck", [])) if STATE.exists() else set()
    except Exception as e:
        errlog.skip("branch_sync_watch: reading state", e)
        seen = set()

    cleared = seen - names
    if names == seen:
        print("no change: %d stuck (%s)" % (len(names), ", ".join(sorted(names)) or "none"))
        return 0

    if names or cleared:
        text = render(status, run, stuck, cleared)
        title = ("Branch sync — %d branch(es) need a human" % len(names)) if names \
                else "Branch sync — all branches clear"
        try:
            discord_api.post_embed(text, title=title, color=0xE3B341 if names else 0x2EA043)
            print("posted to discord:", title)
        except Exception as e:
            errlog.err("branch_sync_watch: posting to discord", e)
            return 1        # do NOT record as seen; retry next run rather than swallow the alert

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"stuck": sorted(names), "run": run["databaseId"],
                                 "generated": status["generated"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
