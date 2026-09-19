#!/usr/bin/env python3
"""Watch the family's Puddle Ducks account: bookable courtesy classes, and the children's criteria.

WHAT CHANGED, AND WHY THE OLD ANSWER WAS THE WRONG ONE (2026-09-19). This used to read availability
off the pool's PUBLIC Classes Timetable, where `data-available` means "this course has a spare
place". That is a fact about the TERM, not about any particular Sunday, so the alert it produced
could never have named a date, and on 2026-09-07 it told the operator a place was open without being
able to say open WHEN. He booked a slot himself twelve days later and asked why he had not been
told. The caveat had been written down when the job was built and then built on anyway.

The question is "can I book a courtesy class on the coming Sunday", and exactly one page answers it:
`/mypuddleduck/catchupclasses?childPersonPK=…`, which lists what is bookable right now, by date.
That page is now the only availability source; the timetable is gone.

HOW COURTESY CLASSES ACTUALLY WORK, from the portal's own notice:
  * telling them you cannot attend a lesson earns one;
  * bookings open SIX DAYS before the class and close ONE HOUR before it starts;
  * you may hold only ONE at a time, so while one is booked there is nothing to offer.

THE RULES (operator, 2026-09-19). Say nothing unless every one of these holds:
  1. the slot is on a date he is still down to attend. A courtesy class on a lesson he cancelled is
     useless, because being unable to attend that day is why he cancelled it. The overview's
     Scheduled Lessons is the register: a date missing from it is a date he cancelled.
  2. he has a courtesy class to spend. The page's empty state covers both "no spaces" and "no valid
     Courtesy Class" in one sentence, and that ambiguity costs nothing, because both mean silence.
  3. he has not already booked one.
  4. otherwise, no more than one mention every REMIND_EVERY_S (48 h), plus one on Sunday
     morning, which is the last morning the thing can still be acted on.
  5. Sunday evening the target becomes the next Sunday and every flag resets. The cadence state is
     keyed on the target date, so that happens on its own rather than needing a second job.

READ-ONLY, WITH ONE PERMITTED EXCEPTION. The operator has cleared following a "Book a Courtesy
Class" link, which only lists and needs further selections before anything happens. NEVER follow a
"cancel" link: only one courtesy class can be held at a time, so the cancel link acts immediately on
the one booking there is. That distinction is the whole rule now; the earlier blanket ban came from
following exactly such a cancel link by hand on 2026-09-05.

EVERY PAGE READ HERE IS BEHIND THE LOGIN, which is the other lesson of the 2026-09-17 redesign: back
then the public timetable kept parsing while the signed-in half returned nothing, and the job
reported success twice a day for two days while publishing that nothing as fact. So each read must
prove it read something: no Sign Out link, no Scheduled Lessons rows, no courtesy link, a courtesy
page that is neither a list nor the no-spaces notice, or criteria panels that vanish where there
were panels before, all fail the run loudly instead of turning into good news.
"""
import datetime
import json
import os
import re
import sys
import time
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
# The authoritative source, and the only one that answers the question actually being asked. See
# the module docstring: the pool timetable that used to be read here answers a DIFFERENT question.
CATCHUP = "https://my.puddleducks.com/mypuddleduck/catchupclasses?childPersonPK=%s"
SITE = "my.puddleducks.com"
CHILD = os.environ.get("PD_CHILD", "Akiko")
DAY = os.environ.get("PD_DAY", "Sunday")
LEVEL = os.environ.get("PD_LEVEL", "Level 1 & 2 Swim Academy")
TIME = os.environ.get("PD_TIME", "15:15")
STATE = Path.home() / ".local/share/moprox/puddleducks-watch.json"
# How often to say "that place is STILL open" while nothing has changed. See main().
REMIND_EVERY_S = float(os.environ.get("PD_REMIND_EVERY_H", "48")) * 3600.0
# When the week rolls over. Sunday's class is the target all week; once it has been and gone the
# target becomes the Sunday after, and the cadence starts again from nothing (operator, 2026-09-19).
ROLLOVER_HOUR = int(os.environ.get("PD_ROLLOVER_HOUR", "18"))
# The last hour on the day itself at which saying anything is still useful. Puddle Ducks closes
# courtesy bookings ONE HOUR before the class starts, so a 15:15 class shuts at 14:15.
SAME_DAY_CUTOFF_HOUR = int(os.environ.get("PD_SAME_DAY_CUTOFF_HOUR", "13"))
WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
           "friday": 4, "saturday": 5, "sunday": 6}

