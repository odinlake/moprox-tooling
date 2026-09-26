#!/usr/bin/env python3
"""Yoto player: REST inventory + MQTT query and control.

    yoto.py library|devices|card <id>|status|nowplaying|play <cardId> [chapterKey] [trackKey] [secondsIn]
    yoto.py pause|resume|stop|sleep <seconds>|volume <step 0-8>|ambient <r> <g> <b>

Credentials live in ~/.config/claude-dev/yoto.env (0600, not in git). The access token is
short-lived and is minted from the refresh token on every run, so nothing here expires.

THREE THINGS THAT COST AN EVENING TO LEARN, DO NOT REDISCOVER THEM:

1. VOLUME USES TWO DIFFERENT SCALES. Reports (`data/events`, `data/status`) give a STEP, 0..8,
   alongside `volumeMax: 8`. The `volume/set` COMMAND does not take that number. Measured on a v3e:
   0->0, 10->1, 20->3, 30->4, 40->6, 50->8, and anything above 50 saturates at 8 and emits no event
   at all. So the command is 0..50 across the 8 steps, step = floor(cmd / 6.25). `status` reports the
   COMMAND scale (cmd 19/32/44 -> status.volume 21/34/43) while `events` reports the STEP (3/5/7), so
   the two reports disagree by design and must never be merged. Yoto's own docs say
   0-100, which is wrong. Feeding a reported volume straight back into the command MUTES the player,
   which is exactly what happened the first time. Use --step and let vol_cmd() convert.

2. THE DEVICE CODE GRANT IS OFF for a dashboard-created client, whatever type you pick, so headless
   auth is authorization_code + PKCE with `http://localhost:8765/callback` registered as a callback
   URL. The client is PUBLIC: there is no secret, and there must not be one.

3. SCOPES ARE NOT GRANTED BY DEFAULT. Ask for all of them at once or you will be sent back for
   another browser round trip: `family:devices:control family:devices:view family:library:view
   offline_access`. The API names the missing one in its 403 body, which is the only reason this was
   debuggable.
"""
import base64, json, math, os, ssl, sys, threading, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from tokenlock import token_lock, write_atomic

ENV = os.path.expanduser("~/.config/claude-dev/yoto.env")
API = "https://api.yotoplay.com"
TOKEN_URL = "https://login.yotoplay.com/oauth/token"
BROKER = "aqrphjqbp3u2z-ats.iot.eu-west-2.amazonaws.com"


def env():
    d = {}
    for ln in open(ENV):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


# yoto.env is shared by every dev session on this box, so the token refresh is serialised through
# tokenlock (services/lib) -- see its docstring for the failure it prevents. The lock is a separate
# yoto.env.lock so the env itself can be replaced atomically underneath it.
def _locked():
    return token_lock(ENV)


def _write(key, val):
    """Atomic single-key update. CALLER MUST HOLD THE LOCK -- flock is per-fd, so a nested acquire
    from the same process would deadlock, which is why this and _put are separate."""
    lines = [l for l in open(ENV).read().splitlines() if not l.startswith(key + "=")]
    lines.append(f"{key}={val}")
    write_atomic(ENV, "\n".join(lines) + "\n")     # a reader never sees a truncated env


def _put(key, val):
    with _locked():
        _write(key, val)


def token():
    """A usable access token: reuse the stored one until it expires, else spend the refresh token.

    REFRESH TOKENS ROTATE. Auth0 invalidates the old one the moment it is used, so any script that
    refreshes and forgets to persist the replacement silently bricks every later run. That is exactly
    how this broke an hour after it was written: an ad-hoc calibration script refreshed, kept only the
    access token, and the stored refresh token died with `invalid_grant`. Never refresh outside here.
    """
    def _usable(e):
        cur = e.get("YOTO_ACCESS_TOKEN")
        if not cur:
            return None
        try:
            b = cur.split(".")[1]
            b += "=" * (-len(b) % 4)
            if json.loads(base64.urlsafe_b64decode(b)).get("exp", 0) - time.time() > 300:
                return cur
        except Exception:
            pass
        return None

    hit = _usable(env())
    if hit:
        return hit
    # The whole refresh is one critical section, and the check is REPEATED inside it. Two sessions
    # arriving together would otherwise both find the token stale and both spend the same refresh
    # token; the second replay trips Auth0's reuse detection, which revokes the entire family and
    # forces a browser re-consent. Re-reading under the lock means the loser simply picks up the
    # token the winner just wrote.
    with _locked():
        e = env()
        hit = _usable(e)
        if hit:
            return hit
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": e["YOTO_CLIENT_ID"],
                                       "refresh_token": e["YOTO_REFRESH_TOKEN"]}).encode()
        r = urllib.request.urlopen(urllib.request.Request(
            TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=45)
        t = json.loads(r.read())
        # Persist the ROTATED refresh token FIRST: if the process dies between the two writes, a
        # stale refresh token on disk is unrecoverable, whereas a stale access token just refreshes.
        if t.get("refresh_token"):
            _write("YOTO_REFRESH_TOKEN", t["refresh_token"])   # rotation: persist or be locked out
        _write("YOTO_ACCESS_TOKEN", t["access_token"])
        return t["access_token"]


def get(path, tok):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        API + path, headers={"Authorization": "Bearer " + tok}), timeout=45).read())


