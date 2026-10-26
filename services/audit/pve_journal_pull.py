#!/usr/bin/env python3
"""Pull the Proxmox host's own journal into the fleet log (ident `pve-host`), every 5 minutes.

No agent can log in to the PVE host, so changes made there (an LXC's RAM raised, a guest restarted,
a config edited in the web UI) left no trace the estate could read: on 2026-10-25 claude-loop went
from ~0.9 GB to ~3.9 GB and no record says who did it. pvedaemon logs every API config change with
the acting user ("<root@pam> update CT 111: -memory 4096"); this carries those lines over.

Uses the dashboard's read-only token (~/.config/proxmox/pve-metrics.env). It needs Sys.Syslog on
/nodes/<node>, which PVEAuditor lacks; until that is granted this logs one warning per run and
ships nothing (see README.md for the grant).
"""
import json, os, ssl, subprocess, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402

API = os.environ["PVE_API_URL"].rstrip("/")
NODE = os.environ["PVE_NODE"]
AUTH = f"PVEAPIToken={os.environ['PVE_TOKEN_ID']}={os.environ['PVE_TOKEN_SECRET']}"
CURSOR = Path(os.environ.get("PVE_JOURNAL_CURSOR", Path.home() / ".local/state/moprox/pve-journal.cursor"))
KEEP = ("pvedaemon", "pveproxy", "pct", "qm", "vzdump", "sshd", "pam_unix", "login", "sudo", "pvesh",
        "update CT", "update VM", "migrate", "shutdown", "reboot", "lxc-start", "lxc-stop")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE      # self-signed PVE cert, as services/metrics/collect.py


def fetch(params):
    req = urllib.request.Request(f"{API}/api2/json/nodes/{NODE}/journal?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": AUTH})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))
    with opener.open(req, timeout=30) as r:
        return json.load(r)["data"]


def main():
    cur = CURSOR.read_text().strip() if CURSOR.exists() else ""
    try:
        lines = fetch({"startcursor": cur} if cur else {"lastentries": 500})
    except urllib.error.HTTPError as e:
        if e.code == 403:
            errlog.warn("token lacks Sys.Syslog on /nodes/%s; Proxmox host changes are NOT recorded" % NODE)
            return
        raise
    # The API returns the cursor as the first and last lines ("-- cursor: ..."); the rest are log lines.
    shipped, last = 0, cur
    for ln in lines:
        if ln.startswith("-- cursor: "):
            last = ln[len("-- cursor: "):]
            continue
        if any(k in ln for k in KEEP):
            subprocess.run(["logger", "-t", "pve-host", ln[:2000]], check=True, timeout=10)
            shipped += 1
    if last:
        CURSOR.parent.mkdir(parents=True, exist_ok=True)
        CURSOR.write_text(last)
    print(f"pve-host: {shipped} line(s) shipped of {len(lines)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        errlog.err("pve_journal_pull.py: pulling the Proxmox host journal", e)
        sys.exit(1)
