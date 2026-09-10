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
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/lib"))
import errlog  # noqa: E402  — a notable condition must be findable by priority; see below

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


def _seen(path):
    """Rows already in the file this run is about to overwrite, keyed by `ts`.

    This is the whole of the run's memory, and it is deliberately the output file rather than a
    state file of its own: the output is committed to private-data, so it survives a redeploy and
    is the same thing a human reads. Empty means "no baseline", which makes every row new — loud,
    not quiet, which is the right way round for a first run or a file that was hand-removed."""
    try:
        return {r.get("ts"): r for r in json.loads(path.read_text())}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        errlog.warn(f"amex: cannot read {path.name}, so this run cannot tell a new row from a "
                    f"standing one and will report every row as new", exc)
        return {}


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
                # Settled at birth, whichever kind this is: a bare alert only reaches here when it
                # has no post_time, and the wallet join is on post_time, so there is no clock to
                # join it on and no later run can recover it; an unrecognised wording is never
                # offered to the join at all. Every bare-no-amount row therefore carries `settled`,
                # which is what the novelty gate below is entitled to read.
                "settled": True,
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
    # A bare alert whose join window has not closed yet is not a miss: its wallet row fires seconds
    # later and lands in a later ingest batch roughly 1% of the time (10 s of a 900 s timer). The
    # window is closed once the archive holds a notification from after it, whatever channel.
    newest = max([t for t in (_post_time(e) for e in amex) if t is not None]
                 + [t for t in (_post_time(w) for w, _ in wallet) if t is not None], default=None)
    for e in unpaired:
        t = _post_time(e)
        unparsed.append({
            "ts": e.get("ts"), "kind": "bare-no-amount",
            "card": BARE.match(e["text"].strip())["card"], "text": e["text"].strip(),
            # Recorded, not just computed: an alert first seen with its window open must still be
            # announced on the run that closes it, and the only way to know it never was is to
            # have written down that it was unsettled at the time.
            "settled": not (newest is not None and t is not None and t + WALLET_JOIN_S > newest),
        })

    txns.sort(key=lambda r: r["ts"])
    unparsed.sort(key=lambda r: r["ts"])
    seen_txn, seen_unparsed = _seen(OUT), _seen(OUT_UNPARSED)   # BEFORE the rewrite below
    OUT.write_text(json.dumps(txns, indent=1, ensure_ascii=False) + "\n")
    OUT_UNPARSED.write_text(json.dumps(unparsed, indent=1, ensure_ascii=False) + "\n")

    by_src = {s: sum(1 for r in txns if r["source"] == s)
              for s in ("alert", "wallet-join", "wallet-only")}
    bare_n = sum(1 for u in unparsed if u["kind"] == "bare-no-amount")
    max_dt = max((r["join_dt_s"] for r in txns if r["source"] == "wallet-join"), default=0.0)
    print(f"amex txns: {len(txns)} ({by_src})  unparsed: {len(unparsed)} (bare {bare_n}, "
          f"unrecognised {len(unparsed) - bare_n})")
    # The standing state stays on stdout in full, including both conditions the WARNING lines below
    # used to announce here: nothing a human could read off this run before is missing from it now.
    print(f"  wallet join: max_join_dt_s {max_dt:.2f} of {WALLET_JOIN_S:.0f} bound"
          f"{' — OVER the 0.8 mark, re-measure WALLET_JOIN_S' if max_dt > 0.8 * WALLET_JOIN_S else ''}"
          f", {ambiguous} alerts with >1 candidate in range")
    if bare_n:
        print(f"  standing: {bare_n} bare Amex alert(s) recovered no amount from either channel "
              f"(issue i-20260814-154101); last is "
              f"{[u['ts'] for u in unparsed if u['kind'] == 'bare-no-amount'][-1]}")
    # Both alarms below speak about rows that are NEW in this run. Every quantity above is a
    # reduction over the whole notification archive, and that archive only grows, so any threshold
    # over one is monotone: once it trips it stays tripped, and a unit on a 15-minute timer then
    # repeats it for ever. That is not a hypothetical. `bare_n` has been non-zero since Amex went
    # bare on 2026-08-06 and `max_dt > 0.8 * bound` since one 8.86 s pair landed, so on
    # 2026-09-10 EVERY run printed both WARNING lines, with 113 unchanged and nothing to do about
    # either. services/freshness/lanes.json already reached this conclusion once and wrote it down,
    # retiring the amex-alert-detail lane on 2026-08-15 because it "was asserting something nobody
    # expects to be true again and fired hourly for six days". Only the lane was retired; the
    # producer went on asserting it.
    #
    # And they were asserting it onto stdout, which journald files at info — so the one that IS
    # news, "a pair came in at 8.86 s of a 10 s bound", was invisible to a priority query for the
    # 15 days it has been true, next to one that can never be anything else. The docstring above
    # says what to do when the join approaches its bound ("if it approaches the bound, raise it")
    # and the mechanism that was supposed to say so could not be found.
    slow = [r for r in txns if r["source"] == "wallet-join" and r["ts"] not in seen_txn
            and r["join_dt_s"] > 0.8 * WALLET_JOIN_S]
    if slow or ambiguous:
        # ambiguous is NOT gated on novelty: >1 candidate in range means the greedy join in _join()
        # is no longer provably exact, which is a wrong amount against a merchant, not a margin.
        errlog.warn(f"amex: wallet join at its bound — {len(slow)} new pair(s) over "
                    f"{0.8 * WALLET_JOIN_S:.0f}s (worst {max((r['join_dt_s'] for r in slow), default=0.0):.2f}s "
                    f"of {WALLET_JOIN_S:.0f}s), {ambiguous} alert(s) with >1 candidate in range; "
                    f"re-measure WALLET_JOIN_S before a real pair falls outside it")
    # Waiting one run for an unsettled alert costs nothing: it stays in unparsed and is reported
    # on the run that closes its window. An absent `settled` key is read as True — the rows already
    # in the file predate this field and have long since settled, so a deploy is not a wipeout that
    # re-announces the whole standing backlog.
    def already_said(u):
        prev = seen_unparsed.get(u["ts"])
        return prev is not None and prev.get("settled", True)

    fresh = [u for u in unparsed
             if u["kind"] == "bare-no-amount" and u["settled"] and not already_said(u)]
    if fresh:
        errlog.warn(f"amex: {len(fresh)} new bare Amex alert(s) recovered no amount from either "
                    f"channel and are unrecoverable ({bare_n} standing, issue i-20260814-154101); "
                    f"last is {fresh[-1]['ts']}")


if __name__ == "__main__":
    main()