# Structured, not text-scraped. An earlier innerText parse of this page silently mixed adjacent rows
# together and reported both "available" and "unavailable" for the same class — there are TWO
# parallel Sunday 15:15 Level 1 & 2 classes at this pool, and a line-based reading cannot tell them
# apart. The DOM can: one `.timetable__class` per class, each with its own data-available.
# The courtesy-booking page, which lists what can ACTUALLY be booked right now, by date. Its empty
# state is a single sentence covering two different situations ("no spaces available ... or you
# don't currently have a valid Courtesy Class"), and that ambiguity costs nothing here: both mean
# there is nothing to tell the operator about. What must NOT be confused with either is a page that
# no longer parses, so `sorry` is matched explicitly and anything that is neither a Sorry nor a list
# is an error, not an absence.
OFFERS_JS = r"""
(() => {
  const body = document.body.innerText.replace(/\s+/g,' ');
  // Scoped to the Scheduled Lessons block. Unscoped, this also swept up the PAYMENTS carousel,
  // which uses the same component, and counted two payment rows as lessons.
  const h = Array.from(document.querySelectorAll('h2')).find(e => /Scheduled Lessons/i.test(e.innerText||''));
  const scope = h ? (h.closest('.tabs') || h.parentElement.parentElement) : document;
  const rows = [];
  scope.querySelectorAll('.carousel-table__fields').forEach(dl => {
    const f = {}, dts = dl.querySelectorAll('dt'), dds = dl.querySelectorAll('dd');
    for (let i = 0; i < dts.length && i < dds.length; i++)
      f[(dts[i].innerText||'').trim().toLowerCase()] = (dds[i].innerText||'').replace(/\s+/g,' ').trim();
    if (f.date && f.time) rows.push({date: f.date, pool: f.pool||'', time: f.time, cls: f['class']||''});
  });
  return JSON.stringify({
    signedIn: !!Array.from(document.querySelectorAll('a')).find(a => /sign out/i.test(a.innerText||'')),
    heading: /Book a Courtesy Class for/i.test(body),
    sorry: /no spaces available|don.t currently have a valid Courtesy Class/i.test(body),
    offers: rows
  }); })()
"""


