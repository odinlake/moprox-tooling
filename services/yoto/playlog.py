#!/usr/bin/env python3
"""The Yoto's play history, for mo.lan/yoto/stats. Yoto itself keeps none (its API has no history
endpoint), so the record comes from Home Assistant's core Yoto integration media_player, and is kept
here in our own SQLite for good (HA purges its recorder after ~10 days).

HOW IT ARRIVES: HA PUSHES it. private-config-ha packages/yoto_playlog.yaml posts every change of
state / title / card / volume to yoto-web's POST /api/playlog, which calls record(). No HA token is
needed on this side; the operator asked not to be asked for one.

sync() is an optional backfill that PULLS the same history over HA's REST API. It only runs when a
token happens to exist in ~/.config/claude-dev/ha.env (HA_TOKEN, optional HA_URL / HA_YOTO_ENTITY);
without one it does nothing. Rows from either path land in the same table and dedupe on timestamp.

    playlog.py sync         backfill from HA, if a token exists
    playlog.py stats [days] print the stats JSON the page renders

What is stored: one row per CHANGE of (state, title, card, volume). HA writes a row for every
attribute update, position included, so consecutive identical tuples are collapsed on the way in.
`volume` is the raw hardware step: HA's volume_level is the step over the player's absolute
maximum of 16 (yoto_api HARDWARE_VOLUME_MAX), NOT over the day/night cap, so step = level * 16.

PLAYERS (2026-10-23, when the travel Mini joined the account): every row and config row carries
`player`, the Yoto deviceId it came from. Rows from before then have NULL, which means the main
player (YOTO_DEVICE_ID): HA's automation fires for every Yoto media_player, and until then there was
only one. Every query goes through _is(), which reads NULL as `main`.
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
    # The player's day/night settings, one row per CHANGE (record_config). Recorded from 2026-10-02;
    # nights before the first row are drawn with the earliest known settings and marked assumed.
    c.execute("create table if not exists yoto_config (ts real primary key, day_time text, night_time text,"
              " day_max integer, night_max integer)")
    # sleepguard.py's own window (it is NOT tied to the Yoto's night mode; operator 2026-10-02), one
    # row per change, written by sleepguard itself on every run.
    c.execute("create table if not exists guard_config (ts real primary key, start text, end text,"
              " deep_start text, deep_end text)")
    for t in ("rows", "yoto_config"):
        if "player" not in [r[1] for r in c.execute("pragma table_info(%s)" % t)]:
            c.execute("alter table %s add column player text" % t)
    return c


def _is(player, main):
    """SQL condition + params for "this row is from `player`"; NULL rows are the main player's."""
    return "coalesce(player, ?) = ?", (main or "", player or main or "")


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


def record(p, player=None, main=None):
    """One pushed change from HA: {ts, state, title, album, artist, volume_level}, from `player`
    (a deviceId; None = main). Returns True if stored, False if it repeats that player's row
    before it (HA sends one per attribute update)."""
    lvl = p.get("volume_level")
    txt = lambda k: (p.get(k) or "").strip() or None       # Yoto card titles carry trailing spaces
    r = (float(p["ts"]), str(p["state"]), txt("title"), txt("album"), txt("artist"),
         None if lvl in (None, "") else round(float(lvl) * HW_VOLUME_MAX))
    with _lock:
        c = db()
        try:
            w, a = _is(player, main)
            prev = c.execute("select state, title, album, artist, volume from rows where ts < ? and " + w +
                             " order by ts desc limit 1", (r[0],) + a).fetchone()
            if prev == r[1:]:
                return False
            c.execute("insert or ignore into rows (ts, state, title, album, artist, volume, player)"
                      " values (?,?,?,?,?,?,?)", r + (None if player == main else player,))
            meta(c, "synced_to", time.time())
            c.commit()
            return True
        finally:
            c.close()


def sync():
    """Optional backfill: pull every change since the last sync over HA's REST API. Returns rows
    added, or None when no token is configured (the normal case: HA pushes instead)."""
    with _lock:
        e = ha_env()
        if not e.get("HA_TOKEN"):
            return None
        c = db()
        ent = entity(e)
        now = time.time()
        since = float(meta(c, "synced_to") or now - FIRST_SYNC_DAYS * 86400) - 60   # small overlap
        q = urllib.parse.urlencode({"filter_entity_id": ent, "significant_changes_only": "0",
                                    "end_time": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()})
        start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc).isoformat()
        hist = _ha("/api/history/period/%s?%s" % (urllib.parse.quote(start), q), e)
        last = c.execute("select ts, state, title, album, artist, volume from rows where player is null"
                         " order by ts desc limit 1").fetchone()
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


