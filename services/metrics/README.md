# services/metrics — Proxmox → SQLite collector

Polls the Proxmox VE API and appends time series to a local SQLite store. Pure Python stdlib
(`urllib` + `sqlite3`) — no pip, no dependencies. Read-only: it authenticates with a
`PVEAuditor` token and only ever reads.

## Files
- `collect.py` — poll the API → SQLite.
  - default mode: node (CPU/mem/swap/load), storage (sizes), guests (VMs + containers).
  - `collect.py backups`: snapshot the **backup inventory** (what's held, per VM, logical
    sizes, verify state) from the storage API. Run on a slow cadence (daily).
- `schema.sql` — the tables (`node_metrics`, `storage_metrics`, `guest_metrics`,
  `backup_inventory`). Applied automatically on each run.
- `metrics.env.example` — copy, fill in, keep private (it holds the API token).
- `systemd/` — `metrics-collect.timer` (5-min) and `metrics-backups.timer` (daily).

## Config (environment)
| var | meaning |
|---|---|
| `PVE_API_URL` | e.g. `https://pve.example:8006` |
| `PVE_NODE` | node name (`pvesh get /nodes`) |
| `PVE_TOKEN_ID` | e.g. `metrics@pve!collector` |
| `PVE_TOKEN_SECRET` | the token UUID (read-only) |
| `METRICS_DB` | SQLite path (default `~/.local/share/metrics/metrics.db`) |

Mint the token (read-only) on the Proxmox host:
```bash
pveum user add metrics@pve
pveum acl modify / -user metrics@pve -role PVEAuditor
pveum user token add metrics@pve collector --privsep 0   # prints the secret once
```

> TLS: the collector does not verify the Proxmox cert by default (self-signed is common in
> homelabs). Pin your CA in `collect.py` if you prefer strict verification.
