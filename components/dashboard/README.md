# components/dashboard

Turn the metrics store into something you can look at. Two options, by retention horizon:

| | Static charts (`render.py`) | Grafana (`grafana/`) |
|---|---|---|
| Source | the SQLite store (`../../services/metrics/`) | Proxmox **pushes** to InfluxDB |
| Store | long-term, forever | high-res, short window (~weeks) |
| Output | PNG + `index.html`, publish anywhere | live Grafana dashboards |
| Deps | `python3-matplotlib` | an InfluxDB + Grafana box |

## Static charts — `render.py`
Reads the SQLite store and writes PNG charts + `index.html` into `METRICS_SITE`, ready to
publish (e.g. push to a GitHub Pages repo). Includes a **"Backups held"** table: count, VMs,
logical total, physical (datastore used), and a derived **dedup ratio** (logical ÷ physical).

```bash
sudo apt-get install -y python3-matplotlib
METRICS_DB=~/.local/share/metrics/metrics.db METRICS_SITE=~/metrics-site/site \
  python3 render.py
```
Env: `METRICS_DB`, `METRICS_SITE`, `METRICS_DAYS` (window), `METRICS_TITLE`. Hourly timer in
`systemd/`. To auto-publish, point `METRICS_SITE` at a checked-out Pages repo and add a
commit/push step (see the commented lines in `systemd/metrics-render.service`).

## Grafana / InfluxDB — `grafana/`
Optional high-res spot-check stack. See [`grafana/README.md`](./grafana/README.md).
