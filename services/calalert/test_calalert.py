#!/usr/bin/env python3
"""calalert's decisions without Google, claude or Telegram.

    python3 services/calalert/test_calalert.py
"""
import datetime, os, sys, tempfile, types
from pathlib import Path

os.environ["CALALERT_STATE"] = str(Path(tempfile.mkdtemp(prefix="calalert-")) / "state.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sent = []
sys.modules["tg"] = types.SimpleNamespace(send=lambda text, agent=None: sent.append((agent, text)))
import calalert as c  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)
ok = []


def ev(i, mins, title="GP appointment", **kw):
    st = (NOW + datetime.timedelta(minutes=mins)).replace(microsecond=0)
    e = {"id": i, "summary": title, "start": {"dateTime": st.isoformat()},
         "end": {"dateTime": (st + datetime.timedelta(minutes=30)).isoformat()}}
    e.update(kw)
    return e


def check(name, cond):
    assert cond, name
    ok.append(name)


# 1. eligibility
check("timed one-off is eligible", c.eligible(ev("a", 60)) is None)
check("all-day skipped", c.eligible({"id": "b", "start": {"date": "2026-10-02"}}) == "all-day")
check("recurring skipped", c.eligible(ev("c", 60, recurringEventId="x")) == "recurring")
check("declined skipped", c.eligible(ev("d", 60, attendees=[{"self": True, "responseStatus": "declined"}])) == "declined")
check("focus time skipped", c.eligible(ev("e", 60, eventType="focusTime")) == "focusTime")
check("cancelled skipped", c.eligible(ev("f", 60, status="cancelled")) == "cancelled")

# 2. run_once: due window, dedupe, unimportant, reschedule, classifier failure
calendar = []
c.service = lambda: None
c.events = lambda svc, t0, t1: [e for e in calendar if t0 < c.start_of(e) <= t1 + datetime.timedelta(hours=1)]
verdicts = {"GP appointment": {"important": True, "kind": "medical", "reason": "doctor"},
            "Buy: milk": {"important": False, "kind": "other", "reason": "list"}}
judged = []


def judge(e):
    judged.append(e["summary"])
    return verdicts.get(e["summary"]) or {"important": True, "kind": "other", "reason": "x", "failed": True}


c.judge = judge
calendar[:] = [ev("gp", 115), ev("buy", 60, "Buy: milk"), ev("later", 125), ev("weird", 30, "???")]
c.run_once()
titles = [t for _, t in sent]
check("important due event alerted", any("GP appointment" in t for t in titles))
check("alerts use #reminder handle", all(a == "reminder" for a, _ in sent))
check("unimportant not alerted", not any("Buy: milk" in t for t in titles))
check("event beyond 2 h not yet alerted", len([t for t in titles if "GP appointment" in t]) == 1)
check("classifier failure still alerts", any("???" in t and "sent to be safe" in t for t in titles))
n = len(sent)
c.run_once()
check("no repeat alert on next run", len(sent) == n)
check("verdict cached; re-judged only after reschedule", judged.count("GP appointment") == 1 and judged.count("???") == 1)
check("failed verdict not cached", not any("???" in k for k in c.load()["verdict"]))
moved = ev("gp", 90)                       # same event id, new start time
calendar[0] = moved
c.run_once()
check("rescheduled event alerts again", len(sent) == n + 1 and judged.count("GP appointment") == 2)

# 3. formatting
t = c.fmt(ev("x", 120, location="The Shard"), {"kind": "medical"}, NOW)
check("one quiet headline", t.startswith("🔔 *GP appointment* at *") and t.count("🔔") == 1)
check("countdown and location on line 2", t.splitlines()[1] == "In 2 hours · The Shard")

# 4. prune
s = {"sent": {"old|2020-01-01T10:00:00+00:00": "x", ev("n", 60)["id"] + "|" + ev("n", 60)["start"]["dateTime"]: "y"},
     "verdict": {}}
c.prune(s, NOW)
check("prune drops old, keeps fresh", len(s["sent"]) == 1)

print("ok: %d checks" % len(ok))
