#!/usr/bin/env python3
"""Technogym / mywellness cardio fetcher — the per-second belt side of the treadmill sessions.

The mywellness self-service *export* ships only session summaries (its `performedData.st` sample
slot is empty). But the app's own backend holds the full per-second trace, and it is reachable with
an ordinary web login — no app registration. Endpoints (mapped by the open-source lcanis/gymexport):

  POST /cloud/User/Login                                       -> ASP.NET forms-auth cookie
  GET  /cloud/Training           (read EU.currentUser)         -> userId, token, culture
  GET  /cloud/Training/LastPerformedWorkoutSession?from&to     -> hdSessionIdCR list (HTML)
  GET  services…/Training/User/{uid}/GetPerformedWorkoutSessionByIdCr?IdCr&token&AppId&_c  -> analyticsId(s)
  GET  services…/Training/CardioLog/{analyticsId}/Details?facilityId&token&AppId&_c         -> per-second series

The "trusted client" gate is just the `token` (off the Training page) passed as a query param; there
is no secret header. Mirrors polar_fetch.py: creds in ~/.config/claude-dev, raw JSON into
private-data, NO git commit here (private-data-sync.timer sweeps it), NO Telegram post (pure ETL —
coach owns any posting). Run by technogym-fetch.timer.

  --backfill        ignore the seen-set and refetch every session in the window
  --since YYYY-MM-DD start of the listing window (default: cold=2025-01-01, warm=today-45d)
"""
import argparse, json, re, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path

ENV      = Path.home() / ".config/claude-dev/mywellness.env"
CARDIO   = Path.home() / "projects/private-data/technogym/cardio"   # raw {detail, cardio[]} per session
TRACES   = Path.home() / "projects/private-data/technogym/traces"   # derived 1 Hz trace per session (coach)
SEEN     = Path.home() / ".local/share/moprox/technogym-seen.json"
APP_ID   = "ec1d38d7-d359-48d0-a60c-d8c0b8fb9df9"                    # mywellness cloud web app id
BASE     = "https://www.mywellness.com"
SVC      = "https://services.mywellness.com"
UA       = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
POLITE_S = 0.7   # gap between sessions — be a good citizen against the account

import requests


def env():
    d = {}
    for ln in ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); d[k] = v.strip()
    return d


def login(s, e):
    s.get(f"{BASE}/cloud/User/Login", timeout=30)
    s.post(f"{BASE}/cloud/User/Login", timeout=30, data={
        "UserBinder.MifareId": "", "UserBinder.IsFromLogin": "True", "UserBinder.ForceJoinFacility": "",
        "UserBinder.Username": e["MYWELLNESS_USER"], "UserBinder.Password": e["MYWELLNESS_PASS"],
        "UserBinder.KeepMeLogged": "true"})
    tr = s.get(f"{BASE}/cloud/Training", timeout=30).text
    m = re.search(r"JSON\.parse\('(.+?)'\)", tr)
    if not m:
        sys.exit("login failed — no EU.currentUser on Training page (check mywellness.env)")
    u = json.loads(m.group(1).replace('\\"', '"'))
    uid = str(u.get("id", ""))
    if not uid:
        sys.exit("login failed — user payload had no id")
    return uid, str(u.get("token", "")), str(u.get("culture", "en-GB"))


def list_sessions(s, frm, to):
    """(idCr, 'YYYYMMDD') pairs for the window, newest first."""
    html = s.get(f"{BASE}/cloud/Training/LastPerformedWorkoutSession", timeout=30,
                 params={"fromDate": frm.strftime("%d/%m/%Y"), "toDate": to.strftime("%d/%m/%Y")}).text
    ids   = re.findall(r"name=['\"]hdSessionIdCR['\"][^>]*value=['\"](\d+)['\"]", html, re.I)
    dates = re.findall(r"class=['\"][^'\"]*\bdate\b[^'\"]*['\"][^>]*>\s*(\d{8})", html, re.I)
    dates = dates[:len(ids)]
    return list(zip(ids, dates + [""] * (len(ids) - len(dates))))


def fetch_detail(s, uid, tok, cult, idcr):
    r = s.get(f"{SVC}/Training/User/{uid}/GetPerformedWorkoutSessionByIdCr", timeout=30,
              params={"IdCr": idcr, "token": tok, "AppId": APP_ID, "_c": cult},
              headers={"Accept": "application/json"})
    return r.json().get("data", {})


def fetch_cardio(s, tok, cult, analytics_id, facility_id):
    r = s.get(f"{SVC}/Training/CardioLog/{analytics_id}/Details", timeout=30,
              params={"facilityId": facility_id, "token": tok, "AppId": APP_ID, "_c": cult},
              headers={"Accept": "application/json"})
    b = r.json()
    if b.get("errors"):
        return None
    return b.get("data", {})


