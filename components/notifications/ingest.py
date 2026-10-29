#!/usr/bin/env python3
"""Ingest HA-captured notifications into private-data (month-partitioned, redacted, deduped).

Source: ~/ha-notif/notif.jsonl (Syncthing receive-only mirror of HA /config/notif).
Dest:   private-data/notifications/notif-YYYY-MM.jsonl
Second-pass redaction mirrors the HA automation's deny-list (belt-and-braces).
Dedupe key: (app, post_time). Commits to private-data when new lines land.
"""
import fcntl, json, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/lib"))
import errlog  # noqa: E402  — see git(): this program's death was filed at info, git's reason nowhere

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
    """Run git in REPO. A checked failure says WHY at err level, then exits 1.

    `capture_output=True` puts git's own sentence in the exception and NOWHERE else:
    CalledProcessError.__str__ repeats only the argv and the exit status, and an uncaught exception
    reaches journald through Python's default excepthook, which writes to stderr with no `<N>`
    prefix — so journald files every line of it at PRIORITY=6.

    Measured on claude-dev 2026-10-05: twelve consecutive runs, 09:15:57..12:00:47Z, died here on
    `commit` exit 128 while the shared tree sat in a stale rebase. Each left 16 info lines and not
    one err record from this program, so `search_logs(priority=3)` showed only systemd's generic
    "Failed to start notif-ingest.service" — and git's fatal sentence, the one fact that identifies
    the rebase as the cause, was captured by this function and then discarded at every level.

    Still exit 1, so the unit-failed incident stays: the point is that the err record now names the
    cause instead of leaving it to be guessed from a neighbouring unit's journal."""
    try:
        return subprocess.run(["git", "-C", str(REPO), *args],
                              check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        said = " ".join((exc.stderr or "").split()) or " ".join((exc.stdout or "").split())
        errlog.die(f"git {' '.join(args)} failed in {REPO} (exit {exc.returncode}): "
                   f"{said or 'git said nothing on either stream'}")


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


def src_state():
    """What the mirror looked like to THIS run. The only record the estate keeps of it.

    `added` counts (app, post_time) keys absent from DEST, not lines or bytes in SRC, so it reads
    `added=0 dropped=0 committed=0` identically for an unchanged mirror, one truncated to zero
    lines and one duplicated-and-reordered -- and the absent branch below used to exit 0 printing
    nothing at all. Measured at private-data b58d8758 during a 35 h silence:
    moprox-memory/notif-mirror-unobserved-added-zero. ~/ha-notif is the capture chain's only
    observable midpoint on this box and nothing else looks at it: 0 of the 12 freshness lanes
    target it and this file is its only reader in all of moprox-tooling.

    Read BEFORE the ingest loop, so the fields describe what the program was handed.

    These three fields separate mirror states, NOT broken links: a quiet tablet and a stalled
    Syncthing both leave `src_age_s` large, and distinguishing those still needs the HA side.
    """
    try:
        st = SRC.stat()
    except OSError:
        return "src_lines=- src_bytes=- src_age_s=-"
    n = sum(1 for line in SRC.read_text().splitlines() if line.strip())
    return f"src_lines={n} src_bytes={st.st_size} src_age_s={int(time.time() - st.st_mtime)}"


def main():
    src = src_state()
    if not SRC.exists():
        # Was a bare `sys.exit(0)`: the mirror vanishing was indistinguishable in the journal from
        # a run with nothing to do, because it did not even print an `added=` line. errlog exists
        # for exactly this shape. warn, not err: Syncthing has not created the file yet on a fresh
        # box either, and that is expected-but-notable rather than broken.
        errlog.warn(f"notification mirror absent: {SRC}")
        print(f"added=0 dropped=0 committed=0 {src}")
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
            print(f"added={added} dropped={dropped} committed=0 busy=1 {src}")
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
    print(f"added={added} dropped={dropped} committed={committed} {src}")


if __name__ == "__main__":
    main()
