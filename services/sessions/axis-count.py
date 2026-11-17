#!/usr/bin/env python3
"""axis-count.py: warn on Telegram when MX Axis Mundi holds too many open chats.

Axis runs with --capacity 32 (axis-launch.sh). At capacity a new chat started from the app does not
fail, it just never starts, which is how a Spotify chat sat dead on 2026-11-17 with 8/8 slots taken
by chats up to 11 days idle. Operator: "warn me periodically on telegram once I have over 25".

Counts the session processes (`--sdk-url`) in moprox-axis.service's cgroup. Above WARN_OVER it posts
at most once per REPEAT_H hours as #axis; it also posts once when the count drops back under, so a
warning is never left standing. Archiving a chat in the app is what frees its slot.

    axis-count.py           check and warn if needed
    axis-count.py --dry     print the count and what would be sent, post nothing
"""
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from errlog import err

CGROUP    = Path("/sys/fs/cgroup/system.slice/moprox-axis.service/cgroup.procs")
STATE     = Path.home() / ".local/state/moprox-dev/axis-count.json"
CAPACITY  = 32
WARN_OVER = int(os.environ.get("AXIS_WARN_OVER", 25))
REPEAT_H  = float(os.environ.get("AXIS_WARN_REPEAT_H", 24))


def sessions():
    n = 0
    for pid in CGROUP.read_text().split():
        try:
            if b"--sdk-url" in Path(f"/proc/{pid}/cmdline").read_bytes():
                n += 1
        except OSError:
            pass            # benign: the process exited between listing and reading
    return n


def send(text, dry):
    if dry:
        print("WOULD SEND:", text)
        return True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
    import tg
    tg.send(text, agent="axis")
    return True


def main():
    dry = "--dry" in sys.argv
    try:
        n = sessions()
    except OSError as exc:
        err("axis-count: cannot read the moprox-axis cgroup; is the service running?", exc)
        return 1
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    now = time.time()
    print(f"axis: {n}/{CAPACITY} chats open (warn over {WARN_OVER})")
    try:
        if n > WARN_OVER:
            if now - float(st.get("warned_at") or 0) > REPEAT_H * 3600:
                send(f"MX Axis Mundi has {n}/{CAPACITY} chats open. At {CAPACITY} new chats silently "
                     f"fail to start. Archive the ones you are done with in the app; they can be "
                     f"unarchived later.", dry)
                st["warned_at"] = now
        elif st.get("warned_at"):
            send(f"MX Axis Mundi is back to {n}/{CAPACITY} chats open.", dry)
            st["warned_at"] = None
    except Exception as exc:
        err("axis-count: the Telegram warning failed; the operator was not told", exc)
        return 1
    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
