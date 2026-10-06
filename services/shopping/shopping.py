#!/usr/bin/env python3
"""The household shopping list: lives in Home Assistant, reachable from Telegram.

Asked for 2026-10-05: a list that is started now and then and accumulates until someone gets to the
store, with "get shopping list" (and variants) answered on Telegram.

WHERE THE LIST LIVES. HA's to-do entity `todo.shopping_list` is the one true list: it is what the
HA app shows in the store, where items get ticked off. claude-dev holds no HA token (operator's
choice, see no-token-requests), so the two directions use the patterns the estate already has:

  * claude-dev -> HA: POST to a local-only HA webhook (private-config-ha packages/shopping.yaml),
    which adds / ticks off / removes items and then pushes the list back;
  * HA -> claude-dev: HA POSTs the whole list to `serve` (this file, :8031) whenever it changes,
    on HA start, and every 30 min. That copy is what `list` reads, so reading needs no round trip.

    shopping add "<item>" ["<item>" ...]   add, then wait for HA's push to confirm each one landed
    shopping list [--all]                  the open items (--all: ticked ones too)
    shopping done "<item>" ...             tick off (matched case-insensitively against the list)
    shopping remove "<item>" ...           delete outright
    shopping clear-done                    drop ticked items
    shopping text                          the list as the Telegram message (used by the router)
    shopping serve                         the receiver HA pushes to (shopping-web.service)
"""
import datetime, http.server, json, os, sys, time, urllib.request
from pathlib import Path

ENV = Path.home() / ".config/claude-dev/shopping.env"
STATE = Path.home() / ".local/share/moprox/shopping.json"
# Items asked for while HA had no todo.shopping_list (the integration is added in the HA UI, which
# no file can do). Kept here and sent the moment a push says the list exists, so nothing is lost.
PENDING = Path.home() / ".local/share/moprox/shopping-pending.json"
PORT = int(os.environ.get("SHOPPING_PORT", "8031"))
HA_ADDRS = {"10.10.10.7", "127.0.0.1"}       # Home Assistant's agent-subnet leg; loopback for tests
CONFIRM_S = 20                               # how long `add` waits for HA's push before saying so


def env():
    out = {}
    for ln in ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {}


