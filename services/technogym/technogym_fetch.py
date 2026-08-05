#!/usr/bin/env python3
"""Technogym / mywellness cardio fetcher — the per-second belt side of the treadmill sessions.

The mywellness self-service *export* ships only session summaries (its `performedData.st` sample
slot is empty). But the app's own backend holds the full per-second trace, and it is reachable with
an ordinary web login — no app registration. Endpoints (mapped by the open-source lcanis/gymexport):

  POST core…/v2/enduser/authentication/login  (hdr x-mwapps-appid)  -> {token, userContext.id}
  GET  services…/Training/User/{uid}/GetPerformedWorkoutSessionByIdCr?IdCr&token&AppId&_c  -> analyticsId(s)
  GET  services…/Training/CardioLog/{analyticsId}/Details?facilityId&token&AppId&_c         -> per-second series

MIGRATION 2026-08-05 — the /cloud web app was DECOMMISSIONED overnight (worked 02:44 on the 4th,
gone by 02:40 on the 5th): every https://www.mywellness.com/cloud/* now 302s to technogym.com, sets
no cookies, and the old scrape died with "no EU.currentUser on Training page" — which reads like a
credentials fault and is not one. Replaced by the JSON API that the new front end (pronext) uses:

  * Login moved to core.mywellness.com/v2/{channel}/authentication/login, channel "enduser" for our
    app id (their bundle: ApiApplicationChannel={ProfessionalAppId:"pro", EndUserAppId:"enduser"}).
  * It needs the app id as a HEADER, `x-mwapps-appid`. Without it the endpoint returns a bare 401
    with an EMPTY body and no hint — that is the signature of a missing header, not a bad password.
  * The DATA endpoints below are unchanged and still accept this token as a query param, so only
    auth + listing needed rewriting.
  * Session LISTING has no replacement on the end-user side (pronext is the trainer app and never
    calls these), so we enumerate idCr forward instead — see list_sessions.

Mirrors polar_fetch.py: creds in ~/.config/claude-dev, into private-data, NO git
commit here (private-data-sync.timer sweeps it), NO Telegram post (pure ETL — coach owns posting).

We keep ONLY what carries information the Polar feed can't: belt **speed** and **incline**, stored as
change-points (both are piecewise-constant setpoints — an easy run is ~9 points, not 2800 samples).
Deliberately DROPPED (see the 2026-07-31 analysis): per-second HR (redundant — Polar has true 1 Hz);
running power (≈ a fixed function of speed — r=0.87 between sessions, 185±2 W across 39 easy sessions
at a fixed pace — so it carries no signal speed doesn't); cadence (mostly speed-tracking, unused).
All of it is re-fetchable from the API if ever wanted. Result: ~1 KB/session vs ~160 KB raw.

  --backfill        ignore the seen-set and re-walk from --first-idcr
  --since YYYY-MM-DD drop fetched sessions older than this (default: cold=2025-01-01, warm=today-45d)
  --first-idcr N    where a cold/backfill walk starts (default 1000, the account's first session)
"""
import argparse, json, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path

ENV      = Path.home() / ".config/claude-dev/mywellness.env"
CARDIO   = Path.home() / "projects/private-data/technogym/cardio"   # compact per-session record
SEEN     = Path.home() / ".local/share/moprox/technogym-seen.json"
APP_ID   = "ec1d38d7-d359-48d0-a60c-d8c0b8fb9df9"                    # mywellness END-USER app id
CORE     = "https://core.mywellness.com"                             # auth (apiUrlCore in their config)
SVC      = "https://services.mywellness.com"                         # training data (unchanged)
UA       = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
POLITE_S = 0.7   # gap between sessions — be a good citizen against the account
# Forward-walk bounds for listing (see list_sessions). MISS_STREAK must exceed any plausible gap in
# the idCr sequence; observed gaps are 0 (it is a dense per-user counter), so 5 is generous.
MISS_STREAK = 5
MAX_PROBE   = 400
FIRST_IDCR  = 1000

import requests


def env():
    d = {}
    for ln in ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); d[k] = v.strip()
    return d


