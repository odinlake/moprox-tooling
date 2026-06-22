#!/usr/bin/env python3
"""Poll the Proxmox VE API and append metrics to a local SQLite store (Option A, long-term).

Dependency-free (Python stdlib only): no pip, no extra packages. The collector forces a
direct, no-proxy connection to the Proxmox API (the API is normally reachable directly, so
any http(s)_proxy in the environment is bypassed).

Config comes from the environment (see metrics.env.example):
  PVE_API_URL     e.g. https://pve.example:8006
  PVE_NODE        e.g. pve
  PVE_TOKEN_ID    e.g. metrics@pve!collector
  PVE_TOKEN_SECRET  the API token UUID (read-only PVEAuditor role)
  METRICS_DB      path to the SQLite file (default ~/.local/share/metrics/metrics.db)

Run once per invocation; a systemd timer calls it on a cadence (every 5 min by default).
"""
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
from pathlib import Path

API = os.environ["PVE_API_URL"].rstrip("/")
NODE = os.environ["PVE_NODE"]
TOKEN_ID = os.environ["PVE_TOKEN_ID"]
TOKEN_SECRET = os.environ["PVE_TOKEN_SECRET"]
DB = Path(os.environ.get("METRICS_DB", Path.home() / ".local/share/metrics/metrics.db"))
SCHEMA = Path(__file__).with_name("schema.sql")

# PVE uses a self-signed cert; don't verify (homelab). Pin the PVE CA later if desired.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
# Force a direct connection (bypass any http(s)_proxy set in the environment).
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_CTX),
)


def api(path):
    req = urllib.request.Request(f"{API}/api2/json{path}")
    req.add_header("Authorization", f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}")
    with _OPENER.open(req, timeout=15) as r:
        return json.load(r)["data"]


def collect_core(db, ts):
    # --- node ---
    s = api(f"/nodes/{NODE}/status")
    db.execute(
        "INSERT INTO node_metrics(ts,node,cpu,mem_used,mem_total,swap_used,swap_total,"
        "rootfs_used,rootfs_total,loadavg1,uptime) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, NODE, s.get("cpu"),
         s.get("memory", {}).get("used"), s.get("memory", {}).get("total"),
         s.get("swap", {}).get("used"), s.get("swap", {}).get("total"),
         s.get("rootfs", {}).get("used"), s.get("rootfs", {}).get("total"),
         (s.get("loadavg") or [None])[0], s.get("uptime")),
    )

    # --- storage (sizes) ---
    for st in api(f"/nodes/{NODE}/storage"):
        db.execute(
            "INSERT INTO storage_metrics(ts,node,storage,type,total,used,avail) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, NODE, st.get("storage"), st.get("type"),
             st.get("total"), st.get("used"), st.get("avail")),
        )

    # --- guests (VMs + containers) ---
    for kind in ("qemu", "lxc"):
        for g in api(f"/nodes/{NODE}/{kind}"):
            vmid = g["vmid"]
            try:
                c = api(f"/nodes/{NODE}/{kind}/{vmid}/status/current")
            except Exception as e:  # a guest may vanish between list and query
                print(f"skip {kind} {vmid}: {e}", file=sys.stderr)
                continue
            db.execute(
                "INSERT INTO guest_metrics(ts,node,vmid,name,kind,status,cpu,mem,maxmem,"
                "disk,maxdisk,netin,netout,uptime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, NODE, vmid, c.get("name"), kind, c.get("status"), c.get("cpu"),
                 c.get("mem"), c.get("maxmem"), c.get("disk"), c.get("maxdisk"),
                 c.get("netin"), c.get("netout"), c.get("uptime")),
            )


def collect_backups(db, ts):
    """Snapshot the backup inventory (what's held + logical sizes) from backup-capable storages.
    Run on a SLOW cadence (daily) — backups change daily, and listing them every 5 min would
    bloat the table. Datastore physical/dedup space comes from storage_metrics."""
    stores = [s for s in api(f"/nodes/{NODE}/storage") if "backup" in (s.get("content") or "")]
    for s in stores:
        store = s["storage"]
        try:
            items = api(f"/nodes/{NODE}/storage/{store}/content?content=backup")
        except Exception as e:
            print(f"skip storage {store}: {e}", file=sys.stderr)
            continue
        for b in items:
            v = b.get("verification")
            verified = v.get("state") if isinstance(v, dict) else (v if isinstance(v, str) else None)
            db.execute(
                "INSERT INTO backup_inventory(ts,node,storage,vmid,volid,ctime,size,format,"
                "verified,protected) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, NODE, store, b.get("vmid"), b.get("volid"), b.get("ctime"),
                 b.get("size"), b.get("format"), verified, 1 if b.get("protected") else 0),
            )


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA.read_text())
    ts = int(time.time())
    if len(sys.argv) > 1 and sys.argv[1] == "backups":
        collect_backups(db, ts)   # daily inventory snapshot
    else:
        collect_core(db, ts)      # node + storage + guests (5-min cadence)
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
