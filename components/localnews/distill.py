#!/usr/bin/env python3
"""Classify pending local-news posts (webscout reader) via claude -p on the Max sub.

Filters per operator prefs: DROP pets & opinion/chatter; KEEP crime/accident/incident;
discretionary council/events. Significance 0-5 weights proximity (near Tooting/Colliers
Wood > borough > farther). Prefetches full text for kept items so taps are instant."""
import json, os, re, shutil, subprocess, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

# Resolve the CLI ABSOLUTELY. Under systemd the service PATH is minimal and does not include
# ~/.local/bin, so a bare "claude" raises FileNotFoundError. That is exactly what happened here:
# Claude Code moved to the native installer at ~/.local/bin/claude, every classify() started
# throwing, the bare except below scored it 0, and the unit went on reporting success. Result:
# ~1,200 posts scored 0 across 21 days with nothing anywhere saying so.
CLAUDE = os.path.expanduser("~/.local/bin/claude")
if not os.path.exists(CLAUDE):
    CLAUDE = shutil.which("claude") or "claude"

BASE = "http://10.10.10.8:8004"
TOKEN = (Path.home() / ".config/claude-dev/reader-token").read_text().strip()
NEAR = ["Colliers Wood", "Tooting", "West Tooting", "Tooting Bec", "Lavender Fields", "Merton"]
MID = ["Mitcham", "Wimbledon", "Earlsfield", "Summerstown", "Furzedown", "Balham",
       "Streatham", "Morden", "Pollards Hill", "Wandsworth"]

PROMPT = """Classify this Nextdoor post for a local-news brief. Reply ONLY with JSON:
{{"title": "<=60 chars, factual", "blurb": "<=160 chars, what happened", "category":
"crime|accident|incident|council|event|pets|opinion|chatter|photo|services|other",
"significance": 0-5, "keywords": ["k1","k2"]}}
significance: 5=serious crime/danger nearby, 4=notable incident/witnessed event, 3=council
action/local event worth knowing, 2=minor, 1=trivial, 0=noise. Proximity tiers: NEAR={near};
MID={mid}; other areas = farther, score lower. pets/opinion/chatter/photo always <=1.
POST (area: {area}, when: {when}): {body}"""


def call(path, data=None):
    req = urllib.request.Request(BASE + path, headers={"X-Reader-Token": TOKEN})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def classify(p):
    prompt = PROMPT.format(near=", ".join(NEAR), mid=", ".join(MID),
                           area=p.get("area") or "?", when=p.get("when_rel") or "?",
                           body=(p.get("body") or "")[:900])
    r = subprocess.run([CLAUDE, "-p", "--output-format", "text", "--model", "haiku"],
                       input=prompt, capture_output=True, text=True, timeout=120)
    # Report what the CLI said, not what its silence did to json.loads. An expired OAuth session
    # exits non-zero with the reason on stderr and nothing on stdout; the slice below then hands
    # json.loads an empty string and every post reports "JSONDecodeError: Expecting value: line 1
    # column 1 (char 0)" — a batch of identical parse errors for a classifier that was never asked.
    # That is what the 19 posts of 2026-09-07T09:50Z say in the journal, and the credential the
    # whole box was missing appears nowhere in them.
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[:300] or '<no output>'}")
    txt = r.stdout.strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < i:
        raise ValueError(f"claude -p exit 0 but no JSON object in its output: {txt[:300]!r}")
    return json.loads(txt[i:j + 1])


# The WHOLE body is a media player's elapsed/duration overlay, e.g. "00:01 / 00:06". Measured
# 2026-09-09 against the reader store, n=3251 posts: exactly four bodies match this shape, and the
# three of them the working classifier has seen are exactly the three posts stuck in the pending
# queue, for 11, 9 and 5 days (moprox-memory/localnews-stuck-bodies-are-duration-pairs.md).
#
# The shape is the discriminator, and it has to be. Shortness is NOT: 59 posts have a body of <= 20
# characters and 56 of them annotate normally. Neither is "has no letters": the same measurement
# found emoji-only bodies ("☀️ ☁️") that `claude -p` handles perfectly well today, returning "Post
# contains only weather emoji with no textual content". Those must keep going to the classifier —
# anything that swallows them here is this change breaking a case that works. An anchored timecode
# pair is the one body we have measured no reader and no classifier can make text out of.
#
# The optional hours group is the same overlay one length up; the corpus holds only the mm:ss form
# because the stuck posts are 6 to 54 seconds long.
TIMECODE_PAIR = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})? / \d{1,2}:\d{2}(?::\d{2})?$")


def timecode_only(p):
    """True when the post body is nothing but a media player's timecode pair.

    Handing one to `claude -p` spends a call to be told, in prose, that the post text is missing;
    classify() rightly reports that as a failure; the post is left pending; and the next run sends
    it again, for ever. That is not a classifier fault and no retry can fix it — the duration pair
    is already in the store before classify() runs, so the fault is at or before the writer,
    nextdoor_index.py on the webscout box.

    NOT "the body is missing". A body that is empty or whitespace is a body the reader LOST, and
    losing it is recoverable — such a post stays pending and comes back when extraction is fixed."""
    return bool(TIMECODE_PAIR.match((p.get("body") or "").strip()))


