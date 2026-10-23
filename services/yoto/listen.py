#!/usr/bin/env python3
"""Yoto listener: record every message the player publishes, with our arrival time, and feed each
one to brightguard (light the screen for 15 s at night when someone actually touches the player).

Stage 1 of the night-brightness listener (flash the screen to 1 for 15 s on a manual interaction,
else 0). Before anything acts on these messages we need to know, from real use, which ones a hand
on the player produces (knob, buttons, card in/out) and which ones come from automatic chapter
changes, the sleep timer and our own commands. This only records. It sends nothing to the player
apart from one events + status report request per connection, so the log starts from a full state.

    listen.py                append JSON lines to ~/.local/share/moprox/yoto/listen.jsonl

Every player on the account is recorded (2026-10-23, when the travel Mini joined): the main one
(YOTO_DEVICE_ID) to listen.jsonl as before, each other to listen-<deviceId>.jsonl. Only the main
player gets brightguard and freshstart; they change its settings, and nobody asked for that on the
others. A player that is off just leaves its session idle until it comes back.

Each line: {"t": arrival unix time (ms precision), "topic": "events|status|response|...", "m": payload}.
`m.eventUtc` (the player's clock, whole seconds) against `t` gives the player->here latency.

Client ID is LISTEN<deviceId>, NOT the DASH<deviceId> that yoto.py uses: the broker drops an
existing session when a second one connects with the same ID, so sharing it would make this
listener and every `yoto` command knock each other off.
"""
import json, os, ssl, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yoto
import brightguard
import freshstart

LOG = Path(os.environ.get("YOTO_LISTEN_LOG", str(Path.home() / ".local/share/moprox/yoto/listen.jsonl")))
SESSION = 45 * 60          # reconnect with a fresh access token well inside its lifetime


def log_for(dev, main):
    return LOG if dev == main else LOG.with_name("%s-%s%s" % (LOG.stem, dev, LOG.suffix))


def write(topic, m, log=LOG):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "topic": topic, "m": m}, ensure_ascii=False) + "\n")


GUARD = brightguard.Guard()
FRESH = freshstart.Guard()


def session(tok, dev, log=LOG, guards=True):
    """One broker session; returns when it drops or SESSION has elapsed."""
    import paho.mqtt.client as mqtt
    up, down = threading.Event(), threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="LISTEN" + dev, transport="websockets")
    c.username_pw_set(f"{dev}?x-amz-customauthorizer-name=PublicJWTAuthorizer", tok)
    c.tls_set_context(ssl.create_default_context())
    c.ws_set_options(path="/mqtt")

    def on_connect(cl, u, f, rc, props=None):
        if str(rc) == "Success" or rc == 0:
            for t in ("data/events", "data/status", "response"):
                cl.subscribe(f"device/{dev}/{t}", 0)
            up.set()

    def on_message(cl, u, msg):
        try:
            m = json.loads(msg.payload.decode())
        except Exception:
            m = msg.payload.decode(errors="replace")
        topic = msg.topic.split("/")[-1]
        write(topic, m, log)
        if not guards:
            return
        try:                                 # the guard must never cost the log
            GUARD.feed(topic, m, time.time())
        except Exception as e:
            print("brightguard: %s: %s" % (type(e).__name__, e), flush=True)
        try:                                 # nor may the fresh-start check
            FRESH.feed(topic, m, time.time())
        except Exception as e:
            print("<3>freshstart: %s: %s" % (type(e).__name__, e), flush=True)

    c.on_connect = on_connect
    c.on_message = on_message
    c.on_disconnect = lambda *a, **k: down.set()
    c.connect(yoto.BROKER, 443, keepalive=60)
    c.loop_start()
    try:
        if not up.wait(25):
            print("could not connect to the broker", flush=True)
            return
        write("_connected", {"online": yoto.online(tok, dev)}, log)
        c.publish(f"device/{dev}/command/events/request", "", 0)
        c.publish(f"device/{dev}/command/status/request", "", 0)
        down.wait(SESSION)
        write("_disconnected", {"dropped": down.is_set()}, log)
    finally:
        c.loop_stop()
        c.disconnect()


def run(dev, main):
    log = log_for(dev, main)
    print("yoto-listen: %s -> %s" % (dev, log), flush=True)
    while True:
        try:
            session(yoto.token(), dev, log, guards=dev == main)
        except Exception as e:
            print("session error (%s): %s: %s" % (dev, type(e).__name__, e), flush=True)
            time.sleep(30)
        time.sleep(1)


def main():
    main_dev = yoto.env()["YOTO_DEVICE_ID"]
    GUARD.restore()
    started = set()
    while True:
        # the main player always; the others as the account lists them, checked hourly so a newly
        # added player is picked up without a restart
        try:
            devs = [d["deviceId"] for d in yoto.get("/device-v2/devices/mine", yoto.token()).get("devices") or []]
        except Exception as e:
            print("<4>yoto-listen: could not list players: %s: %s" % (type(e).__name__, e), flush=True)
            devs = []
        for dev in [main_dev] + devs:
            if dev not in started:
                started.add(dev)
                threading.Thread(target=run, args=(dev, main_dev), daemon=True, name=dev).start()
        time.sleep(3600)


if __name__ == "__main__":
    main()
