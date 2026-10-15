"""Fresh start: the first time a physical card goes in after new content arrived, play it from chapter 1.

The player keeps a resume point per card (chapter + position) and goes back to it on every insert.
Nothing in the API clears that point; card/stop leaves it too (see yoto.py `play`). For a card whose
newest chapter is first (godnattkvart.py), resuming means the children never hear the new one.

So whoever adds content calls mark(card, title). listen.py feeds every player message to Guard.feed,
and when status.cardInserted goes to 1 (a physical card; 2 = remote) with activeCard on the mark
list, this sends card/start at chapter 01 with secondsIn=0 (the only form the player treats as a
START rather than a resume) and clears the mark. The player will already have resumed for a moment,
so the old episode can be heard for a second or two before the new one starts.

Insert, not play: a remote play (bard, the picker, the Yoto app) reports cardInserted 2 and leaves
the mark in place, and a transition seen before the listener knows the previous state is ignored, so
a listener restart with the card already in does not restart a story mid-way.
"""
import json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import errlog, yoto

MARK = Path.home() / ".local/share/moprox/yoto/fresh-start.json"


def _load():
    try:
        return json.loads(MARK.read_text())
    except FileNotFoundError:
        return {}


def _save(d):
    MARK.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARK.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, MARK)


def mark(card, title):
    d = _load()
    d[card] = {"title": title, "t": round(time.time())}
    _save(d)


class Guard:
    def __init__(self):
        self.inserted = None            # last status.cardInserted seen; None until the first report

    def feed(self, topic, m, t):
        if topic != "status" or not isinstance(m, dict):
            return
        s = m.get("status")
        if not isinstance(s, dict) or "cardInserted" not in s:
            return
        prev, self.inserted = self.inserted, s.get("cardInserted")
        if prev is None or prev == 1 or self.inserted != 1:
            return
        card = s.get("activeCard")
        marks = _load()
        if card in marks:
            threading.Thread(target=self._restart, args=(card, marks[card].get("title")), daemon=True).start()

    def _restart(self, card, title):
        try:
            with yoto.Link(yoto.token(), yoto.env()["YOTO_DEVICE_ID"]) as link:
                link.send("card/start", {"uri": "https://yoto.io/" + card, "chapterKey": "01",
                                         "trackKey": "01", "secondsIn": 0}, wait=6)
            d = _load()
            d.pop(card, None)
            _save(d)
            print("freshstart: %s inserted, restarted at chapter 1 (%s)" % (card, title), flush=True)
        except Exception as e:
            errlog.err("freshstart.py: restarting %s at chapter 1" % card, e)