def now_playing(link, wait=6):
    """What the device says it is doing, from data/events -- the only honest answer.

    `events/request` asks for a full report rather than waiting for something to change. The fields
    are documented at yoto.dev/players-mqtt/mqtt-docs: cardId, chapterKey/chapterTitle,
    trackKey/trackTitle, position, trackLength, playbackStatus, source. Nothing in /response or
    data/status names the chapter, so this is the ONLY way to check a chapter actually started.
    """
    link.msgs.clear()
    link.send("events/request", None, wait=wait,
              until=lambda new: any(k == "events" and ("chapterKey" in m or "chapterTitle" in m)
                                    for k, m in new))
    for kind, m in link.msgs:
        if kind == "events" and ("chapterTitle" in m or "chapterKey" in m):
            return {k: m.get(k) for k in ("cardId", "chapterKey", "chapterTitle", "trackKey",
                                          "trackTitle", "position", "trackLength",
                                          "playbackStatus", "source")}
    return {"playbackStatus": "unknown", "note": "no chapter event returned"}


def vol_cmd(step):
    """Reported step (0..8) -> the value `volume/set` actually wants. See note 1 in the docstring."""
    return max(0, min(50, math.ceil(max(0, min(8, int(step))) * 6.25)))


class Link:
    """One MQTT session. Subscribes to both report topics plus /response before publishing."""

    def __init__(self, tok, device):
        import paho.mqtt.client as mqtt
        self.dev, self.msgs, self.ready = device, [], threading.Event()
        try:
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="DASH" + device,
                                 transport="websockets")
        except Exception:
            self.c = mqtt.Client(client_id="DASH" + device, transport="websockets")
        self.c.on_connect = self._up
        self.c.on_message = self._msg
        self.c.username_pw_set(f"{device}?x-amz-customauthorizer-name=PublicJWTAuthorizer", tok)
        self.c.tls_set_context(ssl.create_default_context())
        self.c.ws_set_options(path="/mqtt")

    def _up(self, c, u, f, rc, props=None):
        if str(rc) == "Success" or rc == 0:
            for t in ("data/status", "data/events", "response"):
                c.subscribe(f"device/{self.dev}/{t}", 0)
            self.ready.set()

    def _msg(self, c, u, m):
        try:
            self.msgs.append((m.topic.split("/")[-1], json.loads(m.payload.decode())))
        except Exception:
            pass

    def __enter__(self):
        self.c.connect(BROKER, 443, keepalive=300)
        self.c.loop_start()
        if not self.ready.wait(25):
            raise SystemExit("could not connect to the Yoto broker")
        return self

    def __exit__(self, *a):
        self.c.loop_stop()
        self.c.disconnect()

    def send(self, action, payload=None, wait=6, until=None):
        """Publish, then return as soon as the reply lands -- `wait` is now a CEILING, not a sleep.

        It used to be a flat `time.sleep(wait)`, so every call cost its full budget even though the
        round trip is under a second (measured on a v3e: 0.10 s to connect+subscribe, 0.78-0.88 s to
        a card/start ack). A verified play cost ~12 s of which ~11.5 s was this sleep. A fixed sleep
        is also the LESS reliable choice, because it cannot tell "no reply yet" from "no reply
        coming" -- it just hopes the budget was generous enough.

        `until` is a predicate over the messages that arrived since publishing; the default waits
        for anything on /response. Messages are appended, never cleared, so a caller that wants a
        clean slate still clears first.
        """
        base = len(self.msgs)
        if until is None:
            until = lambda new: any(kind == "response" for kind, _ in new)
        self.c.publish(f"device/{self.dev}/command/{action}", json.dumps(payload) if payload else "", 0)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            new = self.msgs[base:]
            if new and until(new):
                break
            time.sleep(0.01)
        return self.msgs

    def state(self, wait=8):
        """events and status are returned SEPARATELY and must stay that way.

        They disagree about volume on purpose (see note 1): events carries the 0..8 step beside
        `volumeMax: 8`, status carries the command-scale value in `volume`/`userVolume`. Merging them
        produces `volume: 34, volumeMax: 8`, which is meaningless, and was this tool's first bug.
        """
        self.c.publish(f"device/{self.dev}/command/events/request", "", 0)
        self.c.publish(f"device/{self.dev}/command/status/request", "", 0)
        time.sleep(wait)
        out = {"events": {}, "status": {}}
        for topic, p in self.msgs:
            if not isinstance(p, dict):
                continue
            if topic == "events":
                out["events"].update(p)
            elif topic == "status" and isinstance(p.get("status"), dict):
                out["status"].update(p["status"])
        return out


# All scopes the estate needs, requested together (trap 3 above). user:content:manage is what creating
# and updating MYO playlists requires (yoto.dev/authentication/scopes); it was not in the first grant.
SCOPES = ("family:devices:control family:devices:view family:library:view "
          "user:content:manage offline_access")
