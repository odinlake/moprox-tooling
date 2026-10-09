#!/usr/bin/env python3
"""Kick off a Wattbike Hub pull the instant Polar tells us a RIDE landed, without ever letting
Wattbike's flakiness delay the coach read.

Shape (operator, 2026-09-25): start at the EARLIEST possible moment, run in PARALLEL with the rest
of the Polar work, and be joined BEFORE coach is invoked -- but on a short leash, because a hung
Hub must not hold up a session read that is otherwise ready to go.

Two timeouts, deliberately different:
  JOIN_S  how long the coach handoff will wait. This is the one the operator feels.
  HARD_S  the background work's own ceiling, well past JOIN_S. If the pull is merely slow it still
          finishes and lands on disk after coach has gone ahead without it; the next ride (or any
          rerun) picks the data up regardless, since the fetcher skips what it already has.

THE PULL NEEDS NO CREDENTIAL (analyst, 2026-10-04). Until today this module shelled out to the
webscout box over ssh+sudo+xvfb to drive a headed Playwright browser holding a Hub login, and the
result was rsynced back. That path pulled the 2026-09-25 ride and then failed on every ride after
it: 09-28, 09-29, 09-30 and 10-02 were all still missing from private-data/wattbike this morning.
It was never necessary. hub.wattbike.com/main.bundle.js serves `parse:{appId,jsKey,apiUrl}` to
anonymous visitors; a Parse query signed with those keys lists the user's RideSessions; and the
processed per-second files are served unauthenticated from v2/files/<userObjectId>_<id>.<ext>. The
one ride the browser route did land refetches BYTE-IDENTICAL over this route (.wbs sha256
63bcf96e..., 2857937 B), so nothing is given up by dropping the browser.

The only private input is the user's Parse objectId, which we read out of the sessions.json we
already hold (override with WATTBIKE_USER_ID). Bootstrapping an empty store still needs a logged-in
session to learn that id once -- the old ssh route is kept as a fallback for exactly that, and for
the day Wattbike closes the anonymous read.

Nothing here raises: every failure path returns a line for the prompt and lets the read proceed.
"""
import datetime, json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

BOX      = os.environ.get("WATTBIKE_BOX", "agent@10.10.10.8")
SSH_KEY  = os.environ.get("WATTBIKE_SSH_KEY", str(Path.home() / ".ssh/claude-dev-ops"))
DEST     = Path(os.environ.get("WATTBIKE_DEST", str(Path.home() / "projects/private-data/wattbike")))
JOIN_S   = int(os.environ.get("WATTBIKE_JOIN_S", "120"))    # coach waits at most this long
HARD_S   = int(os.environ.get("WATTBIKE_HARD_S", "420"))    # the pull's own ceiling
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=10", "-i", SSH_KEY]

REMOTE = ("sudo -u webscout env HOME=/opt/webscout "
          "PLAYWRIGHT_BROWSERS_PATH=/opt/webscout/.cache/ms-playwright "
          "WEBSCOUT_HEADED=1 PYTHONUNBUFFERED=1 "
          "xvfb-run -a /opt/webscout/venv/bin/python /opt/webscout/wattbike_fetch.py")

BUNDLE = os.environ.get("WATTBIKE_BUNDLE", "https://hub.wattbike.com/main.bundle.js")
KEYS_RE = re.compile(r'parse:\s*\{[^}]*?appId:\s*"([A-Za-z0-9]+)"[^}]*?jsKey:\s*"([^"]+)"'
                     r'[^}]*?apiUrl:\s*"([^"]+)"', re.S)
UA = {"User-Agent": "Mozilla/5.0"}
HTTP_S = 60
LIMIT  = 200                                                # the Hub's own page cap, not ours

# The anonymous (phone-app-unpaired) recovery pass. See _anonymous_pass.
MATCH_S     = int(os.environ.get("WATTBIKE_MATCH_S", "60"))      # same ride iff within this
WINDOW_PAD_S = int(os.environ.get("WATTBIKE_WINDOW_PAD_S", "180"))  # half-width of a serial query
LOOKBACK_D  = int(os.environ.get("WATTBIKE_LOOKBACK_D", "45"))   # how far back to bother asking
MAX_WINDOWS = int(os.environ.get("WATTBIKE_MAX_WINDOWS", "12"))  # queries per run; a hit is loud