def main():
    pending = call("/api/pending")
    kept = 0
    failed = 0
    timecoded = 0
    timecoded_ids = []
    first_failure = None
    for p in pending:
        if timecode_only(p):
            # NOT the fabricated score the comment below forbids: nothing was classified, and the
            # record says so in its own title and quotes the body it was given. other/0 can never
            # reach the brief — that filter needs significance >= 3 AND a news category. Deciding
            # it here is the only thing that stops the re-send, because the post finally leaves the
            # pending queue.
            body = (p.get("body") or "").strip()
            call("/api/annotate", {"id": p["id"], "category": "other", "significance": 0,
                                   "title": "Post body is a media player timecode",
                                   "blurb": "Body is %r — a player overlay, not post text, so "
                                            "nothing was sent to the classifier (area: %s)."
                                            % (body[:40], p.get("area") or "?"),
                                   "keywords": []})
            timecoded += 1
            timecoded_ids.append(p["id"])
            print(f"  - {p.get('id')}: body {body[:40]!r} is a media timecode, not text; resolved "
                  f"other/0 without the classifier", flush=True)
            continue
        try:
            a = classify(p)
        except Exception as exc:
            # NEVER write a fabricated score. Scoring 0 on failure is indistinguishable from a
            # genuine "this post is noise", so a broken classifier looks exactly like a quiet
            # neighbourhood — which is how this went unnoticed for three weeks. Leave the post
            # pending instead: the next run retries it, and the backlog is itself the alarm.
            print(f"  ! classify failed for {p.get('id')}: {type(exc).__name__}: {exc}", flush=True)
            failed += 1
            if first_failure is None:
                first_failure = exc
            continue
        a["id"] = p["id"]
        call("/api/annotate", a)
        if a.get("significance", 0) >= 3 and a.get("category") in ("crime", "accident", "incident", "council", "event"):
            kept += 1
            try:
                urllib.request.urlopen(BASE + "/p/" + p["id"], timeout=90).read()  # prefetch full text
            except Exception:
                pass
    # `sent` is what the classifier was actually ASKED about. Every rate below is against that and
    # not against len(pending): a post that was never sent cannot be evidence about the classifier.
    sent = len(pending) - timecoded
    ok = sent - failed
    print(f"annotated={ok} timecode-only={timecoded} failed={failed} brief-worthy={kept}",
          flush=True)
    # Say it at a level the estate can QUERY. Both lines above are plain stdout, which journald
    # files at info, so a run that classifies most posts and drops a few is invisible: it exits 0,
    # raises no unit-failed incident, and matches no priority<=4 search. Only a total wipeout is
    # loud, and only via the exit below. That blind spot is the steady state, not a corner case —
    # every run from 2026-08-30 to 2026-09-08 failed 1-4 posts and every one of them was green.
    # The failed posts stay pending, and nothing watches that backlog either: there is no
    # local-news lane in services/freshness/lanes.json, by that file's own _doc.
    if failed:
        errlog.warn(f"distill: {failed} of {sent} classification(s) failed and were left "
                    f"pending (annotated {ok})", first_failure)
    # EVERY timecode-only post is said at the same level a failed one is, and for the same reason:
    # under the code above these posts WERE failures and warned here, so anything less would be this
    # change buying a green log with a lost signal. The count is what discriminates — one video post
    # a fortnight reads as one line a fortnight, while a body-extraction regression reads as "17 of
    # 20", which is the sentence that sends a human to the reader. The ids are named so it can be
    # checked, and this fires on ANY number of them, not only on a whole batch.
    if timecoded:
        errlog.warn(f"distill: {timecoded} of {len(pending)} pending post(s) had a media timecode "
                    f"as their whole body and were resolved as other/0 without the classifier — "
                    f"the reader captured a player overlay in place of post text: "
                    f"{', '.join(timecoded_ids[:10])}")
    # A total wipeout is a broken classifier, not a quiet day. Exit non-zero so the unit goes red,
    # which puts it in logview's incident queue instead of dying silently in a green log line.
    #
    # But "all of them" has to mean something. On 2026-08-17T20:50 this fired on 1/1 — a single
    # `claude -p` returned non-JSON, the batch happened to hold one post, and the unit went red for a
    # classifier that was fine on the next run and every run since. One sample cannot distinguish a
    # broken classifier from a flaky call, so below MIN_WIPEOUT this warns and exits clean. Nothing is
    # lost by waiting: a failed post is never annotated (see above), so it stays pending and the
    # backlog is still the alarm — it just takes a real wipeout to say so.
    MIN_WIPEOUT = 3
    # A whole batch of nothing but player overlays is not a street full of silent videos, it is the
    # reader handing us something that is not post text at all. It stays exactly as loud as it is
    # today — under the code above every one of these failed and this same branch went red on the
    # 3/3 batch of 2026-09-09 — but it now clears itself, which the old one never could: the posts
    # were annotated above, so they leave the queue and the next run is green unless the reader is
    # still doing it.
    if timecoded == len(pending) and len(pending) >= MIN_WIPEOUT:
        sys.exit(f"every pending post ({timecoded}/{len(pending)}) had a media timecode as its "
                 f"whole body — suspect the reader's body extraction, not the classifier")
    if sent and ok == 0:
        if sent >= MIN_WIPEOUT:
            sys.exit(f"every classification failed ({failed}/{sent}) — classifier is broken")
        print(f"  ! all {failed} of this batch failed, below the {MIN_WIPEOUT}-post bar for calling "
              f"the classifier broken — left pending for the next run", flush=True)


if __name__ == "__main__":
    main()
