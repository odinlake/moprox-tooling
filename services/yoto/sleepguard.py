#!/usr/bin/env python3
"""Night-time sleep-timer guard for the children's Yoto. Run every 10 min by yoto-sleepguard.timer.

    20:30-08:00  if the player is playing and has no sleep timer running, set one:
                 22:00-05:00 -> 20 min, otherwise 45 min.

Anything started from a physical card, the player's buttons or Yoto's own app runs with no timer at
all (there is no default; a stream such as Sleep Radio plays forever), which is the gap this closes.
A running timer is never touched, so a longer one set by hand is left alone. An idle or paused
player is left alone too: a timer there would only tick down before anyone pressed play.

Order of checks matters: REST presence first (costs nothing on the device), then one events report
over MQTT, then at most one sleep-timer/set. Assumed, not measured: a report request does not reset
the player's idle-shutdown clock, so polling does not keep it awake.

    sleepguard.py            one pass (what the timer runs)
    sleepguard.py --dry      decide and log, send nothing
"""
import datetime, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yoto

START, END = datetime.time(20, 30), datetime.time(8, 0)
DEEP_START, DEEP_END = datetime.time(22, 0), datetime.time(5, 0)


def wanted(now):
    """Seconds of sleep timer to set at `now`, or None outside the window."""
    t = now.time()
    if not (t >= START or t < END):
        return None
    return 20 * 60 if (t >= DEEP_START or t < DEEP_END) else 45 * 60


def report(link, wait=8):
    """The events report, or {} if the player sent none within `wait`."""
    link.msgs.clear()
    link.send("events/request", None, wait=wait,
              until=lambda new: any(k == "events" and "sleepTimerActive" in m for k, m in new))
    ev = {}
    for kind, m in link.msgs:
        if kind == "events" and isinstance(m, dict):
            ev.update(m)
    return ev


def main():
    dry = "--dry" in sys.argv
    secs = wanted(datetime.datetime.now())
    if secs is None:
        return
    tok = yoto.token()
    dev = yoto.env()["YOTO_DEVICE_ID"]
    on = yoto.online(tok, dev)
    if on is not True:
        print("player %s: nothing to do" % ("off" if on is False else "presence unknown"))
        return
    with yoto.Link(tok, dev) as link:
        ev = report(link)
        if "sleepTimerActive" not in ev:
            print("player online but sent no events report; leaving it alone")
            return
        what = "%s / %s" % (ev.get("playbackStatus"), ev.get("chapterTitle") or ev.get("cardId") or "no card")
        if ev.get("playbackStatus") != "playing":
            print("not playing (%s); nothing to do" % what)
            return
        if ev.get("sleepTimerActive"):
            print("timer already running (%ss left), %s" % (ev.get("sleepTimerSeconds"), what))
            return
        if dry:
            print("DRY: would set %d min, %s" % (secs // 60, what))
            return
        link.send("sleep-timer/set", {"seconds": secs})
        time.sleep(1)
        after = report(link)
        ok = after.get("sleepTimerActive") is True
        print("set %d min sleep timer, %s -> %s (%ss left)"
              % (secs // 60, what, "confirmed" if ok else "NOT confirmed", after.get("sleepTimerSeconds")))


if __name__ == "__main__":
    main()
