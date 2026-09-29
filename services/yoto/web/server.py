#!/usr/bin/env python3
"""mo.lan/yoto -- pick stories off a playlist in the order you tap them, then play them on the Yoto.

    GET  /                       the picker (index.html)
    GET  /api/playlists          the family's own MYO playlists (store cards and twins left out)
    GET  /api/playlist/<cardId>  its chapters, each with an image url
    GET  /api/img/<name>         a cover, downscaled once and cached
    GET  /stats                  what has been played, when, how often (stats.html)
    GET  /api/stats?days=N       its data (playlog.stats; N=0 is everything), synced from HA first
    POST /api/play               {card, keys[], volume, sleep, rest}  -> myo.queue(), bard's routine
    POST /api/stop               stop the player

Playback is myo.queue(): the SAME function bard runs, so the page and the agent cannot drift apart.
It never reorders the playlist a child knows; it reorders that playlist's "(shuffle)" twin.

Served by Caddy on the web box at /yoto/ (prefix stripped) behind Authelia; the nftables gate in
this directory admits only the web box's isolated leg to the port.
"""
import hashlib, io, json, os, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "lib"))
import yoto, myo, errlog, playlog

PORT = int(os.environ.get("YOTO_WEB_PORT", "8030"))
COVERS = Path(os.environ.get("YOTO_COVERS", str(Path.home() / ".local/share/moprox/yoto/covers")))
THUMBS = COVERS / ".thumbs"
# The icon set (same mo "M" + corner glyph as mo/search and mo/mail, here a Y) and the PWA manifest.
STATIC = {"/icon.svg": "image/svg+xml", "/icon-yoto-180.png": "image/png", "/icon-yoto-512.png": "image/png",
          "/apple-touch-icon.png": "image/png", "/mo-yoto.webmanifest": "application/manifest+json"}
ORIGINS = {"https://mo.lan", "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
SYNC_EVERY = 15 * 60                  # background pull from HA; HA's own recorder keeps only ~10 days
SYNC_FRESH = 60                       # a page load re-syncs if the last pull is older than this
_sync = {"at": 0, "error": None}
PLAY_LOCK = threading.Lock()          # one play at a time: two taps racing would interleave orders
_cache = {}                           # small TTL cache: the library barely changes


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def library():
    tok = yoto.token()
    lib = myo.req("GET", "/card/family/library", tok)
    cards = lib.get("cards") or lib
    rows = [(c.get("card") or c) for c in (cards.values() if isinstance(cards, dict) else cards)]
    twins = {v for k, v in yoto.env().items() if k.startswith("YOTO_TWIN_")}
    me = next((r.get("userId") for r in rows if r.get("cardId") in twins), None)
    out = []
    for r in rows:
        # Own MYO playlists only: store cards (userId "yoto") cannot be reordered, and twins are an
        # implementation detail -- the child picks the playlist she knows.
        if r.get("cardId") in twins or r.get("userId") == "yoto" or (me and r.get("userId") != me):
            continue
        cover = ((r.get("metadata") or {}).get("cover") or {}).get("imageL")
        out.append({"cardId": r["cardId"], "title": (r.get("title") or "").strip(),
                    "cover": cover if cover and cover.startswith("https://") else None})
    return out


def local_cover(title):
    p = COVERS / (title.strip() + ".jpg")
    return p if p.exists() else None


def playlist(cid):
    # /card, not /content, for DISPLAY: it resolves chapter icons to fetchable https URLs (/content
    # has them as yoto:#<sha>). Nothing read here is ever POSTed back -- queue() re-reads /content.
    tok = yoto.token()
    d = myo.req("GET", "/card/" + cid, tok)
    card = d.get("card") or d
    cover = ((card.get("metadata") or {}).get("cover") or {}).get("imageL")
    chs = []
    for ch in (card.get("content") or {}).get("chapters") or []:
        t = (ch.get("title") or "").strip()
        icon = (ch.get("display") or {}).get("icon16x16")
        img, kind = None, None
        if local_cover(t):
            img, kind = "api/img/" + urllib.parse.quote(t + ".jpg"), "cover"
        elif icon and icon.startswith("https://"):
            img, kind = icon, "icon"          # 16x16 pixel art; the page scales it up pixelated
        elif cover and cover.startswith("https://"):
            img, kind = cover, "cardcover"
        chs.append({"key": ch.get("key"), "title": t, "img": img, "kind": kind,
                    "duration": ch.get("duration") or sum((tr.get("duration") or 0) for tr in ch.get("tracks") or [])})
    return {"cardId": cid, "title": (card.get("title") or "").strip(), "chapters": chs}


def sync_playlog():
    """Pull HA's Yoto history into playlog. Never raises; the page shows the error instead."""
    try:
        playlog.sync()
        _sync["error"] = None
    except Exception as e:
        if str(e) != _sync["error"]:                 # once per distinct error, not every 15 min
            errlog.err("yoto-web playlog sync", e)
        _sync["error"] = str(e)
    _sync["at"] = time.time()


def sync_loop():
    while True:
        sync_playlog()
        time.sleep(SYNC_EVERY)


def stats(days):
    if time.time() - _sync["at"] > SYNC_FRESH:
        sync_playlog()
    out = playlog.stats(days)
    out["sync_error"] = _sync["error"]
    return out


def thumb(name):
    src = COVERS / name
    if src.parent != COVERS or not src.exists():
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    out = THUMBS / (hashlib.sha1(name.encode()).hexdigest() + ".jpg")
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        im.thumbnail((600, 900))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=82, optimize=True)
        tmp = out.with_suffix(".tmp"); tmp.write_bytes(buf.getvalue()); os.replace(tmp, out)
    return out.read_bytes()


