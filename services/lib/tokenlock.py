"""tokenlock — one refresher at a time for every rotating credential on this box.

THE FAILURE THIS EXISTS TO PREVENT
  Most OAuth providers the estate talks to ROTATE the refresh token: using it returns a new one and
  kills the old. Several processes share each credential file (three dev sessions, bard, coach, the
  timers), and when two of them find the access token stale at the same moment, both spend the same
  refresh token. The second request is a replay, and the provider's reuse detection revokes the
  whole token family -- which only a human in a browser can repair. Yoto bricked this way on
  2026-09-25; ultrahuman and audible had the identical unguarded read-refresh-write.

  Using a valid ACCESS token concurrently is harmless. Only the refresh is dangerous, so only the
  refresh is serialised: hold the lock from "is it stale?" through "new tokens are on disk", and
  re-check staleness INSIDE the lock so the loser picks up what the winner just wrote.

USE
    from tokenlock import token_lock, write_atomic

    tok = load()
    if fresh(tok):                          # fast path, no lock
        return tok
    with token_lock(TOKEN_FILE):            # everything below is one critical section
        tok = load()                        # RE-READ: someone may have refreshed while we waited
        if fresh(tok):
            return tok
        new = refresh(tok)                  # give the HTTP call a timeout well under WAIT_S
        write_atomic(TOKEN_FILE, serialise(new))
        return new

THE BYPASS
  flock is released by the kernel when the holder exits or dies, so a crashed refresher never
  leaves the lock stuck. What can still hold it forever is a holder that is alive but HUNG. After
  `wait` seconds we log who holds it (at err level, so it reaches search_logs) and proceed without
  the lock rather than wedge every consumer behind one stuck process. That is a real, small risk --
  if the hung holder later completes its refresh we may still replay -- so `wait` defaults well
  above any refresh's HTTP timeout: a healthy holder is never bypassed.

  flock is per HOST. Nothing here protects a credential file copied to a second box; never share
  one rotating refresh token between machines -- give each box its own grant.
"""
import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import errlog

WAIT_S = 120          # > any refresh's HTTP timeout (they are 30-45 s), so only a hung holder hits it
POLL_S = 0.25


def _holder(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return json.loads(os.read(fd, 4096) or b"{}")
    except Exception:
        return {}


@contextlib.contextmanager
def token_lock(path, wait=WAIT_S):
    """Exclusive refresh rights on the credential at `path`, across every process on this box.

    Yields True when the lock is held, False when it was bypassed after `wait` seconds.
    NOT re-entrant: flock is per open file, so a nested acquire from the same process waits on
    itself until the bypass. Take it once, around the whole read-refresh-persist.
    """
    lock = str(path) + ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    h = _holder(fd)
                    errlog.err(f"tokenlock: {lock} held for >{wait}s by pid {h.get('pid')} "
                               f"({h.get('cmd', '?')}, since {h.get('since', '?')}) -- BYPASSING; "
                               f"a refresh-token replay is possible if that holder is still working")
                    break
                time.sleep(POLL_S)
        if held:
            # Who holds it, for the bypass message of whoever waits on us. Advisory only.
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": os.getpid(), "cmd": " ".join(sys.argv)[:200],
                                     "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode())
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_atomic(path, data, mode=0o600):
    """Replace `path` with `data` (str or bytes) so no reader ever sees it empty or half-written."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
