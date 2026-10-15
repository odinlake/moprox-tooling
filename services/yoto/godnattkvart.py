#!/usr/bin/env python3
"""Keep the "Radioapans godnattkvart" Yoto card (hsGyZ) up to date with Sveriges Radio.

    godnattkvart.py [--dry]

SR airs a new 15-minute episode every Sunday (programid 4801). The show has no podcast feed, so
Yoto's own podcast list cannot follow it, and SR keeps each episode online for only ~30 days.
This fetches SR's episode list, downloads every episode newer than the card's last chapter into
~/yoto-audio/godnattkvart/<date>.m4a, and appends each as "Godnattkvart <d month yyyy>" through
myo.append (which transcodes, keeps the cover and is idempotent on the audio), then sorts the card
NEWEST FIRST (operator 2026-10-15: a physical card is linked, and the new episode should be what it
plays). When the card is at Yoto's chapter limit the oldest (last) chapter is dropped first.

The player remembers where a card was stopped and resumes there; nothing in the API clears that.
So a new episode is also recorded in freshstart.MARK, and freshstart (fed by listen.py) restarts the
card at chapter 1 the first time it is inserted afterwards. The (shuffle) twin needs nothing: queue
rewrites it from the source on every play. One #bard line on Telegram when something was added.
Run weekly by yoto-godnattkvart.timer (Monday morning).
"""
import datetime, json, os, re, sys, urllib.request
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import errlog, freshstart, myo, yoto

CARD = "hsGyZ"
PROGRAM = 4801
DIR = os.path.expanduser("~/yoto-audio/godnattkvart")
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti",
          "september", "oktober", "november", "december"]
TZ = ZoneInfo("Europe/Stockholm")


def title_for(d):
    return "Godnattkvart %d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


def date_of(title):
    m = re.search(r"(\d{1,2}) (\w+) (\d{4})", title or "")
    if m and m.group(2).lower() in MONTHS:
        return datetime.date(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))


def episodes():
    url = ("https://api.sr.se/api/v2/episodes/index?programid=%d&format=json&audioquality=hi&size=50"
           % PROGRAM)
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    out = []
    for e in data.get("episodes") or []:
        ms = int(re.search(r"\d+", e["publishdateutc"]).group())
        d = datetime.datetime.fromtimestamp(ms / 1000, TZ).date()
        files = (e.get("broadcast") or {}).get("broadcastfiles") or []
        audio = files[0]["url"] if files else ((e.get("listenpodfile") or {}).get("url"))
        if audio:
            out.append((d, audio))
        else:
            print("<4>godnattkvart: %s has no audio yet, skipped" % d, file=sys.stderr)
    return sorted(out)


def newest_first(tok):
    """Sort the card by air date, newest first; a no-op POST is skipped. Chapters without a date in
    their title (none today) keep their place at the end."""
    import time
    time.sleep(3)          # Yoto read-after-write lag: a read right after a POST can be stale
    chapters = myo.chapters_of(CARD, tok)
    ordered = sorted(chapters, key=lambda c: date_of(c.get("title")) or datetime.date.min, reverse=True)
    if [c.get("title") for c in ordered] != [c.get("title") for c in chapters]:
        myo.set_order(CARD, ordered, tok)
        print("godnattkvart: sorted newest first, now starts with %s" % ordered[0].get("title"))


def main():
    dry = "--dry" in sys.argv
    tok = yoto.token()
    chapters = myo.chapters_of(CARD, tok)
    have = {date_of(c.get("title")) for c in chapters} - {None}
    last = max(have) if have else datetime.date.min
    new = [(d, u) for d, u in episodes() if d > last and d not in have]
    if not new:
        if not dry:
            newest_first(tok)
        print("godnattkvart: up to date (last %s, %d chapters)" % (last, len(chapters)))
        return
    if dry:
        print("godnattkvart: would add " + ", ".join(title_for(d) for d, _ in new))
        return
    os.makedirs(DIR, exist_ok=True)
    added = []
    for d, url in new:
        path = os.path.join(DIR, d.isoformat() + ".m4a")
        if not os.path.isfile(path):
            urllib.request.urlretrieve(url, path + ".part")
            os.replace(path + ".part", path)
        chapters = myo.chapters_of(CARD, tok)
        if len(chapters) >= myo.MAX_CHAPTERS:
            print("godnattkvart: card full, dropping %s" % chapters[-1].get("title"))
            myo.set_order(CARD, chapters[:-1], tok)
        r = myo.append(CARD, path, title_for(d), tok)
        print(json.dumps(r, ensure_ascii=False))
        if r.get("status") == "added":
            added.append(title_for(d))
    newest_first(tok)
    if added:
        freshstart.mark(CARD, added[-1])
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "forward"))
        import tg
        tg.send("🐒 *Radioapan*: added %s to the godnattkvart card. It plays first, from the start, next time the card goes in." % ", ".join(added), agent="bard")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        errlog.err("godnattkvart.py: updating the Radioapan card", e)
        raise
