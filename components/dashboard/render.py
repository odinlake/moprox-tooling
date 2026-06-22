#!/usr/bin/env python3
"""Render long-term charts from the SQLite metrics store into a static site (Option A).

Uses matplotlib (Debian package: python3-matplotlib). Output is plain PNG + index.html, ready
to publish (e.g. to GitHub Pages).

Chart conventions: metric units, horizontal axis labels only (no rotated/angled text),
readable defaults. Keep it simple; add panels as you find them useful.

Config:
  METRICS_DB     SQLite path (default ~/.local/share/metrics/metrics.db)
  METRICS_SITE   output dir   (default ~/metrics-site/site)
  METRICS_DAYS   window in days (default 90)
  METRICS_TITLE  page title   (default "Proxmox metrics")
"""
import os
import sqlite3
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

DB = Path(os.environ.get("METRICS_DB", Path.home() / ".local/share/metrics/metrics.db"))
SITE = Path(os.environ.get("METRICS_SITE", Path.home() / "projects/metrics-site/site"))
DAYS = int(os.environ.get("METRICS_DAYS", "90"))
GIB = 1024 ** 3


def _fmt_time_axis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    for label in ax.get_xticklabels():
        label.set_rotation(0)          # horizontal only (convention)
        label.set_horizontalalignment("center")
    ax.grid(True, alpha=0.3)


def _save(fig, name):
    SITE.mkdir(parents=True, exist_ok=True)
    path = SITE / name
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return name


def chart_storage(db, since):
    rows = db.execute(
        "SELECT ts, storage, used, total FROM storage_metrics WHERE ts >= ? ORDER BY ts",
        (since,)).fetchall()
    if not rows:
        return None
    series = {}
    for ts, storage, used, total in rows:
        if used is None:
            continue
        series.setdefault(storage, ([], []))
        series[storage][0].append(mdates.epoch2num(ts))
        series[storage][1].append(used / GIB)
    fig, ax = plt.subplots(figsize=(10, 4))
    for storage, (xs, ys) in sorted(series.items()):
        ax.plot(xs, ys, label=storage)
    ax.xaxis_date()
    _fmt_time_axis(ax)
    ax.set_ylabel("used (GiB)")
    ax.set_title("Storage used over time")
    ax.legend(loc="upper left", fontsize=8)
    return _save(fig, "storage.png")


def chart_guest_mem(db, since):
    rows = db.execute(
        "SELECT ts, name, mem FROM guest_metrics WHERE ts >= ? AND status='running' ORDER BY ts",
        (since,)).fetchall()
    if not rows:
        return None
    series = {}
    for ts, name, mem in rows:
        if mem is None:
            continue
        series.setdefault(name or "?", ([], []))
        series[name][0].append(mdates.epoch2num(ts))
        series[name][1].append(mem / GIB)
    fig, ax = plt.subplots(figsize=(10, 4))
    for name, (xs, ys) in sorted(series.items()):
        ax.plot(xs, ys, label=name)
    ax.xaxis_date()
    _fmt_time_axis(ax)
    ax.set_ylabel("memory used (GiB)")
    ax.set_title("Guest memory use over time")
    ax.legend(loc="upper left", fontsize=8)
    return _save(fig, "guest_mem.png")


def backup_summary(db):
    """HTML: what backups are held + the space they take. Logical size comes from the latest
    backup_inventory snapshot; physical/dedup from the backup storage's latest storage_metrics."""
    row = db.execute("SELECT MAX(ts) FROM backup_inventory").fetchone()
    if not row or not row[0]:
        return ""
    latest = row[0]
    by_store = db.execute(
        "SELECT storage, COUNT(*), COALESCE(SUM(size),0), COUNT(DISTINCT vmid) "
        "FROM backup_inventory WHERE ts=? GROUP BY storage", (latest,)).fetchall()
    out = ["<h2>Backups held</h2><table border=1 cellpadding=4 cellspacing=0>",
           "<tr><th>store</th><th>backups</th><th>VMs</th><th>logical</th>"
           "<th>physical (used)</th><th>dedup</th></tr>"]
    for store, n, logical, vms in by_store:
        # physical used for this store from the most recent storage_metrics row
        u = db.execute("SELECT used FROM storage_metrics WHERE storage=? AND used IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", (store,)).fetchone()
        used = u[0] if u else None
        dedup = f"{logical/used:.1f}×" if used else "—"
        phys = f"{used/GIB:.1f} GiB" if used else "—"
        out.append(f"<tr><td>{store}</td><td>{n}</td><td>{vms}</td>"
                   f"<td>{logical/GIB:.1f} GiB</td><td>{phys}</td><td>{dedup}</td></tr>")
    out.append("</table>")
    out.append(f"<p style='color:#666'>Inventory as of {time.strftime('%Y-%m-%d %H:%M', time.localtime(latest))}; "
               "logical = pre-dedup sum, physical = datastore used, dedup ≈ logical/physical.</p>")
    return "\n".join(out)


def main():
    if not DB.exists():
        raise SystemExit(f"no metrics db at {DB} yet — let the collector run first")
    db = sqlite3.connect(DB)
    since = int(time.time()) - DAYS * 86400
    charts = [c for c in (chart_storage(db, since), chart_guest_mem(db, since)) if c]
    backups_html = backup_summary(db)
    db.close()

    title = os.environ.get("METRICS_TITLE", "Proxmox metrics")
    html = ["<!doctype html><meta charset=utf-8>",
            f"<title>{title}</title>",
            f"<h1>{title} — long-term</h1>",
            f"<p>Window: last {DAYS} days. Generated {time.strftime('%Y-%m-%d %H:%M %Z')}.</p>"]
    for c in charts:
        html.append(f'<p><img src="{c}" alt="{c}" style="max-width:100%"></p>')
    if backups_html:
        html.append(backups_html)
    if not charts and not backups_html:
        html.append("<p>No data yet.</p>")
    (SITE / "index.html").write_text("\n".join(html))
    print(f"wrote {len(charts)} chart(s) to {SITE}")


if __name__ == "__main__":
    main()
