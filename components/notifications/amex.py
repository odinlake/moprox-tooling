#!/usr/bin/env python3
"""Parse Amex transaction alerts out of the notification archive -> finance lane.

Matches com.americanexpress.android.acctsvcs.{uk,us}; format (verified 2026-07-16):
  "You have a £3.25 charge on your American Express Card ending in 11005 at STARBUCKS."
Writes private-data/finance/amex-notifs.json (full rewrite each run; source of truth
is the notifications archive). Reconciliation against statements happens in the
spending-tracker build, not here.
"""
import json, re
from pathlib import Path

NOTIF = Path.home() / "projects/private-data/notifications"
OUT = Path.home() / "projects/private-data/finance/amex-notifs.json"
AMEX_PKGS = ("com.americanexpress.android.acctsvcs.uk", "com.americanexpress.android.acctsvcs.us")
CHARGE = re.compile(
    r"You have an? (?P<cur>[£$€])(?P<amount>[\d,]+\.\d{2}) charge on your American Express Card"
    r" ending in (?P<card>\d{4,5}) at (?P<merchant>.+?)\.?\s*$")


def main():
    txns = []
    for f in sorted(NOTIF.glob("notif-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("app") not in AMEX_PKGS:
                continue
            m = CHARGE.match(e.get("text") or "")
            if not m:
                continue
            txns.append({
                "ts": e.get("ts"),
                "amount": float(m["amount"].replace(",", "")),
                "currency": m["cur"],
                "card": m["card"],
                "merchant": m["merchant"].strip(),
            })
    OUT.write_text(json.dumps(txns, indent=1, ensure_ascii=False) + "\n")
    print(f"amex txns: {len(txns)}")


if __name__ == "__main__":
    main()
