# grafana — InfluxDB + Grafana spot-check stack (optional)

The standard Proxmox monitoring stack: Proxmox's built-in **Metric Server** *pushes* metrics
to **InfluxDB**, and **Grafana** draws live dashboards. High-resolution data with **short
retention** (weeks) — complements the long-term SQLite collector.

A nice property: this is **token-free** on the Proxmox side. Proxmox pushes (it doesn't pull),
so there's no API token to manage — you just point its Metric Server at the InfluxDB.

## Build
`create-lxc.sh` is an **example** that creates a Debian LXC on the Proxmox host and installs
InfluxDB 1.8 + Grafana. The IP defaults are RFC-5737 documentation addresses — set `CTID`,
`BRIDGE`, `IPCIDR`, `GATEWAY` for your network:
```bash
CTID=103 BRIDGE=vmbr0 IPCIDR=10.x.x.x/24 GATEWAY=10.x.x.1 \
SSH_PUBKEY_FILE=/root/.ssh/id_ed25519.pub ./create-lxc.sh
```
Then finish in the UIs (the script prints the steps):
1. **Proxmox → Datacenter → Metric Server → InfluxDB** pointing at the box.
2. **Grafana** (`:3000`, default `admin/admin` — change it): add the InfluxDB data source and
   import a community "Proxmox via InfluxDB" dashboard.

Retention defaults to `28d` (`INFLUX_RETENTION`) so it stays high-res but doesn't grow forever.

## Verify on the host
The LXC template version string and the InfluxDB/Grafana apt repos move occasionally; if the
`influx` CLI differs (InfluxDB 2.x), create a bucket with a retention period and point the
Metric Server at it with an org/token instead of a database.