def _get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict(UA, **(headers or {})))
    try:
        r = urllib.request.urlopen(req, timeout=HTTP_S)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _user_id():
    """The one private input. Env wins; otherwise read it off the store we already have."""
    if os.environ.get("WATTBIKE_USER_ID"):
        return os.environ["WATTBIKE_USER_ID"]
    try:
        rows = json.loads((DEST / "sessions.json").read_text())
    except Exception:
        return None
    for s in rows or []:
        uid = ((s.get("user") or {}).get("objectId"))
        if uid:
            return uid
    return None


def _files_of(s):
    """Yield (ext, local_name, entry) for every file a RideSession names.

    sessionData is NOT uniform: the whole-ride classes (wbs, wbsr, tcx, fit) map the extension to
    one {name} dict, but `wbss` maps it to a LIST of per-segment dicts, one per contiguous stretch
    of the ride. An isinstance(ent, dict) guard silently drops the list form, which is how the
    estate ended up holding 0 .wbss files while the index named one for 3 of its 4 sessions
    (analyst, 2026-10-04). Segment files keep the span out of their remote name -- there can be
    more than one per session -- so the local name is the remote one minus the user-id prefix.
    """
    for ext, ent in (s.get("sessionData") or {}).items():
        for e in (ent if isinstance(ent, list) else [ent]):
            if not isinstance(e, dict) or not e.get("name"):
                continue
            if isinstance(ent, list):
                local = e["name"].split("_", 1)[-1]
            else:
                local = "%s.%s" % (s["objectId"], ext)
            yield ext, local, e


def _query(api, app, jsk, where):
    """One RideSession page. Returns (http_status, results_or_None)."""
    body = {"_method": "GET", "_ApplicationId": app, "_JavaScriptKey": jsk,
            "_ClientVersion": "js1.11.1", "include": "sessionSummary", "order": "startDate",
            "limit": LIMIT, "where": where}
    st, b = _get(api + "/classes/RideSession", data=json.dumps(body).encode(),
                 headers={"Content-Type": "text/plain"})
    try:
        return st, json.loads(b.decode("utf-8", "replace")).get("results")
    except ValueError:
        return st, None