# REWRITTEN 2026-09-19: the portal was redesigned on or about 2026-09-17 and every selector in the
# previous version silently stopped matching. A booked Courtesy Class is no longer a paragraph on the
# overview ("You have booked a Courtesy Class for X on"); it is a ROW in the Scheduled Lessons
# carousel, flagged by `.carousel-table__field-value--courtesy` and a badge with
# aria-label="Courtesy class". `signedIn` and `lessons` are returned so the caller can tell a real
# "nothing booked" from a page that no longer parses -- the failure that ran for two days undetected,
# because the PUBLIC timetable kept working and the job therefore looked healthy.
OVERVIEW_JS = r"""
(() => {
  // Scoped to the Scheduled Lessons block. Unscoped, this also swept up the PAYMENTS carousel,
  // which uses the same component, and counted two payment rows as lessons.
  const h = Array.from(document.querySelectorAll('h2')).find(e => /Scheduled Lessons/i.test(e.innerText||''));
  const scope = h ? (h.closest('.tabs') || h.parentElement.parentElement) : document;
  const rows = [];
  scope.querySelectorAll('.carousel-table__fields').forEach(dl => {
    const f = {}, dts = dl.querySelectorAll('dt'), dds = dl.querySelectorAll('dd');
    for (let i = 0; i < dts.length && i < dds.length; i++)
      f[(dts[i].innerText||'').trim().toLowerCase()] = (dds[i].innerText||'').replace(/\s+/g,' ').trim();
    if (!f.date || !f.time) return;               // not a lesson row
    rows.push({
      courtesy: !!dl.querySelector('.carousel-table__field-value--courtesy, [aria-label="Courtesy class"]'),
      date: f.date, pool: f.pool || '', time: f.time, cls: f['class'] || ''
    });
  });
  const booked = rows.filter(r => r.courtesy);
  return JSON.stringify({
    signedIn: !!Array.from(document.querySelectorAll('a')).find(a => /sign out/i.test(a.innerText||'')),
    lessons: rows.length,
    booked: booked.length > 0,
    rows: booked,
    // Every date the child is still down to attend. A date MISSING from here is one he has told
    // them he cannot make, which is exactly the date a courtesy class on it would be useless.
    attending: rows.filter(r => !r.courtesy).map(r => r.date),
    block: booked.map(r => [r.date, r.pool, r.time, r.cls].filter(Boolean).join(' ')).join(' | ').slice(0, 400),
    links: Array.from(document.querySelectorAll('a[href*="catchup" i]'))
             .map(a => ((a.innerText||'').trim()) + ' => ' + a.getAttribute('href'))
  }); })()
"""


# REWRITTEN 2026-09-19 with the rest of the portal. The criteria are still a collapsed accordion that
# reads without a click, but it is now `#criteria-accordion-content-<childPersonPK>` and the status is
# TEXT ("Achieved" / "In progress") in `.criteria-table__status strong`. That is a straight
# improvement: the old markup carried the state only in a 16x16 icon filename with no alt text, which
# had to be confirmed by rendering the PNGs. The child's name comes off "Full name:" in the enclosing
# `.panel`. A child with no current class (Yuko) has no panel, which is enrolment, not a fault.
CRITERIA_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('[id^="criteria-accordion-content-"]').forEach(p => {
    const box = p.closest('.panel');
    const m = box ? (box.innerText||'').match(/Full name:\s*([^\n]+)/i) : null;
    const items = [];
    p.querySelectorAll('.criteria-table__item').forEach(it => {
      const t = it.querySelector('.criteria-table__text');
      const st = it.querySelector('.criteria-table__status strong');
      if (!t) return;
      items.push({txt: (t.innerText||'').replace(/\s+/g,' ').trim(),
                  status: st ? (st.innerText||'').trim() : '',
                  achieved: st ? /achieved/i.test(st.innerText||'') : false});
    });
    out.push({pk: p.id.replace('criteria-accordion-content-',''),
              child: m ? m[1].trim() : '', items: items});
  });
  return JSON.stringify(out); })()
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


def parse_date(d):
    """'20-09-26' -> date(2026, 9, 20). None if it is not that shape."""
    try:
        dd, mm, yy = [int(x) for x in (d or "").split("-")]
        return datetime.date(2000 + yy if yy < 100 else yy, mm, dd)
    except Exception:
        return None


def target_sunday(now):
    """The Sunday this cycle is about.

    All week the target is the coming Sunday. Once Sunday evening arrives the class has happened,
    so the target becomes the Sunday after and every flag resets: that rollover is what the
    operator asked for, and keying the cadence state on this date is what makes it happen without
    a separate reset job.
    """
    wd = WEEKDAY.get(norm(DAY), 6)
    ahead = (wd - now.weekday()) % 7
    if ahead == 0 and now.hour >= ROLLOVER_HOUR:
        ahead = 7
    return now.date() + datetime.timedelta(days=ahead)


def matches(offer):
    """Is this offered courtesy class the one being watched for?"""
    return (norm(LEVEL) in norm(offer.get("cls"))
            and norm(offer.get("time")).startswith(norm(TIME)))


