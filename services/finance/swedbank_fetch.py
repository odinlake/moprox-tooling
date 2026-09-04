#!/usr/bin/env python3
"""swedbank_fetch.py — pull Swedbank transactions via Enable Banking into private-data.

WHY THIS RUNS ON A TIMER AND NOT ON DEMAND. Swedbank serves a ROLLING 90-day window: ask for
anything older and the API answers 422 WRONG_TRANSACTIONS_PERIOD, naming the earliest date it will
serve. So a transaction is retrievable for 90 days and then gone forever. The consent has existed
since 2026-06-27 with nothing consuming it, which means everything before 2026-05-17 is already
unrecoverable. Every day this does not run, another day falls off the end.

The local store is therefore the archive, not a cache: transactions are merged into
private-data/statements/swedbank/<account>.jsonl and never deleted, so history accumulates past the
bank's window.

Auth is an RS256 JWT signed with the app's private key (kid = application id) — no refresh dance;
the SESSION is what expires, on 2026-12-23, and re-authorising it needs BankID in a browser.

    swedbank_fetch.py            incremental: from the newest stored booking date, minus a few days
    swedbank_fetch.py --full     ask for the whole window the bank still allows
    swedbank_fetch.py --dry      fetch and report; write nothing
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

PRIV = Path(os.environ.get("PRIVATE_DATA", Path.home() / "projects/private-data"))
SEC = PRIV / "secrets"
OUTDIR = PRIV / "statements/swedbank"
DERIVED = PRIV / "finance/swedbank.json"
# Re-ask for a few days either side of what we already hold: a booked transaction can appear late,
# and re-fetching is free next to losing one.
OVERLAP_DAYS = int(os.environ.get("SWEDBANK_OVERLAP_DAYS", "5"))
TIMEOUT = 45


def env():
    d = {}
    for ln in (SEC / "enablebanking.env").read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip().strip("'\"")
    return d


def token(cfg):
    import jwt                                   # PyJWT; system package on claude-dev
    now = int(time.time())
    return jwt.encode({"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                       "iat": now, "exp": now + 3600},
                      (SEC / "enablebanking_private.pem").read_text(),
                      algorithm="RS256", headers={"typ": "JWT", "kid": cfg["EB_APP_ID"]})


RETRY_429 = 4                 # attempts, not retries: 1 try + 3 backoffs
RETRY_BACKOFF_S = (5, 20, 60)


def api(cfg, tok, path, **params):
    """One GET, retrying a 429 rather than losing the day's window to it.

    Enable Banking rate-limits per consent. On 2026-08-15 a single burst took out all 7 accounts at
    once and the run reported success, which matters here more than in most lanes: the API keeps a
    ROLLING 90-DAY window, so a day skipped is a day that eventually falls off the end and is gone
    for good. Retrying costs seconds; not retrying costs history.

    Retry-After is honoured when the server sends one — it knows its own window better than a fixed
    schedule does — and capped, so a hostile or absurd value cannot park a timer-driven run forever.
    Only 429 is retried: a 4xx that is not a rate limit will not fix itself by waiting."""
    url = cfg["EB_API_BASE"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    for attempt in range(RETRY_429):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == RETRY_429 - 1:
                raise
            wait = RETRY_BACKOFF_S[attempt]
            hdr = e.headers.get("Retry-After") if e.headers else None
            if hdr and hdr.strip().isdigit():
                wait = min(int(hdr.strip()), 120)
            errlog.warn(f"swedbank: 429 on {path} — waiting {wait}s "
                        f"(attempt {attempt + 1}/{RETRY_429})")
            time.sleep(wait)


def slug(acct):
    """Stable, readable filename per account: product + last 4 of the IBAN."""
    iban = ((acct.get("account_id") or {}).get("iban") or acct.get("uid", ""))[-4:]
    name = (acct.get("product") or "account").lower().replace(" ", "-")
    return f"{name}-{iban}"


def key(t):
    """Identity of a transaction. entry_reference is Swedbank's own and is present on every row
    seen so far; fall back to a composite rather than dropping the row."""
    return (t.get("entry_reference") or t.get("transaction_id")
            or "|".join([str(t.get("booking_date")), str((t.get("transaction_amount") or {}).get("amount")),
                         str((t.get("creditor") or {}).get("name")), str(t.get("reference_number"))]))


def load(path):
    out = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            if ln.strip():
                try:
                    t = json.loads(ln)
                    out[key(t)] = t
                except ValueError as e:
                    errlog.skip(f"swedbank: parsing {path.name}", e)
    return out


def newest(existing):
    ds = [t.get("booking_date") for t in existing.values() if t.get("booking_date")]
    return max(ds) if ds else None


def fetch_account(cfg, tok, uid, date_from):
    """All transactions from date_from, following continuation keys. Returns (rows, actual_from).

    A 422 names the earliest date the consent still covers — use it rather than guessing, and say so,
    because that date moving forward IS the data loss this service exists to outrun."""
    rows, cont, actual = [], None, date_from
    while True:
        try:
            d = api(cfg, tok, f"/accounts/{uid}/transactions",
                    date_from=actual, continuation_key=cont)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 422 and "WRONG_TRANSACTIONS_PERIOD" in body and not cont:
                try:
                    allowed = json.loads(body)["detail"]["date_from"]
                except (ValueError, KeyError):
                    raise
                errlog.warn(f"swedbank: {uid} asked from {actual}, bank allows only {allowed} — "
                            f"anything earlier is gone from the API for good")
                actual = allowed
                continue
            raise
        rows += d.get("transactions") or []
        cont = d.get("continuation_key")
        if not cont:
            return rows, actual


def _chain(rows):
    """Put the store in the order the bank's own running balance implies, or say where it breaks.

    Every row carries balance_after_transaction, so a complete store is a closed chain: each balance
    is the previous one plus the signed amount. Within a booking date the file order is NOT the
    ledger order (the file sorts by (booking_date, entry_reference)), so the intra-day orderings are
    searched. Returns (rows_in_ledger_order, None), or (None, first_bad_booking_date)."""
    from decimal import Decimal
    from itertools import permutations

    def sgn(r):
        a = Decimal(r["transaction_amount"]["amount"])
        return a if r["credit_debit_indicator"] == "CRDT" else -a

    def bal(r):
        return Decimal(r["balance_after_transaction"]["amount"])

    rows = [r for r in rows if r.get("balance_after_transaction") and r.get("booking_date")]
    if len(rows) < 2:
        return rows, None
    days = {}
    for r in rows:
        days.setdefault(r["booking_date"], []).append(r)
    prev, ordered = None, []
    for d in sorted(days):
        grp = days[d]
        # n! over a day's transactions; beyond a handful, fall back to stored order rather than hang.
        cands = [tuple(grp)] if len(grp) > 8 else permutations(grp)
        hit = None
        for p in cands:
            if prev is None:
                if all(bal(p[i]) + sgn(p[i + 1]) == bal(p[i + 1]) for i in range(len(p) - 1)):
                    hit = p
                    break
                continue
            b, good = prev, True
            for r in p:
                b += sgn(r)
                if b != bal(r):
                    good = False
                    break
            if good:
                hit = p
                break
        if hit is None:
            return None, d
        ordered.extend(hit)
        prev = bal(hit[-1])
    return ordered, None


def ladder_ok(rows):
    """Does the store still reconcile against the bank's own running balance?

    A missing INTERIOR row is detectable without asking the bank anything — and the interior is
    exactly where this lane loses data. The 422 clamp in fetch_account advances to whatever floor
    the bank still serves, never comparing it to what we already hold; if it ever steps past our
    newest booking date, the rows in between are gone for good and the store closes over the hole
    silently.

    Measured on the real store (analyst cycle 286, 2026-08-29): interior gaps of 1..4 consecutive
    rows were caught 51/51. Loss at either END of the store is NOT detectable this way (0/4) — a
    truncation leaves a chain that still closes. This is a gap check, not a completeness proof.

    Returns (ok, first_bad_date)."""
    ordered, bad = _chain(rows)
    return ordered is not None, bad


def closing(rows):
    """The account's STOCK, not its flow: balance after the last row in ledger order.

    The bank's own number, never a reconstruction. Returns (booking_date, balance) or (None, None)
    when the store is empty or the chain does not close — in which case the last row is not knowable
    and guessing would be worse than saying nothing. Carries ladder_ok's endpoint blindness: a store
    truncated at its newest end still chains, so this is the newest balance the estate HOLDS, which
    is not necessarily the account's balance today."""
    ordered, _ = _chain(rows)
    if not ordered:
        return None, None
    last = ordered[-1]
    return last["booking_date"], float(last["balance_after_transaction"]["amount"])