AUTH_URL = "https://login.yotoplay.com/authorize"
REDIRECT = "http://localhost:8765/callback"


def auth(pasted=None):
    """Re-consent with the full scope set. Two calls: `auth` prints the link and stashes the PKCE
    verifier; `auth <callback url>` exchanges the single-use code and persists BOTH tokens.

    Headless auth is not available for a dashboard client (trap 2), so a person opens the link,
    approves, and pastes back the localhost URL the browser lands on -- the page will not load,
    the URL bar is what matters."""
    import hashlib, secrets
    e = env()
    if not pasted:
        ver = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
        _put("YOTO_PKCE_VERIFIER", ver)
        q = urllib.parse.urlencode({"audience": "https://api.yotoplay.com", "scope": SCOPES,
                                    "response_type": "code", "client_id": e["YOTO_CLIENT_ID"],
                                    "code_challenge": chal, "code_challenge_method": "S256",
                                    "redirect_uri": REDIRECT})
        print(AUTH_URL + "?" + q)
        return
    code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [None])[0]
    if not code:
        sys.exit("no ?code= in that URL")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": e["YOTO_CLIENT_ID"],
                                   "code_verifier": e["YOTO_PKCE_VERIFIER"], "code": code,
                                   "redirect_uri": REDIRECT}).encode()
    r = urllib.request.urlopen(urllib.request.Request(TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded"}), timeout=30)
    j = json.loads(r.read())
    _put("YOTO_ACCESS_TOKEN", j["access_token"])
    _put("YOTO_ACCESS_EXPIRES", str(int(time.time()) + int(j.get("expires_in", 86400)) - 60))
    _put("YOTO_REFRESH_TOKEN", j["refresh_token"])          # rotates: persist or brick (trap 1)
    print("ok: tokens stored; scope granted:", j.get("scope"))


def main():
    a = sys.argv[1:] or ["status"]
    cmd, rest = a[0], a[1:]
    if cmd == "auth":                      # before token(): re-consent is what you run when token() cannot
        auth(sys.argv[2] if len(sys.argv) > 2 else None); return
    tok = token()
    dev = env()["YOTO_DEVICE_ID"]

    if cmd == "devices":
        print(json.dumps(get("/device-v2/devices/mine", tok), indent=1)); return
    if cmd == "library":
        lib = get("/card/family/library", tok)
        cards = lib.get("cards") or lib
        for c in (cards.values() if isinstance(cards, dict) else cards):
            cd = c.get("card") or c
            print(f"{cd.get('cardId'):<8} {cd.get('title')}")
        return
    if cmd == "card":
        d = get(f"/card/{rest[0]}", tok)
        c = d.get("card") or d
        for ch in (c.get("content") or {}).get("chapters") or []:
            print(f"  chapter {ch.get('key')}  {ch.get('title')}  {ch.get('duration')}s")
            for tr in ch.get("tracks") or []:
                print(f"     track {tr.get('key')}  {tr.get('title')}  {tr.get('duration')}s")
        return

    with Link(tok, dev) as link:
        if cmd == "nowplaying":
            print(json.dumps(now_playing(link), ensure_ascii=False, indent=1)); return
        if cmd == "status":
            print(json.dumps(link.state(), indent=1))
        elif cmd == "play":
            # secondsIn IS NOT OPTIONAL. card/start on the card that is already active RESUMES at its
            # saved position, and a resume ignores chapterKey/trackKey completely -- so `play <card> 02`
            # sat there replaying chapter 1 from wherever it last stopped, acking "card-play: OK" every
            # time. card/stop does NOT clear that position, so stopping first does not help either, and
            # neither does the yoto.io/<card>-<chapter> deep-link form. Sending secondsIn is what makes
            # this a START; then the chapter argument is honoured. Measured on v3e fw v2.23.3.
            # A 4th arg seeks that many seconds into the chosen chapter.
            pl = {"uri": f"https://yoto.io/{rest[0]}",
                  "secondsIn": int(rest[3]) if len(rest) > 3 else 0}
            if len(rest) > 1:
                pl["chapterKey"] = rest[1]
                # Each MYO chapter holds exactly one track, keyed "01". Both were sent in the run
                # that verified this, so the default matches what was actually tested.
                pl["trackKey"] = rest[2] if len(rest) > 2 else "01"
            link.send("card/start", pl, wait=6)
            # Say what is ACTUALLY playing, not what we asked for. `card-play: OK` only means the
            # command was received -- it is returned just the same when the device ignores every
            # field and resumes something else, which is how the chapter bug above went unnoticed
            # through a whole evening of "OK" responses. data/events carries the ground truth.
            print(json.dumps(now_playing(link)))
        elif cmd in ("pause", "resume", "stop"):
            print(link.send(f"card/{cmd}"))
        elif cmd == "sleep":
            print(link.send("sleep-timer/set", {"seconds": int(rest[0])}))
        elif cmd == "volume":
            print(link.send("volume/set", {"volume": vol_cmd(rest[0])}))
        elif cmd == "ambient":
            print(link.send("ambients/set", {"r": int(rest[0]), "g": int(rest[1]), "b": int(rest[2])}))
        else:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