def _one_hz(samples, idx, n):
    """Event-sampled channel -> 1 Hz step-held grid of length n+1 (belt speed/grade hold until changed)."""
    grid = [None] * (n + 1)
    for smp in samples:
        t = int(smp.get("t", -1)); vs = smp.get("vs", [])
        if 0 <= t <= n and idx < len(vs):
            grid[t] = float(vs[idx])
    last = 0.0
    for i in range(len(grid)):
        if grid[i] is None:
            grid[i] = last
        else:
            last = grid[i]
    return grid


def _hr_1hz(hr, n):
    grid = [None] * (n + 1)
    for h in hr:
        t = int(h.get("t", -1))
        if 0 <= t <= n:
            grid[t] = int(h.get("hr", 0))
    last = 0
    for i in range(len(grid)):
        if grid[i] is None:
            grid[i] = last
        else:
            last = grid[i]
    return grid


def build_trace(idcr, detail, cardios):
    """Compact 1 Hz trace for coach: the pace axis the HR-only Polar feed can't see."""
    an = (cardios[0] or {}).get("analitics", {}) if cardios else {}
    chans = {(c.get("pr") or {}).get("name"): c.get("i") for c in an.get("descriptor", [])}
    samples = an.get("samples", [])
    n = max((int(x.get("t", 0)) for x in samples), default=0)
    def ch(name):
        return _one_hz(samples, chans[name], n) if name in chans else None
    meta = cardios[0] or {}
    return {
        "idCr": idcr,
        "startedOn": detail.get("startedOn"),
        "closedOn": detail.get("closedOn"),
        "facilityId": detail.get("startedInFacilityId"),
        "equipment": meta.get("equipmentType"),
        "machine": meta.get("name"),
        "sport": "run" if (meta.get("equipmentType") == "Treadmill") else (meta.get("equipmentType") or "").lower(),
        "dur_s": n,
        "speed_kph": ch("Speed"),
        "grade_pct": ch("Grade"),
        "cadence_spm": ch("RunningCadence"),
        "power_w": ch("RunningPower"),
        "hdist_m": ch("HDistance"),
        "hr_bpm": _hr_1hz(an.get("hr", []), n),   # secondary — prefer Polar HR when joining
        "hr_zones": an.get("hrZones", []),
        "laps": an.get("laps", []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since")
    args = ap.parse_args()

    CARDIO.mkdir(parents=True, exist_ok=True)
    TRACES.mkdir(parents=True, exist_ok=True)
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    cold = not seen

    if args.since:
        frm = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        frm = date(2025, 1, 1) if (cold or args.backfill) else (date.today() - timedelta(days=45))
    to = date.today()

    e = env()
    s = requests.Session(); s.headers["User-Agent"] = UA
    uid, tok, cult = login(s, e)
    pairs = list_sessions(s, frm, to)
    todo = [p for p in pairs if args.backfill or p[0] not in seen]
    print(f"[technogym] {len(pairs)} sessions in {frm}..{to}; {len(todo)} to fetch"
          f"{' (backfill)' if args.backfill else ''}", flush=True)

    ok = 0
    for idcr, ymd in todo:
        try:
            detail = fetch_detail(s, uid, tok, cult, idcr)
            acts = detail.get("physicalActivities", []) or []
            cardios = []
            for a in acts:
                aid = a.get("analyticsId") or a.get("analiticsId")
                fid = ((a.get("performedPhysicalActivity") or {}).get("facilityId")
                       or detail.get("startedInFacilityId"))
                if aid:
                    cd = fetch_cardio(s, tok, cult, aid, fid)
                    if cd:
                        cardios.append(cd)
            (CARDIO / f"{idcr}.json").write_text(
                json.dumps({"detail": detail, "cardio": cardios}, separators=(",", ":")))
            if cardios:
                (TRACES / f"{idcr}.json").write_text(
                    json.dumps(build_trace(idcr, detail, cardios), separators=(",", ":")))
            seen.add(idcr); ok += 1
            n = build_trace(idcr, detail, cardios)["dur_s"] if cardios else 0
            print(f"  [{ok}/{len(todo)}] {ymd} idCr={idcr}: {len(cardios)} cardio, {n}s trace", flush=True)
        except Exception as exc:  # keep going; a bad session shouldn't abort the run
            print(f"  ! idCr={idcr} failed: {exc}", flush=True)
        time.sleep(POLITE_S)

    SEEN.write_text(json.dumps(sorted(seen)))
    print(f"[technogym] done: {ok} new/updated, {len(seen)} total seen", flush=True)


if __name__ == "__main__":
    main()