def amount(t):
    """Signed SEK. DBIT is money out; the API reports magnitudes with a separate indicator."""
    a = float((t.get("transaction_amount") or {}).get("amount") or 0)
    return -a if t.get("credit_debit_indicator") == "DBIT" else a


# Classification. Swedbank names the ACCOUNT HOLDER as counterparty on transfers and fees, so the
# usable description lives in remittance_information. These rules are deliberately few and literal —
# with 18 rows a rule you cannot read is worse than no rule. The one that matters: a transfer is not
# spending, and 2026-06 is 240 000 SEK of transfer against ~355 SEK of actual spend.
TRANSFER = ("överföring", "overforing", "egen överföring")
FEE = ("pris ", "debiteringsavg", "avgift")


def kind(t):
    v = amount(t)
    text = " ".join(str(x) for x in (t.get("remittance_information") or [])).lower()
    if any(w in text for w in TRANSFER):
        return "transfer"
    if v < 0 and any(w in text for w in FEE):
        return "fee"
    return "spend" if v < 0 else "income"


def counterparty(t):
    for side in ("creditor", "debtor"):
        n = ((t.get(side) or {}).get("name") or "").strip()
        if n and n.upper() != "MIKAEL ONSJÖ".upper():
            return n
    ri = t.get("remittance_information")
    if isinstance(ri, list) and ri:
        return str(ri[0])[:60]
    return (t.get("note") or "").strip()[:60] or "?"


