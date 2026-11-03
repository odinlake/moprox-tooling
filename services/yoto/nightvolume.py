"""Night default volume: at night, a story started after a quiet spell begins at step 5.

The Yoto's night volume LIMIT stays 8 (the operator's choice, 2026-11-03), so the knob can still go
up to 8; this only sets where a fresh start begins. Rule: at night (the player's own `day` flag from
status, 0 = night), when playback starts and nothing has played for at least 30 min, set volume to
step 5. A play started remotely (bard, the picker, the Yoto app or Home Assistant: a command response
within 3 s) is left alone, since whoever sent it chose its volume. So is the player's own alarm
(status cardInserted 4; the weekday 06:45 "Time for School" clip has its own alarm volume).

The player ramps its own volume at a start (a bare 0, then back to the old level about 2 s later, see
brightguard.py), so the set waits for that to pass, then checks the next report and tries once more
if the player did not take it.

    nightvolume.py [listen.jsonl]     replay the log: print when it would have fired
"""
import json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import yoto

STEP = 5
QUIET = 30 * 60
SETTLE = 3.0                 # past the player's own start ramp
QUIET_AFTER_REMOTE = 3.0


class Guard:
    def __init__(self, act=True):
        self.act = act
        self.day = None
        self.last_play = None    # when a report last said playing; None until known
        self.playing = False
        self.last_remote = 0.0
        self.inserted = None     # status.cardInserted: 1 card, 2 remote, 4 alarm
        self.triggers = []

    def seed(self, path, tail=2_000_000):
        """Learn day/night and the last play from the end of the log, so a restart knows them."""
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
            if d.get("topic") == "status" and isinstance(m, dict) and "day" in (m.get("status") or {}):
                self.day = m["status"]["day"]
            elif d.get("topic") == "events" and isinstance(m, dict) and m.get("playbackStatus") == "playing":
                self.last_play = d["t"]
        self.playing = False     # a restart cannot tell; the next report says

    def feed(self, topic, m, t):
        if not isinstance(m, dict):
            return None
        if topic == "response":
            st = m.get("status")
            if isinstance(st, dict) and not any(k in st for k in ("status/request", "events", "status", "card-update")):
                self.last_remote = t
            return None
        if topic == "status":
            s = m.get("status") or {}
            if "day" in s:
                self.day = s["day"]
            if "cardInserted" in s:
                self.inserted = s["cardInserted"]
            return None
        if topic != "events" or "playbackStatus" not in m:
            return None
        now_playing = m["playbackStatus"] == "playing"
        why = None
        if now_playing and not self.playing and self.day == 0 and self.last_play is not None \
                and t - self.last_play >= QUIET and t - self.last_remote >= QUIET_AFTER_REMOTE:
            why = "start after %d min quiet" % ((t - self.last_play) // 60)
        self.playing = now_playing
        if now_playing:
            self.last_play = t
        if why:
            self.triggers.append((t, why))
            if self.act:
                threading.Thread(target=self._set, args=(why,), daemon=True).start()
        return why

    def _set(self, why):
        time.sleep(SETTLE)
        if self.inserted == 4:   # the status naming an alarm arrives about 1 s after the start
            print("nightvolume: %s, but it is the alarm; left alone" % why, flush=True)
            return
        try:
            with yoto.Link(yoto.token(), yoto.env()["YOTO_DEVICE_ID"]) as link:
                for attempt in (1, 2):
                    link.msgs.clear()
                    link.send("volume/set", {"volume": yoto.vol_cmd(STEP)}, wait=3)
                    link.send("events/request", None, wait=6,
                              until=lambda new: any(k == "events" and "volume" in m for k, m in new))
                    got = [m.get("volume") for k, m in link.msgs if k == "events" and isinstance(m, dict) and "volume" in m]
                    if got and got[-1] == STEP:
                        print("nightvolume: %s, set to %d" % (why, STEP), flush=True)
                        return
                print("<4>nightvolume: %s, set to %d NOT confirmed (reported %s)" % (why, STEP, got[-1:] or "nothing"), flush=True)
        except Exception as e:
            print("<3>nightvolume: %s: %s: %s" % (why, type(e).__name__, e), flush=True)


def replay(path):
    g = Guard(act=False)
    for line in open(path):
        d = json.loads(line)
        g.feed(d["topic"], d["m"], d["t"])
    for t, why in g.triggers:
        print(time.strftime("%m-%d %H:%M:%S", time.localtime(t)), why)


if __name__ == "__main__":
    replay(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".local/share/moprox/yoto/listen.jsonl"))
