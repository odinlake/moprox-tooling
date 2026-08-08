#!/usr/bin/env python3
"""Parse Amex transaction alerts out of the notification archive -> finance lane.

Matches com.americanexpress.android.acctsvcs.{uk,us}. Two alert wordings exist:

  detailed  "You have a £3.25 charge on your American Express Card ending in 11005 at STARBUCKS."
  detailed  "You have a 139.00 kr charge on ... at NEXTORY AB."   (foreign: amount-then-code)
  bare      "There was a transaction on your card ending with 11005."

Only the detailed wording carries an amount and merchant. Amex switched this
account to the bare wording on 2026-08-06 (last detailed alert 02:17:41, first
bare 07:31:10) and every alert since has been bare, so nothing is extractable
from that window -- the information is absent from the notification, not merely
unparsed. That matters because the statement PDFs stop at 2026-06-10
(see finance-statement-tail-truncation), which made these alerts the only live
Amex source.

Unmatched Amex alerts are therefore COUNTED and written to
finance/amex-notifs-unparsed.json rather than silently skipped: a wording change
upstream must show up as a git diff, not as a quietly flat transaction count.

Writes private-data/finance/amex-notifs.json (full rewrite each run; source of
truth is the notifications archive). Reconciliation against statements happens in
the spending-tracker build, not here.
"""
import json, re
from pathlib import Path

NOTIF = Path.home() / "projects/private-data/notifications"
OUT = Path.home() / "projects/private-data/finance/amex-notifs.json"
OUT_UNPARSED = Path.home() / "projects/private-data/finance/amex-notifs-unparsed.json"
AMEX_PKGS = ("com.americanexpress.android.acctsvcs.uk", "com.americanexpress.android.acctsvcs.us")

# Amount is either symbol-prefixed (£3.25) or code-suffixed (139.00 kr).
CHARGE = re.compile(
    r"You have an? (?:(?P<sym>[£$€])(?P<amount_sym>[\d,]+\.\d{2})"
    r"|(?P<amount_code>[\d,]+\.\d{2}) (?P<code>[A-Za-z]{2,3}))"
    r" charge on your American Express Card"
    r" ending in (?P<card>\d{4,5}) at (?P<merchant>.+?)\.?\s*$")
# Carries no amount/merchant — recognised so it is reported, not mistaken for noise.
BARE = re.compile(r"There was a transaction on your card ending with (?P<card>\d{4,5})\.?\s*$")


def main():
    txns, unparsed = [], []
    for f in sorted(NOTIF.glob("notif-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("app") not in AMEX_PKGS:
                continue
            text = (e.get("text") or "").strip()
            m = CHARGE.match(text)
            if not m:
                bare = BARE.match(text)
                unparsed.append({
                    "ts": e.get("ts"),
                    "kind": "bare-no-amount" if bare else "unrecognised",
                    "card": bare["card"] if bare else None,
                    "text": text,
                })
                continue
            txns.append({
                "ts": e.get("ts"),
                "amount": float((m["amount_sym"] or m["amount_code"]).replace(",", "")),
                "currency": m["sym"] or m["code"].upper(),
                "card": m["card"],
                "merchant": m["merchant"].strip(),
            })
    OUT.write_text(json.dumps(txns, indent=1, ensure_ascii=False) + "\n")
    OUT_UNPARSED.write_text(json.dumps(unparsed, indent=1, ensure_ascii=False) + "\n")
    bare_n = sum(1 for u in unparsed if u["kind"] == "bare-no-amount")
    print(f"amex txns: {len(txns)}  unparsed: {len(unparsed)} (bare {bare_n}, "
          f"unrecognised {len(unparsed) - bare_n})")
    if unparsed:
        last_txn = txns[-1]["ts"] if txns else "never"
        print(f"  WARNING: {len(unparsed)} Amex alerts carried no amount; "
              f"last extractable txn {last_txn}")


if __name__ == "__main__":
    main()