def usable(offers, attending, now):
    """The offers worth telling him about, with the reasons each one was dropped.

    Three filters, all from the operator's rules (2026-09-19):
      * it has to be the class being watched for, on the target Sunday;
      * it has to be a date he is still down to attend, because a courtesy class on a date he has
        told them he cannot make is the one thing it can never be useful for;
      * and on the day itself it has to be early enough to act on, since bookings close an hour
        before the class.
    """
    want = target_sunday(now)
    keep, dropped = [], []
    for o in offers:
        d = parse_date(o.get("date"))
        if not matches(o):
            continue                                    # a different class entirely; not worth a note
        if d != want:
            dropped.append("%s is not the target Sunday (%s)" % (o.get("date"), want.isoformat()))
            continue
        if o.get("date") not in attending:
            dropped.append("%s is a lesson you cancelled" % o.get("date"))
            continue
        if d == now.date() and now.hour >= SAME_DAY_CUTOFF_HOUR:
            dropped.append("%s closes within the hour" % o.get("date"))
            continue
        keep.append(o)
    return keep, dropped


def look():
    """One browser visit. Returns (courtesy/overview, offers-per-child, criteria panels)."""
    sess = json.loads(webscout.call("open_session", {"site": SITE, "url": OVERVIEW}))["session"]
    try:
        courtesy = webscout.js(sess, OVERVIEW_JS)
        offers = {}
        for ln in courtesy.get("links") or []:
            m = re.search(r"childPersonPK=([0-9a-f-]+)", ln or "", re.I)
            if not m or m.group(1) in offers:
                continue
            webscout.call("goto", {"session": sess, "url": CATCHUP % m.group(1)})
            offers[m.group(1)] = webscout.js(sess, OFFERS_JS)
        webscout.call("goto", {"session": sess, "url": CHILDREN})
        panels = webscout.js(sess, CRITERIA_JS)
    finally:
        try:
            webscout.call("close", {"session": sess})
        except Exception as e:
            errlog.warn("puddleducks_watch: closing the browser session failed: %s" % e)
    return courtesy, offers, panels


def one_hour_before(hhmm):
    """'15:15' -> '14:15'. The booking deadline, which is the only number he has to act on."""
    try:
        h, m = [int(x) for x in (hhmm or "").split(":")[:2]]
        return "%02d:%02d" % ((h - 1) % 24, m)
    except Exception:
        return "an hour before it starts"


def courtesy_lines(keep, repeat):
    o = keep[0]
    lines = ["**Courtesy class free on %s at %s** — %s, %s."
             % (o.get("date"), o.get("time"), o.get("cls"), o.get("pool"))]
    if len(keep) > 1:
        lines.append("%d slots offered." % len(keep))
    lines.append("Book it from My Puddle Ducks, Book a Courtesy Class. Closes at %s."
                 % one_hour_before(o.get("time")))
    if repeat:
        lines.append("_(still free since I last mentioned it.)_")
    return lines


