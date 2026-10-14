#!/usr/bin/env python3
"""market_alert.py: tell the operator AT ONCE when markets are in upheaval, and otherwise say nothing.

The operator (2026-10-14): the valet carries no market news any more unless it truly matters, but
"if there is any kind of financial upheaval obviously tell me asap". A once-a-day brief cannot do
"asap", so this runs every 20 minutes and posts as #valet only when a hard threshold trips:

  - a major equity index down DROP_PCT% or more on the day (S&P 500, Nasdaq 100, FTSE 100,
    Euro Stoxx 50, Nikkei 225)
  - VIX at VIX_LEVEL or above, or up VIX_JUMP_PCT% on the day
  - US 10-year Treasury yield moving YIELD_BP basis points in a day, either way
  - sterling moving FX_PCT% against the dollar in a day
  - US high-yield credit spread (ICE BofA, FRED BAMLH0A0HYM2) at HY_LEVEL% or above, or up
    HY_JUMP_BP basis points in five trading days: the widening that precedes crashes

Each signal posts once per market day per severity tier, so a bad day is one message, and a bad day
that gets worse is two. Numbers are mechanical by design: no agent call, so it is fast and cannot
talk itself into or out of an alert.

    market_alert.py          check and post if anything trips
    market_alert.py --dry    print the readings and what would post; post nothing
"""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog
import tg

STATE = Path(os.environ.get("MARKET_ALERT_STATE", Path.home() / ".local/state/market-alert.json"))
DROP_PCT = float(os.environ.get("MARKET_DROP_PCT", "3"))        # tier 2 at twice this
VIX_LEVEL = float(os.environ.get("MARKET_VIX_LEVEL", "30"))      # tier 2 at 40
VIX_JUMP_PCT = float(os.environ.get("MARKET_VIX_JUMP_PCT", "40"))
YIELD_BP = float(os.environ.get("MARKET_YIELD_BP", "20"))
FX_PCT = float(os.environ.get("MARKET_FX_PCT", "2"))
HY_LEVEL = float(os.environ.get("MARKET_HY_LEVEL", "5.0"))       # % OAS; tier 2 at 7
HY_JUMP_BP = float(os.environ.get("MARKET_HY_JUMP_BP", "75"))

INDICES = {"^GSPC": "S&P 500", "^NDX": "Nasdaq 100", "^FTSE": "FTSE 100",
           "^STOXX50E": "Euro Stoxx 50", "^N225": "Nikkei 225"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) moprox-market-alert"}


def _get(url, timeout=20, headers=UA):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read().decode()


def quote(sym):
    """(day change %, last price, market date) from Yahoo's chart API, or None if unreadable or
    older than a day (a weekend reading is not news)."""
    try:
        meta = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
                               % urllib.parse.quote(sym)))["chart"]["result"][0]["meta"]
        px, prev, t = float(meta["regularMarketPrice"]), float(meta["chartPreviousClose"]), int(meta["regularMarketTime"])
    except Exception as exc:
        errlog.warn(f"market-alert: no quote for {sym} ({exc})")
        return None
    if time.time() - t > 20 * 3600 or not prev:
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime(t + int(meta.get("gmtoffset") or 0)))
    return (px / prev - 1) * 100, px, prev, day


def hy_spread():
    """(latest %, 5-trading-days-earlier %, date) of the US high-yield OAS, from FRED's keyless CSV."""
    try:
        rows = [r for r in csv.reader(io.StringIO(_get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2", timeout=60,
            # FRED's CDN stalls on browser-like agents it cannot verify; a plain tool agent is served.
            headers={"User-Agent": "curl/8.14.1"})))][1:]
        rows = [(d, float(v)) for d, v in rows if v not in ("", ".")]
        return rows[-1][1], rows[-6][1], rows[-1][0]
    except Exception as exc:
        errlog.warn(f"market-alert: no HY spread ({exc})")
        return None


def headlines(n=3):
    """The top business headlines, so the alert says WHY as well as how much."""
    try:
        root = ET.fromstring(_get("https://news.google.com/rss/headlines/section/topic/BUSINESS"
                                  "?hl=en-GB&gl=GB&ceid=GB:en"))
        return [(it.findtext("title") or "", it.findtext("link") or "") for it in root.iter("item")][:n]
    except Exception:
        return []


def checks(state):
    """Every tripped signal as (key, tier, line)."""
    out = []
    for sym, name in INDICES.items():
        q = quote(sym)
        if q and q[0] <= -DROP_PCT:
            tier = 2 if q[0] <= -2 * DROP_PCT else 1
            out.append((f"{sym}:{q[3]}", tier, f"{name} {q[0]:+.1f}% on the day ({q[1]:,.0f})"))
    q = quote("^VIX")
    if q and (q[1] >= VIX_LEVEL or q[0] >= VIX_JUMP_PCT):
        tier = 2 if q[1] >= VIX_LEVEL + 10 else 1
        out.append((f"VIX:{q[3]}", tier, f"VIX (fear gauge) {q[1]:.1f}, {q[0]:+.0f}% on the day"))
    q = quote("^TNX")
    if q and abs(q[1] - q[2]) * 100 >= YIELD_BP:
        bp = (q[1] - q[2]) * 100
        tier = 2 if abs(bp) >= 2 * YIELD_BP else 1
        out.append((f"TNX:{q[3]}", tier, f"US 10-year yield {q[1]:.2f}%, {bp:+.0f} bp on the day"))
    q = quote("GBPUSD=X")
    if q and abs(q[0]) >= FX_PCT:
        tier = 2 if abs(q[0]) >= 2 * FX_PCT else 1
        out.append((f"GBP:{q[3]}", tier, f"Sterling {q[1]:.4f} $, {q[0]:+.1f}% on the day"))
    # FRED publishes once a day and is slow to answer; every 20 min would be pure load.
    h = None
    if time.time() - float(state.get("_hy_checked", 0)) >= 6 * 3600:
        h = hy_spread()
        if h:
            state["_hy_checked"] = time.time()
    if h and (h[0] >= HY_LEVEL or (h[0] - h[1]) * 100 >= HY_JUMP_BP):
        tier = 2 if h[0] >= HY_LEVEL + 2 else 1
        out.append((f"HY:{h[2]}", tier, f"US junk-bond spread {h[0]:.2f}%, "
                                        f"{(h[0] - h[1]) * 100:+.0f} bp in a week ({h[2]})"))
    return out


def main():
    dry = "--dry" in sys.argv[1:]
    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    tripped = checks(state)
    fresh = [(k, t, ln) for k, t, ln in tripped if state.get(k, 0) < t]
    if dry:
        print("tripped:", tripped or "nothing")
    if not fresh:
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        print(f"market-alert: quiet ({len(tripped)} already reported)")
        return 0
    lines = ["⚠️ *Market upheaval*"] + [f"• {ln}" for _, _, ln in fresh]
    news = headlines()
    if news:
        lines.append("Headlines: " + "; ".join(f"[{t}]({u})" for t, u in news))
    text = "\n".join(lines)
    if dry:
        print(text)
        return 0
    try:
        if not tg.send(text, agent="valet"):
            return 1
    except Exception as exc:
        errlog.err("market-alert: telegram post failed, the alert reached nobody", exc)
        return 1
    state.update({k: t for k, t, _ in fresh})
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 14 * 86400))
    state = {k: v for k, v in state.items() if k.startswith("_") or k.split(":", 1)[-1] >= cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(f"market-alert: posted {len(fresh)} signal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
