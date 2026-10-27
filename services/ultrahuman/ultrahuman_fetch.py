#!/usr/bin/env python3
"""Ultrahuman ring fetcher — nightly RHR + sleep (incl. stage split) into private-data.

The route here is the **partner OAuth API**, documented at
https://vision.ultrahuman.com/developer/docs?type=oauth. Register an app in the Vision UI
(Developer -> Create OAuth Application), which yields a confidential client:

  authorize  GET  https://auth.ultrahuman.com/authorise
                    ?response_type=code&client_id&redirect_uri&scope&state      (NO PKCE)
  token      POST https://partner.ultrahuman.com/api/partners/oauth/token       (client_secret)
  data       GET  https://partner.ultrahuman.com/api/partners/v1/user_data/metrics?date=YYYY-MM-DD
             GET  .../user_data/user_info
  scopes     profile ring_data cgm_data

TRAP, cost us a lot of time on 2026-08-02: api.ultrahuman.com ALSO publishes
`/.well-known/oauth-authorization-server`, advertising an entirely DIFFERENT OAuth server for an
MCP endpoint (`sleep:read`-style scopes, open dynamic client registration, PKCE). That path
registers clients and issues refreshable tokens happily, but `POST api.ultrahuman.com/mcp` then
rejects them with `invalid bearer token` even though the same server's /oauth/introspect calls them
active. Do NOT follow discovery here — it is not the API that serves data. This file is the one
that works.

Mirrors technogym_fetch.py / polar_fetch.py: creds in ~/.config/claude-dev, writes into
private-data, NO git commit (private-data-sync.timer sweeps it), NO Telegram post (pure ETL).

Storage, per the 2026-08-02 sizing (53 KiB/day compact, 8.6 KiB gzipped, ~19 MiB/yr raw):
  raw/YYYY/YYYY-MM-DD.json.gz   full API response, gzipped — ~90% of it is the intra-night
                                hr/hrv/temp/movement/sleep graphs. Kept deliberately: those series
                                are what made the earlier ring analysis possible, and dropping them
                                to save ~18 MiB/yr would be a false economy.
  daily.jsonl                   one flat scalar record per day — what the dashboard reads.

  --backfill        refetch days already on disk (default: skip them)
  --since YYYY-MM-DD  start of the window (default: today-10d; account opened 2026-03-21)
  --until YYYY-MM-DD  end of the window (default: today)
"""
import argparse, gzip, json, sys, time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog
from tokenlock import token_lock, write_atomic

ENV    = Path.home() / ".config/claude-dev/ultrahuman.env"
TOKEN  = Path.home() / ".config/claude-dev/ultrahuman-partner-token.json"
OUT    = Path.home() / "projects/private-data/ultrahuman"
RAW    = OUT / "raw"
DAILY  = OUT / "daily.jsonl"

TOKEN_URL = "https://partner.ultrahuman.com/api/partners/oauth/token"
API       = "https://partner.ultrahuman.com/api/partners/v1/user_data"
POLITE_S  = 1.2          # gap between day requests — no documented rate limit, so don't find one
SKEW_S    = 300          # refresh this long before expiry rather than waiting for a 401
# A day is only FINAL once we fetched it well after the day ended. The cloud lags the ring: on
# 2026-08-09 a 10:34 fetch returned HR through 04:03 only, and the 19:20 run skipped the day because
# the file already existed — so every day from 2026-08-02 froze at ~4 h of coverage while the ring
# was worn and syncing normally. Refetching 08-08 afterwards returned 85 points over 23.9 h: the
# data was always there, we just never went back for it. 06:00 the next morning is the margin.
SETTLE_HOUR = 6


def env():
    d = {}
    for ln in ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); d[k] = v.strip()
    return d


