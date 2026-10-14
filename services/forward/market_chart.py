#!/usr/bin/env python3
"""market_chart.py: the weekly crash-risk chart, eight small panels, posted mid-week as #valet.

The operator wanted something like Exponential View's five AI-bubble gauges (boomorbubble.ai) but
updated more often than quarterly (2026-10-14). Those gauges lag by months and carry no market
prices at all, which is why they read 0/5 red while Oracle's CDS sat at a record. This chart is the
fast complement: free daily/weekly series from FRED and Yahoo, two years of history, each panel with
its danger line drawn in and a plain-word status, so no colour carries meaning alone.

  1 US high-yield spread (FRED BAMLH0A0HYM2)     widened months before 2000, 2007 and 2020
  2 St. Louis Fed Financial Stress Index         official weekly composite of 18 series
  3 Chicago Fed National Financial Conditions    weekly, credit/banking-weighted
  4 VIX / VIX3M                                  > 1 = short-term fear above medium-term: panic
  5 Semiconductor index drawdown from peak       the AI-specific equity gauge
  6 Nasdaq-100 vs its equal-weight version       a few giants carrying the index = fragile
  7 US 10y minus 2y Treasury yield               the classic recession signal
  8 Oracle vs Nvidia                             AI borrowers vs AI sellers (stand-in for CDS)

Thresholds are conventional readings, not forecasts; see the per-panel comments.

    market_chart.py          build and post
    market_chart.py --dry    build to ./market-chart.png and print the caption; post nothing
"""
import csv
import datetime as dt
import io
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog
import tg

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Reference palette (dataviz skill): one series hue, status steps reserved for the danger line/zone.
SERIES, CRIT, WARN = "#2a78d6", "#d03b3b", "#fab219"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
YEARS = 2


def _get(url, ua="Mozilla/5.0 (X11; Linux x86_64) moprox-market-chart", timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}),
                                timeout=timeout) as r:
        return r.read().decode()


def fred(sid):
    # FRED's CDN stalls on browser-like agents it cannot verify; a plain tool agent is served.
    rows = list(csv.reader(io.StringIO(_get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + sid, ua="curl/8.14.1"))))[1:]
    start = dt.date.today() - dt.timedelta(days=365 * YEARS)
    out = [(dt.date.fromisoformat(d), float(v)) for d, v in rows if v not in ("", ".")]
    return [(d, v) for d, v in out if d >= start]