def save(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


def open_items(d=None):
    d = load() if d is None else d
    return [i for i in d.get("items") or [] if i.get("status") != "completed"]


def ha(action, items=()):
    """Fire the HA webhook. HA answers 200 with no body whatever the automation then does, so
    success is judged from the list it pushes back, not from this call."""
    e = env()
    url = "%s/api/webhook/%s" % (e["SHOPPING_HA"], e["SHOPPING_WEBHOOK"])
    body = json.dumps({"action": action, "items": list(items)}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def wait_push(since, check):
    """Wait for a push newer than `since` that satisfies `check(state)`; return the state or None."""
    end = time.time() + CONFIRM_S
    while time.time() < end:
        d = load()
        if d.get("received", 0) > since and check(d):
            return d
        time.sleep(0.5)
    return None


def match(name, items):
    q = name.strip().lower()
    return (next((i for i in items if i["summary"].strip().lower() == q), None)
            or next((i for i in items if q in i["summary"].lower()), None))


def text(show_done=False):
    d = load()
    if not d:
        return "🛒 I have not heard from Home Assistant yet, so I can't show the list."
    if not d.get("exists", True):
        w = pending()
        return ("🛒 Home Assistant has no shopping list yet (Settings → Devices & services → Add "
                "integration → Shopping List)." + (" Waiting to be added: " + ", ".join(w) + "." if w else ""))
    items = open_items(d)
    age = time.time() - d.get("received", 0)
    stale = " _(last update from HA %d h ago)_" % (age // 3600) if age > 3 * 3600 else ""
    if not items:
        out = "🛒 The shopping list is empty." + stale
    else:
        out = "🛒 *Shopping list* (%d)%s\n" % (len(items), stale) + "\n".join(
            "• " + i["summary"] + (" _(%s)_" % i["description"] if i.get("description") else "") for i in items)
    if show_done:
        done = [i for i in d.get("items") or [] if i.get("status") == "completed"]
        if done:
            out += "\nTicked: " + ", ".join(i["summary"] for i in done)
    return out


def pending():
    try:
        return json.loads(PENDING.read_text())
    except FileNotFoundError:
        return []


def flush_pending():
    """Send queued items now that the list exists. Called from the receiver, off the request thread."""
    items = pending()
    if not items:
        return
    try:
        ha("add", items)
        PENDING.unlink()
        print("flushed %d queued item(s): %s" % (len(items), ", ".join(items)), flush=True)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
        import tg
        tg.send("🛒 The shopping list exists now; added what was waiting: %s." % ", ".join(items), agent="shopping")
    except Exception as e:
        print("<3>shopping: flushing queued items failed: %s: %s" % (type(e).__name__, e), flush=True)


def split_items(s):
    """"milk, eggs, salt and vinegar crisps" -> ["milk", "eggs", "salt and vinegar crisps"]. Only
    commas, semicolons and newlines separate items: splitting on "and" would cut a product in two."""
    import re
    parts = re.split(r"\s*[,;\n]\s*", s.strip())
    return [p.strip(" .") for p in parts if p.strip(" .")]


def add(names):
    """Add and confirm; returns the open items. Raises RuntimeError if HA does not confirm."""
    if load().get("exists") is False:
        raise RuntimeError("Home Assistant has no shopping list")
    have = {i["summary"].strip().lower() for i in open_items()}
    new = [n for n in names if n.strip().lower() not in have]
    if new:
        since = time.time()
        ha("add", new)
        want = [n.strip().lower() for n in new]
        if wait_push(since, lambda d: all(any(i["summary"].strip().lower() == w for i in open_items(d))
                                          for w in want)) is None:
            raise RuntimeError("HA did not confirm within %d s" % CONFIRM_S)
    return new, [n for n in names if n not in new], open_items()


def clear():
    """Blank the list: every item, open or ticked. Returns how many went."""
    items = [i["summary"] for i in load().get("items") or []]
    if items:
        since = time.time()
        ha("remove", items)
        if wait_push(since, lambda d: not d.get("items")) is None:
            raise RuntimeError("HA did not confirm within %d s" % CONFIRM_S)
    return len(items)


def cmd_add(names):
    if load().get("exists") is False:
        q = pending() + [n for n in names if n not in pending()]
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps(q, ensure_ascii=False))
        print(json.dumps({"queued": names, "why": "Home Assistant has no todo.shopping_list yet; queued "
                          "and added automatically once it exists", "waiting": q}, ensure_ascii=False))
        return
    since = time.time()
    ha("add", names)
    want = [n.strip().lower() for n in names]
    d = wait_push(since, lambda d: all(any(i["summary"].strip().lower() == w for i in open_items(d))
                                       for w in want))
    if d is None:
        sys.exit("sent to HA, but its list did not come back with %s within %d s"
                 % (", ".join(names), CONFIRM_S))
    print(json.dumps({"added": names, "open": [i["summary"] for i in open_items(d)]}, ensure_ascii=False))


def cmd_change(action, names):
    items = open_items() if action == "done" else load().get("items") or []
    hits, missed = [], []
    for n in names:
        i = match(n, items)
        (hits.append(i["summary"]) if i else missed.append(n))
    if hits:
        since = time.time()
        ha(action, hits)
        gone = lambda d: not any(i["summary"] in hits for i in (open_items(d) if action == "done"
                                                                else d.get("items") or []))
        if wait_push(since, gone) is None:
            sys.exit("sent %s to HA, but its list did not confirm it within %d s" % (action, CONFIRM_S))
    print(json.dumps({action: hits, "not_on_list": missed, "open": [i["summary"] for i in open_items()]},
                     ensure_ascii=False))


class H(http.server.BaseHTTPRequestHandler):
    server_version = "shopping"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.path != "/api/shopping" or self.client_address[0] not in HA_ADDRS:
            return self.reply(403, {"error": "forbidden"})
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            items = [{"summary": i.get("summary", ""), "status": i.get("status"),
                      "description": i.get("description"), "uid": i.get("uid")}
                     for i in d.get("items") or [] if i.get("summary")]
            save({"received": time.time(), "exists": bool(d.get("exists", True)), "items": items,
                  "ha_ts": d.get("ts")})
            print("push: %d items (%d open), exists=%s" % (len(items), len(open_items()), d.get("exists")),
                  flush=True)
            if d.get("exists", True) and pending():
                import threading
                threading.Thread(target=flush_pending, daemon=True).start()
            return self.reply(200, {"ok": True})
        except Exception as e:
            print("<3>shopping: bad push from HA: %s: %s" % (type(e).__name__, e), flush=True)
            return self.reply(400, {"error": str(e)[:200]})

    def do_GET(self):
        if self.path == "/api/shopping" and self.client_address[0] in ("127.0.0.1",):
            return self.reply(200, load())
        return self.reply(404, {"error": "not found"})


def main(a):
    if not a:
        sys.exit(__doc__)
    if a[0] == "serve":
        print("shopping receiver on :%d" % PORT, flush=True)
        http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    elif a[0] == "add" and len(a) > 1:
        cmd_add(a[1:])
    elif a[0] in ("done", "remove") and len(a) > 1:
        cmd_change(a[0], a[1:])
    elif a[0] == "clear-done":
        since = time.time()
        ha("clear_done")
        ok = wait_push(since, lambda d: not any(i.get("status") == "completed" for i in d.get("items") or []))
        print("cleared" if ok else "sent; HA did not confirm within %d s" % CONFIRM_S)
    elif a[0] == "list":
        d = load()
        print(json.dumps({"open": [i["summary"] for i in open_items(d)],
                          **({"all": d.get("items")} if "--all" in a else {}),
                          "updated": datetime.datetime.fromtimestamp(d.get("received", 0)).isoformat(timespec="minutes")},
                         ensure_ascii=False))
    elif a[0] == "text":
        print(text("--all" in a))
    elif a[0] == "refresh":
        ha("push")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
