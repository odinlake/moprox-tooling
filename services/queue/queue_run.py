#!/usr/bin/env python3
"""queue_run.py — run the planner or the dev agent over its queue on the issue board.

Operator, 2026-11-16, after a weekly roundup of 30 lines, 15 of them code fixes filed under his
name: "A queue for DEV planner (use best model). A queue for regular DEV. A queue for me (only when
the problem is proved to be external, or a consequential design decision that affects me is
required). Run the planner once a day or so if there are items in its queue. Run the dev agent
frequently if there are items in its queue. Analyst sends items to planner, not to me."

A queue is an issue's `owner` on mo.lan/issues (logview enforces who may file to `operator`):

  planner   Everything that needs judgement lands here. The planner (best model, queues.json)
            turns each item into a dev item with a plan, merges duplicates, closes what is not
            worth doing, and is the ONLY thing that may file to the operator. It runs when its
            queue has something it has not seen, a few times a day at most; an `urgent` item jumps
            that cap so a real breakage does not wait for tomorrow.
  dev       One item per run, often. Two failures or STALE days in the queue send the item BACK TO
            THE PLANNER, never to the operator: the dev queue must not become the next silent pile.

    queue_run.py planner|dev          one pass (what the timers run)
    queue_run.py planner|dev --dry    say what would run; run nothing, write nothing
    queue_run.py planner --force      ignore the run cap (not the empty-queue check)
"""
import fcntl, json, os, re, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE.parent / "agents"))
sys.path.insert(0, str(HERE.parent / "forward"))
import errlog  # noqa: E402

API = os.environ.get("ISSUES_API", "http://logview.lan:8016/api/issues")
CONF = Path(os.environ.get("QUEUE_CONF", HERE / "queues.json"))
LOCAL_CONF = Path(os.environ.get("QUEUE_LOCAL_CONF", Path.home() / ".config/moprox/queues.json"))
STATE_DIR = Path(os.environ.get("QUEUE_STATE_DIR", Path.home() / ".local/state"))
DAY = 86400


def conf(role):
    c = json.loads(CONF.read_text())[role]
    try:
        c.update(json.loads(LOCAL_CONF.read_text()).get(role) or {})
    except FileNotFoundError:
        pass
    except ValueError as exc:
        errlog.err("queue: %s is not valid JSON, using the checked-in settings" % LOCAL_CONF, exc)
    return c


def model_flags(c):
    """The CLI flags for this role's model and effort. Both optional: empty = the CLI default."""
    out = []
    if c.get("model"):
        out += ["--model", str(c["model"])]
    if c.get("effort"):
        out += ["--effort", str(c["effort"])]
    return out


def board():
    with urllib.request.urlopen(API + "?status=open&limit=500", timeout=20) as r:
        return json.loads(r.read().decode()).get("issues") or []


def write(**fields):
    req = urllib.request.Request(API, json.dumps(fields).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read().decode())
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def since(i):
    """When the issue entered its current queue."""
    return _ts(i.get("owner_since")) or _ts(i.get("updated")) or _ts(i.get("created"))


def load(role):
    try:
        return json.loads((STATE_DIR / ("queue-%s.json" % role)).read_text())
    except (FileNotFoundError, ValueError):
        return {"runs": [], "attempts": {}, "seen": {}}


