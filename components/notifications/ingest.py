#!/usr/bin/env python3
"""Ingest HA-captured notifications into private-data (month-partitioned, redacted, deduped).

Source: ~/ha-notif/notif.jsonl (Syncthing receive-only mirror of HA /config/notif).
Dest:   private-data/notifications/notif-YYYY-MM.jsonl
Second-pass redaction mirrors the HA automation's deny-list (belt-and-braces).
Dedupe key: (app, post_time). Commits to private-data when new lines land.
"""
import fcntl, json, re, subprocess, sys, time
from pathlib import Path

SRC = Path.home() / "ha-notif/notif.jsonl"
DEST = Path.home() / "projects/private-data/notifications"
REPO = DEST.parent
# Same name private-data-sync.sh derives (`basename $REPO`-sync.lock), so the two actually pair.
LOCK = f"{REPO.name}-sync.lock"

DENY_PKGS = {
    "io.homeassistant.companion.android",
    "com.google.android.apps.authenticator2", "com.authy.authy",
    "com.beemdevelopment.aegis", "com.azure.authenticator",
    "com.x8bit.bitwarden", "com.bitwarden.authenticator",
    "com.google.android.apps.messaging", "com.android.messaging",
}
OTP = re.compile(
    r"(one[- ]?time|verification|security|login|access)\s*code|\botp\b"
    r"|(code|passcode)\D{0,15}\d{3,8}|\d{3,8}\D{0,15}(code|passcode)|password", re.I)


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          check=check, capture_output=True, text=True)


def acquire_sync_lock(timeout=300):
    """flock the lock private-data-sync.sh already takes, so its `git add -A; commit; pull --rebase`
    cannot interleave with ours. Returns the held handle, or None if the sweeper held it throughout.

    The path search mirrors the shell's exactly — /run/lock first, /tmp only if that cannot be opened
    for writing — because two parties that disagree about the path hold two different locks and are
    no more serialised than none at all."""
    fh = None
    for path in (Path("/run/lock") / LOCK, Path("/tmp") / LOCK):
        try:
            fh = open(path, "w")
            break
        except OSError:
            continue
    if fh is None:
        print(f"<3>cannot open {LOCK} in /run/lock or /tmp", file=sys.stderr)
        sys.exit(1)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.5)


def pending():
    """True when notifications/ holds work git has not committed — whether this run appended it or an
    earlier run appended it and then died before committing. `--no-optional-locks` keeps this probe
    from taking index.lock itself, which is the very thing we are queueing to avoid touching."""
    r = git("--no-optional-locks", "status", "--porcelain", "--", "notifications", check=False)
    return bool(r.stdout.strip())


def seen_keys():
    keys = set()
    for f in DEST.glob("notif-*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                keys.add((e.get("app"), e.get("post_time")))
    return keys


def main():
    if not SRC.exists():
        sys.exit(0)
    seen = seen_keys()
    added, dropped = 0, 0
    for line in SRC.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        key = (e.get("app"), e.get("post_time"))
        if key in seen:
            continue
        seen.add(key)
        body = f"{e.get('title') or ''} {e.get('text') or ''}"
        if e.get("app") in DENY_PKGS or OTP.search(body):
            dropped += 1
            continue
        month = (e.get("ts") or "")[:7] or "unknown"
        with open(DEST / f"notif-{month}.jsonl", "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        added += 1
    # NOT `if added:` — the append above is durable before the commit, so a run that died in git left
    # its line on disk, and every later run then saw it via seen_keys() and computed added=0. Under
    # the old guard that orphan was never staged by this program again; it reached git only when some
    # unrelated later append made added>0 and the unconditional `git add` swept it up. Asking git what
    # is uncommitted instead of asking ourselves what we just wrote makes the next run the retry.
    committed = 0
    if pending():
        fh = acquire_sync_lock()
        if fh is None:
            # Contention is not a failure: the work is on disk and pending() will still be true in
            # 15 min. Exiting 1 here is what turned a lost sub-second race into a unit-failed incident.
            print(f"added={added} dropped={dropped} committed=0 busy=1")
            return
        try:
            git("add", "notifications")
            numstat = git("diff", "--cached", "--numstat", "--", "notifications").stdout
            committed = sum(int(c) for c, *_ in (l.split("\t") for l in numstat.splitlines())
                            if c.isdigit())
            if committed:
                git("commit", "-q", "-m", f"notifications: ingest {committed} new")
        finally:
            fh.close()
    # committed counts what git is actually recording, not what this run appended; the two differ
    # exactly when an earlier run's orphan is being swept up with it. Keeping the message equal to the
    # insertions preserves `git log --numstat` mismatch as a clean detector of an unhandled write.
    print(f"added={added} dropped={dropped} committed={committed}")


if __name__ == "__main__":
    main()
