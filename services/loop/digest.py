#!/usr/bin/env python3
"""digest.py — one well-written daily summary of what the loop agents have been doing.

Since 2026-11-17 the firehose (odinlake-ai-log) carries only accepted work in real time: verified
findings and landed changes. The operator found a post per cycle, plus every dispute, strike and
preflight gap, excessive. Everything else is recorded by notify.log() into
<state>/<agent>/digest.jsonl, and this script turns the window since its last post into ONE
message: what the analyst has been looking into, what it established, what burndown changed, and
what (if anything) is stuck and needs a human.

The prose is written by `claude -p` on the Max subscription. If that fails, a plain mechanical
digest is posted instead, because a missing digest and a quiet day look identical from outside.

    digest.py            write and post the digest for the window since the last post (max 7 days)
    digest.py --dry      print it, post nothing, leave the cursor alone
    digest.py --plain    skip the model, post the mechanical digest
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import notify
from errlog import err

ROOT   = notify.DIGESTS
CURSOR = ROOT / "digest.cursor"
AGENTS = ("analyst", "burndown")
CLAUDE = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
MAX_WINDOW = 7 * 86400
MATERIAL_MAX = 60000     # chars of raw events handed to the writer

PROMPT = """You are writing the once-a-day summary of the estate's autonomous analyst loop for its
operator, Mikael, who reads it on Telegram. Below are the raw events from the last {hours} hours:
each cycle's closing line, verified findings, landed changes, disputed claims, strikes and
harness faults, for the agents {agents}.

Write the summary in plain text (no markdown, no asterisks, no headings with #, no em dashes),
at most about 2500 characters. Structure:
1. One or two sentences: what the analyst has been looking into, as themes, not cycle numbers.
2. "Established:" the verified findings and landed changes that matter, one short line each,
   most consequential first. Say what is wrong or what changed in operator terms. Skip trivia.
3. "Contested:" only if disputes are substantive, at most three lines.
4. "Needs you:" only things that need a human (unlanded changes, broken tools, credential
   outages, repeated strikes or halts). Omit the section if there are none.
5. A final line with the totals: cycles run, accepted/disputed/rejected, spend.
Be concrete and terse. Do not invent anything that is not in the events. Output only the summary.

EVENTS:
{events}
"""


def load_events(since):
    out = []
    for a in AGENTS:
        f = ROOT / a / "digest.jsonl"
        if not f.exists():
            continue
        for ln in f.read_text().splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("ts", 0) > since:
                out.append(e)
    return sorted(out, key=lambda e: e["ts"])


def render(events):
    lines = []
    for e in events:
        t = time.strftime("%a %H:%M", time.localtime(e["ts"]))
        lines.append(f"[{t} {e.get('agent')} {e.get('kind')}] {e.get('text')}")
    s = "\n\n".join(lines)
    return s if len(s) <= MATERIAL_MAX else "(older events truncated)\n" + s[-MATERIAL_MAX:]


def plain(events, hours):
    by = {}
    for e in events:
        by.setdefault(e.get("kind"), []).append(e)
    cyc = len(by.get("cycle", []))
    head = (f"DAILY DIGEST ({hours}h): {cyc} cycles, {len(by.get('finding', []))} findings, "
            f"{len(by.get('landed', []))} landed, {len(by.get('disputed', []))} disputed.")
    body = []
    for k in ("unlanded", "tool-broken", "creds", "strike", "preflight"):
        for e in by.get(k, []):
            body.append(f"- {k}: " + " ".join(e["text"].split())[:200])
    for e in by.get("cycle", [])[-8:]:
        body.append("- " + " ".join(e["text"].split())[:240])
    return head + ("\n\n" + "\n".join(body) if body else "")


def write(events, hours):
    prompt = PROMPT.format(hours=hours, agents=", ".join(AGENTS), events=render(events))
    try:
        r = subprocess.run([CLAUDE, "-p", "--model", "sonnet"], input=prompt,
                           capture_output=True, text=True, timeout=600)
        text = (r.stdout or "").strip()
        if r.returncode == 0 and len(text) > 80:
            return text.replace(" \u2014 ", ", ").replace("\u2014", "-")   # operator rule: no em dashes
        err("digest: claude -p returned nothing usable; posting the plain digest",
            RuntimeError(f"rc={r.returncode} stderr={(r.stderr or '')[:300]}"))
    except Exception as exc:
        err("digest: claude -p failed; posting the plain digest", exc)
    return None


def main():
    argv = sys.argv[1:]
    dry = "--dry" in argv
    now = int(time.time())
    try:
        since = int(CURSOR.read_text().strip())
    except (OSError, ValueError):
        since = now - 86400
    since = max(since, now - MAX_WINDOW)
    hours = max(1, round((now - since) / 3600))
    events = load_events(since)
    if not events:
        text = (f"DAILY DIGEST ({hours}h): no loop activity recorded. If the agents should have "
                f"run, they are not running.")
    else:
        text = None if "--plain" in argv else write(events, hours)
        text = text or plain(events, hours)
    if dry:
        print(text)
        return 0
    # Chunk on paragraph boundaries; notify clips at its own limit.
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) > 3500:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    parts.append(cur)
    if all(notify.send(p, "analyst") for p in parts):
        CURSOR.write_text(str(now))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
