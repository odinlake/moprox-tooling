#!/usr/bin/env python3
"""report.py — post a digest of the ledger to the firehose.

The loop reports each cycle as it happens, but the ledger accumulated seventeen cycles of accepted
findings and tested-and-rejected hypotheses before there was any send path at all. That work was
never wrong, merely unread. This dumps it.

Also useful on its own: `report.py --accepted` re-posts the standing findings, which is the closest
thing the estate has to "what does the analyst currently believe".

    report.py            digest: accepted in full, rejected as one-liners
    report.py --accepted only the accepted findings
    report.py --dry      print to stdout, post nothing
    report.py burndown   read that agent's ledger instead of the analyst's

Each loop agent has kept its own ledger since 2026-09-04; a bare `report.py` still means the
analyst, which is what every existing invocation of it meant.
"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify

ROOT = Path.home() / ".local/share/moprox/loop"
LIMIT = 3500          # per Telegram message, leaving room for the #handle and a chunk header


def chunks(header, items):
    """Pack lines into as few messages as possible without splitting an item across two."""
    out, cur = [], header
    for it in items:
        if len(cur) + len(it) + 2 > LIMIT:
            out.append(cur)
            cur = header + " (cont.)\n" + it
        else:
            cur += "\n" + it
    if cur.strip() != header.strip():
        out.append(cur)
    return out


def main():
    argv = sys.argv[1:]
    dry = "--dry" in argv
    only_accepted = "--accepted" in argv
    agent = next((a for a in argv if not a.startswith("-")), "analyst")
    # The pre-split path, kept as a fallback: an agent whose state was never migrated still has its
    # ledger at the old shared location, and reading nothing there would print "no ledger" at a
    # store that plainly exists.
    LEDGER = ROOT / agent / "ledger.json"
    if not LEDGER.exists() and (ROOT / "ledger.json").exists():
        LEDGER = ROOT / "ledger.json"

    if not LEDGER.exists():
        print(f"no ledger at {LEDGER}", file=sys.stderr)
        return 1
    led = json.loads(LEDGER.read_text())

    accepted = led.get("accepted") or []
    tried = led.get("tried") or []
    msgs = []

    msgs.append("LEDGER (%s): %d cycles, %d accepted, %d tested and rejected."
                % (agent, led.get("cycle", 0), len(accepted), len(tried)))

    if accepted:
        items = []
        for a in accepted:
            items.append("\n[cycle %s] %s%s\n  evidence: %s"
                         % (a.get("cycle", "?"), a.get("claim") or a.get("item") or "?",
                            (" -> %s %s" % (a.get("repo"), a.get("sha"))) if a.get("sha") else "",
                            (a.get("evidence") or "")[:300]))
        msgs += chunks("ACCEPTED (verifier passed)", items)

    if tried and not only_accepted:
        items = ["- [c%s] %s" % (t.get("cycle", "?"),
                                 (t.get("claim") or t.get("item") or "?")[:200]) for t in tried]
        msgs += chunks("REJECTED (verifier failed — do not re-derive)", items)

    for m in msgs:
        if dry:
            print("-" * 60); print(m)
        elif not notify.send(m):
            print("send failed; aborting the rest", file=sys.stderr)
            return 1
    print(f"{len(msgs)} message(s) {'rendered' if dry else 'posted'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
