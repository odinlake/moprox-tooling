#!/usr/bin/env python3
"""The Yoto's play history, for mo.lan/yoto/stats. Yoto itself keeps none (its API has no history
endpoint), so the record is Home Assistant's: the core Yoto integration's media_player, whose state
and attributes HA's recorder logs. HA purges its recorder after ~10 days, so sync() copies the
history into our own SQLite and it is kept here for good.

    playlog.py sync         pull new history from HA (yoto-web also does this itself)
    playlog.py stats [days] print the stats JSON the page renders

HA access: ~/.config/claude-dev/ha.env, HA_TOKEN (a long-lived token the operator created) and
optional HA_URL (default http://ha.lan:8123) and HA_YOTO_ENTITY (default: the only media_player
whose entity id contains "yoto", else it must be set).

What is stored: one row per CHANGE of (state, title, card, volume). HA writes a row for every
attribute update, position included, so consecutive identical tuples are collapsed on the way in.
`volume` is the raw hardware step: HA's volume_level is the step over the player's absolute
maximum of 16 (yoto_api HARDWARE_VOLUME_MAX), NOT over the day/night cap, so step = level * 16.
"""
import datetime, json, os, sqlite3, sys, threading, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HA_ENV = os.path.expanduser("~/.config/claude-dev/ha.env")
DB = os.environ.get("YOTO_PLAYLOG_DB") or os.path.expanduser("~/.local/share/moprox/yoto/playlog.sqlite")
TZ = ZoneInfo("Europe/London")
HW_VOLUME_MAX = 16
FIRST_SYNC_DAYS = 10          # HA's default recorder retention; asking further back returns nothing
PLAY_GAP = 30 * 60            # the same chapter resumed within this is one play, not two
SESSION_GAP = 15 * 60         # playing stretches closer than this are one listening session
_lock = threading.Lock()


def ha_env():
    d = {}
    try:
        for ln in open(HA_ENV):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    except FileNotFoundError:
        pass                                        # benign: sync() then raises "no HA_TOKEN" itself
    return d


def _ha(path, e):
    url = e.get("HA_URL", "http://ha.lan:8123").rstrip("/") + path
    r = urllib.request.urlopen(urllib.request.Request(
        url, headers={"Authorization": "Bearer " + e["HA_TOKEN"]}), timeout=30)
    return json.loads(r.read())


def entity(e):
    if e.get("HA_YOTO_ENTITY"):
        return e["HA_YOTO_ENTITY"]
    ids = [s["entity_id"] for s in _ha("/api/states", e)
           if s["entity_id"].startswith("media_player.") and "yoto" in s["entity_id"]]
    if len(ids) != 1:
        raise RuntimeError("set HA_YOTO_ENTITY in %s; yoto media players found: %s" % (HA_ENV, ids or "none"))
    return ids[0]


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("create table if not exists rows (ts real primary key, state text, title text,"
              " album text, artist text, volume integer)")
    c.execute("create table if not exists meta (k text primary key, v text)")
    return c


def meta(c, k, v=None):
    if v is None:
        r = c.execute("select v from meta where k=?", (k,)).fetchone()
        return r[0] if r else None
    c.execute("insert or replace into meta values (?,?)", (k, str(v)))


def _row(s):
    a = s.get("attributes") or {}
    lvl = a.get("volume_level")
    ts = datetime.datetime.fromisoformat(s.get("last_updated") or s["last_changed"]).timestamp()
    return (ts, s.get("state"), a.get("media_title"), a.get("media_album_name"), a.get("media_artist"),
            None if lvl is None else round(lvl * HW_VOLUME_MAX))


def sync():
    """Pull every state/attribute change since the last sync. Returns rows added."""
    with _lock:
        e = ha_env()
        if not e.get("HA_TOKEN"):
            raise RuntimeError("no HA_TOKEN in %s" % HA_ENV)
        c = db()
        ent = entity(e)
        now = time.time()
        since = float(meta(c, "synced_to") or now - FIRST_SYNC_DAYS * 86400) - 60   # small overlap
        q = urllib.parse.urlencode({"filter_entity_id": ent, "significant_changes_only": "0",
                                    "end_time": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()})
        start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc).isoformat()
        hist = _ha("/api/history/period/%s?%s" % (urllib.parse.quote(start), q), e)
        last = c.execute("select ts, state, title, album, artist, volume from rows order by ts desc limit 1").fetchone()
        prev = last[1:] if last else None
        added = 0
        for s in sorted((hist[0] if hist else []), key=lambda s: s.get("last_updated") or ""):
            r = _row(s)
            if last and r[0] <= last[0]:
                continue                            # the overlap, and HA's synthetic "state at start"
            if r[1:] == prev:
                continue
            c.execute("insert or ignore into rows values (?,?,?,?,?,?)", r)
            prev, added = r[1:], added + 1
        meta(c, "synced_to", now)
        meta(c, "entity", ent)
        c.commit()
        c.close()
        return added


