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
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ENV    = Path.home() / ".config/claude-dev/ultrahuman.env"
TOKEN  = Path.home() / ".config/claude-dev/ultrahuman-partner-token.json"
OUT    = Path.home() / "projects/private-data/ultrahuman"
RAW    = OUT / "raw"
DAILY  = OUT / "daily.jsonl"

TOKEN_URL = "https://partner.ultrahuman.com/api/partners/oauth/token"
API       = "https://partner.ultrahuman.com/api/partners/v1/user_data"
POLITE_S  = 1.2          # gap between day requests — no documented rate limit, so don't find one
SKEW_S    = 300          # refresh this long before expiry rather than waiting for a 401


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
    tok = json.loads(TOKEN.read_text())
    expires_at = tok["created_at"] + tok["expires_in"]
    if time.time() < expires_at - SKEW_S:
        return tok["access_token"]
    e = env()
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": e["UH_CLIENT_ID"], "client_secret": e["UH_CLIENT_SECRET"]})
    r.raise_for_status()
    tok = r.json()
    TOKEN.write_text(json.dumps(tok)); TOKEN.chmod(0o600)
    print("refreshed access token", file=sys.stderr)
    return tok["access_token"]


def fetch_day(bearer, d):
    r = requests.get(f"{API}/metrics", params={"date": d.isoformat()},
                     headers={"Authorization": f"Bearer {bearer}"}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from metrics — token rejected; re-run the authorize flow")
    r.raise_for_status()
    return r.json()


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
    rec["morning_alertness"] = val("morning_alertness")

    nr = by.get("night_rhr") or {}
    rec["night_rhr_avg"] = nr.get("avg") if isinstance(nr, dict) else None
    st = by.get("steps") or {}
    rec["steps"] = st.get("total") if isinstance(st, dict) else None
    hv = by.get("hrv") or {}
    rec["hrv_avg"] = hv.get("avg") if isinstance(hv, dict) else None

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args()

    until = date.fromisoformat(a.until) if a.until else date.today()
    since = date.fromisoformat(a.since) if a.since else until - timedelta(days=10)

    RAW.mkdir(parents=True, exist_ok=True)
    bearer = access_token()

    got = 0
    days = [since + timedelta(days=i) for i in range((until - since).days + 1)]
    for d in days:
        f = RAW / f"{d.year}" / f"{d.isoformat()}.json.gz"
        if f.exists() and not a.backfill:
            continue
        try:
            payload = fetch_day(bearer, d)
        except requests.HTTPError as ex:
            print(f"{d}: HTTP {ex.response.status_code}", file=sys.stderr); continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode()))
        got += 1
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
    print(f"fetched {got} new day(s); daily.jsonl has {len(rows)} rows")


if __name__ == "__main__":
    main()