def record_config(cfg, player=None, main=None):
    """Store a player's day/night settings if they differ from its last stored row. `cfg` is
    /device-v2/<id>/config's `config` object. Returns True when a change was stored."""
    row = (cfg.get("dayTime"), cfg.get("nightTime"),
           int(cfg["maxVolumeLimit"]) if cfg.get("maxVolumeLimit") else None,
           int(cfg["nightMaxVolumeLimit"]) if cfg.get("nightMaxVolumeLimit") else None)
    if not row[0] or not row[1]:
        raise ValueError("Yoto config has no dayTime/nightTime: %s" % sorted(cfg)[:20])
    with _lock:
        c = db()
        try:
            w, a = _is(player, main)
            last = c.execute("select day_time, night_time, day_max, night_max from yoto_config where " + w +
                             " order by ts desc limit 1", a).fetchone()
            if last == row:
                return False
            c.execute("insert into yoto_config (ts, day_time, night_time, day_max, night_max, player)"
                      " values (?,?,?,?,?,?)", (time.time(),) + row + (None if player == main else player,))
            c.commit()
            return True
        finally:
            c.close()


# The guard went live in moprox-tooling c76fd73 at this instant with 20:30-08:00 (20-min timers
# 22:00-05:00) and that window did not change before recording began on 2026-10-02, so the first row
# is back-dated to it: those nights are KNOWN, not assumed. Nights before it had no guard at all.
GUARD_BORN = datetime.datetime(2026, 9, 28, 20, 56, 33, tzinfo=TZ).timestamp()
GUARD_FIRST = ("20:30", "08:00", "22:00", "05:00")


def record_guard(start, end, deep_start, deep_end):
    """Store sleepguard's window if it differs from the last stored row. True when stored."""
    row = tuple(t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)
                for t in (start, end, deep_start, deep_end))
    with _lock:
        c = db()
        try:
            last = c.execute("select start, end, deep_start, deep_end from guard_config"
                             " order by ts desc limit 1").fetchone()
            if last == row:
                return False
            ts = GUARD_BORN if last is None and row == GUARD_FIRST else time.time()
            c.execute("insert into guard_config values (?,?,?,?,?)", (ts,) + row)
            c.commit()
            return True
        finally:
            c.close()


def _guard_at(guards, t):
    """(start, end) of the guard in force at t, or (None, None) before it existed."""
    cur = None
    for r in guards:
        if r[0] <= t:
            cur = r
    return (cur[1], cur[2]) if cur else (None, None)


def _configs(c, player=None, main=None):
    w, a = _is(player, main)
    return c.execute("select ts, day_time, night_time, day_max, night_max from yoto_config where " + w +
                     " order by ts", a).fetchall()


def _config_at(cfgs, t):
    """(day_time, night_time, assumed) in force at t."""
    if not cfgs:
        return None, None, True
    cur = None
    for r in cfgs:
        if r[0] <= t:
            cur = r
    if cur is None:
        return cfgs[0][1], cfgs[0][2], True
    return cur[1], cur[2], False


SLOT = 600                                   # the night map's resolution: 10 minutes
SLOTS = 86400 // SLOT                        # 144 per day, midnight to midnight


