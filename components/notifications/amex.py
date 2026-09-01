#!/usr/bin/env python3
"""Parse Amex transaction alerts out of the notification archive -> finance lane.

Matches com.americanexpress.android.acctsvcs.{uk,us}. Two alert wordings exist:

  detailed  "You have a £3.25 charge on your American Express Card ending in 11005 at STARBUCKS."
  detailed  "You have a 139.00 kr charge on ... at NEXTORY AB."   (foreign: amount-then-code)
  bare      "There was a transaction on your card ending with 11005."

Only the detailed wording carries an amount and merchant. Amex switched this
account to the bare wording on 2026-08-06 (last detailed alert 02:17:41, first
bare 07:31:10) and every alert since has been bare, so nothing is extractable
FROM THE AMEX ALERT ITSELF. It is recoverable from a second channel: see below.

Unmatched Amex alerts are therefore COUNTED and written to
finance/amex-notifs-unparsed.json rather than silently skipped: a wording change
upstream must show up as a git diff, not as a quietly flat transaction count.

## The Google Wallet side channel

com.google.android.apps.walletnfcrel / channel tapandpay.transactions.low began
posting on 2026-08-27 and carries, for the same card, what the Amex alert lost:

  title "STARBUCKS"   text "£3.25 with Platinum Cashback Credit Card ••1005"

It fires a few seconds AFTER the bare Amex alert for the same purchase, so a bare
alert is joined to the nearest not-yet-used wallet row within WALLET_JOIN_S on
post_time (the device clock; `ts` is the ingest clock and adds its own jitter).

WALLET_JOIN_S is 10 s, NOT the 4 s that wallet-tapandpay-channel-persists
published on 2026-08-31 as "ample". That 4 s was the max observed over
2026-08-27..08-31 (3.72 s) plus slack, and the very next day broke it: 3 of the 4
pairs on 2026-09-01 sit at 6.04, 7.76 and 7.95 s. A tolerance fitted to the days
that happened to line up drops the days that did not, and the dropped rows do not
look like drops -- they look like coverage misses on both channels at once.
10 s is safe rather than merely larger: across all 23 bare alerts in the wallet
era NO alert has more than one wallet row within 10 s, so widening cannot mispair.
The join is not a guess about the future either way, so max_join_dt_s is printed
every run and stored per row as join_dt_s: if it approaches the bound, raise it.

Wallet rows that pair to no Amex alert are kept as source="wallet-only" -- the
Amex lane demonstrably misses purchases the wallet channel announces (GBP 41.42
on 2026-08-28 alone), so dropping them would lose money the estate has captured.

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
WALLET_PKG = "com.google.android.apps.walletnfcrel"
WALLET_CHANNEL = "tapandpay.transactions.low"
WALLET_JOIN_S = 10.0

# Amount is either symbol-prefixed (£3.25) or code-suffixed (139.00 kr).
CHARGE = re.compile(
    r"You have an? (?:(?P<sym>[£$€])(?P<amount_sym>[\d,]+\.\d{2})"
    r"|(?P<amount_code>[\d,]+\.\d{2}) (?P<code>[A-Za-z]{2,3}))"
    r" charge on your American Express Card"
    r" ending in (?P<card>\d{4,5}) at (?P<merchant>.+?)\.?\s*$")
# Carries no amount/merchant — recognised so it is reported, not mistaken for noise.
BARE = re.compile(r"There was a transaction on your card ending with (?P<card>\d{4,5})\.?\s*$")
# Wallet: "£3.25 with Platinum Cashback Credit Card ••1005" / "139.00 kr with ..."
WALLET = re.compile(
    r"^(?:(?P<sym>[£$€])(?P<amount_sym>[\d,]+\.\d{2})"
    r"|(?P<amount_code>[\d,]+\.\d{2}) (?P<code>[A-Za-z]{2,3}))"
    r" with (?P<product>.+?) [••]+(?P<card>\d{4,5})\s*$")


def _post_time(e):
    """Device clock, seconds. None if absent — such a row cannot be joined."""
    v = e.get("post_time")
    return None if v is None else v / 1000.0


def _read_notifications():
    amex, wallet = [], []
    for f in sorted(NOTIF.glob("notif-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("app") in AMEX_PKGS:
                amex.append(e)
            elif e.get("app") == WALLET_PKG and e.get("channel") == WALLET_CHANNEL:
                m = WALLET.match((e.get("text") or "").strip())
                if m:
                    wallet.append((e, m))
    return amex, wallet


def _join(bare_alerts, wallet):
    """Injective nearest-neighbour, wallet strictly after the alert, within the bound.

    Greedy over alerts in post_time order. That is exact here because no alert has
    two wallet candidates in range (see module docstring); if that ever stops being
    true the ambiguity count printed below goes non-zero and greedy is no longer safe.
    """
    used, pairs, unpaired, ambiguous = set(), [], [], 0
    for e, t in sorted(bare_alerts, key=lambda x: x[1]):
        cands = [(wt - t, i) for i, (w, _) in enumerate(wallet)
                 if (wt := _post_time(w)) is not None and 0 <= wt - t <= WALLET_JOIN_S]
        if len(cands) > 1:
            ambiguous += 1
        free = [c for c in cands if c[1] not in used]
        if not free:
            unpaired.append(e)
            continue
        dt, i = min(free)
        used.add(i)
        pairs.append((e, wallet[i], dt))
    return pairs, unpaired, [w for i, w in enumerate(wallet) if i not in used], ambiguous


def _amount(m):
    return float((m["amount_sym"] or m["amount_code"]).replace(",", "")), \
        (m["sym"] or (m["code"] or "").upper())


def main():
    amex, wallet = _read_notifications()
    txns, unparsed, bare_alerts = [], [], []
    for e in amex:
        text = (e.get("text") or "").strip()
        m = CHARGE.match(text)
        if not m:
            bare = BARE.match(text)
            t = _post_time(e)
            if bare and t is not None:
                bare_alerts.append((e, t))       # joinable — decided after the join
                continue
            unparsed.append({
                "ts": e.get("ts"),
                "kind": "bare-no-amount" if bare else "unrecognised",
                "card": bare["card"] if bare else None,
                "text": text,
            })
            continue
        amount, currency = _amount(m)
        txns.append({
            "ts": e.get("ts"), "amount": amount, "currency": currency,
            "card": m["card"], "merchant": m["merchant"].strip(), "source": "alert",
        })

    pairs, unpaired, wallet_only, ambiguous = _join(bare_alerts, wallet)
    for e, (w, m), dt in pairs:
        amount, currency = _amount(m)
        txns.append({
            "ts": e.get("ts"), "amount": amount, "currency": currency,
            "card": BARE.match(e["text"].strip())["card"], "merchant": (w.get("title") or "").strip(),
            "source": "wallet-join", "join_dt_s": round(dt, 3), "wallet_ts": w.get("ts"),
        })
    for w, m in wallet_only:
        amount, currency = _amount(m)
        txns.append({
            "ts": w.get("ts"), "amount": amount, "currency": currency,
            "card": m["card"], "merchant": (w.get("title") or "").strip(),
            "source": "wallet-only",
        })
    for e in unpaired:
        unparsed.append({
            "ts": e.get("ts"), "kind": "bare-no-amount",
            "card": BARE.match(e["text"].strip())["card"], "text": e["text"].strip(),
        })

    txns.sort(key=lambda r: r["ts"])
    unparsed.sort(key=lambda r: r["ts"])
    OUT.write_text(json.dumps(txns, indent=1, ensure_ascii=False) + "\n")
    OUT_UNPARSED.write_text(json.dumps(unparsed, indent=1, ensure_ascii=False) + "\n")

    by_src = {s: sum(1 for r in txns if r["source"] == s)
              for s in ("alert", "wallet-join", "wallet-only")}
    bare_n = sum(1 for u in unparsed if u["kind"] == "bare-no-amount")
    max_dt = max((r["join_dt_s"] for r in txns if r["source"] == "wallet-join"), default=0.0)
    print(f"amex txns: {len(txns)} ({by_src})  unparsed: {len(unparsed)} (bare {bare_n}, "
          f"unrecognised {len(unparsed) - bare_n})")
    print(f"  wallet join: max_join_dt_s {max_dt:.2f} of {WALLET_JOIN_S:.0f} bound, "
          f"{ambiguous} alerts with >1 candidate in range")
    if max_dt > 0.8 * WALLET_JOIN_S or ambiguous:
        print(f"  WARNING: wallet join is near its bound or ambiguous; re-measure WALLET_JOIN_S")
    if bare_n:
        print(f"  WARNING: {bare_n} bare Amex alerts recovered no amount from either channel; "
              f"last is {[u['ts'] for u in unparsed if u['kind'] == 'bare-no-amount'][-1]}")


if __name__ == "__main__":
    main()
