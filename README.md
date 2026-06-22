# moprox-tooling

Self-contained tooling that runs *on top of* a Proxmox estate — collectors, dashboards, and
small apps that consume read-only data and present it. Generic and reusable: it takes all of
its site-specific settings (API endpoint, token, paths) from the environment, so nothing here
hardcodes a particular network.

> Companion to a private platform/IaC repo that provisions the Proxmox host and VMs and hands
> this tooling the read-only API token it needs. This repo is just the applications.

## Layout
```
services/
  metrics/         # pull Proxmox metrics into a local SQLite store (stdlib only, no deps)
    collect.py       # poll the PVE API → SQLite (node/storage/guest; `backups` mode = inventory)
    schema.sql       # the SQLite schema
    metrics.env.example
    systemd/         # collect (5-min) + backups (daily) timers
components/
  dashboard/       # turn the store into something you can look at
    render.py        # SQLite → static charts + index.html (matplotlib) → publish anywhere
    systemd/         # render (hourly) timer
    grafana/         # optional: an InfluxDB + Grafana box for high-res spot-checking
```

## Quick start (long-term metrics → static dashboard)
1. On the Proxmox host, mint a **read-only** API token (`PVEAuditor`) — see
   `services/metrics/metrics.env.example` for the `pveum` commands.
2. On the box that will run the collector:
   ```bash
   cp services/metrics/metrics.env.example ~/.config/metrics.env   # fill in PVE_API_URL/PVE_TOKEN_*
   sudo apt-get install -y python3-matplotlib                       # for the dashboard renderer
   # try it once:
   env $(grep -v '^#' ~/.config/metrics.env | xargs) python3 services/metrics/collect.py
   env $(grep -v '^#' ~/.config/metrics.env | xargs) python3 components/dashboard/render.py
   ```
3. Install the systemd units (edit `User=` and the checkout path in each first), then
   `systemctl enable --now metrics-collect.timer metrics-backups.timer metrics-render.timer`.

## Design notes
- **Read-only.** Everything here uses a `PVEAuditor` token — it can view, never change.
- **Dependency-light.** The collector is pure Python stdlib (`urllib` + `sqlite3`); only the
  optional renderer needs matplotlib.
- **Two retention horizons.** The SQLite collector keeps long-term history forever; the
  optional Grafana/InfluxDB box (`components/dashboard/grafana/`) keeps high-res data for a
  short window for spot-checking.
- **Backups.** `collect.py backups` snapshots the backup inventory (what's held + logical
  sizes) from the PVE storage API; `render.py` shows count, space, and a derived dedup ratio.

Example IPs in scripts use the RFC-5737 documentation ranges (`192.0.2.0/24`); set your own.
