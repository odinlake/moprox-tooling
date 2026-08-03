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
is no secret header. Mirrors polar_fetch.py: creds in ~/.config/claude-dev, into private-data, NO git
commit here (private-data-sync.timer sweeps it), NO Telegram post (pure ETL — coach owns posting).

We keep ONLY what carries information the Polar feed can't: belt **speed** and **incline**, stored as
change-points (both are piecewise-constant setpoints — an easy run is ~9 points, not 2800 samples).
Deliberately DROPPED (see the 2026-07-31 analysis): per-second HR (redundant — Polar has true 1 Hz);
running power (≈ a fixed function of speed — r=0.87 between sessions, 185±2 W across 39 easy sessions
at a fixed pace — so it carries no signal speed doesn't); cadence (mostly speed-tracking, unused).
All of it is re-fetchable from the API if ever wanted. Result: ~1 KB/session vs ~160 KB raw.

  --backfill        ignore the seen-set and refetch every session in the window
  --since YYYY-MM-DD start of the listing window (default: cold=2025-01-01, warm=today-45d)
"""
import argparse, json, re, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path

ENV      = Path.home() / ".config/claude-dev/mywellness.env"
CARDIO   = Path.home() / "projects/private-data/technogym/cardio"   # compact per-session record
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


def _changepoints(samples, idx, ndp=1, tol=0.05):
    """A piecewise-constant channel (belt speed / incline) as [t_sec, value] only where it changes."""
    out, last = [], None
    for smp in samples:
        vs = smp.get("vs", [])
        if idx is None or idx >= len(vs):
            continue
        v = round(float(vs[idx]), ndp)
        if last is None or abs(v - last) > tol:
            out.append([int(smp.get("t", 0)), v]); last = v
    return out


def _last(samples, idx, ndp=1):
    for smp in reversed(samples):
        vs = smp.get("vs", [])
        if idx is not None and idx < len(vs):
            return round(float(vs[idx]), ndp)
    return None


def compact_record(detail, cardios):
    """Lean, faithful session record: identity + one entry per cardio activity with speed/incline
    change-points and the summary totals. Shared by the puller and the one-off migration."""
    acts = []
    for c in cardios or []:
        an = c.get("analitics", {})
        chans = {(ch.get("pr") or {}).get("name"): ch.get("i") for ch in an.get("descriptor", [])}
        smp = an.get("samples", [])
        acts.append({
            "machine": c.get("name"),
            "equipment": c.get("equipmentType"),
            "durationS": max((int(x.get("t", 0)) for x in smp), default=0),
            "distanceM": _last(smp, chans.get("HDistance")),
            "calories": _last(smp, chans.get("Calories"), ndp=0),
            "speed_kph": _changepoints(smp, chans.get("Speed")),
            "grade_pct": _changepoints(smp, chans.get("Grade")),
        })
    eq = acts[0]["equipment"] if acts else None
    return {
        "idCr": detail.get("idCr"),
        "startedOn": detail.get("startedOn"),
        "facilityId": detail.get("startedInFacilityId"),
        "sport": "run" if eq == "Treadmill" else (eq or "").lower(),
        "activities": acts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since")
    args = ap.parse_args()

    CARDIO.mkdir(parents=True, exist_ok=True)
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
            cardios = []
            for a in detail.get("physicalActivities", []) or []:
                aid = a.get("analyticsId") or a.get("analiticsId")
                fid = ((a.get("performedPhysicalActivity") or {}).get("facilityId")
                       or detail.get("startedInFacilityId"))
                if aid:
                    cd = fetch_cardio(s, tok, cult, aid, fid)
                    if cd:
                        cardios.append(cd)
            rec = compact_record(detail, cardios)
            (CARDIO / f"{idcr}.json").write_text(json.dumps(rec, separators=(",", ":")))
            seen.add(idcr); ok += 1
            sp = sum(len(a["speed_kph"]) for a in rec["activities"])
            print(f"  [{ok}/{len(todo)}] {ymd} idCr={idcr}: {len(rec['activities'])} activity, {sp} speed pts", flush=True)
        except Exception as exc:  # keep going; a bad session shouldn't abort the run
            print(f"  ! idCr={idcr} failed: {exc}", flush=True)
        time.sleep(POLITE_S)

    SEEN.write_text(json.dumps(sorted(seen)))
    print(f"[technogym] done: {ok} new/updated, {len(seen)} total seen", flush=True)


if __name__ == "__main__":
    main()
