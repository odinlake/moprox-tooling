#!/usr/bin/env python3
"""Night-time sleep-timer guard for the children's Yoto, and its Home Assistant feed. Run every 2 min
by yoto-sleepguard.timer.

    20:30-08:00  if the player is playing and has no sleep timer running, set one:
                 22:00-05:00 -> 20 min, otherwise 45 min.

Anything started from a physical card, the player's buttons or Yoto's own app runs with no timer at
all (there is no default; a stream such as Sleep Radio plays forever), which is the gap this closes.
A running timer is never touched, so a longer one set by hand is left alone. An idle or paused
player is left alone too: a timer there would only tick down before anyone pressed play.

Order of checks matters: REST presence first (costs nothing on the device), then one events report
over MQTT, then at most one sleep-timer/set. Assumed, not measured: a report request does not reset
the player's idle-shutdown clock, so polling does not keep it awake.

HOME ASSISTANT: every pass, at any hour, the player's state is POSTed to the webhook named by
YOTO_HA_WEBHOOK in yoto.env (HA side: private-config-ha packages/yoto.yaml, which turns it into
sensor.yoto_now_playing, sensor.yoto_volume and binary_sensor.yoto_playing). A switched-off player
is posted as status "off" so HA never shows a stale "playing". Presence unknown, or an online player
that sends no report, posts nothing: HA keeps the last known state rather than a guess.
This is why the pass now runs all day; outside 20:30-08:00 it just never sets a timer.

    sleepguard.py            one pass (what the timer runs)
    sleepguard.py --dry      decide and log, send nothing (the HA payload is printed instead)
"""
import datetime, json, os, sys, time, urllib.request
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


TITLES = os.path.expanduser("~/.local/share/moprox/yoto/card-titles.json")


def card_title(tok, card_id):
    """A card's title, cached on disk: the events report carries only the id. None if unknown."""
    if not card_id:
        return None
    try:
        cache = json.load(open(TITLES))
    except Exception:
        cache = {}
    if card_id not in cache:
        try:
            cache[card_id] = yoto.get("/card/%s" % card_id, tok)["card"]["title"].strip()
        except Exception:
            return None                             # not cached, so the next pass retries
        os.makedirs(os.path.dirname(TITLES), exist_ok=True)
        json.dump(cache, open(TITLES + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(TITLES + ".tmp", TITLES)
    return cache[card_id]


def to_ha(ev, tok):
    """The events report reshaped into the webhook payload packages/yoto.yaml expects."""
    return {"status": ev.get("playbackStatus") or "unknown",
            "card": card_title(tok, ev.get("cardId")), "card_id": ev.get("cardId"),
            "chapter": ev.get("chapterTitle"), "track": ev.get("trackTitle"),
            "source": ev.get("source"), "position": ev.get("position"),
            "track_length": ev.get("trackLength"),
            "sleep_timer": ev.get("sleepTimerSeconds") if ev.get("sleepTimerActive") else 0,
            "volume": ev.get("volume"), "volume_max": ev.get("volumeMax")}


def publish(payload, dry):
    """POST to HA. Never raises: a down HA must not cost the children their sleep timer."""
    e = yoto.env()
    hook = e.get("YOTO_HA_WEBHOOK")
    if not hook:
        return
    if dry:
        print("DRY: would post to HA %s" % json.dumps(payload, ensure_ascii=False))
        return
    url = "%s/api/webhook/%s" % (e.get("YOTO_HA_URL", "http://ha.lan:8123").rstrip("/"), hook)
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as x:
        print("HA publish failed: %s" % x)


def main():
    dry = "--dry" in sys.argv
    secs = wanted(datetime.datetime.now())
    tok = yoto.token()
    dev = yoto.env()["YOTO_DEVICE_ID"]
    on = yoto.online(tok, dev)
    if on is not True:
        if on is False:
            publish({"status": "off"}, dry)
        print("player %s: nothing to do" % ("off" if on is False else "presence unknown"))
        return
    with yoto.Link(tok, dev) as link:
        ev = report(link)
        if "sleepTimerActive" not in ev:
            print("player online but sent no events report; leaving it alone")
            return
        publish(to_ha(ev, tok), dry)
        what = "%s / %s" % (ev.get("playbackStatus"), ev.get("chapterTitle") or ev.get("cardId") or "no card")
        if secs is None:
            return
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
        if "sleepTimerActive" in after:
            publish(to_ha(after, tok), dry)
        print("set %d min sleep timer, %s -> %s (%ss left)"
              % (secs // 60, what, "confirmed" if ok else "NOT confirmed", after.get("sleepTimerSeconds")))


if __name__ == "__main__":
    main()