def derive(accounts, store):
    """Aggregates for the gated /finance dashboard. SEK only, kept apart from the GBP dataset —
    mixing currencies in one total is the one thing this must never do."""
    months, by_account, merchants, recent = {}, {}, {}, []
    KINDS = ("spend", "fee", "transfer", "income")
    for acct in accounts:
        s = slug(acct)
        rows = sorted(store.get(s, {}).values(), key=lambda t: (t.get("booking_date") or ""))
        spend = sum(-amount(t) for t in rows if kind(t) in ("spend", "fee"))
        # Stock as well as flow. Every stored row carries the bank's own balance and until now none
        # of it reached the artifact: the view could say 16 656 SEK went out in August and could not
        # say that 1 409.91 was left. Cheap, and the one number that does not follow from the others.
        bal_date, bal = closing(rows)
        by_account[s] = {"product": acct.get("product"), "name": acct.get("details") or acct.get("product"),
                         "iban_last4": ((acct.get("account_id") or {}).get("iban") or "")[-4:],
                         "n": len(rows), "spend": round(spend, 2),
                         "balance": bal, "balance_date": bal_date}
        for t in rows:
            v, m, k = amount(t), (t.get("booking_date") or "")[:7], kind(t)
            if not m:
                continue
            e = months.setdefault(m, {"month": m, **{x: 0.0 for x in KINDS}})
            # A transfer between two of these accounts is TWO rows, one per side. Count the outgoing
            # leg only: summing both showed 2026-06 as 480 000 SEK moved when 240 000 moved once.
            if k != "transfer" or v < 0:
                e[k] += abs(v)
            if k in ("spend", "fee"):
                merchants[counterparty(t)] = merchants.get(counterparty(t), 0.0) + abs(v)
            recent.append({"date": t.get("booking_date"), "account": s, "amount": round(v, 2),
                           "kind": k, "counterparty": counterparty(t),
                           "reference": (t.get("reference_number") or "")[:40]})
    recent.sort(key=lambda r: (r["date"] or "", r["counterparty"]), reverse=True)
    ms = sorted(months.values(), key=lambda e: e["month"])
    for e in ms:
        for x in KINDS:
            e[x] = round(e[x], 2)
    return {
        "generated": int(time.time()),
        "currency": "SEK",
        "source": "Enable Banking / Swedbank (SWEDSESS)",
        "window_note": ("Swedbank serves a rolling 90-day window; rows older than that exist only "
                        "because this fetcher stored them. Nothing before 2026-05-17 was ever "
                        "retrievable — the consent predates the fetcher by seven weeks."),
        "consent_expires": "2026-12-23",
        "n_txns": sum(a["n"] for a in by_account.values()),
        "kinds_note": ("spend/fee/transfer/income are classified from remittance_information — "
                       "Swedbank names the account holder as counterparty on transfers, so the "
                       "text is the only signal. Transfers are EXCLUDED from spend and counted "
                       "on their OUTGOING leg only (an internal move is two rows): 2026-06 is "
                       "240 000 SEK of transfer against a few hundred of real spending."),
        "balance_note": ("by_account.balance is the bank's own balance_after_transaction on the "
                         "newest row the balance chain reaches — stock, not flow, and not a "
                         "reconstruction. It is the newest balance the ESTATE HOLDS: a store "
                         "truncated at its newest end still chains, so it can lag the real balance. "
                         "null means no rows, or a chain that did not close (the fetch log says so "
                         "at err level)."),
        "period": {"from": ms[0]["month"] if ms else None, "to": ms[-1]["month"] if ms else None},
        "months": ms,
        "by_account": by_account,
        "top_counterparties": [{"name": k, "spend": round(v, 2)} for k, v in
                               sorted(merchants.items(), key=lambda kv: -kv[1])[:25]],
        "recent": recent[:200],
    }


