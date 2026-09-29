#!/usr/bin/env python3
"""Home Assistant feed for the children's Yoto: what is playing and at what volume. Long-running
(yoto-hafeed.service).

STOPGAP. Home Assistant has a core Yoto integration (2026.6+, cdnninja/yoto_api) that gives a real
media_player. When HA runs that directly, retire this service and private-config-ha packages/yoto.yaml.
Until then this copies that integration's connection pattern, which is the nearest thing to guidance
Yoto has published (yoto.dev gives no rate limits or polling advice):

  - ONE long-lived MQTT connection, keepalive 60 s, QoS 1, reconnecting with 1..60 s exponential
    backoff and a FRESH access token on every connect (AWS IoT enforces the token TTL).
  - `events/request` every 240 s. Yoto stops pushing data/events ~5 min after the last request even
    on a live socket; its own guidance is 4m55s, the library keeps a minute of margin.
  - REST `/device-v2/devices/mine` every 300 s for presence, beside the MQTT `presence` topic.
  - Its OWN client id. yoto.Link uses the fixed `DASH<deviceId>`, and AWS IoT drops an existing
    connection when another arrives with the same id, so sharing it would make this feed and
    yoto-web / sleepguard knock each other off.

data/events are DELTAS: they are merged into one running snapshot, which is cleared when the player
goes offline. A post goes to the webhook named by YOTO_HA_WEBHOOK in yoto.env ONLY WHEN THE PAYLOAD
DIFFERS from the last successful one (HA_LAST), so HA records changes, not a heartbeat; an "off"
is sent once. Position and timer seconds are left out because they change constantly. Volume is
the raw step the player reports; its max is left out on purpose, because the operator sets
different day and night limits by hand, so any ratio to it means nothing.

    hafeed.py          run forever (what the service runs)
    hafeed.py --dry    run forever, print payloads instead of posting them
"""
import json, os, ssl, sys, threading, time, urllib.request, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yoto

KEEPALIVE, HEARTBEAT, REST_EVERY = 60, 240, 300
BACKOFF_MIN, BACKOFF_MAX = 1, 60
STATE = os.path.expanduser("~/.local/share/moprox/yoto")
TITLES = os.path.join(STATE, "card-titles.json")
HA_LAST = os.path.join(STATE, "ha-last.json")


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def card_title(card_id):
    """A card's title, cached on disk: the events report carries only the id. None if unknown."""
    if not card_id:
        return None
    try:
        cache = json.load(open(TITLES))
    except Exception:
        cache = {}
    if card_id not in cache:
        try:
            cache[card_id] = yoto.get("/card/%s" % card_id, yoto.token())["card"]["title"].strip()
        except Exception:
            return None                             # not cached, so the next change retries
        _save(TITLES, cache)
    return cache[card_id]


def to_ha(ev):
    """The merged events snapshot reshaped into the payload packages/yoto.yaml expects."""
    card = ev.get("cardId") if ev.get("cardId") not in (None, "", "none") else None
    return {"status": ev.get("playbackStatus") or "unknown",
            "card": card_title(card), "card_id": card,
            "chapter": ev.get("chapterTitle"), "track": ev.get("trackTitle"),
            "source": ev.get("source"), "sleep_timer": bool(ev.get("sleepTimerActive")),
            "volume": ev.get("volume")}


def publish(payload, dry):
    """POST to HA if the payload changed. Never raises; a failed post is retried on the next change
    or tick because HA_LAST is only written after a success."""
    e = yoto.env()
    hook = e.get("YOTO_HA_WEBHOOK")
    if not hook:
        return
    try:
        if json.load(open(HA_LAST)) == payload:
            return
    except Exception:
        pass
    if dry:
        print("DRY: would post %s" % json.dumps(payload, ensure_ascii=False))
        return
    url = "%s/api/webhook/%s" % (e.get("YOTO_HA_URL", "http://ha.lan:8123").rstrip("/"), hook)
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as x:
        print("HA publish failed: %s" % x)
        return
    _save(HA_LAST, payload)
    print("HA: %s" % json.dumps(payload, ensure_ascii=False))


