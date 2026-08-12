#!/usr/bin/env python3
"""Recover every audit objection the harness truncated, from the refuters' own transcripts.

Until this cycle, `refute()` captured a skeptic's verdict as `_clip(defect, 300)` — the ledger's
copy was the ONLY copy, because a disputed claim is published nowhere, and it was cut at 300 chars
mid-sentence with a bare "…". 32 of the 34 objections ever raised came back at exactly that cap.

The refuters are `claude -p` subprocesses run with cwd=$HOME, so their full stdout is retained in
~/.claude/projects/-home-mikael/*.jsonl. Each one's last assistant message ends in the JSON verdict
object it was asked for, `defect` intact. This walks every transcript on the host, harvests those
verdicts, and matches each to a ledger objection by prefix — a 299-char prefix match is not
something two independent objections collide on, and the run below found 0 ambiguous matches.

Writes STATE/objections/c<cycle>-<lens>.txt. Idempotent; never overwrites with something shorter.
Does NOT touch ledger.json: the loop holds it open for the length of a cycle and saves it at the
end, so an edit here would be silently reverted. ledger_digest() reads this directory instead.
"""
import json
import os
import sys
from glob import glob
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".local/share/moprox/loop"
LEDGER = STATE / "ledger.json"
OBJECTIONS = STATE / "objections"


def fold(s):
    return " ".join(str(s).split())


def harvest():
    """Every {"refuted": true, "defect": ...} verdict on this host, from transcript tails."""
    out = []
    for f in glob(str(HOME / ".claude/projects/*/*.jsonl")):
        texts = []
        try:
            with open(f, errors="replace") as fh:
                for ln in fh:
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    msg = rec.get("message") or {}
                    if rec.get("type") == "assistant" and isinstance(msg.get("content"), list):
                        for c in msg["content"]:
                            if c.get("type") == "text":
                                texts.append(c["text"])
        except OSError:
            continue
        if not texts:
            continue
        last = texts[-1]
        i, j = last.rfind("{"), last.rfind("}")
        if i < 0 or j <= i:
            continue
        try:
            v = json.loads(last[i:j + 1])
        except ValueError:
            continue
        if isinstance(v, dict) and v.get("refuted") and str(v.get("defect", "")).strip():
            out.append(fold(v["defect"].strip()))
    return out


def main():
    defects = harvest()
    led = json.loads(LEDGER.read_text())
    rows = [(e.get("cycle"), o)
            for v in led.values() if isinstance(v, list)
            for e in v if isinstance(e, dict)
            for o in (e.get("objections") or [])]

    OBJECTIONS.mkdir(parents=True, exist_ok=True)
    wrote = skipped = ambiguous = unmatched = 0
    for cyc, o in rows:
        name = o.split("]", 1)[0].lstrip("[") if o.startswith("[") else "unknown"
        stem = o.split("] ", 1)[-1].rstrip("…")
        hits = sorted({d for d in defects if d.startswith(stem)})
        if not hits:
            # Cycle 5's two objections came from the retrospective panel that built the audit gate,
            # not from refute(), so they were never clipped and there is nothing to recover.
            cut = o.endswith("…")
            unmatched += cut
            print(f"c{cyc}-{name}: no transcript match ({len(o)} chars in ledger"
                  f"{', TRUNCATED — text lost' if cut else ', complete — nothing to recover'})")
            continue
        if len(hits) > 1:
            ambiguous += 1
            print(f"c{cyc}-{name}: {len(hits)} distinct matches — NOT archived")
            continue
        full = f"[{name}] {hits[0]}"
        dst = OBJECTIONS / f"c{cyc}-{name}.txt"
        if dst.exists() and len(dst.read_text()) >= len(full):
            skipped += 1
            continue
        dst.write_text(full)
        wrote += 1
        print(f"c{cyc}-{name}: {len(o)} -> {len(full)} chars")

    print(f"\n{wrote} written, {skipped} already current, "
          f"{ambiguous} ambiguous, {unmatched} unmatched, of {len(rows)} ledger objections")
    return 1 if (ambiguous or unmatched) else 0


if __name__ == "__main__":
    sys.exit(main())
