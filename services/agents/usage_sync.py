#!/usr/bin/env python3
"""Pull other hosts' agent-usage ledgers to this box, where the dashboard's Stats tab is built.

The analyst runs on claude-loop and appends to `~/.local/share/moprox/agent-usage.jsonl` THERE.
The Stats page is built here, from the file of the same name HERE — so 116 cycles and 169.2M tokens
were invisible while the page looked perfectly healthy. Nothing was broken enough to notice: the
path is identical on both boxes, so the code reads correct.

Each host lands in its own file (`agent-usage-<name>.jsonl`), never merged into the local ledger:
the pull is a whole-file REPLACE, so re-running can't duplicate rows, and a host that goes away
leaves its history intact. `ledger.py` unions them for the readers.

The loop guest's copy is owned by `mikael` while we log in as `agent`, hence `sudo -n cat` — read
only, and the guest is single-homed/isolated (ADR 0007), so the ssh grant is by design.
"""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

D = Path.home() / ".local/share/moprox"
KEY = Path.home() / ".ssh/claude-dev-ops"
REMOTE = "/home/mikael/.local/share/moprox/agent-usage.jsonl"
HOSTS = {"loop": "agent@10.10.10.11"}          # claude-loop (CT 114) — the continuous-agent guest


def pull(name, target):
    dest = D / ("agent-usage-%s.jsonl" % name)
    cmd = ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new", target, "sudo -n cat %s" % REMOTE]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ssh %s: %s" % (target, (r.stderr or "").strip()[:200]))
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    good = 0
    for ln in lines:
        try: json.loads(ln); good += 1
        except Exception as e: errlog.skip("usage_sync.py: %s ledger line" % name, e)
    if not good:
        raise RuntimeError("%s returned %d lines, none parseable — refusing to replace" % (name, len(lines)))
    # A ledger only ever grows. A short read (truncated transfer, half-written file) must NOT
    # overwrite a longer one, or the page silently loses history and looks merely "quiet".
    old = len(dest.read_text().splitlines()) if dest.exists() else 0
    if good < old:
        raise RuntimeError("%s returned %d rows but we already hold %d — refusing to shrink"
                           % (name, good, old))
    tmp = dest.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.rename(dest)
    return good, good - old


def main():
    D.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, target in HOSTS.items():
        try:
            n, new = pull(name, target)
            print("%s: %d rows (+%d)" % (name, n, new))
        except Exception as e:
            errlog.err("usage_sync.py: pulling %s (%s)" % (name, target), e)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
