"""Idle restart: after 8 h of silence, the Radioapan card starts at chapter 1 instead of resuming.
Operator ask (2026-11-16). The card (hsGyZ) is sorted newest first, so chapter 1 is the latest
episode; the player otherwise resumes each card where it stopped, which after a few days lands the
children mid-way through some months-old episode (2026-11-13 19:59: resumed at chapter 9 after 13 h).
Rule: on the main player, when playback STARTS on CARD from a physical card (status.cardInserted 1)
and nothing has played for at least IDLE, send card/start at chapter 01, secondsIn 0 (the form the
player treats as a start, see freshstart.py). Inserting the card and pressing play on a card left in
both count. Left alone: remote plays (cardInserted 2: bard, the picker, the Yoto app, Home Assistant),
the alarm (4), and a start that is already at chapter 01 within its first 30 s.
"Silence" is the last `playing` report from the player, any card, as nightvolume.py measures it; seed()
reads it back from the log so a listener restart does not forget it. The decision waits for the
start's own events (cardId) and status (cardInserted) reports, which come 1-5 s after the bare
{"playbackStatus": "playing"} (5 s on a card's first play after an update, 2026-10-15); the old
position can be heard for those seconds before the jump. No reports within WAIT s: nothing is done.
A restart here also clears a pending freshstart mark for the card: the children now hear chapter 1.
    idlestart.py [listen.jsonl]     replay the log: print when it would have fired
"""
import json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import errlog

CARD = "hsGyZ"                   # Radioapans godnattkvart
IDLE = 8 * 3600
WAIT = 15                        # seconds to wait for the start's card and cardInserted reports
AT_START = 30                    # seconds into chapter 01 that already count as "the start"


class Guard:
    def __init__(self, act=True):
        self.act = act
        self.last_play = None    # last `playing` report; None until known
        self.playing = False
        self.inserted = None
        self.card = None
        self.chapter = None
        self.position = None
        self.pending = None      # (start time, idle seconds) awaiting this start's card + cardInserted
        self.card_t = self.ins_t = 0.0
        self.triggers = []

    def seed(self, path, tail=2_000_000):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - tail))
                lines = f.read().decode(errors="replace").splitlines()[1:]
        except OSError:
            return
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("m")
            if d.get("topic") == "events" and isinstance(m, dict) and m.get("playbackStatus") == "playing":
                self.last_play = d["t"]
        self.playing = False

    def feed(self, topic, m, t):
        if not isinstance(m, dict):
            return None
        if topic == "status":
            s = m.get("status") or {}
            if "cardInserted" in s:
                self.inserted = s["cardInserted"]
                self.ins_t = t
        elif topic == "events":
            if "cardId" in m:
                self.card, self.card_t = m["cardId"], t
            if "chapterKey" in m:
                self.chapter = m["chapterKey"]
            if "position" in m:
                self.position = m["position"]
            if "playbackStatus" in m:
                now_playing = m["playbackStatus"] == "playing"
                if now_playing and not self.playing and self.last_play is not None and t - self.last_play >= IDLE:
                    self.pending = (t, t - self.last_play)
                self.playing = now_playing
                if now_playing:
                    self.last_play = t
        if not self.pending:
            return None
        t0 = self.pending[0]
        if t - t0 > WAIT:                        # never got both reports: do nothing rather than guess
            print("<4>idlestart: start at %s never reported its card and cardInserted within %d s; left alone"
                  % (time.strftime("%H:%M:%S", time.localtime(t0)), WAIT), flush=True)
            self.pending = None
            return None
        if self.card_t < t0 or self.ins_t < t0 or self.card in (None, "none"):
            return None                          # still waiting for this start's own reports
        why = self.decide()
        if why and self.act:
            threading.Thread(target=self._restart, args=(why,), daemon=True).start()
        return why

    def decide(self):
        """Settle a pending start: return the reason it fired, or None (and say why not)."""
        t0, idle = self.pending
        self.pending = None
        hrs = idle / 3600
        if self.card != CARD:
            return None
        if self.inserted != 1:
            print("idlestart: %s started after %.1f h idle, but not from a physical card (cardInserted %s); left alone"
                  % (CARD, hrs, self.inserted), flush=True)
            return None
        if self.chapter == "01" and (self.position or 0) <= AT_START:
            return None
        why = "start after %.1f h idle at chapter %s, %ss" % (hrs, self.chapter, self.position)
        self.triggers.append((t0, why))
        return why

    def _restart(self, why):
        try:
            import yoto, freshstart
            with yoto.Link(yoto.token(), yoto.env()["YOTO_DEVICE_ID"]) as link:
                link.send("card/start", {"uri": "https://yoto.io/" + CARD, "chapterKey": "01",
                                         "trackKey": "01", "secondsIn": 0}, wait=6)
            marks = freshstart._load()
            if marks.pop(CARD, None) is not None:
                freshstart._save(marks)
            print("idlestart: %s %s; restarted at chapter 1" % (CARD, why), flush=True)
        except Exception as e:
            errlog.err("idlestart.py: restarting %s at chapter 1" % CARD, e)


def replay(path):
    g = Guard(act=False)
    for line in open(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        g.feed(d["topic"], d["m"], d["t"])
    for t, why in g.triggers:
        print(time.strftime("%m-%d %H:%M:%S", time.localtime(t)), why)


if __name__ == "__main__":
    replay(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".local/share/moprox/yoto/listen.jsonl"))
