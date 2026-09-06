#!/usr/bin/env python3
"""Watch the family's Puddle Ducks account: courtesy-class places, and the children's swim criteria.

TWO WATCHES, ONE VISIT. Both answers come off pages this job already has to load, so they share a
browser session and, when both have something to say, a single Telegram message.

1. COURTESY PLACES. Puddle Ducks gives you a Courtesy Class when you tell them in advance that your
child cannot attend a paid lesson; you then re-book that credit into another class that has a free
place. The target here is the Sunday 15:15 Level 1 & 2 Swim Academy at the Aspire Centre, one slot
after Akiko's own 14:15 class at the same pool. Places come and go as other families mark absences,
so the question "can I book it right now" has to be asked repeatedly — twice a day, per the
operator's instruction (2026-09-05).

2. SWIM CRITERIA. Each child's "Criteria to move to next class level" list, and which of them the
teacher has ticked. Added on the operator's instruction (2026-09-06) after the list turned out to
hold fewer ticks than he expected — "I guess they reset them at some point" — which is exactly the
kind of change nobody sees, because it happens silently in a collapsed panel nobody opens. So this
reports movement in BOTH directions: newly ticked, and un-ticked.

TWO SOURCES, BOTH READ-ONLY, AND THAT IS NOT AN ACCIDENT.
  * the pool's public Classes Timetable  — the availability half. Each class is a
    `.timetable__class` carrying `data-available="available"|"unavailable"` and a button reading
    "Book Now" or "Join wait list". Structured markup, no login needed, nothing on the page acts.
  * the account overview                 — the entitlement half: whether a Courtesy Class is already
    booked, and for when.
  * My Children                          — the criteria. They are NOT a page of their own: each
    child has a `#my-criteria-<childPersonPK>` bootstrap collapse panel, present in the DOM whether
    or not it is expanded, so it reads without a click. A criterion is a `p.mypuddle-body-text`
    holding a code, a description and one 16x16 icon with NO alt text and no legend on the page.
    The filename is the only signal, and both PNGs were rendered to be sure:
    `dialog_ok_apply_16x16.png` is a TICK, `dialog_close_16x16.png` is an X.

**NEVER FOLLOW A LINK ON THIS PORTAL.** Its action controls are plain GET anchors with no
confirmation step: "Cancel Courtesy Class" is an `<a href>` that goes straight to
`editcatchupclasses` and lands on a success page. One was followed by hand while mapping the site
(2026-09-05); the booking survived, but only by luck. Every URL this job visits is a page whose
address is written down HERE, and nothing in it clicks. Keep it that way.

WHEN IT SPEAKS. Only on a CHANGE. For places, the edge from "no" to "yes"; for criteria, a diff
against the last snapshot. A place that stays open for a week is one message, not fourteen; the estate has learned that lesson twice already (the
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
CHILDREN = "https://my.puddleducks.com/mypuddleduck/mychildren"
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
STATE = Path.home() / ".local/share/moprox/puddleducks-watch.json"

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


# One entry per child who HAS a criteria panel — a child with no current class (Yuko, 2026-09-06)
# has none, and that is a fact about enrolment, not a parse failure. The child's name comes from the
# "Current class for <name>" heading in the same `.my-children-div` block, so the report can name
# them without this file hard-coding who is in the family.
CRITERIA_JS = r"""
(() => {
  const TICK = 'dialog_ok_apply';
  const out = [];
  document.querySelectorAll('[id^="my-criteria-"]').forEach(panel => {
    const box = panel.closest('.my-children-div') || panel.parentElement;
    const head = box ? Array.from(box.querySelectorAll('p'))
                          .find(e => /Current class for/i.test(e.innerText||'')) : null;
    const strong = head ? head.querySelector('strong') : null;
    const items = [];
    panel.querySelectorAll('p.mypuddle-body-text').forEach(p => {
      const txt = (p.innerText||'').replace(/\s+/g,' ').trim();
      const img = p.querySelector('img');
      if (!txt || !img) return;                 // the panel's own heading has no icon
      const src = (img.getAttribute('src')||'');
      items.push({txt: txt, achieved: src.indexOf(TICK) !== -1});
    });
    out.push({pk: panel.id.replace('my-criteria-',''),
              child: strong ? (strong.innerText||'').trim()
                            : (head ? (head.innerText||'').replace(/Current class for/i,'').trim() : ''),
              items: items});
  });
  return JSON.stringify(out);
})()
"""


def split_code(txt):
    """"L1.10 Balance independently on a sea horse" -> ("L1.10", "Balance independently...").

    The code is the stable identity; the wording is what gets shown. Puddle Ducks' own ordering is a
    string sort, so L1.10 sits between L1.1 and L1.2 — this file never relies on that order.
    """
    parts = (txt or "").split(None, 1)
    if len(parts) == 2 and any(ch.isdigit() for ch in parts[0]):
        return parts[0], parts[1]
    return txt, txt


def criteria_map(panels):
    """{pk: {"child": name, "items": {code: {"desc":…, "achieved": bool}}}}"""
    out = {}
    for pnl in panels or []:
        items = {}
        for it in pnl.get("items") or []:
            code, desc = split_code(it.get("txt"))
            items[code] = {"desc": desc, "achieved": bool(it.get("achieved"))}
        out[pnl.get("pk")] = {"child": pnl.get("child") or "?", "items": items}
    return out


def criteria_lines(now, before):
    """Human-readable diff. Empty list means nothing changed.

    Reports movement in BOTH directions on purpose. An un-tick is the case that prompted this watch
    and is the one nobody would otherwise notice — a newly-ticked skill gets mentioned poolside, a
    quietly cleared one does not.
    """
    lines = []
    for pk, cur in sorted(now.items(), key=lambda kv: kv[1]["child"]):
        old = (before or {}).get(pk)
        name = cur["child"]
        items, prev = cur["items"], (old or {}).get("items") or {}
        if not old:
            continue                        # first sighting of this child: seeded, not announced
        gained = [c for c in items if items[c]["achieved"]
                  and c in prev and not prev[c]["achieved"]]
        lost = [c for c in items if not items[c]["achieved"]
                and c in prev and prev[c]["achieved"]]
        added = [c for c in items if c not in prev]
        gone = [c for c in prev if c not in items]
        if not (gained or lost or added or gone):
            continue
        done = sum(1 for c in items if items[c]["achieved"])
        if added and gone and not (set(items) & set(prev)):
            # A level change swaps the WHOLE list; saying "18 added, 20 removed" would bury it. The
            # test is that old and new share no code at all — an earlier version asked whether the
            # ADDED codes were absent from the previous set, which is true by definition, so one
            # criterion being renamed was announced as the child changing level.
            lines.append("**%s — criteria list replaced**: %d out, %d in, %d of %d ticked."
                         % (name, len(gone), len(added), done, len(items)))
        else:
            lines.append("**%s — swim criteria changed** (%d of %d ticked)"
                         % (name, done, len(items)))
            for label, codes in (("Newly ticked", gained), ("Un-ticked", lost),
                                 ("New criteria", added), ("Removed", gone)):
                if not codes:
                    continue
                src = items if label != "Removed" else prev
                lines.append("%s: %s" % (label, "; ".join(
                    "%s %s" % (c, src[c]["desc"]) for c in sorted(codes))))
    return lines


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
    """One browser visit, three pages. Returns (timetable rows, courtesy panel, criteria panels)."""
    sess = json.loads(webscout.call("open_session", {"site": SITE, "url": TIMETABLE}))["session"]
    try:
        rows = webscout.js(sess, TIMETABLE_JS)
        webscout.call("goto", {"session": sess, "url": OVERVIEW})
        courtesy = webscout.js(sess, OVERVIEW_JS)
        webscout.call("goto", {"session": sess, "url": CHILDREN})
        panels = webscout.js(sess, CRITERIA_JS)
    finally:
        try:
            webscout.call("close", {"session": sess})
        except Exception as e:
            # Not fatal — webscout reaps idle tabs after 900 s — but never silent.
            errlog.warn("puddleducks_watch: closing the browser session failed: %s" % e)
    return rows, courtesy, panels


def courtesy_lines(open_rows, courtesy):
    lines = ["**%s %s %s** at the Aspire Centre has a place open." % (DAY, TIME, LEVEL)]
    if len(open_rows) > 1:
        lines.append("Both parallel classes are showing places.")
    lines.append("Book it from the pool timetable: %s" % TIMETABLE)
    block = (courtesy.get("block") or "").strip()
    if block:
        lines.append("Courtesy panel says: _%s_" % block[:300])
    for ln in courtesy.get("links") or []:
        lines.append("Link on the page: `%s`" % ln[:160])
    return lines


def main():
    try:
        rows, courtesy, panels = look()
    except Exception as e:
        errlog.err("puddleducks_watch: reading the Puddle Ducks portal", e)
        return 1
    if not rows:
        # An empty timetable means the page changed shape, not that every class is full. Saying
        # "no places" here would be the localnews failure again: a plausible value standing in for
        # a broken read.
        errlog.err("puddleducks_watch: the timetable parsed to ZERO classes — the page markup has "
                   "probably changed (%s). Not treating that as 'no availability'." % TIMETABLE)
        return 1
    targets = target_rows(rows)
    if not targets:
        errlog.err("puddleducks_watch: no %s %s %s row in a timetable of %d classes — the class may "
                   "have moved or been renamed; check before trusting this job again."
                   % (DAY, TIME, LEVEL, len(rows)))
        return 1

    open_rows = [r for r in targets if norm(r.get("avail")) == "available"]
    booked = bool(courtesy.get("booked"))
    alertable = bool(open_rows) and not booked

    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception as e:
        errlog.skip("puddleducks_watch: reading state", e)
        state = {}
    was = bool(state.get("alertable"))

    now = criteria_map(panels)
    before = state.get("criteria") or {}
    # A panel that parses to ZERO criteria is a broken read, not a cleared child. Publishing that
    # diff would announce twenty un-ticks that never happened AND overwrite the only record of what
    # was there — the same shape as the timetable guard above, and worth the same refusal.
    empty = [pk for pk, c in now.items() if not c["items"]]
    if empty:
        errlog.err("puddleducks_watch: criteria panel(s) %s parsed to ZERO items — treating the "
                   "read as broken and keeping the previous snapshot." % ", ".join(empty))
        now = before
    changes = criteria_lines(now, before) if before else []
    if not before and now:
        print("seeded criteria for %d child(ren): %s"
              % (len(now), ", ".join("%s %d/%d" % (c["child"],
                                                   sum(1 for i in c["items"].values() if i["achieved"]),
                                                   len(c["items"])) for c in now.values())))

    lines = []
    if alertable and not was:
        lines += courtesy_lines(open_rows, courtesy)
    if changes:
        if lines:
            lines.append("")
        lines += changes
    if lines:
        # ONE message even when both watches fire. Two notifications seconds apart about the same
        # account is the drip this estate keeps deciding it does not want.
        lines.append("")
        lines.append("(Read-only — nothing on the portal was clicked.)")
        try:
            tg.send("\n".join(lines), agent="puddleducks")
            print("alerted: courtesy=%s criteria_lines=%d"
                  % (bool(alertable and not was), len(changes)))
        except Exception as e:
            errlog.err("puddleducks_watch: sending the Telegram alert", e)
            return 1                # do NOT record it; retry next run rather than lose the alert
    else:
        print("no alert: open=%d/%d booked=%s (was alertable=%s), criteria unchanged for %d child(ren)"
              % (len(open_rows), len(targets), booked, was, len(now)))

    state.update({"alertable": alertable, "open": len(open_rows), "targets": len(targets),
                  "booked": booked, "courtesy_block": (courtesy.get("block") or "")[:600],
                  "criteria": now})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
