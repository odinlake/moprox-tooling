-- SQLite schema for the long-term Proxmox metrics store (Option A).
-- Append-only time series; one row per poll per object. Sizes are bytes, cpu is a 0..1 ratio.
-- Long-term by design: we keep these forever (they're small and downsampled by the timer
-- cadence), unlike the Grafana/InfluxDB store which keeps high-res data only for weeks.

CREATE TABLE IF NOT EXISTS node_metrics (
  ts            INTEGER NOT NULL,          -- unix epoch seconds
  node          TEXT    NOT NULL,
  cpu           REAL,                       -- 0..1
  mem_used      INTEGER,
  mem_total     INTEGER,
  swap_used     INTEGER,
  swap_total    INTEGER,
  rootfs_used   INTEGER,
  rootfs_total  INTEGER,
  loadavg1      REAL,
  uptime        INTEGER
);

CREATE TABLE IF NOT EXISTS storage_metrics (
  ts            INTEGER NOT NULL,
  node          TEXT    NOT NULL,
  storage       TEXT    NOT NULL,
  type          TEXT,
  total         INTEGER,
  used          INTEGER,
  avail         INTEGER
);

CREATE TABLE IF NOT EXISTS guest_metrics (
  ts            INTEGER NOT NULL,
  node          TEXT    NOT NULL,
  vmid          INTEGER NOT NULL,
  name          TEXT,
  kind          TEXT,                       -- 'qemu' | 'lxc'
  status        TEXT,                       -- 'running' | 'stopped' | ...
  cpu           REAL,                       -- 0..1
  mem           INTEGER,
  maxmem        INTEGER,
  disk          INTEGER,                    -- current usage if reported, else 0
  maxdisk       INTEGER,                    -- allocated
  netin         INTEGER,
  netout        INTEGER,
  uptime        INTEGER
);

-- Backup inventory: a daily snapshot of what backups are held (PBS or vzdump), via the PVE
-- storage content API. `size` is the LOGICAL (pre-dedup) backup size; the deduplicated
-- physical space is the datastore's `used` in storage_metrics. Dedup ratio ≈ sum(size)/used.
CREATE TABLE IF NOT EXISTS backup_inventory (
  ts        INTEGER NOT NULL,   -- when this inventory snapshot was taken
  node      TEXT,
  storage   TEXT    NOT NULL,
  vmid      INTEGER,
  volid     TEXT,               -- e.g. pbs-fleet:backup/vm/110/2026-06-21T...
  ctime     INTEGER,            -- backup creation time (epoch)
  size      INTEGER,            -- logical size (bytes), pre-dedup
  format    TEXT,
  verified  TEXT,               -- verification state ('ok'/'failed'/...) if present
  protected INTEGER             -- 1 if protected from prune
);

CREATE INDEX IF NOT EXISTS idx_node_ts    ON node_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_storage_ts ON storage_metrics(ts, storage);
CREATE INDEX IF NOT EXISTS idx_guest_ts   ON guest_metrics(ts, vmid);
CREATE INDEX IF NOT EXISTS idx_backup_ts  ON backup_inventory(ts, storage);
