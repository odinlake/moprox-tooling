#!/usr/bin/env python3
"""Guard the shared Claude Max OAuth credentials that the three moprox-dev sessions run on.

Background (outage 2026-07-25). The `moprox-dev@{one,two,three}` sessions authenticate ONLY with the
subscription OAuth in ~/.claude/.credentials.json — the unit clears ANTHROPIC_API_KEY et al, so no API
key is involved. They read those credentials ONCE at startup. When the box booted with a dead refresh
token, all three came up wedged *alive*: the process was healthy, so `Restart=always` never fired,
`systemctl --failed` was clean, and the only evidence was `Not logged in · Run /login` in the PTY log.
A later `/login` fixed the file but could not reach the already-running sessions.

The access token lasts ~8h and is refreshed in-process; the REFRESH token lasts ~4 weeks and is rotated
as a side effect of that refresh. So a continuously-running session stays healthy indefinitely and the
failure only bites after downtime spanning the expiry — i.e. exactly at a boot, hitting all three at once.

Two modes, one script:

  --gate   ExecStartPre guard for moprox-dev@.service. Exits non-zero when the refresh token is
           positively known to be expired, so systemd's Restart backoff retries (<=120s) instead of
           launching a session that will wedge. The moment you `/login`, all three self-heal with no
           systemctl needed, and the failure is visible as a restart loop rather than a healthy corpse.

           FAIL OPEN is the rule: unreadable file, missing file, unexpected JSON shape, absent
           timestamp -> exit 0. This check must never be the reason all three sessions are down. It
           blocks only on a timestamp it actually parsed and that is actually in the past.

  --warn   Daily timer (moprox-creds-warn.timer). Telegrams a heads-up while there is still time to
           act, turning a surprise outage into a scheduled 30-second `/login`. Always exits 0 so the
           timer never goes red on a transport blip.

`tg` is imported lazily inside --warn only: the gate runs on every session start and must not depend on
the Telegram stack (telegramify_markdown, convo) being importable.
"""
import argparse, json, os, socket, sys, time
from datetime import datetime
from pathlib import Path

CREDS = Path(os.environ.get("CLAUDE_CREDS", Path.home() / ".claude/.credentials.json"))


def refresh_expiry_ms(path=CREDS):
    """Epoch-ms expiry of the refresh token, or None if it can't be POSITIVELY determined.

    None means 'unknown' and every caller must treat it as fine — see the fail-open rule above."""
    try:
        oauth = json.loads(path.read_text()).get("claudeAiOauth")
    except Exception:
        return None
    if not isinstance(oauth, dict):
        return None
    v = oauth.get("refreshTokenExpiresAt")
    # bool is an int subclass; a stray True must not read as epoch 1
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def describe(exp_ms, now_ms=None):
    """(days_left, human_expiry) for a known expiry."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    return (exp_ms - now_ms) / 86_400_000.0, datetime.fromtimestamp(exp_ms / 1000).strftime("%Y-%m-%d %H:%M")


def gate():
    exp = refresh_expiry_ms()
    if exp is None:
        return 0                                     # unknown -> never block
    days, when = describe(exp)
    if days > 0:
        return 0
    # stderr -> journald, so `journalctl -u moprox-dev@one` explains the restart loop
    print("moprox-creds-check: refresh token EXPIRED %s (%.1f days ago) — run /login on claude-dev; "
          "holding session start until credentials are refreshed" % (when, -days), file=sys.stderr)
    return 1


DEV_REMEDY = ("Run `/login` on claude-dev, then "
              "`sudo systemctl restart moprox-dev@one moprox-dev@two moprox-dev@three`.\n"
              "Left alone, the next boot after expiry wedges all three sessions at \"Not logged in\".")


def warn(threshold_days, remedy=None, handle="dev"):
    """Telegram a heads-up. NAMES THE HOST, because more than one box runs on these credentials now.

    claude-loop went dark for 38 hours on 2026-09-04 when its refresh token hit its four-week
    ceiling, and this timer did not cover it: the script was written for the dev sessions and its
    remedy line names units that do not exist there. A warning that tells you to fix the wrong box
    is worse than none, so the remedy is now the caller's to supply.
    """
    host = socket.gethostname()
    exp = refresh_expiry_ms()
    if exp is None:
        msg = ("**Claude credentials unreadable on %s** — `%s` is missing or not in the expected "
               "shape.\nVerify with `moprox-creds-check --status` on that box." % (host, CREDS))
    else:
        days, when = describe(exp)
        if days > threshold_days:
            return 0
        head = ("**Claude credentials EXPIRED on %s** %s" % (host, when) if days <= 0 else
                "**Claude credentials on %s expire in %.1f days** (%s)" % (host, days, when))
        msg = head + "\n" + (remedy or DEV_REMEDY)
    try:
        sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
        import tg
        tg.send(msg, agent=handle)
    except BaseException as e:                       # a transport blip must not red the timer. NOT
        # `except Exception`: tg.creds() raises SystemExit when telegram.env is missing, which is
        # exactly the degraded case where we still want the timer green and the reason in the journal.
        print("moprox-creds-check: warn send failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
    return 0


def status():
    exp = refresh_expiry_ms()
    print("host: %s" % socket.gethostname())
    print("creds file: %s (%s)" % (CREDS, "present" if CREDS.exists() else "MISSING"))
    if exp is None:
        print("refresh token: expiry UNKNOWN — gate fails open, sessions start normally")
        return 0
    days, when = describe(exp)
    print("refresh token: expires %s (%.1f days %s)" % (when, abs(days), "left" if days > 0 else "AGO"))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--gate", action="store_true", help="ExecStartPre guard; non-zero only on a parsed, expired token")
    g.add_argument("--warn", action="store_true", help="Telegram a heads-up when expiry is near; always exits 0")
    g.add_argument("--status", action="store_true", help="print credential expiry for a human")
    ap.add_argument("--days", type=float, default=5.0, help="--warn threshold in days (default 5)")
    ap.add_argument("--remedy", default=None,
                    help="what the operator should DO, appended to the --warn message. Default is "
                         "the claude-dev sessions remedy; any other box must pass its own.")
    ap.add_argument("--handle", default="dev", help="#handle the warning is sent under (default dev)")
    a = ap.parse_args()
    sys.exit(gate() if a.gate else warn(a.days, a.remedy, a.handle) if a.warn else status())