def _nights(c, segs, t0, t1, first, player=None, main=None):
    """One column per DAY, midnight to midnight local. Each: date, 144 ten-minute slots of seconds
    listened, and the Yoto's day/night times that day. (2026-10-02 to 10-04 this was noon to noon,
    keeping a night in one column; the operator asked for midnight back at the edges on 10-04.)"""
    # Every night in the window, ending with TODAY's (the rightmost column is today's weekday,
    # operator 2026-10-02), including nights before recording began: those are shown empty, flagged
    # `recorded: false`, and left out of weekday averages rather than counted as silent.
    end = datetime.datetime.fromtimestamp(t1, TZ).date()
    begin = datetime.datetime.fromtimestamp(t0, TZ).date() + datetime.timedelta(days=1)
    rec_from = datetime.datetime.fromtimestamp(first, TZ).date() if first else end
    dates, d = [], min(begin, end - datetime.timedelta(days=6))
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=1)
    idx = {d: i for i, d in enumerate(dates)}
    slots = [[0.0] * SLOTS for _ in dates]
    for s, e, st, *_ in segs:
        if st != "playing":
            continue
        while s < e:
            dt = datetime.datetime.fromtimestamp(s, TZ)
            # The slot's end in real seconds, not wall-clock arithmetic: when the clocks go back,
            # 01:50 + 10 min on the wall is 02:00 GMT, an hour and ten minutes later, and that
            # whole stretch landed in one slot (25 Oct 2026: 4 h in 01:50-02:00).
            cut = min(e, s - (dt.minute % 10) * 60 - dt.second - dt.microsecond / 1e6 + SLOT)
            day = dt.date()
            k = (dt.hour * 60 + dt.minute) // 10
            if day in idx:
                slots[idx[day]][k] += cut - s
            s = cut
    cfgs = _configs(c, player, main)
    # sleepguard only ever drives the main player
    guards = c.execute("select ts, start, end from guard_config order by ts").fetchall() \
        if (player or main) == main else []
    out = []
    for d, sl in zip(dates, slots):
        noon = datetime.datetime(d.year, d.month, d.day, 12, tzinfo=TZ).timestamp()
        day_t, night_t, assumed = _config_at(cfgs, noon + 12 * 3600)
        g_start, g_end = _guard_at(guards, noon + 12 * 3600)
        out.append({"date": d.isoformat(), "slots": [round(x) for x in sl], "recorded": d >= rec_from,
                    "day_time": day_t, "night_time": night_t, "assumed": assumed,
                    "guard_start": g_start, "guard_end": g_end})
    return out


# --- stats -----------------------------------------------------------------------------------------

ROW = "select ts, state, title, album, artist, volume from rows where "


def _segments(c, t0, t1, player=None, main=None):
    """(start, end, state, title, album, volume) of one player covering [t0, t1], clipped. The row in
    force at t0 is included; the last segment ends at t1, which the caller caps at the last sync."""
    w, a = _is(player, main)
    before = c.execute(ROW + "ts < ? and " + w + " order by ts desc limit 1", (t0,) + a).fetchone()
    rows = ([before] if before else []) + c.execute(
        ROW + "ts >= ? and ts < ? and " + w + " order by ts", (t0, t1) + a).fetchall()
    out = []
    for i, (ts, st, title, album, artist, vol) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else t1
        s, e = max(ts, t0), min(end, t1)
        if e > s:
            out.append((s, e, st, title, album, vol))
    return out


SHUFFLE_SUFFIX = " (shuffle)"
MUTE_BLIP = 3                 # the player itself reports volume 0 for 0.1-2 s at every chapter change
                              # and start (MQTT events, 2026-10-14): a mute between tracks, not a person


def _blip(s, e, vol):
    return vol == 0 and e - s < MUTE_BLIP


def _attribution(c):
    """A function (title, album) -> album that names the PLAYLIST a chapter belongs to.

    The ad-hoc one-shot cards myo.queue() makes are deleted after the next play, and HA only learns a
    card's name from its library, so a chapter played from one often arrives with no album at all
    (all the album-less Halvan chapters of 2026-10-03 and 10-10). The older "(shuffle)" twins carry
    their own name. Either way the chapter TITLE still says which playlist it is: map it through
    every row where the album was a real one. Nothing to go on leaves the title as the card's name
    (Yoto's own daily shows, e.g. "Wake with Jake", never report an album)."""
    seen = {}
    for title, album, n in c.execute("select title, album, count(*) from rows where title is not null"
                                     " and album is not null group by title, album"):
        if not album.endswith(SHUFFLE_SUFFIX):
            seen.setdefault(title, []).append((n, album))
    home = {t: max(v)[1] for t, v in seen.items()}

    def album_of(title, album):
        if album and not album.endswith(SHUFFLE_SUFFIX):
            return album
        if title in home:
            return home[title]
        if album:
            return album[:-len(SHUFFLE_SUFFIX)]
        return title
    return album_of


def _tidy(segs, album_of):
    """The segments as a person would describe them. Every album goes through album_of; the
    metadata-less second at the start of a play takes the chapter that follows it; and runs of
    the same chapter in the same state are ONE segment, however often the volume moved in it (the
    sleep fade writes eight one-second rows, a start often a 0-then-8 blip). A merged segment
    keeps its longest-held volume, plus vmin/vmax when the volume moved."""
    segs = [list(x) for x in segs]
    for i in range(len(segs) - 2, -1, -1):          # backwards: a run of blank rows all fill
        s, n = segs[i], segs[i + 1]
        if s[3] is None and s[4] is None and s[2] == n[2] == "playing" and s[1] == n[0] and n[1] - s[0] < 3600:
            s[3], s[4] = n[3], n[4]
    out = []
    for s, e, st, title, album, vol in segs:
        album = album_of(title, album) if (title or album) else None
        p = out[-1] if out else None
        if p and p["e"] == s and (p["state"], p["title"], p["album"]) == (st, title, album):
            p["e"] = e
        else:
            p = {"s": s, "e": e, "state": st, "title": title, "album": album, "_v": {}}
            out.append(p)
        if vol is not None and not _blip(s, e, vol):
            p["_v"][vol] = p["_v"].get(vol, 0) + e - s
    for p in out:
        v = p.pop("_v")
        p["volume"] = max(v, key=v.get) if v else None
        if len(v) > 1:
            p["vmin"], p["vmax"] = min(v), max(v)
    return out


