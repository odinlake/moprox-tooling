#!/usr/bin/env python3
"""incident_digest.py — one Telegram message a day about the estate's open incidents, or silence.

The incident queue was a pull surface with nothing pulling: no timer, service or digest anywhere
read it, and the analyst asked for it six times in 72 cycles. Detections were landing in a room with
nobody in it — unit failures, stale data lanes, and (since 2026-08-13) services logging errors below
err level. This is the reader.

SINCE 2026-10-28 THIS NO LONGER POSTS INCIDENTS DAILY. Operator: "The messages are all but useless
... I don't think this is the level at which I want to be involved at all." Every new incident now
goes to the `triage` agent (incident_triage.py, hourly), which categorises it, acks noise, files
fixes for burndown and posts to Telegram itself only when the operator's input is needed. What is
left here: the watchdog-overdue alert, and the weekly roundup, built from triage's categories.

History, kept for the reasoning. THE BAR IS DELIBERATELY HIGH. A daily message that always arrives becomes wallpaper, and wallpaper
is how a six-day outage goes unread. Since 2026-10-14 (operator: "rather not see anything unless
there's something new") the daily message carries only:

  - incidents that are NEW (never seen before, and recent), and
  - the watchdog going overdue, once per outage.

Everything known, chronic or growing goes into ONE weekly roundup of what is still recurring and
unaddressed, plus the issues waiting on the operator. That roundup also keeps silence meaningful:
a week with no message at all means the digest itself is dead.

    incident_digest.py          evaluate and post if material
    incident_digest.py --dry    print what it would post (or why not); post nothing
    incident_digest.py --force  post regardless of the bar (for testing the path)
    incident_digest.py --weekly include the weekly roundup now
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog
import tg

URL = os.environ.get("INCIDENT_URL", "http://logview.lan:8016/api/incidents?window=14d&limit=40")
ISSUES_URL = os.environ.get("ISSUES_URL", "http://logview.lan:8016/api/issues?status=open&limit=50")
STATE = Path(os.environ.get("INCIDENT_DIGEST_STATE",
                            Path.home() / ".local/state/incident-digest.json"))
TOP_N = int(os.environ.get("INCIDENT_TOP_N", "5"))
FRESH_H = 24
# A never-reported incident whose last occurrence is days old is not urgent by definition, and
# reporting it as "new" would make the FIRST digest a dump of the whole 14-day window. It stays
# visible in mo.lan/logs; it just does not earn a message. 48 h, so a single failure three days ago
# stays out of the daily ping.
NEW_MAX_AGE_H = float(os.environ.get("INCIDENT_NEW_MAX_AGE_H", "48"))
# How long an incident key is remembered after it was last seen. `seen` used to be rebuilt from the
# 14-day queue window alone, so anything quiet for two weeks was forgotten and its next occurrence
# announced as "new": 34 of the 64 distinct keys posted 2026-08-14..10-28 were repeats, one of them
# seven times (operator, 2026-10-28: "too much noise"). A recurrence of something known is not news;
# while it keeps recurring the weekly roundup carries it.
FORGET_DAYS = float(os.environ.get("INCIDENT_FORGET_DAYS", "90"))
TRIAGE_LEDGER = Path(os.environ.get("TRIAGE_LEDGER", Path.home() / ".local/state/incident-triage.json"))
WEEK_URL = os.environ.get("INCIDENT_WEEK_URL",
                          "http://logview.lan:8016/api/incidents?window=7d&limit=500&include_acked=1")
TWO_WEEK_URL = os.environ.get("INCIDENT_2WEEK_URL",
                              "http://logview.lan:8016/api/incidents?window=14d&limit=500&include_acked=1")
# The weekly roundup's period.
LIVENESS_DAYS = float(os.environ.get("INCIDENT_LIVENESS_DAYS", "7"))


def send(text):
    """Post through the estate's ONE transport, tagged #watchdog.

    An earlier version of this file rolled its own urllib post — which is how the first digest went
    out with no #handle and no entry in the conversation log, so the operator could not tell which
    agent had spoken. tg.send() is the single place that enforces the handle convention, converts
    markdown, falls back to plain text on a MarkdownV2 error, and records the message in convo.
    Duplicating a transport duplicates every one of those decisions, badly."""
    try:
        return bool(tg.send(text, agent="watchdog"))
    except Exception as exc:
        errlog.err("incident-digest: telegram post failed — this digest reached nobody", exc)
        return False


def fetch():
    with urllib.request.urlopen(URL, timeout=20) as r:
        return json.loads(r.read().decode()).get("incidents") or []


def open_issues():
    """Open issues are context, never a reason to post: an issue waiting on the operator is not
    news every morning. Counted in a digest that goes out anyway, so the two halves of the board
    are never read apart."""
    try:
        with urllib.request.urlopen(ISSUES_URL, timeout=15) as r:
            return json.loads(r.read().decode()).get("issues") or []
    except Exception as exc:
        errlog.err("incident-digest: could not read the issue list", exc)
        return []


def watchdog_age():
    """Hours since the watchdog last ran, from the fleet journal. None if it has not run at all.

    Nothing else can report this: the watchdog is what notices a dead checker, so its own death is
    invisible to every other check. This digest is the one thing that speaks unprompted, which makes
    it the only place the answer can land."""
    try:
        u = ("http://logview.lan:8016/api/search?hosts=monitoring&units=watchdog.service"
             "&since=2026-01-01&limit=1")
        with urllib.request.urlopen(u, timeout=15) as r:
            rows = json.loads(r.read().decode()).get("rows") or []
        if not rows:
            return None
        from datetime import datetime
        ts = datetime.fromisoformat(rows[0]["ts"]).timestamp()
        return (time.time() - ts) / 3600.0
    except Exception as exc:
        errlog.warn(f"incident-digest: could not read the watchdog's last run ({exc})")
        return None


def key(i):
    """A lane is one thing, whichever box noticed it. The freshness checker runs on claude-dev and
    the analyst's loop box runs it too, so keying lanes by host reported the same stale lane twice
    (2026-10-11: claude-loop/lane-statements-amex 'new' four days after claude-dev's copy)."""
    unit = i.get("unit", "?")
    return unit if unit.startswith("lane-") else f"{i.get('host', '?')}/{unit}"


def merge(rows):
    """Collapse rows that share a key: worst score, latest occurrence, biggest count."""
    out = {}
    for i in rows:
        k = key(i)
        o = out.get(k)
        if o is None:
            out[k] = dict(i)
            continue
        if int(i.get("score") or 0) > int(o.get("score") or 0):
            o["score"] = i.get("score")
        if age_h(i) < age_h(o):
            o["age_hours"], o["last_message"] = i.get("age_hours"), i.get("last_message")
        o["count"] = max(int(o.get("count") or 0), int(i.get("count") or 0))
        o["first_seen"] = min(int(o.get("first_seen") or 0) or 10**20, int(i.get("first_seen") or 0) or 10**20)
    return list(out.values())


def ago(i):
    h = age_h(i)
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def open_days(i, now):
    """Days since the incident first appeared (logview stamps first_seen in microseconds)."""
    try:
        return max(0.0, (now - int(i.get("first_seen")) / 1e6) / 86400)
    except (TypeError, ValueError):
        return 0.0


def age_h(i):
    """Hours since last seen; the API computes it, so trust its clock rather than ours."""
    try:
        return float(i.get("age_hours"))
    except (TypeError, ValueError):
        return 1e9


def _msg(i):
    """The incident's `last_message` as text.

    logview's /api/incidents returns this field in journald's BYTE-ARRAY form for some rows: a
    JSON list of 0..255 ints rather than a string. Measured on the live 14 d queue (2026-11-04,
    41 rows), two arrive as 300-int lists and both decode cleanly as UTF-8, while a same-host
    sibling of the identical 300-byte shape arrives as a str -- the form is not determined by the
    content, so a reader cannot tell in advance which rows carry it.

    `str()` on such a list yields "[99, 108, 97, ...]", i.e. the message re-rendered as decimal
    numbers, which then loses two thirds of itself to the caller's truncation. The same expression
    stood at the other reader of this field (incident_triage._msg).
    """
    v = i.get("last_message")
    if isinstance(v, list):
        try:
            return bytes(v).decode("utf-8", "replace")
        except (TypeError, ValueError) as exc:
            # A list this cannot read as bytes is a shape nobody has seen: say so at err level and
            # fall back to the old rendering rather than dropping the only description there is.
            errlog.err("incident %s: last_message is a list of non-bytes" % key(i), exc)
            return str(v)
    return str(v or "")


def line(i):
    """One incident, one line plus a short gist (operator, 2026-10-28: concise)."""
    # A log-errors message is boilerplate ("N error-shaped lines matching ... between ..."): no gist.
    detail = "" if i.get("kind") == "log-errors" else " ".join(_msg(i).split())
    out = f"• {key(i)} ({i.get('kind', '?')}), {ago(i)}"
    return out + (f"\n  {detail[:100]}{'…' if len(detail) > 100 else ''}" if detail else "")


TITLE_MAX = 240


def issue_title(i):
    """An issue's whole title, and visibly cut if it is ever absurd.

    This used to be `title[:80]` (and `[:70]` in the daily digest before that), which cut EVERY title
    the board has ever held: lengths 77/88/89. All 33 deliveries of i-20260814-154101 went out as
    "...since 2026-08-06 — is it", dropping the question's object ("the app or the pad?"), and 0 of 33
    carried the whole line (moprox-memory/incident-digest-title-slice-cuts-every-issue). A title is one
    line an agent wrote to be read; the slice saved nothing and cost the sentence. The bound here is a
    sanity limit well above anything the board has carried, not a layout width, and it says when it
    bites — a silent cut is how this went unnoticed for two months."""
    t = (i.get("title") or i.get("id") or "?").strip().replace("\n", " ")
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX] + "… (title cut at %d chars)" % TITLE_MAX