# --- stats -----------------------------------------------------------------------------------------

def _segments(c, t0, t1):
    """(start, end, state, title, album, volume) covering [t0, t1], clipped. The row in force at t0
    is included; the last segment ends at t1, which the caller caps at the last sync."""
    before = c.execute("select * from rows where ts < ? order by ts desc limit 1", (t0,)).fetchone()
    rows = ([before] if before else []) + c.execute(
        "select * from rows where ts >= ? and ts < ? order by ts", (t0, t1)).fetchall()
    out = []
    for i, (ts, st, title, album, artist, vol) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else t1
        s, e = max(ts, t0), min(end, t1)
        if e > s:
            out.append((s, e, st, title, album, vol))
    return out


def _split_hours(s, e):
    """Yield (local datetime of the hour, seconds) for [s, e), split at local hour boundaries."""
    while s < e:
        d = datetime.datetime.fromtimestamp(s, TZ)
        nxt = (d.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)).timestamp()
        cut = min(e, nxt)
        yield d, cut - s
        s = cut


def stats(days=30):
    c = db()
    synced = float(meta(c, "synced_to") or 0)
    first = c.execute("select min(ts) from rows").fetchone()[0]
    t1 = min(time.time(), synced) if synced else time.time()
    if days:
        t0 = t1 - days * 86400
    else:
        t0 = first or t1
    segs = _segments(c, t0, t1)
    play = [x for x in segs if x[2] == "playing"]

    cards, chapters, vols = {}, {}, {}
    heat = [[0] * 24 for _ in range(7)]
    daily = {}
    plays = sessions = 0
    last_play = None                                # (title, album, end)
    last_end = None
    for s, e, _, title, album, vol in play:
        dur = e - s
        album = album or "(unknown card)"
        title = title or "(unknown)"
        new = not (last_play and last_play[:2] == (title, album) and s - last_play[2] <= PLAY_GAP)
        plays += new
        sessions += (last_end is None or s - last_end > SESSION_GAP)
        last_play, last_end = (title, album, e), e
        k = cards.setdefault(album, {"album": album, "listen_s": 0, "plays": 0, "last": 0})
        k["listen_s"] += dur; k["plays"] += new; k["last"] = max(k["last"], e)
        ch = chapters.setdefault((album, title), {"album": album, "title": title, "listen_s": 0, "plays": 0, "last": 0})
        ch["listen_s"] += dur; ch["plays"] += new; ch["last"] = max(ch["last"], e)
        if vol is not None:
            vols[vol] = vols.get(vol, 0) + dur
        for d, secs in _split_hours(s, e):
            heat[d.weekday()][d.hour] += secs
            day = d.date().isoformat()
            daily[day] = daily.get(day, 0) + secs

    # every day in range, zeros included, so gaps read as gaps
    # ...but not before recording began: those days are unknown, not silent
    days_list, d = [], datetime.datetime.fromtimestamp(max(t0, first or t0), TZ).date()
    end_day = datetime.datetime.fromtimestamp(t1, TZ).date()
    while d <= end_day:
        days_list.append({"date": d.isoformat(), "listen_s": round(daily.get(d.isoformat(), 0))})
        d += datetime.timedelta(days=1)

    l0 = t1 - 86400
    last24 = [{"s": s, "e": e, "state": st, "title": ti, "album": al, "volume": v}
              for s, e, st, ti, al, v in _segments(c, l0, t1)]
    c.close()
    rnd = lambda xs: [dict(x, listen_s=round(x["listen_s"])) for x in xs]
    return {
        "range": {"days": days, "start": t0, "end": t1},
        "synced_to": synced or None, "first_ts": first,
        "totals": {"listen_s": round(sum(e - s for s, e, *_ in play)), "plays": plays, "sessions": sessions,
                   "active_days": sum(1 for x in days_list if x["listen_s"] > 0), "days": len(days_list)},
        "cards": rnd(sorted(cards.values(), key=lambda x: -x["listen_s"])[:15]),
        "chapters": rnd(sorted(chapters.values(), key=lambda x: -x["listen_s"])[:30]),
        "heat": [[round(v) for v in row] for row in heat],
        "daily": days_list,
        "volume": {str(k): round(v) for k, v in sorted(vols.items())},
        "last24": {"start": l0, "end": t1, "segments": last24},
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":
        print("added %d rows" % sync())
    elif cmd == "stats":
        print(json.dumps(stats(int(sys.argv[2]) if len(sys.argv) > 2 else 30), indent=1, ensure_ascii=False))
    else:
        sys.exit(__doc__)
