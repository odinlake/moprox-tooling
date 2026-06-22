#!/usr/bin/env bash
# Option B: InfluxDB + Grafana box for fine-grained spot-checking. Run ON the Proxmox host as
# root. Creates a Debian 13 LXC on a LAN bridge so you can reach the Grafana UI from your
# devices. EXAMPLE values below use RFC-5737 documentation IPs (192.0.2.0/24) — set CTID,
# BRIDGE, IPCIDR, GATEWAY for your own network.
#
# Data split: this store keeps HIGH-RESOLUTION metrics for a short window (weeks) for
# spot-checking; the long-term aggregated history lives in the SQLite collector
# (../../services/metrics/). See ../README.md.
set -Eeuo pipefail
trap 'rc=$?; printf "\n\033[1;31m✗ %s: failed at line %s (exit %s): %s\033[0m\n" "${0##*/}" "$LINENO" "$rc" "$BASH_COMMAND" >&2' ERR

CTID="${CTID:-103}"
HOSTNAME_="${HOSTNAME_:-metrics}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
DISK_GB="${DISK_GB:-8}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-2048}"
BRIDGE="${BRIDGE:-vmbr0}"                 # LAN
IPCIDR="${IPCIDR:-192.0.2.21/24}"
GATEWAY="${GATEWAY:-192.0.2.1}"
INFLUX_DB="${INFLUX_DB:-proxmox}"
INFLUX_RETENTION="${INFLUX_RETENTION:-28d}"   # fine-grained, NOT forever
SSH_PUBKEY_FILE="${SSH_PUBKEY_FILE:-/root/.ssh/id_ed25519.pub}"

[[ -f "${SSH_PUBKEY_FILE}" ]] || { echo "ERROR: ${SSH_PUBKEY_FILE} not found" >&2; exit 1; }
pct status "${CTID}" &>/dev/null && { echo "ERROR: CT ${CTID} exists; pct destroy ${CTID} first" >&2; exit 1; }

echo "==> Creating monitoring LXC ${CTID} (${HOSTNAME_}) on ${BRIDGE} at ${IPCIDR}"
pct create "${CTID}" "${TEMPLATE}" \
  --hostname "${HOSTNAME_}" --cores "${CORES}" --memory "${MEMORY}" --swap 256 \
  --rootfs "${ROOTFS_STORAGE}:${DISK_GB}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=${IPCIDR},gw=${GATEWAY}" \
  --onboot 1 --unprivileged 1 --features nesting=1 \
  --ssh-public-keys "${SSH_PUBKEY_FILE}"
pct start "${CTID}"
for _ in $(seq 1 30); do pct exec "${CTID}" -- test -e /etc/resolv.conf && break; sleep 1; done

echo "==> Installing InfluxDB 1.8 + Grafana (from their apt repos; direct egress on the LAN)"
pct exec "${CTID}" -- bash -lc '
  set -e
  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y curl gnupg apt-transport-https
  # InfluxDB
  curl -fsSL https://repos.influxdata.com/influxdata-archive_compat.key \
    | gpg --dearmor -o /usr/share/keyrings/influxdata.gpg
  echo "deb [signed-by=/usr/share/keyrings/influxdata.gpg] https://repos.influxdata.com/debian stable main" \
    > /etc/apt/sources.list.d/influxdata.list
  # Grafana
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /usr/share/keyrings/grafana.gpg
  echo "deb [signed-by=/usr/share/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    > /etc/apt/sources.list.d/grafana.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y influxdb grafana
  systemctl enable --now influxdb grafana-server
'

echo "==> Creating InfluxDB database '${INFLUX_DB}' with ${INFLUX_RETENTION} retention"
pct exec "${CTID}" -- bash -lc "
  for i in \$(seq 1 15); do influx -execute 'SHOW DATABASES' &>/dev/null && break; sleep 1; done
  influx -execute 'CREATE DATABASE ${INFLUX_DB} WITH DURATION ${INFLUX_RETENTION}'
"

cat <<EOF

==> Monitoring LXC ${CTID} up at ${IPCIDR%/*}.
    Grafana UI:  http://${IPCIDR%/*}:3000   (default login admin/admin — change it)
    InfluxDB:    http://${IPCIDR%/*}:8086    db=${INFLUX_DB}, retention=${INFLUX_RETENTION}

Finish in the UIs (one-time):
  1. Proxmox: Datacenter > Metric Server > Add > InfluxDB
       Server ${IPCIDR%/*}, Port 8086, Protocol http, Database ${INFLUX_DB}.
       Proxmox now pushes node/guest/storage metrics here every few seconds.
  2. Grafana: add an InfluxDB data source (URL http://localhost:8086, db ${INFLUX_DB}),
       then import a community "Proxmox via InfluxDB" dashboard from grafana.com/dashboards.

Retention is ${INFLUX_RETENTION} on purpose (high-res, short window for spot-checking). The
long-term history is the Option A SQLite store on the collector host — see ../README.md.
EOF