def _counts(url):
    """key -> (occurrences in the window, kind)."""
    with urllib.request.urlopen(url, timeout=30) as r:
        out = {}
        for i in json.loads(r.read().decode()).get("incidents") or []:
            n, _ = out.get(key(i), (0, ""))
            out[key(i)] = (n + int(i.get("count") or 0), i.get("kind", ""))
        return out


# The roundup's priority tiers, highest first (operator, 2026-11-02): colour by priority, label by
# category, concise. The triage agent assigns each category a tier (incident_triage.TIERS); a
# category it has not tiered yet falls back on its disposition.
TIERS = [("new", "🔴", "New"), ("unexplained", "🟠", "Unexplained"), ("fixing", "🟡", "Fixing"),
         ("standing", "🔵", "Long-standing"), ("explained", "⚪", "Explained")]
BY_DISPOSITION = {"operator": "new", "known": "unexplained", "actionable": "fixing", "noise": "explained"}
# An operator issue with no category behind it this week is long-standing once it is this old.
STANDING_DAYS = 30


def tier_of(meta):
    t = meta.get("tier")
    return t if t in {k for k, _, _ in TIERS} else BY_DISPOSITION.get(meta.get("disposition"), "unexplained")


def gist(ex, n=80):
    ex = " ".join(str(ex or "").split())
    return ex if len(ex) <= n else ex[:n] + "…"


