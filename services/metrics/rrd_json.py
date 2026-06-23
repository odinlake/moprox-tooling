#!/usr/bin/env python3
"""Build public-safe system JSON for the dashboard straight from the Proxmox RRD (no collector,
no store — moprox already keeps ~1y of RRD). Pure stdlib; forces a direct (no-proxy) connection.

Writes:
  <OUT>/system/day.json    node series, 60s buckets, last ~24h   (run every 5 min)
  <OUT>/system/month.json  node series, 30m buckets, last ~30d   (run daily)
  <OUT>/system/now.json    current node + guests + storage snapshot

Env: PVE_API_URL, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET (from ~/.config/proxmox/pve-metrics.env);
     OUT (default ./dist/data)
"""
import json, os, ssl, sys, time, urllib.request

API = os.environ["PVE_API_URL"].rstrip("/"); NODE = os.environ["PVE_NODE"]
TID, TSEC = os.environ["PVE_TOKEN_ID"], os.environ["PVE_TOKEN_SECRET"]
OUT = os.environ.get("OUT", "dist/data")
_CTX = ssl.create_default_context(); _CTX.check_hostname=False; _CTX.verify_mode=ssl.CERT_NONE
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))

def api(path):
    r = urllib.request.Request(f"{API}/api2/json{path}", headers={"Authorization": f"PVEAPIToken={TID}={TSEC}"})
    with _OPENER.open(r, timeout=20) as resp: return json.load(resp)["data"]

def rrd(timeframe):
    rows = api(f"/nodes/{NODE}/rrddata?timeframe={timeframe}&cf=AVERAGE")
    def col(k, scale=1.0): return [round(r[k]*scale, 3) if r.get(k) is not None else None for r in rows]
    return {
        "step_s": (rows[1]["time"]-rows[0]["time"]) if len(rows) > 1 else 60,
        "t":        [r["time"] for r in rows],
        "cpu_pct":  col("cpu", 100.0),
        "mem_used": col("memused"),
        "mem_total":col("memtotal"),
        "load":     col("loadavg"),
        "iowait_pct": col("iowait", 100.0),
        "netin":    col("netin"),
        "netout":   col("netout"),
    }

def snapshot():
    s = api(f"/nodes/{NODE}/status")
    guests = []
    for kind in ("qemu", "lxc"):
        for g in api(f"/nodes/{NODE}/{kind}"):
            guests.append({"vmid": g["vmid"], "name": g.get("name"), "kind": kind,
                           "status": g.get("status"), "cpu_pct": round((g.get("cpu") or 0)*100,1),
                           "mem": g.get("mem"), "maxmem": g.get("maxmem")})
    storage = [{"storage": st["storage"], "type": st.get("type"), "used": st.get("used"), "total": st.get("total")}
               for st in api(f"/nodes/{NODE}/storage") if st.get("total")]
    return {
        "node": NODE, "pve_version": api("/version").get("version"),
        "cpu_pct": round(s["cpu"]*100,2), "cpus": s["cpuinfo"]["cpus"], "model": s["cpuinfo"].get("model"),
        "mem_used": s["memory"]["used"], "mem_total": s["memory"]["total"],
        "load": s.get("loadavg"), "uptime_s": s.get("uptime"),
        "guests": sorted(guests, key=lambda g: g["vmid"]), "storage": storage,
    }

def write(name, obj):
    p = os.path.join(OUT, "system", name); os.makedirs(os.path.dirname(p), exist_ok=True)
    obj["generated"] = int(time.time())
    json.dump(obj, open(p, "w"), separators=(",", ":")); print("wrote", p)

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "day"):   write("day.json", rrd("day"))
    if what in ("all", "month"): write("month.json", rrd("month"))
    if what in ("all", "now"):   write("now.json", snapshot())
