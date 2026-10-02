#!/usr/bin/env python3
"""Passive Yoto listener: record every message the player publishes, with our arrival time.

Stage 1 of the night-brightness listener (flash the screen to 1 for 15 s on a manual interaction,
else 0). Before anything acts on these messages we need to know, from real use, which ones a hand
on the player produces (knob, buttons, card in/out) and which ones come from automatic chapter
changes, the sleep timer and our own commands. This only records. It sends nothing to the player
apart from one events + status report request per connection, so the log starts from a full state.

    listen.py                append JSON lines to ~/.local/share/moprox/yoto/listen.jsonl

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

LOG = Path(os.environ.get("YOTO_LISTEN_LOG", str(Path.home() / ".local/share/moprox/yoto/listen.jsonl")))
SESSION = 45 * 60          # reconnect with a fresh access token well inside its lifetime


def write(topic, m):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "topic": topic, "m": m}, ensure_ascii=False) + "\n")


def session(tok, dev):
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
        write(msg.topic.split("/")[-1], m)

    c.on_connect = on_connect
    c.on_message = on_message
    c.on_disconnect = lambda *a, **k: down.set()
    c.connect(yoto.BROKER, 443, keepalive=60)
    c.loop_start()
    try:
        if not up.wait(25):
            print("could not connect to the broker", flush=True)
            return
        write("_connected", {"online": yoto.online(tok, dev)})
        c.publish(f"device/{dev}/command/events/request", "", 0)
        c.publish(f"device/{dev}/command/status/request", "", 0)
        down.wait(SESSION)
        write("_disconnected", {"dropped": down.is_set()})
    finally:
        c.loop_stop()
        c.disconnect()


def main():
    dev = yoto.env()["YOTO_DEVICE_ID"]
    print("yoto-listen: logging to %s" % LOG, flush=True)
    while True:
        try:
            session(yoto.token(), dev)
        except Exception as e:
            print("session error: %s: %s" % (type(e).__name__, e), flush=True)
            time.sleep(30)
        time.sleep(1)


if __name__ == "__main__":
    main()