def weekly(now):
    """The weekly roundup, one line per triage category, ranked and coloured by priority.

    Operator, 2026-10-28: "briefly tell me what the categories and example errors are along with
    counts so I know what is or isn't piling up". Then 2026-11-02: emphasise what is new and
    genuinely unexplained, put the unexplained-but-minor in the middle, and push down both what has
    an explanation (a lane stale only because he was away) and what is long-standing (he has chosen
    not to act on it, but has not closed it). Issues waiting on him are folded into their category's
    line as "on you" rather than listed a second time."""
    try:
        led = json.loads(TRIAGE_LEDGER.read_text())
    except (FileNotFoundError, ValueError):
        led = {"keys": {}, "categories": {}}
    try:
        wk, two = _counts(WEEK_URL), _counts(TWO_WEEK_URL)
    except Exception as exc:
        errlog.err("incident-digest: could not read weekly counts", exc)
        wk, two = {}, {}
    cats = {}
    for k, (n, kind) in wk.items():
        t = led["keys"].get(k)
        e = cats.setdefault(t["category"] if t else "not yet triaged",
                            {"now": 0, "prev": 0, "lane": True, "keys": set()})
        e["now"] += n
        e["lane"] = e["lane"] and kind == "lane-stale"
        e["keys"].add(k)
    for k, (n, _) in two.items():
        t = led["keys"].get(k)
        e = cats.get(t["category"] if t else "not yet triaged")
        if e is not None:
            e["prev"] += max(0, n - wk.get(k, (0, ""))[0])

    # Each operator issue rides on the category its incident was triaged into; one with no category
    # active this week gets a line of its own.
    iss = open_issues()
    mine = [i for i in iss if (i.get("owner") or "") == "operator"]
    orphans = []
    for i in mine:
        inc = i.get("incident") or ""
        unit = inc.partition("/")[2]
        # An issue names its incident host/unit; the ledger keys a lane by unit alone (see key()).
        t = led["keys"].get(unit if unit.startswith("lane-") else inc)
        if t and t["category"] in cats:
            cats[t["category"]]["on_you"] = True
        else:
            orphans.append(i)

    rows = []                                   # (tier, sort weight, name, text)
    for c, v in cats.items():
        if not v["now"]:
            continue
        meta = led["categories"].get(c, {})
        tier = tier_of(meta) if c != "not yet triaged" else "unexplained"
        if v["lane"]:
            size = "ongoing" if v["prev"] else "new this wk"
        else:
            trend = "" if not v["prev"] else (" ↑" if v["now"] > v["prev"] * 1.25
                                              else " ↓" if v["now"] < v["prev"] * 0.75 else "")
            size = f"{v['now']}/wk{trend}"
        text = c + ("" if size == "new this wk" and tier == "new" else f", {size}") \
            + (", on you" if v.get("on_you") else "")
        # A stale lane's example only restates "silent since"; a log line is worth quoting.
        if tier in ("new", "unexplained") and not v["lane"] and meta.get("example"):
            text += f"\n   e.g. {gist(meta['example'])}"
        rows.append((tier, -v["now"], c, text))
    for i in orphans:
        try:
            from datetime import datetime
            age = (now - datetime.fromisoformat(i.get("created")).timestamp()) / 86400
        except (TypeError, ValueError):
            age = 0
        t = issue_title(i).replace(" — ", ": ")     # operator: no em dashes in what he reads
        rows.append(("standing" if age >= STANDING_DAYS else "new", 0, t, f"{t}, on you"))

    lines = ["🔧 Weekly estate roundup, by priority"]
    for tier, dot, label in TIERS:
        mine_rows = sorted((r for r in rows if r[0] == tier), key=lambda r: r[1])
        if not mine_rows:
            continue
        if tier == "explained":
            # Not real problems: one line, names only.
            lines.append(f"{dot} {label}: " + "; ".join(name.split(",")[0] for _, _, name, _ in mine_rows))
            continue
        for _, _, _, text in mine_rows:
            lines.append(f"{dot} {label}: {text}")
    if not rows:
        lines.append("Nothing logged. (Sent so a silent week stays meaningful.)")
    agents = len(iss) - len(mine)
    if agents:
        lines.append(f"\n{agents} issue(s) with the agents: mo.lan/issues")
    return lines