def access_token():
    """Return a valid bearer, refreshing (and re-saving) when it is close to expiry.

    The token file is the single source of truth for the refresh token: the server ROTATES it on
    every refresh, so a stale copy is worthless. Write it back before returning, and 0600 it —
    it is a bearer credential for health data.
    """
    def fresh(tok):
        return time.time() < tok["created_at"] + tok["expires_in"] - SKEW_S

    tok = json.loads(TOKEN.read_text())
    if fresh(tok):
        return tok["access_token"]
    # Rotation makes a concurrent refresh fatal (the second one replays a spent token), so the
    # refresh is one critical section with the staleness check repeated inside it -- see tokenlock.
    with token_lock(TOKEN):
        tok = json.loads(TOKEN.read_text())
        if fresh(tok):
            return tok["access_token"]
        e = env()
        r = requests.post(TOKEN_URL, timeout=30, data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": e["UH_CLIENT_ID"], "client_secret": e["UH_CLIENT_SECRET"]})
        r.raise_for_status()
        tok = r.json()
        write_atomic(TOKEN, json.dumps(tok))
    print("refreshed access token", file=sys.stderr)
    return tok["access_token"]


def fetch_day(bearer, d):
    r = requests.get(f"{API}/metrics", params={"date": d.isoformat()},
                     headers={"Authorization": f"Bearer {bearer}"}, timeout=60)
    if r.status_code == 401:
        # errlog.die, not `raise SystemExit(msg)`: SystemExit's argument is printed to stderr with
        # no level prefix and journald files that at info, so the ring lane would go stale with the
        # word "token" nowhere a priority query can reach.
        errlog.die("ultrahuman: 401 from metrics — token rejected; re-run the authorize flow")
    r.raise_for_status()
    return r.json()


def report_tz(payload):
    """The zone the vendor stamps its readings in, as the payload itself declares it.

    The night_rhr staleness test in scalars() compares a vendor epoch to a calendar date, and it
    used `date.fromtimestamp()` — naive HOST-local. The vendor stamps each reading at MIDNIGHT in
    its own reporting zone, so midnight-in-London on a BST day is 23:00 UTC the day before and the
    test's answer was a property of the fetch host rather than of the data. Measured 2026-10-19
    over the 209-day archive (2026-03-21..10-15), same payloads, same predicate, TZ varied:

        Europe/London 12 stale   Europe/Stockholm 12   Asia/Tokyo 12   UTC 183   New_York 188

    171 of 209 days flip fresh -> STALE between London and UTC, never the reverse, every one of
    them a BST day carrying a real non-null reading a UTC run would have nulled; the 8 GMT days in
    the archive flip under no zone at all. Nothing has been lost because ultrahuman-fetch.service
    runs on this box and this box is Europe/London — daily.jsonl agrees with the zone-aware verdict
    on 209/209 and with the shipped-under-UTC one on 38/209. The exposure is a host move, a
    container with TZ=UTC, or a replay off-box. The payload declares the zone on 209/209 days.

    When it does not declare one, fall back to host-local — which is exactly today's behaviour, and
    on this host today's answer is right — rather than to UTC, which is the wrong answer measured
    above. But say so at err: on any other host that fallback IS the defect this removes, and a
    guard that has quietly gone back to being host-dependent is indistinguishable from one working.
    """
    name = (payload.get("data") or {}).get("latest_time_zone")
    if not name:
        errlog.err("ultrahuman: payload declares no data.latest_time_zone — the night_rhr "
                   "staleness verdict falls back to host-local time and is a property of this host")
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        # Not a raise: this kills the whole day's record — HR/HRV/sleep series included — over one
        # scalar's freshness flag, and the day would then be skipped as already-on-disk-absent.
        errlog.err(f"ultrahuman: data.latest_time_zone {name!r} is not a known zone — the "
                   f"night_rhr staleness verdict falls back to host-local time", exc)
        return None


def scalars(d, payload):
    """Flatten one day's response to the numbers the dashboard actually plots.

    Everything else stays in the gzipped raw file. `sleep_stages` carries seconds AND percent per
    stage; we keep seconds (percent is derivable and rounds badly).
    """
    by = {m["type"]: m.get("object") for m in payload.get("data", {}).get("metric_data", [])}
    rec = {"date": d.isoformat()}

    def val(k):
        o = by.get(k)
        return o.get("value") if isinstance(o, dict) else None

    rec["sleep_rhr"]       = val("sleep_rhr")
    rec["avg_sleep_hrv"]   = val("avg_sleep_hrv")
    rec["recovery_index"]  = val("recovery_index")
    rec["movement_index"]  = val("movement_index")
    rec["vo2_max"]         = val("vo2_max")
    rec["active_minutes"]  = val("active_minutes")

    # `morning_alertness` is the vendor's display title and it asserts the wrong polarity. The
    # object is labelled `unit: "minutes"` on 177/177 archived days and the values run 2..107, so
    # it is sleep INERTIA — minutes from waking to being alert — and higher is worse. Ultrahuman
    # grades it itself in the same payload (Sleep.sleep_inertia_interpretation.title): its "good"
    # titles span 2..13, its "bad" ones 47..107, no overlap. Stored under that name with no unit
    # anywhere in the row, the only reading left to a later consumer is the inverted one. So the
    # unit goes in the key, as it already does elsewhere in this record (hr_gap_max_min, *_s).
    #
    # `status` travels with it because a null here has five distinguishable causes and they are
    # not the same fact: non_wear_interrupt (3) and charging_interrupt (2) say the ring was off,
    # low_activity (4) and missing_data_interrupt (7) say the night was not scorable, and `pending`
    # (3) says the cloud has not computed it yet and the day is worth refetching. The bare
    # passthrough collapsed all five, plus the 32 days with no status at all, into one None.
    ma = by.get("morning_alertness")
    ma = ma if isinstance(ma, dict) else {}
    rec["morning_inertia_min"]    = ma.get("value")
    rec["morning_inertia_status"] = ma.get("status")

    # night_rhr is ECHOED, not recomputed: when the ring stops delivering, the partner API keeps
    # serving the last real reading — same value, same ORIGINAL timestamp — for every later date.
    # Measured 2026-08-21: 08-18..08-21 all returned avg 52 stamped 2026-08-17T00:00, on days whose
    # hr/steps/hrv/Sleep were all empty. Taken at face value that reads as "RHR 52 every night" and,
    # worse, it is one of the five fields the freshness lane accepts as proof a row has data — so a
    # dead lane could never go stale. Keep the value only if its own timestamp falls in the day.
    nr = by.get("night_rhr") if isinstance(by.get("night_rhr"), dict) else {}
    rec["night_rhr_avg"] = nr.get("avg")
    # The series is a trailing window stamped at each day's midnight and, on a live day, it ENDS on
    # the requested date. So the test is simply: does it contain this date at all? — resolved in
    # the zone the payload declares, NOT the host's, see report_tz().
    stamps = [v.get("timestamp") for v in (nr.get("values") or []) if isinstance(v, dict)]
    if rec["night_rhr_avg"] is not None and stamps:
        tz = report_tz(payload)
        if not any(datetime.fromtimestamp(s, tz).date() == d for s in stamps if s):
            rec["night_rhr_avg"] = None
            rec["night_rhr_stale"] = True
    st = by.get("steps") or {}
    rec["steps"] = st.get("total") if isinstance(st, dict) else None
    hv = by.get("hrv") or {}
    rec["hrv_avg"] = hv.get("avg") if isinstance(hv, dict) else None

    # Coverage of the intra-day HR series. Recorded because the failure this lane actually had was
    # not a missing day — every day was present, with a sleep summary, and only the series behind it
    # was truncated. A freshness check can see this; a row-count cannot.
    #
    # hr_span_h is FIRST-TO-LAST and so is blind to everything between: on 2026-08-14 it reads 23.90
    # off 84 samples whose largest single hole is 894 min. It answers "was the day truncated?", which
    # was the 2026-08-09 fault, and nothing about whether the day was actually sampled. The two
    # fields below are the interior view, on the same timestamps:
    #   hr_cov_5m       fraction of the 5-min bins between first and last sample that hold a sample
    #   hr_gap_max_min  largest gap between consecutive samples
    # Span and coverage disagree hard in the current regime — over the 92 archive days with
    # span >= 23 h, median hr_cov_5m is 0.44 and median hr_gap_max_min is 480.
    hro = by.get("hr") or {}
    hrv_ = (hro.get("values") if isinstance(hro, dict) else None) or []
    tss = sorted(x["timestamp"] for x in hrv_ if isinstance(x, dict) and x.get("timestamp"))
    rec["hr_points"] = len(hrv_)
    rec["hr_span_h"] = round((tss[-1] - tss[0]) / 3600.0, 2) if len(tss) > 1 else 0.0
    if len(tss) > 1:
        bins = len({t // 300 for t in tss})
        total = (tss[-1] // 300) - (tss[0] // 300) + 1
        rec["hr_cov_5m"] = round(bins / total, 3)
        rec["hr_gap_max_min"] = round(max(b - a for a, b in zip(tss, tss[1:])) / 60.0, 1)
    else:
        rec["hr_cov_5m"] = 0.0
        rec["hr_gap_max_min"] = None

    # Skin temperature, counted and not averaged, because the only consumer is the freshness lane's
    # `require` and what it needs is "did the ring report at all". It is here because temp is the one
    # series this flat row did not expose, and on 2026-05-25 it is the ONLY evidence the ring
    # reported: 10 temp samples at 30.6 degC, hr/hrv/steps/Sleep all empty, so every one of that
    # lane's four certifiers is null except `steps`, which reads 0.0. Tighten `steps` alone and that
    # day becomes a measurement the lane cannot see. Unique in the 209-day archive, measured
    # 2026-10-15.
    #
    # Non-zero bins, not len(): an echoed all-zero grid is exactly how `steps` certified five empty
    # days (see has_measurement). temp has never arrived all-zero, so this is hygiene against the
    # class rather than a fix for an observed break. hr_points stays a plain count — the
    # ultrahuman-coverage lane calibrates a density floor on it and 0 is a real HR reading's
    # neighbour there, not a missing one.
    tmo = by.get("temp") or {}
    rec["temp_points"] = sum(
        1 for v in ((tmo.get("values") if isinstance(tmo, dict) else None) or [])
        if isinstance((v.get("value") if isinstance(v, dict) else v), (int, float))
        and (v.get("value") if isinstance(v, dict) else v) != 0)

    s = by.get("Sleep") or {}
    if isinstance(s, dict):
        rec["bedtime_start"] = s.get("bedtime_start")
        rec["bedtime_end"]   = s.get("bedtime_end")
        for st_ in s.get("sleep_stages") or []:
            rec[f"{st_['type']}_s"] = st_.get("stage_time")
        # quick_metrics titles are display strings; map the ones we rely on
        qm = {q.get("title"): q.get("value") for q in (s.get("quick_metrics") or [])
              if isinstance(q, dict)}
        rec["time_in_bed_s"] = qm.get("TIME IN BED")
        rec["total_sleep_s"] = qm.get("TOTAL SLEEP")
        rec["efficiency"]    = qm.get("EFFICIENCY")
        rec["sleep_avg_hr"]  = qm.get("AVG HEART RATE")
        rec["sleep_avg_hrv"] = qm.get("AVG HRV")
    return rec


def has_measurement(payload):
    """Does this payload contain anything the ring actually measured?

    Deliberately not "is the response non-empty": a dead day still returns all 16 metric_data
    entries, and night_rhr still carries an echoed value (see scalars()). Only the series-bearing
    metrics count.

    Nor is a PRESENT series enough. On a day the ring never reported, the steps grid still arrives
    fully binned with every value 0.0, and a truthiness test on `values` accepted that as proof the
    ring measured — sealing five empty days (2026-06-21, 06-22, 07-03, 07-04, 07-05) as final
    against the very ten-day re-fetch window is_final() keeps open for this case. A series counts
    only if some bin carries a non-zero sample. Measured 2026-10-15 over all 209 archived payloads:
    `steps` is the only metric that ever arrives all-zero (15 days), so this costs hr/hrv/temp no
    verdict, and the 10 all-zero-steps days that did sample still certify on hr or temp.
    """
    by = {m.get("type"): m.get("object") for m in
          (payload.get("data") or {}).get("metric_data") or []}
    for k in ("hr", "steps", "hrv", "temp"):
        o = by.get(k)
        if not isinstance(o, dict):
            continue
        if o.get("value") is not None:
            return True
        for v in o.get("values") or []:
            x = v.get("value") if isinstance(v, dict) else v
            if isinstance(x, (int, float)) and x != 0:
                return True
    s = by.get("Sleep")
    return bool(isinstance(s, dict) and s)


def is_final(f, d):
    """Was this day fetched late enough that it cannot still be filling in — AND did it arrive?

    `f.exists()` is NOT the test — that is what froze eight days of partial data. A file written at
    10:34 on the day itself holds whatever the cloud had by then, which is typically only the
    overnight stretch.

    An EMPTY day is never final either, and that is not a nicety. 2026-08-18..21 were fetched the
    following morning, after the settle hour, while the ring was not syncing to the phone at all —
    so by mtime alone they were sealed as complete, holding nothing. The moment the ring syncs,
    Ultrahuman backfills those dates and this fetcher would have skipped every one of them: the
    nights would exist upstream and be permanently absent here, recoverable only by someone
    remembering to pass --backfill. A day with no measurement in it stays open for the ten-day
    window main() scans; the cost is a handful of extra API calls, against silently losing nights.
    """
    if not f.exists():
        return False
    settled = datetime.combine(d + timedelta(days=1), dtime(SETTLE_HOUR, 0)).timestamp()
    if f.stat().st_mtime < settled:
        return False
    try:
        return has_measurement(json.loads(gzip.decompress(f.read_bytes())))
    except Exception as exc:
        errlog.warn(f"ultrahuman: cannot read stored payload {f.name} ({exc}) — will re-fetch")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args()

    until = date.fromisoformat(a.until) if a.until else date.today()
    since = date.fromisoformat(a.since) if a.since else until - timedelta(days=10)

    RAW.mkdir(parents=True, exist_ok=True)
    bearer = access_token()

    # Three different numbers, and the summary line used to report only the first one as if it
    # were the third. is_final() deliberately keeps a day with no measurement in it OPEN for the
    # whole ten-day window, so while the ring is not syncing the same empty days are re-fetched on
    # every run -- and each one was counted as a day "fetched new". At 2026-10-27T10:33:25 this
    # unit printed "fetched 4 new day(s); daily.jsonl has 221 rows". ONE of those four days was
    # new to disk (2026-10-27; the table went 220 -> 221 rows) and NONE of the four carried a
    # measurement: 2026-10-24..10-27 are the ring's current silence, re-fetched because is_final()
    # keeps them open. Three hours later the ultrahuman lane was at err about exactly that --
    # "newest record carrying data is 110.0 h old (limit 36 h), at 2026-10-23". The lane is the
    # alarm and stays the alarm; this line just has to stop contradicting it.
    got = first_time = blank = 0
    # has_measurement() reads a shape; a FRESH payload that does not have it is a vendor change,
    # not a day without a ring reading, and the two must not be added together. It is also not a
    # reason to lose the rebuild below -- daily.jsonl is what the dashboard reads -- so this is the
    # module's own per-record idiom: aggregate, keep going, speak once, and escalate to err if
    # EVERY fetched day was unclassifiable (errlog.Skips). is_final() guards the same call for the
    # same reason on stored files.
    skips = errlog.Skips("ultrahuman: classifying fetched payload")
    days = [since + timedelta(days=i) for i in range((until - since).days + 1)]
    for d in days:
        f = RAW / f"{d.year}" / f"{d.isoformat()}.json.gz"
        if is_final(f, d) and not a.backfill:
            continue
        try:
            payload = fetch_day(bearer, d)
        except requests.HTTPError as ex:
            print(f"{d}: HTTP {ex.response.status_code}", file=sys.stderr); continue
        fresh = not f.exists()      # BEFORE the write, or every day reads as one we already had
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode()))
        got += 1
        first_time += int(fresh)
        try:
            blank += int(not has_measurement(payload))
        except Exception as exc:
            skips.add(exc)
        time.sleep(POLITE_S)

    # Rebuild daily.jsonl from whatever raw days exist — idempotent, so a re-run after a backfill
    # repairs the flat table rather than appending duplicates.
    rows = []
    for f in sorted(RAW.rglob("*.json.gz")):
        try:
            rows.append(scalars(date.fromisoformat(f.stem.replace(".json", "")),
                                json.loads(gzip.decompress(f.read_bytes()))))
        except Exception as ex:
            print(f"skip {f.name}: {ex}", file=sys.stderr)
    DAILY.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    skips.report(total=got)
    # Named only when it happened: has_measurement() reads all 221 archived payloads without
    # raising (measured 2026-10-27), and a clause that says "0 unreadable" every hour trains the
    # eye to skip the line. When it DOES happen the count may not hide inside `blank`, or the line
    # is back to denying what it cannot see.
    unread = f", {skips.n} unreadable" if skips.n else ""
    print(f"fetched {got} day(s), {first_time} first-time, "
          f"{blank} still carrying no measurement{unread}; daily.jsonl has {len(rows)} rows")


if __name__ == "__main__":
    main()
