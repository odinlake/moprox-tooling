#!/usr/bin/env python3
"""services/audit pieces that can be checked without a journal.

    python3 services/audit/tests/test_audit.py
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import agent_action, estate_intent, git_trace_ship, who

# Secrets never reach the journal, whatever shape they arrive in.
for raw in ("curl -H 'Authorization: Bearer abcdefghijklmnop1234' x", "export API_KEY=s3cr3tvalue",
            'curl -H "Authorization: PVEAPIToken=metrics@pve!collector=1234-5678" u', "--password hunter22"):
    out = agent_action.SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", raw)
    assert not any(s in out for s in ("abcdefghijklmnop1234", "s3cr3tvalue", "1234-5678", "hunter22")), out
print("ok: redaction")

# Identity: the env var wins; otherwise the cgroup's unit, with AGENT_ID appended when it adds information.
os.environ["MOPROX_AGENT"] = "moprox-dev@two"
assert who.agent() == "moprox-dev@two"
del os.environ["MOPROX_AGENT"]
assert who.agent() == (who.unit() or os.environ.get("AGENT_ID") or "unknown") or "/" in who.agent()
print("ok: identity ->", who.agent())

# Scope matching: a loop cycle's repo:* covers every repo, and nothing else.
m = estate_intent.matches
assert m("repo:*", "repo:moprox-tooling") and m("repo:moprox-tooling", "repo:*")
assert m("repo:a", "repo:a") and not m("repo:a", "repo:b") and not m("box:*", "repo:a")
print("ok: scope matching")

# git trace2: real git writes the records; only state changes are shipped, with the unit named.
with tempfile.TemporaryDirectory() as tmp:
    td, repo = Path(tmp, "trace"), Path(tmp, "r")
    td.mkdir()
    env = dict(os.environ, GIT_TRACE2_EVENT=str(td), GIT_TRACE2_ENV_VARS="MOPROX_AGENT,AGENT_ID,INVOCATION_ID",
               MOPROX_AGENT="test-agent", GIT_CONFIG_NOSYSTEM="1")
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, check=True, capture_output=True)
    repo.mkdir(); run("init", "-q"); run("status"); run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"); run("branch", "--show-current")
    recs = [git_trace_ship.parse(p) for p in td.iterdir()]
    subs = sorted(git_trace_ship.subcommand(r["argv"]) for r in recs)
    shipped = sorted(git_trace_ship.subcommand(r["argv"]) for r in recs
                     if git_trace_ship.subcommand(r["argv"]) in git_trace_ship.MUTATING
                     and not git_trace_ship.read_only(git_trace_ship.subcommand(r["argv"]), r["argv"][r["argv"].index(git_trace_ship.subcommand(r["argv"])) + 1:]))
    assert {"init", "commit"} <= set(shipped) and "status" not in shipped and "branch" not in shipped, (subs, shipped)
    assert all(r["env"].get("MOPROX_AGENT") == "test-agent" for r in recs), recs[0]["env"]
    assert all("code" in r for r in recs)
print("ok: trace2 parse/filter ->", shipped)