class Feed:
    def __init__(self, dev, dry):
        self.dev, self.dry = dev, dry
        self.ev, self.online = {}, None
        self.lock = threading.Lock()
        self.up, self.dropped = threading.Event(), threading.Event()
        self.client = None

    # --- state -> HA ---------------------------------------------------------------------------

    def post(self):
        with self.lock:
            if self.online is False:
                payload = {"status": "off"}
            elif "playbackStatus" in self.ev:
                payload = to_ha(self.ev)
            else:
                return                              # nothing trustworthy yet: HA keeps its last state
        publish(payload, self.dry)

    def set_online(self, on):
        """Presence from REST or the presence topic. Coming online asks for a full report."""
        if on is None:
            return
        was, self.online = self.online, on
        if on is False:
            with self.lock:
                self.ev.clear()
            self.post()
        elif was is not True:
            self.request_events()

    # --- MQTT ----------------------------------------------------------------------------------

    def request_events(self):
        c = self.client
        if c is not None and self.up.is_set():
            c.publish("device/%s/command/events/request" % self.dev, "", 1)

    def _on_connect(self, c, u, f, rc, props=None):
        if str(rc) != "Success" and rc != 0:
            print("MQTT connect refused: %s" % rc)
            return
        for t in ("data/events", "presence"):
            c.subscribe("device/%s/%s" % (self.dev, t), 1)
        self.up.set()
        self.request_events()

    def _on_disconnect(self, c, u, *a):
        self.up.clear()
        self.dropped.set()

    def _on_message(self, c, u, m):
        try:
            body = json.loads(m.payload.decode())
        except Exception:
            return
        if not isinstance(body, dict):
            return
        kind = m.topic.split("/")[-1]
        if kind == "presence":
            self.set_online({"online": True, "offline": False}.get(body.get("state")))
        elif kind == "events":
            with self.lock:
                self.ev.update(body)
            if self.online is not True:
                self.online = True                  # it is talking, so it is on
            self.post()

    def connect(self):
        import paho.mqtt.client as mqtt
        cid = "MOPROXFEED" + uuid.uuid4().hex
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, transport="websockets")
        c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
        c.username_pw_set("_?x-amz-customauthorizer-name=PublicJWTAuthorizer", yoto.token())
        c.tls_set_context(ssl.create_default_context())
        c.ws_set_options(path="/mqtt")
        self.up.clear()
        self.dropped.clear()
        c.connect(yoto.BROKER, 443, keepalive=KEEPALIVE)
        c.loop_start()
        self.client = c
        if not self.up.wait(25):
            self.close()
            raise ConnectionError("no CONNACK from the Yoto broker within 25 s")

    def close(self):
        c, self.client = self.client, None
        if c is not None:
            c.loop_stop()
            try:
                c.disconnect()
            except Exception:
                pass

    # --- main loop -----------------------------------------------------------------------------

    def run(self):
        backoff = BACKOFF_MIN
        while True:
            try:
                self.connect()
                print("connected")
                backoff = BACKOFF_MIN
                next_hb = time.monotonic() + HEARTBEAT
                next_rest = time.monotonic()
                while not self.dropped.wait(1):
                    now = time.monotonic()
                    if now >= next_rest:
                        next_rest = now + REST_EVERY
                        self.set_online(yoto.online(yoto.token(), self.dev))
                    if now >= next_hb:
                        next_hb = now + HEARTBEAT
                        self.request_events()
                print("MQTT connection dropped")
            except Exception as x:
                print("error: %s; reconnecting in %ds" % (x, backoff))
            self.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


if __name__ == "__main__":
    Feed(yoto.env()["YOTO_DEVICE_ID"], "--dry" in sys.argv).run()