def main():
    argv = sys.argv[1:]
    dry, full = "--dry" in argv, "--full" in argv
    cfg = env()
    sess = json.loads((SEC / "enablebanking_session.json").read_text())
    tok = token(cfg)

    s = api(cfg, tok, "/sessions/" + sess["session_id"])
    if s.get("status") != "AUTHORIZED":
        errlog.err(f"swedbank: session {sess['session_id']} is {s.get('status')}, not AUTHORIZED — "
                   f"re-authorise with BankID; no transactions can be fetched")
        return 1
    valid = (s.get("access") or {}).get("valid_until", "")
    if valid:
        left = (datetime.fromisoformat(valid.replace("Z", "+00:00")).date() - date.today()).days
        if left < 21:
            errlog.warn(f"swedbank: consent expires in {left} days ({valid[:10]}) — re-auth needs "
                        f"BankID in a browser, and the lane goes silent the moment it lapses")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    accounts = sess.get("accounts") or []
    store, added_total, failed = {}, 0, []
    for acct in accounts:
        sl, uid = slug(acct), acct["uid"]
        path = OUTDIR / f"{sl}.jsonl"
        existing = load(path)
        since = None if full else newest(existing)
        date_from = ((datetime.fromisoformat(since).date() - timedelta(days=OVERLAP_DAYS)).isoformat()
                     if since else (date.today() - timedelta(days=400)).isoformat())
        try:
            rows, actual = fetch_account(cfg, tok, uid, date_from)
        except Exception as exc:
            errlog.err(f"swedbank: fetching {sl} ({uid}) failed — that account is NOT updated", exc)
            store[sl] = existing
            failed.append(sl)
            continue
        before = len(existing)
        for t in rows:
            existing[key(t)] = t
        added = len(existing) - before
        added_total += added
        store[sl] = existing
        print(f"{sl:<28} from {actual}  fetched {len(rows):>4}  new {added:>4}  total {len(existing):>5}")
        if not dry:
            ordered = sorted(existing.values(), key=lambda t: (t.get("booking_date") or "", key(t)))
            path.write_text("".join(json.dumps(t, ensure_ascii=False, sort_keys=True) + "\n"
                                    for t in ordered))

    # A run that fetched NOTHING must not look like a run that found nothing new. Before this, the
    # exit code, the systemd result, the artifact's `generated` stamp and the summary line were
    # identical in both cases (analyst cycle 103, 2026-08-15: 7 of 7 accounts 429'd, exit 0, artifact
    # restamped, no incident). The artifact is the load-bearing part — `generated` is a freshness
    # claim, and restamping it from stored copies asserts a currency the run did not establish.
    if failed and len(failed) == len(accounts):
        errlog.err(f"swedbank: ALL {len(accounts)} accounts failed — artifact left untouched at its "
                   f"previous stamp rather than restamped from stored copies")
        return 1
    # The store audits itself against the bank's own balances. This is the only signal in the lane
    # that distinguishes "this account had no transactions" from "we lost the ones it had": the 422
    # warn fires on dormancy too and so carries no information (see swedbank-422-warn-is-dormancy-
    # not-loss). Read-only, and never allowed to take the fetch down with it.
    for sl, rows in sorted(store.items()):
        if sl in failed:
            continue                             # stale copy; a break here would be last run's news
        try:
            ok, bad = ladder_ok(list(rows.values()))
        except Exception as exc:                 # noqa: BLE001 — an audit must not break the fetch
            errlog.warn(f"swedbank: balance-ladder audit of {sl} could not run", exc)
            continue
        if not ok:
            errlog.err(f"swedbank: {sl} does not reconcile against the bank's own running balance "
                       f"at booking_date {bad} — transactions are MISSING from the store before "
                       f"that date, and the bank's 90-day window means they may be unrecoverable")

    out = derive(accounts, store)
    # Partial failure still writes: the accounts that DID fetch are real and their rows would
    # otherwise be dropped on the floor. But the artifact says which ones are stale, so a consumer
    # is never handed a total that quietly excludes an account, and the run still exits non-zero so
    # systemd raises it.
    out["stale_accounts"] = sorted(failed)
    if not dry:
        DERIVED.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"swedbank: {out['n_txns']} transactions across {len(accounts)} accounts, "
          f"{out['period']['from']}..{out['period']['to']}, {added_total} new"
          + (f", {len(failed)} STALE: {', '.join(sorted(failed))}" if failed else "")
          + (" (dry)" if dry else f" -> {DERIVED}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
