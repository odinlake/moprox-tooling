#!/usr/bin/env python3
"""Tell the operator when a Courtesy Class place opens up in Akiko's target Sunday session.

WHAT THIS WATCHES. Puddle Ducks gives you a Courtesy Class when you tell them in advance that your
child cannot attend a paid lesson; you then re-book that credit into another class that has a free
place. The target here is the Sunday 15:15 Level 1 & 2 Swim Academy at the Aspire Centre, one slot
after Akiko's own 14:15 class at the same pool. Places come and go as other families mark absences,
so the question "can I book it right now" has to be asked repeatedly — twice a day, per the
operator's instruction (2026-09-05).

TWO SOURCES, BOTH READ-ONLY, AND THAT IS NOT AN ACCIDENT.
  * the pool's public Classes Timetable  — the availability half. Each class is a
    `.timetable__class` carrying `data-available="available"|"unavailable"` and a button reading
    "Book Now" or "Join wait list". Structured markup, no login needed, nothing on the page acts.
  * the account overview                 — the entitlement half: whether a Courtesy Class is already
    booked, and for when.

**NEVER FOLLOW A LINK ON THIS PORTAL.** Its action controls are plain GET anchors with no
confirmation step: "Cancel Courtesy Class" is an `<a href>` that goes straight to
`editcatchupclasses` and lands on a success page. One was followed by hand while mapping the site
(2026-09-05); the booking survived, but only by luck. Every URL this job visits is a page whose
address is written down HERE, and nothing in it clicks. Keep it that way.

WHEN IT SPEAKS. Only on the EDGE — when the answer turns from "no" to "yes". A place that stays open
for a week is one message, not fourteen; the estate has learned that lesson twice already (the
valet's repeated bullets, branch_sync_watch's nightly conflict drip). Silence means "no change",
and the state resets when the place goes away, so the next opening is announced again.

THE GATE, AND WHAT IS DELIBERATELY NOT ASSUMED. A Courtesy Class already booked means the
entitlement is spent — the terms say cancel it before re-booking — so an open place is not
actionable and nothing is sent. What the portal does NOT show anywhere, while one is booked, is a
COUNT of unused credits; that shape has never been observed. So this job does not pretend to know
it: it reports the courtesy panel's own words alongside the alert, and any `catchup` link it finds,
so the first message sent while nothing is booked shows exactly what the entitlement looks like and
the gate can then be tightened against something real instead of a guess.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "forward"))
import errlog                      # noqa: E402  — no silent swallows; see services/lib/errlog.py
import webscout                    # noqa: E402  — the estate's headless browser, over raw MCP
import tg                          # noqa: E402  — the ONLY outbound Telegram path (handle convention)

OVERVIEW = "https://my.puddleducks.com/my-puddle-duck/overview"
# The Aspire Centre's timetable. Read-only; the poolPK/franchiseFK pair came off the pool link on
# the account overview, and is stable per pool.
TIMETABLE = ("https://my.puddleducks.com/pools/individualpooldetails"
             "?poolPK=a72cd7a8-eacf-4108-aa23-fa224abf2761"
             "&franchiseFK=e5e145a5-ecad-4a90-bbaa-71166dc36c2d")
SITE = "my.puddleducks.com"
CHILD = os.environ.get("PD_CHILD", "Akiko")
DAY = os.environ.get("PD_DAY", "Sunday")
LEVEL = os.environ.get("PD_LEVEL", "Level 1 & 2 Swim Academy")
TIME = os.environ.get("PD_TIME", "15:15")
STATE = Path.home() / ".local/share/moprox/puddleducks-courtesy.json"

# Structured, not text-scraped. An earlier innerText parse of this page silently mixed adjacent rows
# together and reported both "available" and "unavailable" for the same class — there are TWO
# parallel Sunday 15:15 Level 1 & 2 classes at this pool, and a line-based reading cannot tell them
# apart. The DOM can: one `.timetable__class` per class, each with its own data-available.
TIMETABLE_JS = """
(() => { const out = [];
  document.querySelectorAll('.timetable__day').forEach(d => {
    const h = d.querySelector('h2'); const day = h ? (h.innerText||'').trim() : '';
    d.querySelectorAll('.timetable__class').forEach(c => {
      const g = s => { const e = c.querySelector(s); return e ? (e.innerText||'').trim() : ''; };
      const b = c.querySelector('a.timetable__class-btn');
      out.push({day: day, level: g('.class-level'), age: g('.class-age'), time: g('.time'),
                avail: c.getAttribute('data-available'),
                btn: b ? (b.innerText||'').trim() : '',
                href: b ? b.getAttribute('href') : ''});
    });
  });
  return JSON.stringify(out); })()