def main():
    dry = "--dry" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    want_weekly = "--weekly" in sys.argv[1:]
    now = time.time()

    try:
        rows = merge(fetch())
    except Exception as exc:
        errlog.err(f"incident-digest: could not read the queue at {URL}", exc)
        return 1

    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    # key -> last time the queue showed it (epoch s). Before 2026-10-28 the value was a count; a
    # count carries no date, so treat those as seen now and let them age out from here.
    seen = {k: (v if isinstance(v, float) else now) for k, v in state.get("seen", {}).items()}
    # Keys used to carry the host for lanes too; carry those forward so the switch to host-free
    # lane keys does not announce every chronic lane as "new" on the first run.
    for k in list(seen):
        host, _, unit = k.partition("/")
        if unit.startswith("lane-"):
            seen[unit] = max(seen.get(unit, 0.0), seen.pop(k))
    reported = state.get("reported", {})    # key -> when it last earned a message

    # THE DAILY BAR IS "NEW", AND NOTHING ELSE (operator, 2026-10-14: "rather not see anything
    # unless there's something new"). The old bar also posted an incident whose count had doubled
    # and a high-score one weekly; for a stale lane the count grows every single check, so the
    # same lanes came back as "grew 20->44" day after day. Known, unchanged-or-growing items
    # belong to the weekly roundup.
    material, quiet = [], []
    for i in rows:
        k = key(i)
        if k not in seen and age_h(i) <= NEW_MAX_AGE_H:
            material.append((i, "new"))
        else:
            quiet.append(i)

    material.sort(key=lambda t: (-int(t[0].get("score") or 0), -int(t[0].get("count") or 0)))
    top, overflow = material[:TOP_N], material[TOP_N:]
    # What the cap drops must not also be swallowed: rebuilding `seen` from every row would retire
    # a truncated incident's "new" status without it ever having earned a message. Hold it over.
    held = {key(i) for i, _ in overflow}
    next_seen = {k: t for k, t in seen.items() if now - t < FORGET_DAYS * 86400}
    next_seen.update({key(i): float(now) for i in rows if key(i) not in held})

    # The watchdog's own death is the one thing nothing else can report, so it is news, but only
    # once: flag it when it goes overdue, and again only after it has recovered and died again.
    wd = watchdog_age()
    wd_down = wd is None or wd > 2
    wd_news = wd_down and not state.get("wd_alerted")

    # Weekly on Monday's run. A missed Monday (box down) catches up the next day; the very first run
    # only starts the clock, so deploying this does not post a roundup mid-week.
    last_weekly = float(state.get("last_weekly") or 0)
    if not last_weekly:
        state["last_weekly"] = last_weekly = now
    since_weekly = (now - last_weekly) / 86400.0
    weekly_due = want_weekly or (time.localtime(now).tm_wday == 0 and since_weekly > 3) \
        or since_weekly >= LIVENESS_DAYS + 1

    lines = []
    # Incidents are triage's now (see the docstring); the daily post carries only the watchdog.
    top, overflow = [], []
    if force:
        lines.append(f"🔧 New: {len(top)} incident(s)")
        for i, _ in top:
            lines.append(line(i))
        if overflow:
            lines.append(f"+{len(overflow)} more new, tomorrow")
    if wd_news:
        lines.append("\nwatchdog: " + ("no run found in the last 7 days, the checker-checker is DOWN"
                                         if wd is None else f"last ran {wd:.0f} h ago, it should run every 30 min"))
    if weekly_due:
        if lines:
            lines.append("")
        lines.extend(weekly(now))

    state.update({"last_run": now, "reported": reported, "seen": next_seen,
                  "wd_alerted": bool(wd_down)})
    if not lines:
        # Deliberate silence. Recorded, so a long quiet streak is visible in the state file rather
        # than being indistinguishable from a broken timer.
        state["quiet_runs"] = int(state.get("quiet_runs", 0)) + 1
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        print(f"incident-digest: silent, {len(rows)} open, nothing new "
              f"(quiet runs: {state['quiet_runs']})")
        return 0

    lines.append("\nhttps://mo.lan/incidents/")
    text = "\n".join(lines)[:3900]
    if dry:
        print(text)
        return 0
    if not send(text):
        return 1
    reported.update({key(i): now for i, _ in top})
    state.update({"last_posted": now, "quiet_runs": 0})
    if weekly_due:
        state["last_weekly"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(f"incident-digest: posted {len(top)} new, {len(overflow)} held over, "
          f"weekly={'yes' if weekly_due else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
