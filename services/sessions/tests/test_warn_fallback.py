#!/usr/bin/env python3
"""--warn must still deliver on a box with no Telegram stack.

    python3 services/sessions/tests/test_warn_fallback.py

claude-loop has neither telegramify_markdown nor convo, so `import tg` raises there and warn()'s
only behaviour was to print to stderr and return 0 — green timer, no message, on the one box that
had already lost its refresh token once. These cases pin the fallback: nothing is sent above the
threshold, the real HTTP request is built below it, the handle and remedy survive, and a dead
transport still exits 0 — but reaches the journal at err, because a warning that could not be
delivered is otherwise indistinguishable from no warning being due.

No network: urlopen is replaced, so a PASS means the request was CONSTRUCTED, not delivered.
"""
import contextlib, importlib.util, io, json, os, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "creds-check.py"
spec = importlib.util.spec_from_file_location("credscheck", SRC)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

tmp = Path(tempfile.mkdtemp(prefix="warn-"))
ok = []
sent = []


def fake_urlopen(req, *a, **k):
    sent.append((req.full_url, urllib.parse.parse_qs(req.data.decode())))
    class R:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()


urllib.request.urlopen = fake_urlopen


def check(name, cond):
    ok.append(bool(cond))
    print("%s  %s" % ("PASS" if cond else "FAIL", name))


def setup(days_left):
    """A creds file `days_left` from expiry, an env file, and no importable tg."""
    c = tmp / "creds.json"
    c.write_text(json.dumps({"claudeAiOauth": {
        "refreshTokenExpiresAt": (time.time() + days_left * 86400) * 1000}}))
    cc.CREDS = c
    env = tmp / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=BOTTOK\n# a comment\nTELEGRAM_CHAT_ID='424242'\n")
    cc.TG_ENV = env
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    # force `import tg` to fail exactly as it does on claude-loop
    cc.forward_dir = lambda: tmp
    sys.modules.pop("tg", None)
    sent.clear()


# --- above the threshold: still silent -------------------------------------------------
setup(30)
rc = cc.warn(5.0, "REMEDY-TEXT", "loop")
check("no message when expiry is far away", rc == 0 and not sent)

# --- below it: the fallback builds a real sendMessage ----------------------------------
setup(3.2)
rc = cc.warn(5.0, "REMEDY-TEXT", "loop")
check("warn still exits 0", rc == 0)
check("exactly one message sent with no tg installed", len(sent) == 1)
if sent:
    url, form = sent[0]
    body = form["text"][0]
    check("posted to sendMessage with the token from telegram.env",
          url == "https://api.telegram.org/botBOTTOK/sendMessage")
    check("chat id came from telegram.env, quotes stripped", form["chat_id"] == ["424242"])
    check("handle prefixed", body.startswith("#loop "))
    check("names the host and the deadline", "expire in 3.2 days" in body)
    check("carries the caller's remedy, not the dev one", "REMEDY-TEXT" in body and "moprox-dev@" not in body)
    check("markdown stripped rather than left raw", "**" not in body)

# --- creds unreadable: message changes, still sent -------------------------------------
setup(3.2)
cc.CREDS = tmp / "gone.json"
cc.warn(5.0, "REMEDY-TEXT", "loop")
check("unreadable credentials still raise a message", len(sent) == 1 and "unreadable" in sent[0][1]["text"][0])

# --- no creds for the fallback either: exit 0, but LOUD --------------------------------
# journald files an un-prefixed stderr line at PRIORITY=6, so the guard that could not speak used to
# look exactly like the guard with nothing to say. `<3>` is the only thing that reaches a detector.
setup(3.2)
cc.TG_ENV = tmp / "no-env"
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    rc = cc.warn(5.0, "R", "loop")
undelivered = buf.getvalue()
check("a dead transport still exits 0", rc == 0 and not sent)
check("...and says so at err level, naming the deadline it could not deliver",
      any(l.startswith("<3>") and "2026-" in l for l in undelivered.splitlines()))

# --- the fallback WORKS: a delivered warning must not red anything ---------------------
setup(3.2)
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    cc.warn(5.0, "R", "loop")
check("a delivered warning leaves no err record",
      len(sent) == 1 and not any(l.startswith("<3>") for l in buf.getvalue().splitlines()))

print("\n%d/%d passed" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
