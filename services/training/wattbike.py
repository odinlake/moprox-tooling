#!/usr/bin/env python3
"""Kick off a Wattbike Hub pull the instant Polar tells us a RIDE landed, without ever letting
Wattbike's flakiness delay the coach read.

Shape (operator, 2026-09-25): start at the EARLIEST possible moment, run in PARALLEL with the rest
of the Polar work, and be joined BEFORE coach is invoked -- but on a short leash, because a hung
Hub must not hold up a session read that is otherwise ready to go.

Two timeouts, deliberately different:
  JOIN_S  how long the coach handoff will wait. This is the one the operator feels.
  HARD_S  the background work's own ceiling, well past JOIN_S. If the pull is merely slow it still
          finishes and lands on disk after coach has gone ahead without it; the next ride (or any
          rerun) picks the data up regardless, since the fetcher skips what it already has.

The fetch itself runs on the webscout box over ssh: that is where the Hub session lives, and the
token is minted client-side by a browser we only have there. We rsync the result back afterwards.
Nothing here raises: every failure path returns a line for the prompt and lets the read proceed.
"""
import os, subprocess, threading, time
from pathlib import Path

BOX      = os.environ.get("WATTBIKE_BOX", "agent@10.10.10.8")
SSH_KEY  = os.environ.get("WATTBIKE_SSH_KEY", str(Path.home() / ".ssh/claude-dev-ops"))
DEST     = Path.home() / "projects/private-data/wattbike"
JOIN_S   = int(os.environ.get("WATTBIKE_JOIN_S", "120"))    # coach waits at most this long
HARD_S   = int(os.environ.get("WATTBIKE_HARD_S", "420"))    # the pull's own ceiling
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=10", "-i", SSH_KEY]

REMOTE = ("sudo -u webscout env HOME=/opt/webscout "
          "PLAYWRIGHT_BROWSERS_PATH=/opt/webscout/.cache/ms-playwright "
          "WEBSCOUT_HEADED=1 PYTHONUNBUFFERED=1 "
          "xvfb-run -a /opt/webscout/venv/bin/python /opt/webscout/wattbike_fetch.py")


def _run(state):
    t0 = time.time()
    try:
        r = subprocess.run(SSH + [BOX, REMOTE], capture_output=True, text=True, timeout=HARD_S)
        state["out"] = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            state["err"] = "fetch exited %s" % r.returncode
        else:
            DEST.mkdir(parents=True, exist_ok=True)
            # -u so a file we already hold is never overwritten by an older copy; the remote is the
            # source of truth for NEW sessions only.
            rs = subprocess.run(
                ["rsync", "-a", "-u", "-e", " ".join(SSH),
                 "%s:/var/lib/webscout/wattbike/" % BOX, str(DEST) + "/"],
                capture_output=True, text=True, timeout=180)
            if rs.returncode != 0:
                state["err"] = "rsync exited %s: %s" % (rs.returncode, (rs.stderr or "")[:120])
    except subprocess.TimeoutExpired:
        state["err"] = "timed out after %ds" % HARD_S
    except Exception as e:
        state["err"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    finally:
        state["secs"] = round(time.time() - t0, 1)
        state["done"] = True


def start():
    """Fire the pull and return immediately. Call the moment a ride is recognised."""
    state = {"done": False, "out": "", "err": None, "secs": None}
    t = threading.Thread(target=_run, args=(state,), daemon=True)
    t.start()
    return (t, state)


def join_line(handle, timeout=None):
    """Wait up to `timeout` (default JOIN_S) and return ONE line for the coach prompt, or "".

    Returns "" when there was no ride to fetch, so the caller can concatenate unconditionally.
    """
    if not handle:
        return ""
    t, state = handle
    t.join(JOIN_S if timeout is None else timeout)
    if not state["done"]:
        return ("\n\nWATTBIKE: pull still running after %ds and was not waited for further, so the "
                "per-revolution data may not be on disk yet for this session. Do not treat its "
                "absence as meaning the ride lacked power data.\n" % (JOIN_S if timeout is None else timeout))
    if state["err"]:
        return ("\n\nWATTBIKE: pull failed (%s). Read the session from the Polar HR alone and say "
                "nothing about pedal mechanics.\n" % state["err"])
    summary = ""
    for ln in (state["out"] or "").splitlines():
        if ln.startswith("WATTBIKE:"):
            summary = ln.split("WATTBIKE:", 1)[1].strip()
    return ("\n\nWATTBIKE: full-fidelity ride data pulled in %ss (%s). Per-revolution power, "
            "cadence, balance, per-leg pedal effectiveness and a 75-point force curve per stroke "
            "are under private-data/wattbike/raw/<sessionId>.wbs, with the session index in "
            "private-data/wattbike/sessions.json. Use it if this session is the matching ride.\n"
            % (state["secs"], summary or "no summary line"))