def main():
    try:
        courtesy, offers, panels = look()
    except Exception as e:
        errlog.err("puddleducks_watch: reading the Puddle Ducks portal", e)
        return 1

    # THE AUTHENTICATED PAGES MUST PROVE THEY READ SOMETHING. Nothing here is public any more, and
    # "no courtesy class available" is exactly what a page that stopped parsing also looks like.
    if not courtesy.get("signedIn"):
        errlog.err("puddleducks_watch: the overview came back with no Sign Out link, so the stored "
                   "session is not logged in. Refusing to read anything off a logged-out page. "
                   "Re-establish it: establish_session('%s')." % SITE)
        return 1
    if not courtesy.get("lessons"):
        errlog.err("puddleducks_watch: signed in, but the Scheduled Lessons carousel parsed to ZERO "
                   "rows on %s. That is a page-shape change, not an empty diary." % OVERVIEW)
        return 1
    if not offers:
        errlog.err("puddleducks_watch: the overview offered no 'Book a Courtesy Class' link for any "
                   "child, so there was nothing to check availability against. Page shape changed.")
        return 1
    for pk, o in offers.items():
        # A page that is neither the Sorry message nor a list of lessons has changed shape. Treating
        # that as "nothing available" is the whole class of failure this job keeps rediscovering.
        if not o.get("sorry") and not o.get("offers") and not o.get("heading"):
            errlog.err("puddleducks_watch: the courtesy page for child %s showed neither an offer "
                       "list nor the no-spaces notice. Not reading that as 'nothing free'." % pk[:8])
            return 1

    now = datetime.datetime.now()
    booked = bool(courtesy.get("booked"))
    attending = set(courtesy.get("attending") or [])
    all_offers = [x for o in offers.values() for x in (o.get("offers") or [])]
    keep, dropped = usable(all_offers, attending, now)
    # Rule 3, and belt to the page's own braces: one courtesy class at a time, so while one is
    # booked there is nothing to offer and nothing to say.
    if booked:
        keep = []

    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception as e:
        errlog.skip("puddleducks_watch: reading state", e)
        state = {}

    # The cadence state is keyed on the target Sunday, so it empties itself at the Sunday-evening
    # rollover without a second job to do the resetting.
    cycle = target_sunday(now).isoformat()
    if state.get("cycle") != cycle:
        state.update({"cycle": cycle, "last_courtesy_alert": 0, "sunday_said": False})
    last = float(state.get("last_courtesy_alert") or 0)
    due = (time.time() - last) >= REMIND_EVERY_S if last else True
    # Sunday morning gets one guaranteed mention whatever the 48-hour clock says: it is the last
    # morning on which the thing can still be acted on.
    sunday_push = (now.date().isoformat() == cycle and now.hour < SAME_DAY_CUTOFF_HOUR
                   and not state.get("sunday_said"))

    now_c = criteria_map(panels)
    before = state.get("criteria") or {}
    empty = [pk for pk, c in now_c.items() if not c["items"]]
    if empty:
        errlog.err("puddleducks_watch: criteria panel(s) %s parsed to ZERO items — treating the "
                   "read as broken and keeping the previous snapshot." % ", ".join(empty))
        now_c = before
    if before and not now_c:
        errlog.err("puddleducks_watch: %d child(ren) had criteria yesterday and the page yields NO "
                   "panels today. Treating that as a shape change, not as cleared criteria."
                   % len(before))
        now_c = before
    changes = criteria_lines(now_c, before) if before else []
    if not before and now_c:
        print("seeded criteria for %d child(ren): %s"
              % (len(now_c), ", ".join("%s %d/%d" % (c["child"],
                 sum(1 for i in c["items"].values() if i["achieved"]), len(c["items"]))
                 for c in now_c.values())))

    say_courtesy = bool(keep) and (due or sunday_push)
    lines = []
    if say_courtesy:
        lines += courtesy_lines(keep, repeat=bool(last))
    if changes:
        if lines:
            lines.append("")
        lines += changes
    if lines:
        lines.append("")
        lines.append("(Read-only — nothing on the portal was clicked.)")
        try:
            tg.send("\n".join(lines), agent="puddleducks")
            if say_courtesy:
                state["last_courtesy_alert"] = time.time()
                if now.date().isoformat() == cycle:
                    state["sunday_said"] = True
            print("alerted: courtesy=%s criteria_lines=%d" % (say_courtesy, len(changes)))
        except Exception as e:
            errlog.err("puddleducks_watch: sending the Telegram alert", e)
            return 1                # do NOT record it; retry next run rather than lose the alert
    else:
        print("no alert: cycle=%s offers=%d usable=%d booked=%s due=%s sunday_push=%s%s"
              % (cycle, len(all_offers), len(keep), booked, due, sunday_push,
                 (" | dropped: " + "; ".join(dropped[:4])) if dropped else ""))

    state.update({"offers": len(all_offers), "usable": len(keep), "booked": booked,
                  "courtesy_block": (courtesy.get("block") or "")[:400], "criteria": now_c})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