class H(BaseHTTPRequestHandler):
    server_version = "yoto-web"

    def log_message(self, fmt, *a):          # journald gets requests at info, errors via errlog
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def send(self, code, body, ctype="application/json", cache=None):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache or "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/stats", "/stats.html"):
                return self.send(200, (HERE / "stats.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/stats":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                days = max(0, min(3650, int((q.get("days") or ["30"])[0])))
                return self.send(200, stats(days))
            if path in STATIC:
                return self.send(200, (HERE / path[1:]).read_bytes(), STATIC[path], "max-age=86400")
            if path == "/api/playlists":
                return self.send(200, cached("lib", 60, library))
            if path.startswith("/api/playlist/"):
                cid = path.rsplit("/", 1)[1]
                return self.send(200, cached("pl:" + cid, 30, lambda: playlist(cid)))
            if path.startswith("/api/img/"):
                b = thumb(urllib.parse.unquote(path[len("/api/img/"):]))
                return self.send(200, b, "image/jpeg", "max-age=604800") if b else self.send(404, {"error": "no image"})
            return self.send(404, {"error": "not found"})
        except SystemExit as e:              # myo.req exits on an HTTP error from Yoto
            errlog.err("yoto-web GET %s" % path, e)
            return self.send(502, {"error": str(e)[:300]})
        except Exception as e:
            errlog.err("yoto-web GET %s" % path, e, trace=True)
            return self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # A write from another origin is refused: Authelia's cookie would otherwise ride along on a
        # cross-site form post and start the children's speaker.
        if self.headers.get("Origin") not in ORIGINS:
            return self.send(403, {"error": "bad origin"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if path == "/api/play":
                vol = req.get("volume")
                vol = None if vol is None else max(0, min(8, int(vol)))
                sleep = max(0, min(4 * 3600, int(req.get("sleep", 2700))))
                rest = req.get("rest") if req.get("rest") in ("shuffle", "order", "none") else "shuffle"
                keys = [str(k) for k in (req.get("keys") or [])][:100]
                if not PLAY_LOCK.acquire(timeout=30):
                    return self.send(409, {"error": "another play is still starting"})
                try:
                    t0 = time.monotonic()
                    out = myo.queue(str(req["card"]), keys, vol, sleep, rest)
                    out["seconds"] = round(time.monotonic() - t0, 2)
                finally:
                    PLAY_LOCK.release()
                return self.send(200, out)
            if path == "/api/stop":
                with yoto.Link(yoto.token(), yoto.env()["YOTO_DEVICE_ID"]) as link:
                    link.send("card/stop")
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except SystemExit as e:
            errlog.err("yoto-web POST %s" % path, e)
            return self.send(502, {"error": str(e)[:300]})
        except Exception as e:
            errlog.err("yoto-web POST %s" % path, e, trace=True)
            return self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    threading.Thread(target=sync_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("yoto-web on :%d, covers from %s" % (PORT, COVERS), flush=True)
    srv.serve_forever()