"""

# Reads the courtesy panel WITHOUT assuming which shape it is in. `booked` is the one shape actually
# observed; `block` is whatever the panel says, so an unseen shape arrives as evidence rather than
# as a parse failure, and `links` would carry a "Book Courtesy Class" URL the moment one exists.
OVERVIEW_JS = r"""
(() => {
  const anchor = Array.from(document.querySelectorAll('p'))
    .find(e => /You have booked a Courtesy Class for/i.test(e.innerText||''));
  // The SMALLEST block that mentions a courtesy class and carries a detail (a date, or the words
  // an entitlement notice would use). Walking up from the anchor instead swept in the whole
  // dashboard -- the recommend-a-friend blurb, the current class, the next payment -- which is
  // 600 characters of noise around the one line that matters.
  const cands = Array.from(document.querySelectorAll('div')).filter(d => {
    const t = d.innerText || '';
    return /courtesy class/i.test(t) && t.length >= 40 &&
           /\d{2}-\d{2}-\d{4}|available|to book/i.test(t);
  }).sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);
  return JSON.stringify({
    booked: !!anchor,
    block: (cands[0] ? cands[0].innerText : '').replace(/\s+/g,' ').trim().slice(0, 400),
    links: Array.from(document.querySelectorAll('a[href*="catchup" i]'))
             .map(a => ((a.innerText||'').trim()) + ' => ' + a.getAttribute('href'))
  }); })()
"""


def norm(s):
    return " ".join((s or "").split()).lower()


def target_rows(rows):
    """The classes this job is about: right day, right level, right start time.

    There are two of them at 15:15, taught in parallel, and they fill independently — so this is a
    LIST, and a place in either one is a place.
    """
    return [r for r in rows
            if norm(r.get("day")) == norm(DAY)
            and norm(LEVEL) in norm(r.get("level"))
            and norm(r.get("time")).startswith(norm(TIME))]


def look():
    """One browser visit: the timetable and the account overview. Returns (rows, courtesy)."""
    sess = json.loads(webscout.call("open_session", {"site": SITE, "url": TIMETABLE}))["session"]
    try:
        rows = webscout.js(sess, TIMETABLE_JS)
        webscout.call("goto", {"session": sess, "url": OVERVIEW})
        courtesy = webscout.js(sess, OVERVIEW_JS)
    finally:
        try:
            webscout.call("close", {"session": sess})
        except Exception as e:
            # Not fatal — webscout reaps idle tabs after 900 s — but never silent.
            errlog.warn("courtesy_watch: closing the browser session failed: %s" % e)
    return rows, courtesy


def message(open_rows, courtesy):
    lines = ["**%s %s %s** at the Aspire Centre has a place open." % (DAY, TIME, LEVEL)]
    if len(open_rows) > 1:
        lines.append("Both parallel classes are showing places.")
    lines.append("Book it from the pool timetable: %s" % TIMETABLE)
    block = (courtesy.get("block") or "").strip()
    if block:
        lines.append("")
        lines.append("Courtesy panel says: _%s_" % block[:300])
    for ln in courtesy.get("links") or []:
        lines.append("Link on the page: `%s`" % ln[:160])
    lines.append("")
    lines.append("(Nothing was clicked — this only reads. %s's own class is %s 14:15.)"
                 % (CHILD, DAY))
    return "\n".join(lines)


def main():
    try:
        rows, courtesy = look()
    except Exception as e:
        errlog.err("courtesy_watch: reading the Puddle Ducks portal", e)
        return 1
    if not rows:
        # An empty timetable means the page changed shape, not that every class is full. Saying
        # "no places" here would be the localnews failure again: a plausible value standing in for
        # a broken read.
        errlog.err("courtesy_watch: the timetable parsed to ZERO classes — the page markup has "
                   "probably changed (%s). Not treating that as 'no availability'." % TIMETABLE)
        return 1
    targets = target_rows(rows)
    if not targets:
        errlog.err("courtesy_watch: no %s %s %s row in a timetable of %d classes — the class may "
                   "have moved or been renamed; check before trusting this job again."
                   % (DAY, TIME, LEVEL, len(rows)))
        return 1

    open_rows = [r for r in targets if norm(r.get("avail")) == "available"]
    booked = bool(courtesy.get("booked"))
    alertable = bool(open_rows) and not booked

    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception as e:
        errlog.skip("courtesy_watch: reading state", e)
        state = {}
    was = bool(state.get("alertable"))

    if alertable and not was:
        try:
            tg.send(message(open_rows, courtesy), agent="puddleducks")
            print("alerted: %d of %d target classes open" % (len(open_rows), len(targets)))
        except Exception as e:
            errlog.err("courtesy_watch: sending the Telegram alert", e)
            return 1                # do NOT record the edge; retry next run rather than lose it
    else:
        print("no alert: open=%d/%d booked=%s (was alertable=%s)"
              % (len(open_rows), len(targets), booked, was))

    state.update({"alertable": alertable, "open": len(open_rows), "targets": len(targets),
                  "booked": booked, "courtesy_block": (courtesy.get("block") or "")[:600]})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
