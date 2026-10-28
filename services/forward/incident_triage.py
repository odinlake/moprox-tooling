#!/usr/bin/env python3
"""incident_triage.py — hand every never-triaged incident to the `triage` agent; post only what needs him.

Operator, 2026-10-28, on the daily #watchdog digest: "The messages are all but useless. They say
things like '8 error-shaped lines' with no concrete example. I don't think this is the level at
which I want to be involved at all. Error and warning messages should go to a dev agent for
triaging and categorization. ... Actionable things can be dealt with without my oversight. If an
agent determines that something is genuinely new, important, and needs my input it can be posted
right away on telegram."

Each run (incident-triage.timer, hourly):
  1. reads the incident queue (14 d, acked included) and picks the keys not yet in the ledger;
  2. hands up to BATCH of them to one `claude -p` triage run (Max subscription, logview MCP only),
     which finds a real example line, assigns a cause-category, and acks noise / files issues for
     the burndown agent / files operator issues itself;
  3. records each verdict in the ledger, and sends the agent's `telegram` text for the rare
     `operator` item straight away, as #watchdog.

The weekly roundup in incident_digest.py is built from this ledger: categories, counts, examples.

    incident_triage.py            one pass (what the timer runs)
    incident_triage.py --dry      list what would be triaged; run nothing, send nothing
"""
import json, os, re, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog  # noqa: E402

URL = os.environ.get("INCIDENT_URL",
                     "http://logview.lan:8016/api/incidents?window=14d&limit=200&include_acked=1")
LEDGER = Path(os.environ.get("TRIAGE_LEDGER", Path.home() / ".local/state/incident-triage.json"))
BATCH = int(os.environ.get("TRIAGE_BATCH", "8"))
MAX_TG = 2                      # per run: a runaway verdict must not become a flood
DISPOSITIONS = {"noise", "known", "actionable", "operator"}


def key(i):
    """Same identity as incident_digest.key(): a lane is one thing whichever box saw it."""
    unit = i.get("unit", "?")
    return unit if unit.startswith("lane-") else f"{i.get('host', '?')}/{unit}"


def fetch():
    with urllib.request.urlopen(URL, timeout=30) as r:
        return json.loads(r.read().decode()).get("incidents") or []


def load():
    try:
        return json.loads(LEDGER.read_text())
    except (FileNotFoundError, ValueError):
        return {"keys": {}, "categories": {}}


def save(led):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, ensure_ascii=False))
    tmp.replace(LEDGER)


def prompt(batch, cats):
    rows = [{"key": key(i), "host": i.get("host"), "unit": i.get("unit"), "kind": i.get("kind"),
             "count_14d": i.get("count"), "first_seen": i.get("first_seen_iso"),
             "last_seen": i.get("last_seen_iso"), "acked": i.get("acked"),
             "recurred_after_ack": i.get("recurred_after_ack"),
             "summary": str(i.get("last_message") or "")[:600]} for i in batch]
    known = [{"category": c, "desc": v.get("desc", ""), "disposition": v.get("disposition", "")}
             for c, v in sorted(cats.items())]
    return ("Triage these incidents, following your CLAUDE.md. Today is %s.\n\n"
            "EXISTING CATEGORIES (reuse when the cause matches):\n%s\n\nINCIDENTS:\n%s\n"
            % (time.strftime("%Y-%m-%d %H:%M"), json.dumps(known, ensure_ascii=False, indent=1),
               json.dumps(rows, ensure_ascii=False, indent=1)))


def parse(text):
    """The agent's final message should be bare JSON; tolerate a fence or prose around it."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in triage output: %r" % (text or "")[:300])
    return json.loads(m.group(0)).get("items") or []


def apply(led, batch, items, now):
    """Fold verdicts into the ledger. -> [telegram texts]. A key the agent skipped stays untriaged."""
    given = {key(i) for i in batch}
    msgs = []
    for it in items:
        k = it.get("key")
        if k not in given:
            errlog.warn("incident-triage: agent returned an unknown key %r, ignored" % k)
            continue
        disp = it.get("disposition") if it.get("disposition") in DISPOSITIONS else "known"
        cat = (it.get("category") or "uncategorised").strip()[:80]
        led["keys"][k] = {"category": cat, "disposition": disp, "at": now,
                          "example": str(it.get("example") or "")[:240],
                          "issue": str(it.get("issue") or "")}
        c = led["categories"].setdefault(cat, {"first": now})
        c.update(desc=str(it.get("category_desc") or c.get("desc", ""))[:200], disposition=disp,
                 example=led["keys"][k]["example"] or c.get("example", ""), last=now)
        if disp == "operator" and (it.get("telegram") or "").strip():
            msgs.append(it["telegram"].strip())
    return msgs


def main():
    dry = "--dry" in sys.argv[1:]
    try:
        rows = fetch()
    except Exception as exc:
        errlog.err("incident-triage: could not read the queue at %s" % URL, exc)
        return 1
    led = load()
    fresh = {}
    for i in rows:                          # one row per key, the most recent
        k = key(i)
        if k not in led["keys"] and (k not in fresh or i.get("last_seen", 0) > fresh[k].get("last_seen", 0)):
            fresh[k] = i
    todo = sorted(fresh.values(), key=lambda i: -int(i.get("score") or 0))
    if not todo:
        print("incident-triage: nothing new (%d keys triaged)" % len(led["keys"]))
        return 0
    batch = todo[:BATCH]
    if dry:
        for i in batch:
            print("would triage:", key(i), i.get("kind"))
        print("(%d more waiting)" % (len(todo) - len(batch)))
        return 0
    import run
    try:
        out = run.run_agent("triage", prompt(batch, led["categories"]), timeout=1200)
        items = parse(out)
    except Exception as exc:
        errlog.err("incident-triage: triage run failed, %d incident(s) stay untriaged" % len(batch), exc)
        return 1
    now = int(time.time())
    msgs = apply(led, batch, items, now)
    save(led)                               # before Telegram: a send failure must not re-triage
    if msgs:
        import tg
        for m in msgs[:MAX_TG]:
            try:
                tg.send(m, agent="watchdog")
            except Exception as exc:
                errlog.err("incident-triage: operator alert not delivered: %r" % m[:120], exc)
        if len(msgs) > MAX_TG:
            errlog.warn("incident-triage: %d operator alerts beyond the per-run cap, not sent" % (len(msgs) - MAX_TG))
    print("incident-triage: triaged %d (%s), %d to operator, %d still waiting"
          % (len(items), ", ".join("%s=%d" % (d, sum(1 for x in items if x.get("disposition") == d))
                                   for d in sorted(DISPOSITIONS)), len(msgs), len(todo) - len(batch)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