def login(s, e):
    """JSON login against core.mywellness.com. The app id MUST go in the x-mwapps-appid header —
    omit it and you get a bare 401 with an empty body, which looks exactly like a wrong password."""
    r = s.post(f"{CORE}/v2/enduser/authentication/login", timeout=30,
               headers={"Content-Type": "application/json", "Accept": "application/json",
                        "x-mwapps-appid": APP_ID},
               json={"username": e["MYWELLNESS_USER"], "password": e["MYWELLNESS_PASS"],
                     "keepMeLoggedIn": True})
    if r.status_code == 401:
        sys.exit("login failed — 401 from core.mywellness.com. Empty-bodied 401 usually means the "
                 "x-mwapps-appid header was rejected, not that the password is wrong; check both "
                 "(creds in mywellness.env, app id in APP_ID).")
    try:
        b = r.json()
    except ValueError:
        sys.exit(f"login failed — non-JSON reply ({r.status_code}) from core.mywellness.com")
    if b.get("errors"):
        sys.exit(f"login failed — {b['errors']}")
    uid = str((b.get("userContext") or {}).get("id", ""))
    tok = str(b.get("token", ""))
    if not uid or not tok:
        sys.exit("login failed — reply had no userContext.id / token")
    cult = str((b.get("userContext") or {}).get("defaultCulture") or "en-GB")
    return uid, tok, cult


def list_sessions(s, uid, tok, cult, start_idcr):
    """[(idCr, detail), ...] by walking the idCr counter FORWARD from start_idcr.

    The old listing endpoint lived on the decommissioned /cloud app and the new front end has no
    equivalent we can reach (pronext is the trainer app; it never calls the training endpoints). But
    idCr is a dense per-user session counter, and asking for one that doesn't exist is cheap and
    unambiguous: the API answers 200 with an empty `data`. So probe forward until MISS_STREAK
    consecutive blanks. We hand back the detail we just fetched so the caller doesn't re-request it.
    """
    out, misses, idcr, probed = [], 0, int(start_idcr), 0
    while misses < MISS_STREAK and probed < MAX_PROBE:
        detail = fetch_detail(s, uid, tok, cult, idcr)
        if detail:
            out.append((str(idcr), detail)); misses = 0
        else:
            misses += 1
        idcr += 1; probed += 1
        time.sleep(POLITE_S)
    if probed >= MAX_PROBE:
        print(f"  ! walk hit MAX_PROBE={MAX_PROBE} at idCr={idcr}; rerun to continue", flush=True)
    return out


def fetch_detail(s, uid, tok, cult, idcr):
    """Session detail, or {} if that idCr doesn't exist. A non-existent id is NOT an HTTP error —
    the API returns 200 with `data` absent/empty — which is what makes the forward walk cheap."""
    r = s.get(f"{SVC}/Training/User/{uid}/GetPerformedWorkoutSessionByIdCr", timeout=30,
              params={"IdCr": idcr, "token": tok, "AppId": APP_ID, "_c": cult},
              headers={"Accept": "application/json"})
    try:
        d = r.json().get("data") or {}
    except ValueError:
        return {}
    return d if d.get("startedOn") else {}


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
    ap.add_argument("--first-idcr", type=int, default=FIRST_IDCR)
    args = ap.parse_args()

    CARDIO.mkdir(parents=True, exist_ok=True)
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    cold = not seen

    if args.since:
        frm = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        frm = date(2025, 1, 1) if (cold or args.backfill) else (date.today() - timedelta(days=45))

    e = env()
    s = requests.Session(); s.headers["User-Agent"] = UA
    uid, tok, cult = login(s, e)

    # Resume from just past the highest id we already hold; cold/backfill re-walks from the start.
    # Ids in `seen` are strings from the old listing scrape, so compare numerically, not lexically
    # ("999" > "1096" as text, which would have parked the walk forever).
    prior = [int(x) for x in seen if str(x).isdigit()]
    start = args.first_idcr if (cold or args.backfill or not prior) else max(prior) + 1
    pairs = list_sessions(s, uid, tok, cult, start)
    todo = [p for p in pairs if args.backfill or p[0] not in seen]
    print(f"[technogym] walked idCr from {start}: {len(pairs)} session(s) found; {len(todo)} to fetch"
          f"{' (backfill)' if args.backfill else ''}", flush=True)

    ok = 0
    for idcr, detail in todo:
        try:
            ymd = (detail.get("startedOn") or "")[:10]
            if ymd and ymd < frm.isoformat():
                continue                      # --since is now a post-filter; the walk is id-ordered
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
