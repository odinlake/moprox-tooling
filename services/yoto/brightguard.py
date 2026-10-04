"""Night screen guard: light the Yoto's screen briefly when someone actually touches the player.

The night display brightness is 0, so a child who turns the volume to 0 sees nothing and can't tell
why the story stopped. On a MANUAL interaction at night this sets nightDisplayBrightness to 1 for
15 s, then back to 0. It never reacts to anything the player does by itself; when unsure, it does
nothing.

What counts as manual, from the recorded log (listen.jsonl, 2026-10-03/04):
  - knob:  an events message carrying ONLY volume/volumeMax (+eventUtc), with the volume changed.
           The player's own volume moves always carry more: the sleep-timer fade-out (5..1 at 2 s
           intervals) and the 0-blips at a story change both carry sleepTimerSeconds.
  - card:  status.cardInserted changing between 0 and 1 (1 = a physical card; 2 = remote), except
           a 1 -> 0 within 20 s of the sleep timer running out: when the timer fires, the player
           reports the card as removed (2026-10-04 02:40:27).
  - button: playbackStatus moving between playing and paused (never to "stopped": a card ending
           or the sleep timer firing stops it by itself).
  - chapter knob: turning it searches back or forward through the chapters, one change per step.
           On the same card, a change counts as manual when it goes BACKWARDS (the player never
           moves to an earlier chapter by itself), when it comes within 10 s of a change already
           judged manual (the same search continuing), or when it goes forward with more than 90 s
           of the previous chapter left (by a position seen <= 10 s earlier). Automatic changes come
           with 4-6 s left (the player reports position every 5 s); 21 s and 39 s were also seen,
           and track lengths are not exact (one ran 73 s past its trackLength), so a forward step
           near the end of a chapter on its own is treated as automatic.
None of these count within 3 s of a command response on /response: that was a remote command
(ours, the Yoto app's or Home Assistant's), not a hand on the player.

Brightness goes through PUT /device-v2/<id>/config with the FULL config (only the one field
changed). Tested 2026-10-02: applied in 0.5-0.9 s, nothing else touched. It only ever moves
"0" -> "1" -> "0": if the night brightness is anything other than 0, it is someone's own setting and
is left alone. Night is the player's own `day` flag from status (0 = night).
"""
import json, os, threading, time, urllib.request
from pathlib import Path

import yoto

LIT, DARK, HOLD = "1", "0", 15.0
QUIET_AFTER_REMOTE = 3.0
SKIP_LEFT = 90
SEARCH_GAP = 10.0


def _order(key):
    """Chapter keys sort numerically when they are numbers ("01" < "10"), else as text."""
    return (0, int(key), "") if str(key).isdigit() else (1, 0, str(key))
STATE = Path(os.environ.get("YOTO_BRIGHT_STATE", str(Path.home() / ".local/share/moprox/yoto/brightguard.json")))