def yahoo(sym):
    r = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%dy&interval=1d"
                        % (urllib.parse.quote(sym), YEARS)))["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    return {dt.date.fromtimestamp(t): c for t, c in zip(r["timestamp"], closes) if c is not None}


def ratio(a, b, index=True):
    """a/b on common dates; indexed to 100 at the start when `index`."""
    xa, xb = yahoo(a), yahoo(b)
    days = sorted(set(xa) & set(xb))
    s = [(d, xa[d] / xb[d]) for d in days]
    return [(d, v / s[0][1] * 100) for d, v in s] if index else s


def drawdown(sym):
    x = sorted(yahoo(sym).items())
    peak, out = 0, []
    for d, v in x:
        peak = max(peak, v)
        out.append((d, (v / peak - 1) * 100))
    return out


# (title, loader, unit format, watch, danger, direction, note)
# direction "up": danger when value >= danger; "down": danger when value <= danger; None: no status.
PANELS = [
    ("US junk-bond spread", lambda: fred("BAMLH0A0HYM2"), "{:.2f}%", 4.0, 5.0, "up",
     "extra yield on risky US corporate debt"),
    ("St. Louis Fed stress index", lambda: fred("STLFSI4"), "{:+.2f}", 0.0, 1.0, "up",
     "0 = average stress"),
    ("Chicago Fed financial conditions", lambda: fred("NFCI"), "{:+.2f}", -0.2, 0.0, "up",
     "above 0 = tighter than average"),
    ("VIX / 3-month VIX", lambda: ratio("^VIX", "^VIX3M", index=False), "{:.2f}", 0.95, 1.0, "up",
     "above 1 = acute panic"),
    ("Chip stocks: fall from peak", lambda: drawdown("^SOX"), "{:.0f}%", -10.0, -20.0, "down",
     "Philadelphia semiconductor index; -20% = bear market"),
    ("Nasdaq-100 vs equal-weight", lambda: ratio("QQQ", "QQEW"), "{:.0f}", None, None, None,
     "rising = a few giants carry the index"),
    ("US 10y minus 2y yield", lambda: fred("T10Y2Y"), "{:+.2f}", 0.25, 0.0, "down",
     "below 0 = inverted curve, recession signal"),
    ("Oracle vs Nvidia", lambda: ratio("ORCL", "NVDA"), "{:.0f}", None, None, None,
     "AI borrower vs AI seller, indexed to 100"),
]


def status(v, watch, danger, direction):
    if direction is None:
        return None
    hit = (lambda t: v >= t) if direction == "up" else (lambda t: v <= t)
    return "DANGER" if hit(danger) else "WATCH" if hit(watch) else "OK"


def week_change(series):
    last_d, last_v = series[-1]
    prior = [v for d, v in series if d <= last_d - dt.timedelta(days=7)]
    return last_v - prior[-1] if prior else None


def build():
    fig, axes = plt.subplots(4, 2, figsize=(10, 12.5), dpi=130, facecolor=SURF)
    caption = []
    for ax, (title, load, fmt, watch, danger, direction, note) in zip(axes.flat, PANELS):
        ax.set_facecolor(SURF)
        try:
            s = load()
        except Exception as exc:
            errlog.err(f"market-chart: could not load {title}", exc)
            ax.text(0.5, 0.5, "data unavailable", ha="center", va="center", color=INK2,
                    transform=ax.transAxes)
            ax.set_title(title, loc="left", fontsize=10, color=INK, fontweight="bold")
            caption.append(f"• {title}: data unavailable")
            continue
        d, v = zip(*s)
        ax.plot(d, v, color=SERIES, lw=1.6)
        if danger is not None:
            ax.axhline(danger, color=CRIT, lw=1.2, ls="--")
            lo, hi = ax.get_ylim()
            if direction == "up":
                ax.axhspan(danger, max(hi, danger), color=CRIT, alpha=0.06, lw=0)
                ax.set_ylim(lo, max(hi, danger + (hi - lo) * 0.05))
            else:
                ax.axhspan(min(lo, danger), danger, color=CRIT, alpha=0.06, lw=0)
                ax.set_ylim(min(lo, danger - (hi - lo) * 0.05), hi)
            ax.text(d[0], danger, " danger", color=CRIT, fontsize=7.5, va="bottom")
        st = status(v[-1], watch, danger, direction)
        wc = week_change(s)
        val = fmt.format(v[-1])
        head = f"{title}   {val}" + (f"   {st}" if st else "")
        ax.text(0, 1.10, head, transform=ax.transAxes, fontsize=10, color=INK, fontweight="bold",
                va="bottom")
        ax.text(0, 1.02, note, transform=ax.transAxes, fontsize=7.5, color=INK2, va="bottom")
        ax.plot([d[-1]], [v[-1]], "o", ms=5, color=SERIES, mec=SURF, mew=1.5)
        ax.grid(axis="y", color=GRID, lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=7.5)
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
        icon = {"OK": "🟢", "WATCH": "🟡", "DANGER": "🔴", None: "▫️"}[st]
        wtxt = "" if wc is None else (", {:+.%df} on the week" % (2 if ".2f" in fmt else 1)).format(wc)
        caption.append(f"{icon} {title}: {val}{(' ' + st) if st else ''}{wtxt}")
    fig.suptitle(f"Crash-risk gauges, week of {dt.date.today():%d %b %Y}", x=0.02, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    fig.text(0.02, 0.005, "Sources: FRED (ICE BofA, St. Louis Fed, Chicago Fed, US Treasury), Yahoo Finance. "
             "Dashed red = conventional danger line.", fontsize=7, color=INK2)
    fig.tight_layout(rect=(0, 0.015, 1, 0.97), h_pad=3.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURF)
    plt.close(fig)
    n = sum(1 for c in caption if "DANGER" in c)
    head = ("*Weekly crash-risk gauges*: " +
            ("all clear" if n == 0 else f"{n} in the danger zone"))
    return buf.getvalue(), head + "\n" + "\n".join(caption)


def main():
    png, caption = build()
    if "--dry" in sys.argv[1:]:
        Path("market-chart.png").write_bytes(png)
        print(caption)
        return 0
    r = tg.send_photo(png, caption, agent="valet")
    if not r.get("ok"):
        errlog.err(f"market-chart: telegram post failed: {r.get('description')}")
        return 1
    print("market-chart: posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
