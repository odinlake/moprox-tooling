"""notify.py — the loop's one outward voice.

The analyst runs unattended with nobody watching its journal, so anything it finds is invisible
until it says so. This posts to the FIREHOSE group, never the operator's DM: the chat id comes from
telegram-firehose.env, which is layered over telegram.env so it can override the destination while
reusing the bot token. Getting that layering backwards would put loop chatter in the operator's DM,
which is exactly what the firehose group exists to prevent.

Plain text, no MarkdownV2. A firehose carries claims, paths and shell fragments — the characters
MarkdownV2 requires escaping — and a post that fails to render is worse than one that is merely
plain. The formatted lane (coach) has a human reading it; this one does not.

Posting is best-effort but NEVER silent: a failed post is an err, because "the agent said nothing"
and "the agent could not speak" look identical from the outside, and that confusion has already
cost this estate two weeks.
"""
import os, sys, urllib.request, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from errlog import err

CONF     = Path(os.environ.get("LOOP_TG_ENV", Path.home() / ".config/claude-dev/telegram.env"))
FIREHOSE = Path(os.environ.get("LOOP_TG_FIREHOSE", Path.home() / ".config/claude-dev/telegram-firehose.env"))
API      = "https://api.telegram.org/bot%s/sendMessage"
LIMIT    = 3900          # Telegram's hard cap is 4096; leave room for the #handle prefix


def _kv(path):
    out = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def creds():
    """Token from telegram.env; chat id from the firehose file if present. Order matters."""
    d = _kv(CONF)
    d.update(_kv(FIREHOSE))
    return d.get("TELEGRAM_BOT_TOKEN"), d.get("TELEGRAM_CHAT_ID")


def send(text, agent="analyst"):
    """Post one message. Returns True on success; logs at err and returns False otherwise."""
    tok, chat = creds()
    if not tok or not chat:
        err(f"telegram: no credentials — looked in {CONF} and {FIREHOSE}; the agent cannot report",
            RuntimeError("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"))
        return False
    body = ("#%s %s" % (agent, text))[:LIMIT]
    data = urllib.parse.urlencode({"chat_id": chat, "text": body,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(API % tok, data=data), timeout=20)
        return True
    except Exception as exc:
        err("telegram: posting the cycle report failed — this finding reached nobody", exc)
        return False
