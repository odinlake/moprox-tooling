#!/usr/bin/env python3
"""Run a Claude Code agent headless on the subscription (Max plan), in its own CLAUDE.md context dir.

Each agent is just a directory with a CLAUDE.md. We strip any inference tokens from the env so it
uses the /login subscription creds (not the metered API). Used by the Polar fetcher (coach) and the
Telegram router (steward / coach / dev).
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
USAGE = HOME / ".local/share/moprox/agent-usage.jsonl"   # per-call token ledger (one line per invocation)
AGENTS = {
    "coach":   HOME / "projects/private-data/agents/coach",
    "steward": HOME / "projects/private-data/agents/steward",
    "dev":     HOME / "projects/private-data/agents/dev",     # the persona dir (loads its CLAUDE.md)
    "valet":   HOME / "projects/private-data/agents/valet",
    "theming": HOME / "projects/private-data/agents/theming",
    "analyst": HOME / "projects/private-data/agents/analyst",   # loop agent (read-mostly)
    "bard":    HOME / "projects/private-data/agents/bard",      # kids' Yoto entertainer
    # bard again, same persona dir, but with FULL permissions for one job: finishing a home recording
    # docwatch has just put on a Yoto playlist (identify the book, fix the title, fetch the cover).
    # The operator asked for this explicitly (2026-10-04): what is needed varies per book, so a
    # fixed script cannot do it. Never reached from Telegram; only docwatch calls it.
    "bard-curate": HOME / "projects/private-data/agents/bard",
    # ...and for the operator's OWN listening (2026-10-06): bookscout's per-article judge. Read-only
    # tools plus the bookscout verbs; never reached from Telegram.
    "bard-books": HOME / "projects/private-data/agents/bard",
    # incident triage (operator, 2026-10-28): reads every new incident so he never has to. Called
    # only by services/forward/incident_triage.py, never from Telegram.
    "triage":  HOME / "projects/private-data/agents/triage",
    # The two work-queue agents (operator, 2026-11-16), called only by services/queue/queue_run.py.
    # Their model and effort are NOT here: queue_run passes them from queues.json as `extra`.
    "planner": HOME / "projects/private-data/agents/planner",
    "devq":    HOME / "projects/private-data/agents/devq",
}
REPOS = [HOME / "projects/moprox-homelab", HOME / "projects/moprox-tooling", HOME / "projects/private-data"]
BOOK  = HOME / ".local/share/moprox"                          # the book of works lives here
# The estate's semantic-memory store (its own git repo since 2026-08-03). EVERY agent that maintains
# a durable notebook needs this in --add-dir: the notebooks now live at <SHARED_MEM>/agents/ and are
# only SYMLINKED into each agent dir, so the permission layer resolves the real path and denies the
# write unless this directory is granted. Omitting it makes the agent silently unable to save memory.
SHARED_MEM = HOME / "projects/moprox-memory"
# Files the operator sent over Telegram (documents/photos/voice), downloaded by telegram_poll via
# tg_files.py. An agent asked to "file this letter" must be able to READ the thing it was handed, so
# every agent that talks to the operator gets this directory.
TG_FILES = HOME / ".local/share/moprox/telegram-files"
LOCAL_BIN = HOME / ".local/bin"
# Resolve the CLI absolutely: under systemd the service PATH is minimal and won't find ~/.local/bin.
CLAUDE = shutil.which("claude") or str(LOCAL_BIN / "claude")

# The dev agent acts on simple, reversible work (operator's chosen autonomy). Irreversible / outward /
# privileged commands are denied at the TOOL layer here; the persona handles the nuance + book-of-works.
DEV_DENY = ",".join("Bash(%s)" % p for p in (
    "git push:*", "git reset --hard:*", "git clean:*", "sudo:*", "rm:*",
    "reboot:*", "shutdown:*", "dd:*", "mkfs:*", "gh:*"))   # service mutation needs sudo (denied); read-only systemctl ok
TRAINING_DATA = HOME / ".cache/moprox-dashboard-ghpages/dashboard/data/training"   # classified history
CONVO_TOOL = "Bash(convo:*)"            # restricted: agents may run ONLY the `convo` helper, no free bash
DRIVE_TOOL = "Bash(drive-put:*)"        # ...plus the one-verb Drive uploader (services/drive/drive-put)
SHOPPING_TOOL = "Bash(shopping:*)"      # the household shopping list in HA (services/shopping)
YOTO_TOOL = "Bash(yoto:*)"              # bard's Yoto verb: player + playlist
BOOKS_TOOL = "Bash(bookscout:*)"        # bard's books verb: Audible catalogue/library + review log
THEMING_REPO = HOME / "projects/theming"               # the theme-ontology/theming working copy
# theming pushes feature BRANCHES and opens PRs, but never bypasses protections: deny force-push +
# direct master/main pushes, the dangerous `gh` verbs (merge a PR, delete/archive a repo), and the
# usual catastrophic/outward commands. Plain `git push` of a feature branch and `gh pr create` ARE
# allowed; server-side branch protection on master/main is the real backstop for "no self-merge".
# `git push *master*` used to be in here. It matches the BRANCH NAME, not the target ref, so it
# refused `git push origin ai-feature-expanse-master-sync` -- a correctly-named ai-feature branch
# whose only sin was the word "sync-to-master" in it (2026-09-01, first live conflict dispatch). It
# would equally refuse a perfectly ordinary `ai-feature-add-mastery-theme`. Narrowed to the refspec
# forms that actually push TO master/main. The real guard is the pre-push hook in the agent's clone,
# which parses the remote ref rather than grepping the command line, and refuses master, dev-*, and
# anything not named ai-feature-*; this list is the belt to its braces.
THEMING_DENY = ",".join("Bash(%s)" % p for p in (
    "git push --force:*", "git push -f:*",
    "git push *:master", "git push *:main", "git push * master", "git push * main",
    "git push origin master:*",
    "git reset --hard:*", "git clean:*", "sudo:*", "rm:*", "reboot:*", "shutdown:*",
    "dd:*", "mkfs:*", "gh pr merge:*", "gh repo delete:*", "gh repo archive:*"))
YOTO_SHARE = HOME / ".local/share/moprox/yoto"          # covers/<chapter title>.jpg live here
DOCINDEX_DL = HOME / "docindex/dl"                       # where docwatch downloaded the recording
# Full permissions minus the catastrophic and the outward: it fetches from the web and writes
# covers, it never needs root, deletion, a push, or the host's power button.
CURATE_DENY = ",".join("Bash(%s)" % p for p in (
    "sudo:*", "rm -rf:*", "git push:*", "git reset --hard:*", "reboot:*", "shutdown:*",
    "dd:*", "mkfs:*", "gh:*"))
# devq works the dev queue unattended and has to do what a dev session does: push to main, deploy
# with sudo, ssh to a guest. So it gets everything except the acts that cannot be taken back.
DEVQ_DENY = ",".join("Bash(%s)" % p for p in (
    "git push --force:*", "git push -f:*", "git reset --hard:*", "git clean:*",
    "rm -rf /:*", "rm -rf ~:*", "sudo rm -rf:*", "sudo git:*",
    "reboot:*", "shutdown:*", "sudo reboot:*", "sudo shutdown:*", "dd:*", "mkfs:*",
    "gh repo delete:*", "gh repo archive:*"))
AGENT_FLAGS = {
    # planner: reads everything, changes nothing but the issue board. Bash is for looking (git log,
    # journalctl, ssh to read a remote unit); edits, pushes, sudo and rm are denied at the tool layer.
    "planner": ["--allowedTools", "Bash,Read,Grep,Glob,mcp__logview",
                "--disallowedTools", DEV_DENY,
                "--mcp-config", str(AGENTS["planner"] / "mcp.json"), "--strict-mcp-config",
                "--add-dir", str(REPOS[0]), str(REPOS[1]), str(REPOS[2]), str(SHARED_MEM)],  # variadic: keep last
    "devq": ["--dangerously-skip-permissions",
             "--disallowedTools", DEVQ_DENY,
             "--mcp-config", str(AGENTS["devq"] / "mcp.json"), "--strict-mcp-config"],
    # dev already has broad Bash (can run convo directly); the rest get the convo helper as their one
    # way to read/search the shared conversation on demand.
    "dev": ["--permission-mode", "acceptEdits",
            "--allowedTools", "Bash,Edit,Write,Read,Grep,Glob",
            "--disallowedTools", DEV_DENY,
            # shares THIS session's memory (also symlinked into the dev project dir so it auto-loads);
            "--add-dir", str(REPOS[0]), str(REPOS[1]), str(REPOS[2]), str(BOOK), str(SHARED_MEM)],  # BOOK covers TG_FILES; variadic: keep last
    # coach: a real analyst — Python (matplotlib + the analysis engine), can build & SEND charts to
    # Telegram, do web research (WebSearch/WebFetch), edit its own memory. Catastrophic commands denied.
    "coach": ["--permission-mode", "acceptEdits",
              "--allowedTools", "Bash,Edit,Write,Read,Grep,Glob,WebSearch,WebFetch",
              "--disallowedTools", DEV_DENY,
              # tooling + private-data + dash data + the memory store (coach-memory.md lives there now)
              "--add-dir", str(REPOS[1]), str(REPOS[2]), str(TRAINING_DATA), str(SHARED_MEM),
              str(TG_FILES)],  # variadic: keep last
    # steward: only the convo helper, to investigate routing history when answering meta questions
    "steward": ["--allowedTools", CONVO_TOOL],
    # valet: writes its own preference memory (learns what to surface) + the convo helper
    # valet: morning brief + afternoon catch-up; reads (only) the Google Workspace MCP (read-only,
    # impersonating Mikael) for overnight email + calendar, scoped to ONLY this agent.
    "valet": ["--permission-mode", "acceptEdits",
              "--allowedTools", "Read,Grep,Glob,Edit,Write,mcp__google,%s,%s,%s" % (CONVO_TOOL, DRIVE_TOOL, SHOPPING_TOOL),
              "--mcp-config", str(AGENTS["valet"] / "mcp.json"), "--strict-mcp-config",
              "--add-dir", str(SHARED_MEM), str(TG_FILES)],   # valet-memory.md lives in the memory store; variadic: keep last
    # theming: theme-ontology expert. Answers data questions via the totolo MCP (scoped to ONLY this
    # agent with --mcp-config + --strict-mcp-config) and prepares branch-only edits to the theming repo.
    "theming": ["--permission-mode", "acceptEdits",
                "--allowedTools",
                "Bash,Edit,Write,Read,Grep,Glob,WebSearch,WebFetch,mcp__totolo",   # all totolo MCP tools
                "--disallowedTools", THEMING_DENY,
                "--mcp-config", str(AGENTS["theming"] / "mcp.json"), "--strict-mcp-config",
                "--add-dir", str(THEMING_REPO), str(SHARED_MEM)],   # + theming-memory.md; variadic: keep last
    # bard: the kids' Yoto entertainer. Almost every request is one command (play/stop/volume/
    # shuffle/sleep), so it is configured for REFLEX, not deliberation: a fast model, and a persona
    # that answers in one line and only slows down when something is genuinely unusual (operator,
    # 2026-09-25). Its whole surface is the single `yoto` verb -- no free Bash, because nothing it
    # does needs it and a children's speaker is not a place for improvisation.
    "bard": ["--model", "sonnet",
             "--permission-mode", "acceptEdits",
             "--allowedTools", "Read,Grep,Glob,Edit,Write,%s,%s,%s" % (YOTO_TOOL, BOOKS_TOOL, CONVO_TOOL),
             "--add-dir", str(SHARED_MEM), str(HOME / "projects/private-data/audible"),
             str(HOME / "projects/private-data/books")],   # bard-memory.md lives in the memory store; variadic: keep last
    # triage: the logview MCP only (search logs, ack, file/update issues), no Bash and no edits. Its
    # one outward act is a Telegram message, and that is sent by the harness from its JSON verdict.
    "triage": ["--allowedTools", "Read,Grep,Glob,mcp__logview",
               "--mcp-config", str(AGENTS["triage"] / "mcp.json"), "--strict-mcp-config",
               "--add-dir", str(SHARED_MEM)],   # read-only use: what the estate already knows; variadic: keep last
    "bard-books": ["--model", "sonnet",
                   "--allowedTools", "Read,Grep,Glob,%s" % BOOKS_TOOL,
                   "--add-dir", str(HOME / "projects/private-data/audible"), str(SHARED_MEM)],
    "bard-curate": ["--model", "sonnet",
                    "--dangerously-skip-permissions",
                    "--disallowedTools", CURATE_DENY,
                    # webscout's real browser: a publisher or book site behind a Cloudflare check
                    # refuses curl, and without this bard gave up on the cover (2026-11-16).
                    "--mcp-config", str(AGENTS["bard-curate"] / "mcp.json"), "--strict-mcp-config",
                    "--add-dir", str(YOTO_SHARE), str(DOCINDEX_DL), str(REPOS[1]), str(SHARED_MEM)],  # variadic: keep last
}

# regen (operator, 2026-11-20): every agent the operator addresses on Telegram may re-run anything
# that has posted in its name, e.g. valet regenerating the crash-risk gauges. The unit list is
# derived by services/forward/regen.py from what actually posted as #<agent>, so this grant does not
# need updating when a new job starts posting. It needs MOPROX_AGENT, which run_agent sets.
REGEN_TOOL = "Bash(regen:*)"
REGEN_NOTE = ("If the operator asks you to regenerate, refresh, redo or re-post something that was "
              "posted on Telegram in your name by a scheduled job, run `regen list` to see the jobs "
              "that post as you and `regen run <unit>` to re-run one. It posts the result itself.")
TELEGRAM_AGENTS = ("coach", "dev", "steward", "valet", "theming", "bard")
for _a in TELEGRAM_AGENTS:
    _f = AGENT_FLAGS[_a]
    if "--allowedTools" in _f:
        _i = _f.index("--allowedTools") + 1
        _f[_i] = _f[_i] + "," + REGEN_TOOL

# On claude-dev (CLI 2.1.330) none of the agents' CLAUDE.md @imports loads natively. Headless mode
# is not the cause; measured 2026-11-23 with `claude -p ... --tools ""`, a plain in-project
# `@file.md ` does load. Two separate things keep ours dark:
#   1. the target lives outside the project dir: every agent notebook (@<agent>-memory.md) is a
#      symlink into ~/projects/moprox-memory (probably an external-includes approval that -p never
#      gives; not proved, and claude-loop on 2.1.223 does load them);
#   2. a `.` or `,` straight after the path: coach's `@training-context.md.` and `@part_c_gaps.md.`
# The effect was that every notebook was write-only: coach could not give the max HR from its
# training context, and valet never saw its own "closed, never surface again" notes, so it kept
# nagging about things the operator had told it were done. So the harness expands the imports
# itself, fresh on every run, and hands them over with --append-system-prompt-file. Each file is
# capped: a notebook that grew past the cap keeps its head (the how-to-use-me section) and its
# newest entries, and says where the full file is.
# Both paths must not be live at once: do not make the native import work (delete those periods,
# approve external includes, replace the symlinks) without removing this expander, or the same
# content is sent twice on every run. moprox-memory/claude-p-ignores-claude-md-imports has it all.
IMPORT_CAP = 40000          # chars per imported file
IMPORT_HEAD = 8000


def _imports(agent):
    import re
    root = AGENTS[agent]
    try:
        text = (root / "CLAUDE.md").read_text()
    except OSError:
        return ""
    out = []
    for name in dict.fromkeys(re.findall(r"(?<![\w`])@([A-Za-z0-9_.\-/]+\.md)\b", text)):
        f = root / name
        try:
            body = f.read_text()
        except OSError as exc:
            print("run.py: %s imports %s, which cannot be read: %s" % (agent, name, exc), file=sys.stderr)
            continue
        if len(body) > IMPORT_CAP:
            body = (body[:IMPORT_HEAD] + "\n\n[... %d chars omitted: the full file is %s; Read it for "
                    "older entries ...]\n\n" % (len(body) - IMPORT_CAP, f.resolve())
                    + body[-(IMPORT_CAP - IMPORT_HEAD):])
        out.append("===== %s (imported by your CLAUDE.md; this is its current content) =====\n%s"
                   % (name, body))
    return "\n\n".join(out)


def _appended(agent):
    """Everything run_agent appends to the system prompt for this agent, or ''."""
    parts = [_imports(agent)]
    if agent in TELEGRAM_AGENTS:
        parts.append(REGEN_NOTE)
    return "\n\n".join(x for x in parts if x)

def _log_usage(agent, j):
    """Append one line of token accounting from the --output-format json result envelope (or an
    {"_error": ...} marker on failure)."""
    u = j.get("usage") or {}
    rec = {"ts": int(time.time()), "agent": agent,
           "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
           "cache_read": u.get("cache_read_input_tokens", 0),
           "cache_write": u.get("cache_creation_input_tokens", 0),
           "cost_usd": j.get("total_cost_usd"), "ms": j.get("duration_ms"),
           "turns": j.get("num_turns"), "error": j.get("_error")}
    try:
        USAGE.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE, "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec

# Best-effort freshening of a lane an agent is about to read. Coach's belt traces (speed/incline) come
# from the Technogym console, which syncs to mywellness LATER than Polar does — measured 2026-08-03, a
# session ending ~12:18 appeared ~12:50, while polar-fetch had it within 5 min. So pull right before
# coach runs, on EVERY path (a new session via polar_fetch, and a Telegram reply via route.py — the
# reply usually comes later, which is exactly when the trace has landed). Cheap: ~3 s, and it fetches
# nothing when there is nothing new. Never allowed to block or fail the agent.
PRE_RUN = {"coach": [sys.executable, str(HOME / "projects/moprox-tooling/services/technogym/technogym_fetch.py")]}


def _pre_run(agent):
    cmd = PRE_RUN.get(agent)
    if not cmd:
        return
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        pass          # a stale trace is a degraded read, not a failed one — never block the agent


def _diagnose(out, err):
    """Why the CLI failed, named — instead of the first 300 bytes of the envelope.

    `claude -p --output-format json` reports a failure in the RESULT ENVELOPE, not on stderr, and
    the envelope leads with the zeroed usage counters. Measured 2026-09-07 (CLI 2.1.223, and the
    same shape in the 2026-09-07 credential outage on claude-dev): the whole envelope is 827 chars
    and `subtype`, `errors`, `terminal_reason` ALL sort after `usage`, so a fixed 300-byte prefix
    reports only the part that is identical on every failure —
    `total_cost_usd:0, input_tokens:0, output_tokens:0` — and every caller downstream then invents
    its own explanation for it. Pull the diagnostic fields out by name. stderr is kept whenever
    there is any, and unparseable output still comes through raw: nothing here may report LESS than
    the old prefix did.
    """
    err = (err or "").strip()
    bits = []
    try:
        j = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        j = None
    if isinstance(j, dict):
        for k in ("subtype", "terminal_reason", "stop_reason"):
            if j.get(k):
                bits.append("%s=%s" % (k, j[k]))
        bits += [str(e) for e in (j.get("errors") or [])]
        if (j.get("result") or "").strip():
            bits.append(j["result"].strip()[:300])
        if not bits:                       # an envelope that names nothing: say so, don't go quiet
            bits.append("envelope names no cause: %s" % (out or "").strip()[:300])
    else:
        bits.append((out or "").strip()[:300] or "no output")
    if err:
        bits.append("stderr: " + err[:300])
    return " | ".join(bits)


def run_agent(agent, prompt, timeout=600, extra=None):
    """`extra`: CLI flags a caller decides per run (the queue agents' --model/--effort). They go
    BEFORE the agent's own flags, because those end in a variadic --add-dir."""
    _pre_run(agent)
    cwd = AGENTS[agent]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env["PATH"] = "%s:%s" % (LOCAL_BIN, env.get("PATH", "/usr/bin:/bin"))   # claude shells out to node
    env["MOPROX_AGENT"] = agent       # whose posts `regen` may re-run (services/forward/regen.py)
    import tempfile
    appended = _appended(agent)
    tf = None
    if appended:
        tf = tempfile.NamedTemporaryFile("w", prefix="agent-%s-" % agent, suffix=".md", delete=False)
        tf.write(appended); tf.close()
    cmd = ([CLAUDE, "-p", prompt, "--output-format", "json"] + list(extra or [])
           + (["--append-system-prompt-file", tf.name] if tf else [])
           + AGENT_FLAGS.get(agent, []))      # last: they end in a variadic --add-dir
    try:
        r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log_usage(agent, {"_error": "timeout"})
        raise RuntimeError("timed out after %ds — too big a task to finish in one go" % timeout)
    finally:
        if tf:
            os.unlink(tf.name)
    if r.returncode != 0:
        why = _diagnose(r.stdout, r.stderr)
        _log_usage(agent, {"_error": "exit %d: %s" % (r.returncode, why)})
        raise RuntimeError("agent %s failed (exit %d): %s" % (agent, r.returncode, why))
    try:                                              # json envelope: {result, usage, total_cost_usd, ...}
        j = json.loads(r.stdout)
        _log_usage(agent, j)
        return (j.get("result") or "").strip()
    except (json.JSONDecodeError, TypeError):         # fall back to raw text if the format ever changes
        return r.stdout.strip()

if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2]))
