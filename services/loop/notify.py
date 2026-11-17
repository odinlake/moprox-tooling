"""notify.py — the loop's one outward voice.

The analyst runs unattended with nobody watching its journal, so anything it finds is invisible
until it says so. This posts to the FIREHOSE group, never the operator's DM: the chat id comes from
telegram-firehose.env, which is layered over telegram.env so it can override the destination while
reusing the bot token. Getting that layering backwards would put loop chatter in the operator's DM,
which is exactly what the firehose group exists to prevent.

Plain text, no MarkdownV2. A firehose carries claims, paths and shell fragments — the characters
MarkdownV2 requires escaping — and a post that fails to render is worse than one that is merely
plain. That is why this lane is unformatted, unlike coach's: what it carries, not who reads it.
The operator is a member of the firehose group, so write every post as one the operator may read.

WHAT IS POSTED (operator, 2026-11-17: "the posting is excessive"): only accepted work, meaning
verified findings and landed changes, posts in real time. Everything else a cycle used to post
(cycle reports, disputes, strikes, preflight gaps, broken tools, creds warnings) goes through log()
instead: it lands in <state>/<agent>/digest.jsonl, and digest.py turns a day of it into one
well-written summary. Faults still reach errlog, so the incident lane sees them at once; only the
Telegram chatter moved. send() records too, so the digest sees the whole day.

Posting is best-effort but NEVER silent: a failed post is an err, because "the agent said nothing"
and "the agent could not speak" look identical from the outside, and that confusion has already
cost this estate two weeks.
"""
import json, os, sys, time, urllib.request, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from errlog import err

CONF     = Path(os.environ.get("LOOP_TG_ENV", Path.home() / ".config/claude-dev/telegram.env"))
FIREHOSE = Path(os.environ.get("LOOP_TG_FIREHOSE", Path.home() / ".config/claude-dev/telegram-firehose.env"))
API      = "https://api.telegram.org/bot%s/sendMessage"
DIGESTS  = Path(os.environ.get("LOOP_DIGEST_ROOT", Path.home() / ".local/share/moprox/loop"))
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


def log(text, agent="analyst", kind="note"):
    """Record an event for the daily digest without posting it. Never raises: losing a digest line
    must not cost a cycle, but it is an err, not a silence."""
    try:
        f = DIGESTS / agent / "digest.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "agent": agent, "kind": kind,
                                 "text": str(text)[:4000]}) + "\n")
        return True
    except Exception as exc:
        err(f"digest: could not record a {kind} event for {agent}", exc)
        return False


def send(text, agent="analyst", kind=None):
    """Post one message. Returns True on success; logs at err and returns False otherwise.
    With kind, the post is also recorded for the daily digest."""
    if kind:
        log(text, agent, kind)
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
