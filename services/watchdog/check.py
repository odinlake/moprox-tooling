#!/usr/bin/env python3
"""check.py — is anything that MUST keep running silent?

The estate's detectors have no detector. A stopped timer produces no failure, no log line and no
incident: the queue simply stays empty, which is exactly what a good week looks like. Every other
check here answers "did something go wrong?"; this one answers "is anything still asking?".

Built as a QUERY over the fleet journal, like the incident queue and for the same reason: systemd
already logs every unit's activity from every guest, so nothing needs instrumenting, no heartbeat
file can go stale in a way nobody notices, and a unit added to watch.json is covered the moment its
first record arrives. The check is simply: when did this (host, unit) last say ANYTHING, and is that
longer ago than its declared tolerance?

A breach logs a journal record carrying MSGID_NO_HEARTBEAT, so it lands in the same incident queue
as everything else, with UNIT=heartbeat-<host>-<unit> so each silent unit is its own incident.

WHAT WATCHES THIS: nothing, and that is not fixable from inside — a process cannot report its own
death. Two things bound it instead. The daily digest reports this unit's own last run whenever it
posts, and if the estate has been quiet for a week the digest sends one liveness line anyway, so
total silence from BOTH is itself the signal. Do not delete that weekly line thinking it is noise.

    check.py           evaluate and log breaches
    check.py --dry     evaluate and print; log nothing
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/logview")
try:
    import errlog
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import errlog

HERE = Path(__file__).resolve().parent
CONF = Path(os.environ.get("WATCHDOG_CONF", HERE / "watch.json"))
JOURNAL_DIR = os.environ.get("WATCHDOG_JOURNAL_DIR", "/var/log/journal/remote")
MSGID_NO_HEARTBEAT = "c1b7f0a94e2d4c8fa6d35b0e7f21c4d3"      # must match logview/server.py
TIMEOUT = 60


def last_seen(host, unit):
    """Newest journal timestamp for this unit on this host, or None if it has never been seen.

    -u, not _SYSTEMD_UNIT=: for a oneshot the interesting records ("Starting…", "Finished…") are
    written by pid 1 ABOUT the unit and carry UNIT=, which only -u expands to. Getting this wrong
    returns zero rows for a perfectly healthy unit — i.e. it would report everything as dead.
    """
    argv = ["journalctl", f"--directory={JOURNAL_DIR}", "--output=json", "--no-pager", "--all",
            "--reverse", "--lines=1", "-u", unit, f"_HOSTNAME={host}"]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    for line in p.stdout.splitlines():
        try:
            return int(json.loads(line)["__REALTIME_TIMESTAMP"]) / 1e6
        except (ValueError, KeyError, TypeError):
            continue
    return None


def raise_incident(e, hours, last):
    when = ("last seen " + datetime.fromtimestamp(last, tz=timezone.utc).astimezone()
            .isoformat(timespec="seconds")) if last else "NEVER seen in the journal"
    msg = (f"{e['host']}/{e['unit']} has been silent for {hours:.1f} h "
           f"(tolerance {e['max_hours']} h) — {when}. {e.get('why', '')} "
           f"A stopped checker reports nothing and looks exactly like a quiet estate, which is why "
           f"this is checked from outside rather than waited for.")
    fields = "\n".join([f"MESSAGE_ID={MSGID_NO_HEARTBEAT}", "PRIORITY=3",
                        f"UNIT=heartbeat-{e['host']}-{e['unit']}",
                        f"WATCH_HOST={e['host']}", f"WATCH_UNIT={e['unit']}",
                        f"SILENT_HOURS={hours:.1f}", f"MESSAGE={msg}"]) + "\n"
    subprocess.run(["logger", "--journald"], input=fields, text=True, check=True, timeout=20)


def main():
    dry = "--dry" in sys.argv[1:]
    try:
        conf = json.loads(CONF.read_text())
        units = conf["units"]
    except Exception as exc:
        errlog.err(f"watchdog: cannot read {CONF} — NOTHING is being watched", exc)
        return 1
    if not units:
        errlog.err(f"watchdog: {CONF} declares no units — NOTHING is being watched")
        return 1

    now, breaches, ok = time.time(), 0, 0
    for e in units:
        try:
            last = last_seen(e["host"], e["unit"])
        except Exception as exc:
            errlog.err(f"watchdog: querying {e['host']}/{e['unit']} failed — it is UNCHECKED", exc)
            continue
        hours = (now - last) / 3600.0 if last else float("inf")
        if last and hours <= e["max_hours"]:
            ok += 1
            continue
        breaches += 1
        line = (f"SILENT  {e['host']}/{e['unit']}  "
                f"{'never seen' if not last else f'{hours:.1f} h'} > {e['max_hours']} h")
        if dry:
            print(line)
            continue
        print(line)
        try:
            raise_incident(e, hours if last else -1, last)
        except Exception as exc:
            errlog.err(f"watchdog: could not raise the silence incident for "
                       f"{e['host']}/{e['unit']}", exc)
    print(f"watchdog: {ok} of {len(units)} units alive, {breaches} silent{' (dry)' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
