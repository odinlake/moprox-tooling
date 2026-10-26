#!/usr/bin/env python3
"""estate-intent: say what you are about to do to shared state, and see who else is doing what.

    estate-intent open  [--ttl MIN] SCOPE WHAT...  -> prints an id; SCOPE e.g. repo:moprox-tooling, box:claude-loop
    estate-intent close ID [OUTCOME...]
    estate-intent check SCOPE [--hours N] -> lists intents still open on SCOPE (exit 0 regardless)

Coordination, not locking: nothing is refused. Records go to the journal (ident `estate-intent`)
and so to the fleet log; `check` reads them back through logview, which makes it work across boxes.
A loop cycle opens `repo:*` (it may land in any repo); `check` matches it for every repo.

Born 2026-10-26: a dev session merged two PRs into moprox-tooling 7 and 11 minutes into a loop
cycle, and the cycle's verified patch was rejected at push. Neither side could see the other.
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import who  # noqa: E402

LOGVIEW = os.environ.get("LOGVIEW_URL", "http://logview.lan:8016")
LINE = re.compile(r"INTENT (open|close) id=(\S+) scope=(\S+) by=(\S+)(?: ttl=(\d+))? :: ?(.*)")


def log(msg):
    subprocess.run(["logger", "-t", "estate-intent", msg], check=True, timeout=10)


def scan(hours):
    q = urllib.parse.urlencode({"grep": "INTENT ", "since": f"-{int(hours)}h", "limit": 500})
    with urllib.request.urlopen(f"{LOGVIEW}/api/search?{q}", timeout=20) as r:
        rows = json.load(r).get("rows", [])
    opened, closed = {}, set()
    for row in sorted(rows, key=lambda r: r.get("ts", "")):
        if row.get("ident") != "estate-intent":
            continue
        m = LINE.search(row.get("msg", ""))
        if not m:
            continue
        kind, iid, scope, by, ttl, what = m.groups()
        if kind == "open" and ttl and _age_min(row["ts"]) > int(ttl):
            continue                    # its owner died without closing it (a crashed cycle)
        if kind == "open":
            opened[iid] = {"id": iid, "scope": scope, "by": by, "what": what, "ts": row["ts"], "host": row.get("host")}
        else:
            closed.add(iid)
    return [v for k, v in opened.items() if k not in closed]


def _age_min(ts):
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60


def matches(a, b):
    return a == b or a.endswith(":*") and b.split(":")[0] == a[:-2] or b.endswith(":*") and a.split(":")[0] == b[:-2]


def main(argv):
    if len(argv) < 2 or argv[0] not in ("open", "close", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, me = argv[0], who.agent()
    if cmd == "open":
        ttl = ""
        if argv[1] == "--ttl":
            ttl, argv = f" ttl={int(argv[2])}", [argv[0]] + argv[3:]
        iid = uuid.uuid4().hex[:8]
        log(f"INTENT open id={iid} scope={argv[1]} by={me}{ttl} :: {' '.join(argv[2:])}")
        print(iid)
    elif cmd == "close":
        log(f"INTENT close id={argv[1]} scope=- by={me} :: {' '.join(argv[2:])}")
    else:
        hours = float(argv[argv.index("--hours") + 1]) if "--hours" in argv else 3
        try:
            live = [i for i in scan(hours) if matches(i["scope"], argv[1]) and i["by"] != me]
        except Exception as e:      # coordination is advisory; an unreachable logview must not block work
            print(f"estate-intent: could not read intents from {LOGVIEW}: {e}", file=sys.stderr)
            subprocess.run(["logger", "-t", "estate-intent", "-p", "user.err", f"check failed: {e}"])
            return 0
        for i in live:
            print(f"OPEN since {i['ts'][:16]} by {i['by']} on {i['host']} [{i['scope']}]: {i['what']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