def save(role, st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / ("queue-%s.json" % role)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the agent's answer: %r" % (text or "")[:300])
    return json.loads(m.group(0))


def brief(i, body=0):
    r = {"id": i.get("id"), "owner": i.get("owner"), "title": i.get("title"),
         "in_queue_days": round((time.time() - since(i)) / DAY, 1)}
    if i.get("urgent"):
        r["urgent"] = True
    if body:
        r.update(by=i.get("by") or "", incident=i.get("incident") or "",
                 created=i.get("created"), body=(i.get("body") or "")[:body],
                 last_note=(i.get("note") or "")[:1500])
    return r


# ---------------------------------------------------------------- planner

def planner_due(items, st, c, now, force=False):
    """(run?, why). Runs only for something the planner has not already looked at.

    `seen` maps id -> the issue's `updated` stamp when the planner last had it in a batch. An item
    it chose to keep is therefore not re-read three times a day; it comes back when someone adds
    to it, or once a day regardless so nothing is parked forever on one reading."""
    runs = [t for t in st.get("runs", []) if now - t < DAY]
    last = max(runs, default=0)
    fresh = [i for i in items if st.get("seen", {}).get(i["id"]) != i.get("updated")]
    urgent = [i for i in fresh if i.get("urgent")]
    if force:
        return True, "forced"
    if urgent:
        if len(runs) >= c["urgent_max_runs_per_day"]:
            return False, "urgent item waiting but %d runs in 24 h" % len(runs)
        if now - last < c["urgent_min_gap_h"] * 3600:
            return False, "urgent item waiting, last run %.0f min ago" % ((now - last) / 60)
        return True, "%d urgent" % len(urgent)
    if not fresh and now - last < DAY:
        return False, "nothing new since the last run"
    if len(runs) >= c["max_runs_per_day"]:
        return False, "%d runs in 24 h (cap %d)" % (len(runs), c["max_runs_per_day"])
    if now - last < c["min_gap_h"] * 3600:
        return False, "last run %.1f h ago (gap %g h)" % ((now - last) / 3600, c["min_gap_h"])
    return True, "%d new" % len(fresh) if fresh else "daily re-read"


def planner_prompt(batch, others):
    return ("Plan these, following your CLAUDE.md. Today is %s.\n\n"
            "YOUR QUEUE (%d item(s), urgent first):\n%s\n\n"
            "THE OTHER QUEUES (for duplicates and context; do not re-plan them):\n%s\n"
            % (time.strftime("%Y-%m-%d %H:%M"), len(batch),
               json.dumps([brief(i, 4000) for i in batch], ensure_ascii=False, indent=1),
               json.dumps([brief(i) for i in others], ensure_ascii=False, indent=1)))


def run_planner(c, dry, force):
    now = time.time()
    st = load("planner")
    rows = board()
    items = sorted((i for i in rows if i.get("owner") == "planner"),
                   key=lambda i: (not i.get("urgent"), since(i)))
    if not items:
        print("queue-planner: queue empty")
        return 0
    ok, why = planner_due(items, st, c, now, force)
    if not ok:
        print("queue-planner: %d waiting, not running: %s" % (len(items), why))
        return 0
    batch = items[:int(c["batch"])]
    if dry:
        print("queue-planner: would run (%s) on: %s" % (why, ", ".join(i["id"] for i in batch)))
        return 0
    import run
    st.setdefault("runs", []).append(now)
    st["runs"] = [t for t in st["runs"] if now - t < 2 * DAY]
    save("planner", st)                       # the cap counts attempts: a crash loop must not spin
    try:
        out = parse(run.run_agent(c["agent"], planner_prompt(batch, [i for i in rows if i not in batch]),
                                  timeout=int(c["timeout_s"]), extra=model_flags(c)))
    except Exception as exc:
        errlog.err("queue-planner: run failed, %d item(s) stay in the queue" % len(batch), exc)
        return 1
    after = {i["id"]: i for i in board()}
    seen = st.setdefault("seen", {})
    moved = {}
    for i in batch:
        a = after.get(i["id"])
        dest = "closed" if a is None else a.get("owner")
        moved[dest] = moved.get(dest, 0) + 1
        if a is not None and dest == "planner":
            seen[i["id"]] = a.get("updated")
    for k in [k for k in seen if k not in after]:
        del seen[k]
    save("planner", st)
    msgs = [str(x.get("telegram") or "").strip() for x in out.get("items") or []
            if (after.get(x.get("id")) or {}).get("owner") == "operator"]
    msgs = [m for m in msgs if m]
    if msgs:
        import tg
        for m in msgs[:int(c["max_telegram"])]:
            try:
                tg.send(m, agent="planner")
            except Exception as exc:
                errlog.err("queue-planner: operator message not delivered: %r" % m[:120], exc)
        if len(msgs) > c["max_telegram"]:
            errlog.warn("queue-planner: %d operator messages beyond the per-run cap, not sent; "
                        "the issues are on his queue" % (len(msgs) - c["max_telegram"]))
    print("queue-planner: planned %d (%s), %d still waiting, model=%s effort=%s"
          % (len(batch), ", ".join("%s=%d" % kv for kv in sorted(moved.items())),
             len(items) - len(batch), c.get("model") or "default", c.get("effort") or "default"))
    return 0


# ---------------------------------------------------------------- dev

def back_to_planner(i, why, dry):
    print("queue-dev: %s back to the planner: %s" % (i["id"], why))
    if not dry:
        write(id=i["id"], owner="planner", by="queue-dev",
              note="[queue-dev] back to the planner: %s. Re-plan it, split it, or close it." % why)


def run_dev(c, dry, force):
    now = time.time()
    st = load("dev")
    att = st.setdefault("attempts", {})
    items = [i for i in board() if i.get("owner") == "dev"]
    for k in [k for k in att if k not in {i["id"] for i in items}]:
        del att[k]
    keep = []
    for i in items:
        days = (now - since(i)) / DAY
        try:
            if att.get(i["id"], 0) >= c["max_failures"]:
                back_to_planner(i, "%d failed attempts" % att[i["id"]], dry)
                att.pop(i["id"], None)
            elif days >= c["stale_days"]:
                back_to_planner(i, "%.0f days in the dev queue without landing" % days, dry)
            else:
                keep.append(i)
        except Exception as exc:
            errlog.err("queue-dev: could not hand %s back to the planner" % i["id"], exc)
    if not dry:
        save("dev", st)
    if not keep:
        print("queue-dev: queue empty")
        return 0
    runs = [t for t in st.get("runs", []) if now - t < DAY]
    if len(runs) >= c["max_runs_per_day"] and not force:
        print("queue-dev: %d waiting, not running: %d runs in 24 h" % (len(keep), len(runs)))
        return 0
    # Urgent first, then fewest attempts (a failing item must not starve the rest), then oldest.
    i = sorted(keep, key=lambda i: (not i.get("urgent"), att.get(i["id"], 0), since(i)))[0]
    if dry:
        print("queue-dev: would work %s (%d waiting): %s" % (i["id"], len(keep), i.get("title")))
        return 0
    import run
    st["runs"] = runs + [now]
    att[i["id"]] = att.get(i["id"], 0) + 1        # counted before the run: a timeout is an attempt
    save("dev", st)
    prompt = ("Work this one issue, following your CLAUDE.md. Today is %s. Attempt %d of %d.\n\n%s\n"
              % (time.strftime("%Y-%m-%d %H:%M"), att[i["id"]], c["max_failures"],
                 json.dumps(brief(i, 12000), ensure_ascii=False, indent=1)))
    try:
        out = parse(run.run_agent(c["agent"], prompt, timeout=int(c["timeout_s"]),
                                  extra=model_flags(c)))
    except Exception as exc:
        errlog.warn("queue-dev: %s attempt %d did not finish" % (i["id"], att[i["id"]]), exc)
        try:
            write(id=i["id"], by="queue-dev",
                  note="[queue-dev] attempt %d did not finish: %s" % (att[i["id"]], str(exc)[:300]))
        except Exception as exc2:
            errlog.err("queue-dev: could not record the failed attempt on %s" % i["id"], exc2)
        return 0
    outcome, note = str(out.get("outcome") or "failed"), str(out.get("note") or "")[:1500]
    cur = {x["id"]: x for x in board()}.get(i["id"])
    try:
        if outcome == "done":
            att.pop(i["id"], None)
            if cur is not None and cur.get("owner") == "dev":     # it said done but left it open
                write(id=i["id"], status="closed", by="queue-dev",
                      note="[queue-dev] closed on the agent's report: %s" % note)
        elif outcome == "blocked":
            att.pop(i["id"], None)
            if cur is not None and cur.get("owner") == "dev":
                back_to_planner(i, "blocked: %s" % note, False)
        elif cur is not None:
            write(id=i["id"], by="queue-dev",
                  note="[queue-dev] attempt %d %s: %s" % (att.get(i["id"], 0), outcome, note))
    except Exception as exc:
        errlog.err("queue-dev: could not record the outcome of %s" % i["id"], exc)
    save("dev", st)
    print("queue-dev: %s %s, %d waiting. %s" % (i["id"], outcome, len(keep) - 1, note[:160]))
    return 0


def main():
    args = sys.argv[1:]
    role = args[0] if args and args[0] in ("planner", "dev") else None
    if not role:
        print(__doc__)
        return 2
    dry, force = "--dry" in args, "--force" in args
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / ("queue-%s.lock" % role), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("queue-%s: the previous run is still going, skipping" % role)
        return 0
    try:
        return (run_planner if role == "planner" else run_dev)(conf(role), dry, force)
    except Exception as exc:
        errlog.err("queue-%s: could not read or write the issue board at %s" % (role, API), exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
