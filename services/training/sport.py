"""What kind of session is this? The ONE place the estate decides, used by every stage: the live
Polar poster (services/forward/polar_fetch.py: its receipt, the coach handoff, the Wattbike pull) and
the training build (services/training/build.py -> the dashboard's categories).

Matched on the NAME, not Polar's numeric sport id: ids for anything but running are unverified here,
and guessing one would silently mis-route the first real session. Names come from AccessLink's
`sport` + `detailed_sport_info` (live) or the export's session name + sport name (archive).

Seen in the live feed as of 2026-09-30: RUNNING/TREADMILL_RUNNING (52), OTHER/STRENGTH_TRAINING (12),
CYCLING/INDOOR_CYCLING (5). Strength arrives as sport "OTHER", so a check on `sport` alone calls it
"other"; that is why every label here is the two fields together. Anything matching nothing is still
INGESTED as "other" and named by what Polar called it (polar_label), never dropped.
"""
RUN_WORDS = ("RUN", "JOG")
RIDE_WORDS = ("CYCLING", "BIKING", "BIKE", "SPINNING", "HANDCYCLING")
STRENGTH_WORDS = ("STRENGTH", "WEIGHT", "RESISTANCE", "CROSSFIT", "CIRCUIT")

KINDS = ("run", "ride", "strength", "other")


def kind(label):
    """'run' | 'ride' | 'strength' | 'other' for a free-text sport label."""
    u = str(label or "").upper()
    if any(w in u for w in RUN_WORDS):
        return "run"
    if any(w in u for w in RIDE_WORDS):
        return "ride"
    if any(w in u for w in STRENGTH_WORDS):
        return "strength"
    return "other"


def ex_label(ex):
    """AccessLink exercise -> the label kind() matches on."""
    return "%s %s" % (ex.get("sport") or "", ex.get("detailed_sport_info") or "")


def polar_label(ex):
    """What Polar itself called it, most specific first: 'strength training', not 'other'."""
    raw = ex.get("detailed_sport_info") or ex.get("sport") or "session"
    return str(raw).replace("_", " ").lower()


def display(ex):
    """The name to show a human: the estate's word when it recognises the sport, else Polar's own."""
    k = kind(ex_label(ex))
    return {"run": "run", "ride": "ride", "strength": "strength"}.get(k) or polar_label(ex)