class Guard:
    def __init__(self, act=True):
        self.act = act                      # False: classify and log only (replay/tests)
        self.day = None
        self.volume = None
        self.inserted = None
        self.playback = None
        self.last_remote = 0.0
        self.timer_end = None
        self.chapter = None                 # (cardId, chapterKey) last seen
        self.left = None                    # (seconds of that chapter left, when seen)
        self.searching = 0.0                # when a chapter change was last judged manual               # when the running sleep timer reaches 0, by its last report
        self.until = 0.0                    # when the screen goes dark again
        self.lit = False
        self.lock = threading.Lock()
        self.triggers = []                  # (t, why), for replay

    # -- classification ---------------------------------------------------------------------
    def feed(self, topic, m, t):
        """One message from the broker; returns why it counted as manual, or None."""
        if not isinstance(m, dict):
            return None
        why = None
        if topic == "response":
            st = m.get("status")
            if isinstance(st, dict) and not any(k in st for k in ("status/request", "events", "status", "card-update")):
                self.last_remote = t        # a command someone sent remotely, not a report request
            return None
        remote = t - self.last_remote < QUIET_AFTER_REMOTE
        if topic == "status":
            s = m.get("status") or {}
            if "day" in s:
                self.day = s["day"]
            ins = s.get("cardInserted")
            if ins is not None:
                timer_fired = self.timer_end is not None and abs(t - self.timer_end) < 20
                if self.inserted is not None and ins != self.inserted and {ins, self.inserted} == {0, 1} and not remote \
                        and not (ins == 0 and timer_fired):
                    why = "card %s" % ("in" if ins == 1 else "out")
                self.inserted = ins
        elif topic == "events":
            keys = set(m) - {"eventUtc"}
            if m.get("sleepTimerActive") is False:
                pass                        # keep the last end time: the stop it causes comes after
            elif isinstance(m.get("sleepTimerSeconds"), (int, float)):
                self.timer_end = t + m["sleepTimerSeconds"]
            if "volume" in m:
                if keys <= {"volume", "volumeMax"} and self.volume is not None and m["volume"] != self.volume and not remote:
                    why = "knob %s->%s" % (self.volume, m["volume"])
                self.volume = m["volume"]
            ck = (m.get("cardId"), m.get("chapterKey"))
            if ck[1]:
                if self.chapter and ck != self.chapter and ck[0] == self.chapter[0] and not remote:
                    a, b = _order(self.chapter[1]), _order(ck[1])
                    step = "chapter %s->%s" % (self.chapter[1], ck[1])
                    if b < a:
                        why = why or step + " (backwards)"
                    elif t - self.searching <= SEARCH_GAP:
                        why = why or step + " (search continues)"
                    elif self.left and t - self.left[1] <= 10 and self.left[0] > SKIP_LEFT:
                        why = why or step + " (%ds left)" % self.left[0]
                    if why and why.startswith("chapter"):
                        self.searching = t
                if ck != self.chapter:
                    self.left = None
                self.chapter = ck
                if isinstance(m.get("position"), (int, float)) and m.get("trackLength"):
                    self.left = (m["trackLength"] - m["position"], t)
            pb = m.get("playbackStatus")
            if pb:
                if self.playback in ("playing", "paused") and pb in ("playing", "paused") and pb != self.playback and not remote:
                    why = why or "button %s" % pb
                self.playback = pb
        if why and self.day == 0:
            self.triggers.append((t, why))
            if self.act:
                self.light(t, why)
            return why
        return None

    # -- acting --------------------------------------------------------------------------------
    def light(self, t, why):
        with self.lock:
            self.until = t + HOLD
            if self.lit:
                return
            if self._set(LIT, expect=DARK):
                self.lit = True
                print("lit (%s)" % why, flush=True)
                threading.Thread(target=self._dark_later, daemon=True).start()

    def _dark_later(self):
        while True:
            with self.lock:
                left = self.until - time.time()
                if left <= 0:
                    self._set(DARK, expect=LIT)
                    self.lit = False
                    print("dark", flush=True)
                    return
            time.sleep(min(left, 1.0))

    def restore(self):
        """At startup: if a previous run left the screen lit, put it back."""
        try:
            if json.loads(STATE.read_text()).get("lit"):
                self._set(DARK, expect=LIT)
        except FileNotFoundError:
            pass
        except Exception as e:
            print("restore: %s" % e, flush=True)

    def _set(self, value, expect):
        """nightDisplayBrightness expect -> value; False (and no write) if it isn't `expect`."""
        try:
            tok = yoto.token()
            dev = yoto.env()["YOTO_DEVICE_ID"]
            d = yoto.get("/device-v2/%s/config" % dev, tok)["device"]
            cfg = dict(d["config"])
            if str(cfg.get("nightDisplayBrightness")) != expect:
                STATE.write_text(json.dumps({"lit": False}))
                return False
            cfg["nightDisplayBrightness"] = value
            r = urllib.request.Request(yoto.API + "/device-v2/%s/config" % dev, method="PUT",
                                       data=json.dumps({"name": d.get("name"), "config": cfg}).encode(),
                                       headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
            urllib.request.urlopen(r, timeout=20).read()
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"lit": value == LIT, "at": time.time()}))
            return True
        except Exception as e:
            print("brightness %s failed: %s: %s" % (value, type(e).__name__, e), flush=True)
            return False


def replay(path):
    """Run the recorded log through the rules without acting; print what would have fired."""
    g = Guard(act=False)
    for line in open(path):
        d = json.loads(line)
        g.feed(d["topic"], d["m"], d["t"])
    for t, why in g.triggers:
        print(time.strftime("%m-%d %H:%M:%S", time.localtime(t)), why)


if __name__ == "__main__":
    import sys
    replay(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".local/share/moprox/yoto/listen.jsonl"))