def _split_hours(s, e):
    """Yield (local datetime of the hour, seconds) for [s, e), split at local hour boundaries."""
    while s < e:
        d = datetime.datetime.fromtimestamp(s, TZ)
        nxt = (d.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)).timestamp()
        cut = min(e, nxt)
        yield d, cut - s
        s = cut


def players(main=None):
    """{deviceId: rows} for every player with a row; the NULL rows count as main's."""
    c = db()
    try:
        out = {}
        for p, n in c.execute("select coalesce(player, ?), count(*) from rows group by 1", (main or "",)):
            out[p] = out.get(p, 0) + n
        return out
    finally:
        c.close()


def stats(days=30, player=None, main=None):
    c = db()
    synced = float(meta(c, "synced_to") or 0)
    w, a = _is(player, main)
    first = c.execute("select min(ts) from rows where " + w, a).fetchone()[0]
    # pushed data is current to now: the last row stays in force until the next push changes it
    t1 = time.time()
    if days:
        t0 = t1 - days * 86400
    else:
        t0 = (first or t1) - 86400
    album_of = _attribution(c)
    segs = _segments(c, t0, t1, player, main)
    play = [(x["s"], x["e"], x["state"], x["title"], x["album"])
            for x in _tidy(segs, album_of) if x["state"] == "playing"]

    cards, chapters, vols = {}, {}, {}
    heat = [[0] * 24 for _ in range(7)]
    daily = {}
    plays = sessions = 0
    last_play = None                                # (title, album, end)
    last_end = None
    for s, e, st, _, _, vol in segs:                # time at each volume: the raw rows, unmerged
        if st == "playing" and vol is not None and not _blip(s, e, vol):
            vols[vol] = vols.get(vol, 0) + e - s
    for s, e, _, title, album in play:
        dur = e - s
        album = album or "(unknown card)"
        title = title or "(unknown)"
        # the first second of a start arrives before the metadata: time counts, a play does not
        known = title != "(unknown)" or album != "(unknown card)"
        new = known and not (last_play and last_play[:2] == (title, album) and s - last_play[2] <= PLAY_GAP)
        plays += new
        sessions += (last_end is None or s - last_end > SESSION_GAP)
        if known:
            last_play = (title, album, e)
        last_end = e
        k = cards.setdefault(album, {"album": album, "listen_s": 0, "plays": 0, "last": 0})
        k["listen_s"] += dur; k["plays"] += new; k["last"] = max(k["last"], e)
        ch = chapters.setdefault((album, title), {"album": album, "title": title, "listen_s": 0, "plays": 0, "last": 0})
        ch["listen_s"] += dur; ch["plays"] += new; ch["last"] = max(ch["last"], e)
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

    nights = _nights(c, segs, t0, t1, first, player, main)
    l0 = t1 - 86400
    last24 = _tidy(_segments(c, l0, t1, player, main), album_of)
    c.close()
    # a blank second with nothing after it to name it stays "(unknown card)"; at 0 s it is noise
    rnd = lambda xs: [dict(x, listen_s=round(x["listen_s"])) for x in xs if x["plays"] or x["listen_s"] >= 0.5]
    return {
        "range": {"days": days, "start": t0, "end": t1},
        "synced_to": synced or None, "first_ts": first,
        "totals": {"listen_s": round(sum(e - s for s, e, *_ in play)), "plays": plays, "sessions": sessions,
                   "active_days": sum(1 for x in days_list if x["listen_s"] > 0), "days": len(days_list)},
        "cards": rnd(sorted(cards.values(), key=lambda x: -x["listen_s"])[:15]),
        "chapters": rnd(sorted(chapters.values(), key=lambda x: -x["listen_s"])[:30]),
        "heat": [[round(v) for v in row] for row in heat],
        "nights": nights,
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