def _parse_date(epoch):
    return {"__type": "Date",
            "iso": datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def _hub_start(s):
    """Epoch seconds of a RideSession's startDate, or None if it has none we can read."""
    try:
        iso = ((s.get("startDate") or {}).get("iso") or "").replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(iso).timestamp() if iso else None
    except Exception:
        return None


def _polar_indoor_starts(polar_in):
    """[(polar_id, epoch_start)] for every INDOOR_CYCLING exercise on disk, newest first."""
    out = []
    for p in sorted(Path(polar_in).glob("exercise_*.json")):
        try:
            s = (json.loads(p.read_text()) or {}).get("summary") or {}
            if s.get("detailed_sport_info") != "INDOOR_CYCLING":
                continue
            t = (datetime.datetime.fromisoformat(s["start_time"])
                 - datetime.timedelta(minutes=s.get("start_time_utc_offset") or 0)
                 ).replace(tzinfo=datetime.timezone.utc).timestamp()
            out.append((s.get("id") or p.stem.split("_", 1)[-1], t))
        except Exception as e:
            errlog.skip("wattbike: polar exercise %s" % p.name, e)
    return sorted(out, key=lambda r: -r[1])


def _serial_windows(starts, pad=None):
    """One NARROW window per unmatched ride: [(lo, hi)] aligned with `starts`.

    Deliberately not one min..max span over the unmatched set. The serial is a shared commercial
    gym bike (38 sessions in 17 days on 28002545, mostly other riders), the Hub answers with at
    most LIMIT rows ascending and offers no skip, and the rides we can never match do not leave the
    set -- so a span wide enough to cover them crosses the cap and pages the NEWEST ride out, which
    reads as "absent" rather than "unknown". A +/-pad window around one start cannot reach the cap,
    and if it ever does, _anonymous_pass treats that page as truncated instead of conclusive.
    """
    pad = WINDOW_PAD_S if pad is None else pad
    return [(t - pad, t + pad) for (_pid, t) in starts]


def _stored_rows(dest):
    """Rows already in the index, INCLUDING summary-only ones an earlier run recovered.

    The live user-pointer query cannot return those -- they have no user pointer, which is the
    whole reason they needed recovering -- so without reading them back the set of unmatched Polar
    rides never shrinks and every run re-asks the Hub about every ride it has already claimed.
    """
    p = Path(dest) / "sessions.json"
    try:
        return json.loads(p.read_text()) or []
    except FileNotFoundError:
        return []                                    # bootstrapping an empty store
    except Exception as e:
        errlog.err("wattbike: %s is unreadable, so rides already recovered will be asked about "
                   "again" % p, e)
        return []


def _anonymous_pass(api, app, jsk, rows, polar_in, stored=(), now=None):
    """Recover rides the monitor uploaded with the phone app UNPAIRED. Returns (new_rows, note).

    Those RideSessions carry `associated: false` and no `user` key at all, so pull()'s user-pointer
    query -- the only query this module made until 2026-10-07 -- structurally cannot return them;
    the estate was missing the 2026-09-22 and 2026-09-28 indoor rides for that reason alone
    (analyst, moprox-memory/wattbike-hub-anonymous-uploads-serial-route). They ARE reachable by the
    bike's serial, but the serial is not identity: it is a gym bike other people ride. So a
    candidate is accepted only when it is the UNIQUE Hub record within MATCH_S of an INDOOR_CYCLING
    start Polar gave us. Recovery is summary-only on purpose -- the per-second file an anonymous
    record names 404s on the files route -- so the stored row carries wattbike_recovered and no
    sessionData, and nothing downstream may read it as a full-fidelity ride.

    Every give-up path here is loud and none of them is allowed to lose the user pass's result.
    """
    now = time.time() if now is None else now
    # The serial whitelist is the ONLY handle on identity this route has, so where it comes from
    # sets the route's reach. Taken from `rows` alone it is the set of bikes some PAIRED ride has
    # been seen on, which makes an unpaired ride's recoverability hang on an unrelated LATER ride
    # happening to pair on the same bike: the 2026-09-22 ride was on 28002287, whose first paired
    # ride was 2026-10-05, so that hole was structurally unreachable here for 12.98 d and then
    # recovered on the first attempt (analyst, 2026-10-09). Rows recovered earlier carry their
    # serial, so fold `stored` in too. A ride that is both the first-ever use of a bike AND
    # unpaired still cannot be reached by this route at all -- that is the residual gap.
    serials = sorted({(s.get("wattbikeDevice") or {}).get("serialNumber")
                      for s in list(rows) + list(stored)} - {None, ""})
    if not serials:
        return [], ""
    have = [t for t in (_hub_start(s) for s in list(rows) + list(stored)) if t is not None]
    unmatched = [(pid, t) for (pid, t) in _polar_indoor_starts(polar_in)
                 if 0 <= now - t <= LOOKBACK_D * 86400
                 and not any(abs(t - h) <= MATCH_S for h in have)]
    notes = []
    if len(unmatched) > MAX_WINDOWS:
        # Newest first, so a ceiling hit drops the rides nobody is about to read -- but it is still
        # a gap in coverage and it is not allowed to be silent.
        errlog.err("wattbike: %d unpaired Polar indoor rides within %dd exceeds the %d-window "
                   "per-run ceiling, so the %d oldest were not asked about"
                   % (len(unmatched), LOOKBACK_D, MAX_WINDOWS, len(unmatched) - MAX_WINDOWS))
        notes.append("%d unpaired over window ceiling, not asked"
                     % (len(unmatched) - MAX_WINDOWS))
        unmatched = unmatched[:MAX_WINDOWS]
    found = []
    for (pid, t), (lo, hi) in zip(unmatched, _serial_windows(unmatched)):
        cands, truncated = [], False
        for ser in serials:
            st, got = _query(api, app, jsk,
                             {"wattbikeDevice.serialNumber": ser,
                              "startDate": {"$gte": _parse_date(lo), "$lte": _parse_date(hi)}})
            if st != 200 or got is None:
                errlog.err("wattbike: serial window for Polar %s on %s returned HTTP %s"
                           % (pid, ser, st))
                notes.append("%s: serial query HTTP %s" % (pid, st))
                truncated = True                 # the window is unresolved, not empty
                continue
            if len(got) >= LIMIT:
                errlog.err("wattbike: serial window %s..%s on %s came back at the %d-row cap, so "
                           "the page is a truncated prefix and Polar %s cannot be resolved from it"
                           % (_parse_date(lo)["iso"], _parse_date(hi)["iso"], ser, LIMIT, pid))
                notes.append("%s: window truncated at %d" % (pid, LIMIT))
                truncated = True
                continue
            cands += [c for c in got
                      if _hub_start(c) is not None and abs(_hub_start(c) - t) <= MATCH_S]
        if len(cands) == 1 and not truncated:
            c = dict(cands[0])
            c["wattbike_recovered"] = {"polar_id": pid, "route": "serial+window",
                                       "dt_s": round(_hub_start(cands[0]) - t, 2),
                                       "summary_only": True}
            found.append(c)
            notes.append("%s: recovered %s summary-only" % (pid, c.get("objectId")))
        elif not truncated:
            notes.append("%s: %d hub candidate(s) within %ds, not claimed"
                         % (pid, len(cands), MATCH_S))
    return found, "; ".join(notes)


def pull(dest=None):
    """Fetch every session for the stored user and every sessionData file we do not hold.

    Returns (n_sessions, n_new_files, note). Raises on a route that is no longer open, so the
    caller can fall back; returns normally -- possibly with 0 new files -- when it worked.
    """
    dest = Path(dest or DEST)
    uid = _user_id()
    if not uid:
        raise RuntimeError("no user objectId: %s/sessions.json is absent or empty and "
                           "WATTBIKE_USER_ID is unset" % dest)
    st, raw = _get(BUNDLE)
    m = KEYS_RE.search(raw.decode("utf-8", "replace")) if st == 200 else None
    if not m:
        raise RuntimeError("bundle no longer exposes parse:{appId,jsKey,apiUrl} (HTTP %s)" % st)
    app, jsk, api = m.groups()
    st, rows = _query(api, app, jsk,
                      {"user": {"__type": "Pointer", "className": "_User", "objectId": uid}})
    if st != 200 or not rows:
        raise RuntimeError("RideSession query returned HTTP %s with %s results"
                           % (st, 0 if not rows else len(rows)))

    # The user pointer cannot see an upload made with the app unpaired; ask by serial as well.
    polar_in = Path(os.environ.get("WATTBIKE_POLAR_IN")
                    or (dest.parent / "polar" / "incoming"))
    stored = _stored_rows(dest)
    try:
        recovered, anon_note = _anonymous_pass(api, app, jsk, rows, polar_in, stored=stored)
    except Exception as e:
        # The user pass already succeeded; losing it because the recovery pass broke would be a
        # regression. Loud, then carry on with what we have.
        errlog.err("wattbike: anonymous recovery pass failed, user-associated rides unaffected", e)
        recovered, anon_note = [], "anonymous pass failed: %s" % type(e).__name__
    # The index below is rewritten wholesale from the user query, which by construction cannot
    # return a row recovered earlier -- so carry those forward or the first write after a recovery
    # drops the ride again, and this time silently, because _anonymous_pass now counts it as held.
    live = {r.get("objectId") for r in rows}
    kept = [s for s in stored if "wattbike_recovered" in s and s.get("objectId") not in live]
    rows = list(rows) + kept + recovered

    (dest / "raw").mkdir(parents=True, exist_ok=True)
    new, missed = [], []
    for s in rows:
        for ext, local, ent in _files_of(s):
            p = dest / "raw" / local
            if p.exists():
                continue
            code, data = _get(api + "/files/" + ent["name"])
            if code != 200 or not data:
                missed.append("%s.%s(%s)" % (s["objectId"], ext, code))
                continue
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(p)
            new.append(p.name)
    # Written last: the index is the thing other code reads, so it should never be ahead of
    # the files it points at.
    (dest / "sessions.json").write_text(json.dumps(rows, indent=1) + "\n")
    note = "%d sessions, %d new files" % (len(rows), len(new))
    if recovered:
        note += ", %d summary-only recovered by serial" % len(recovered)
    if anon_note:
        note += " [anon: %s]" % anon_note
    if missed:
        note += ", %d unreadable: %s" % (len(missed), ",".join(missed[:6]))
    return len(rows), len(new), note


def _run_ssh(state):
    """The pre-2026-10-04 route: drive the Hub in a browser on the webscout box, rsync back."""
    r = subprocess.run(SSH + [BOX, REMOTE], capture_output=True, text=True, timeout=HARD_S)
    state["out"] = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        state["err"] = "fetch exited %s" % r.returncode
        return
    DEST.mkdir(parents=True, exist_ok=True)
    # -u so a file we already hold is never overwritten by an older copy; the remote is the
    # source of truth for NEW sessions only.
    rs = subprocess.run(
        ["rsync", "-a", "-u", "-e", " ".join(SSH),
         "%s:/var/lib/webscout/wattbike/" % BOX, str(DEST) + "/"],
        capture_output=True, text=True, timeout=180)
    if rs.returncode != 0:
        state["err"] = "rsync exited %s: %s" % (rs.returncode, (rs.stderr or "")[:120])


def _run(state):
    t0 = time.time()
    try:
        try:
            _, _, note = pull()
            state["out"] = "WATTBIKE: %s (direct, no credential)\n" % note
        except Exception as e:
            state["route"] = "ssh after direct failed: %s: %s" % (type(e).__name__, str(e)[:120])
            _run_ssh(state)
    except subprocess.TimeoutExpired:
        state["err"] = "timed out after %ds" % HARD_S
    except Exception as e:
        state["err"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    finally:
        state["secs"] = round(time.time() - t0, 1)
        state["done"] = True


def start():
    """Fire the pull and return immediately. Call the moment a ride is recognised."""
    state = {"done": False, "out": "", "err": None, "secs": None, "route": "direct"}
    t = threading.Thread(target=_run, args=(state,), daemon=True)
    t.start()
    return (t, state)


def join_line(handle, timeout=None):
    """Wait up to `timeout` (default JOIN_S) and return ONE line for the coach prompt, or "".

    Returns "" when there was no ride to fetch, so the caller can concatenate unconditionally.
    """
    if not handle:
        return ""
    t, state = handle
    t.join(JOIN_S if timeout is None else timeout)
    if not state["done"]:
        return ("\n\nWATTBIKE: pull still running after %ds and was not waited for further, so the "
                "per-revolution data may not be on disk yet for this session. Do not treat its "
                "absence as meaning the ride lacked power data.\n" % (JOIN_S if timeout is None else timeout))
    if state["err"]:
        return ("\n\nWATTBIKE: pull failed (route=%s, %s). Read the session from the Polar HR alone "
                "and say nothing about pedal mechanics.\n" % (state.get("route"), state["err"]))
    summary = ""
    for ln in (state["out"] or "").splitlines():
        if ln.startswith("WATTBIKE:"):
            summary = ln.split("WATTBIKE:", 1)[1].strip()
    return ("\n\nWATTBIKE: full-fidelity ride data pulled in %ss (%s). Per-revolution power, "
            "cadence, balance, per-leg pedal effectiveness and a force curve per stroke "
            "are under private-data/wattbike/raw/<sessionId>.wbs, with the session index in "
            "private-data/wattbike/sessions.json. The curve is laps[0].data[i].polar.force, a "
            "comma-separated string of polar.cnt samples at a FIXED 100 Hz, so its length is "
            "6000/cadence (58..190 points, median 69 over the 16298 strokes held on "
            "2026-10-04), and curves at different cadences must be resampled to a common "
            "length before they are compared. polar.lcnt is how many of those leading samples "
            "are the left leg, and the crank angle is PER HALF, not uniform over the "
            "revolution: left sample k is 180*k/lcnt deg, right sample j (force[lcnt+j]) is "
            "180+180*j/(cnt-lcnt) deg. That reproduces the published anglePeakForce angles on "
            "32596/32596 peaks; a uniform 360*k/cnt grid is wrong by a median 2.6 deg (max 98) "
            "because lcnt is a time split with median share 0.4853, not cnt/2. Resample each "
            "half separately. Two of the derived fields are arithmetic, not measurement: "
            "pes.combinedCoefficient is the UNWEIGHTED mean of leftCoefficient and "
            "rightCoefficient (16298/16298 strokes, exact at 4 dp where that mean is "
            "representable), so cite two PES numbers and not three; and `balance` is "
            "100*sum(force[:lcnt])/sum(force) to a median 0.019 pts, so balance CAN be recomputed "
            "over any sub-interval of the ride. The per-leg coefficients canNOT: they are not the "
            "half-curve's mean/peak ratio (median residual 0.125 left, 0.141 right), so there is "
            "no per-interval PES. Use it if this session is the matching ride.\n"
            % (state["secs"], summary or "no summary line"))


if __name__ == "__main__":
    import sys
    try:
        n, new, note = pull()
        print("WATTBIKE: %s" % note)
    except Exception as e:
        print("WATTBIKE: direct pull failed: %s: %s" % (type(e).__name__, e))
        sys.exit(1)
