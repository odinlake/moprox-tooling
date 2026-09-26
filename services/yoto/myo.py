#!/usr/bin/env python3
"""Build or update one MYO playlist on the family's Yoto account from local audio files.

    myo.py <playlist title> <file.mp3> [file.mp3 ...]   build/update from local audio
    myo.py reorder <card> reverse                       reverse (no audio moves, ~0.5 s)
    myo.py reorder <card> <title> [title ...]           named first, rest as-is
    myo.py shuffle <card> [title ...]                   named first, rest shuffled
    myo.py copy <card> <new title>                      a second card over the same audio (~1 s)
    myo.py queue <card> [title ...] [--volume N] [--sleep S] [--rest shuffle|order|none]
                                                        bard's play routine, on the card's twin

One chapter per file, in the order given, titled from the file's ID3 title (falling back to the
filename). Idempotent: the card id is remembered in yoto.env under YOTO_CARD_<slug>, and a later run
with the same title OVERWRITES that card (POST /content with cardId), so a series can grow as more
books arrive without producing a second playlist. Uploads are keyed on sha256, so a file already
transcoded is not sent again.

The flow is yoto.dev/myo/uploading-to-cards, verbatim: GET uploadUrl -> PUT the bytes -> poll
/media/upload/<id>/transcoded until transcodedSha256 -> POST /content with trackUrl yoto:#<sha>.
Needs the user:content:manage scope (yoto.py auth). Linking the playlist to a physical MYO card is
done once in the Yoto app; there is no API for that step.
"""
import hashlib, json, os, random, re, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yoto
from mutagen.id3 import ID3

API = yoto.API


def req(method, path, tok, body=None, ctype="application/json", raw=False):
    data = body if raw else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(path if path.startswith("http") else API + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + tok, "Accept": "application/json",
                                        **({"Content-Type": ctype} if data is not None else {})})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            t = resp.read()
            return json.loads(t) if t and not raw else t
    except urllib.error.HTTPError as e:
        sys.exit("%s %s -> %s %s" % (method, path, e.code, e.read().decode()[:400]))


def title_of(path):
    try:
        t = ID3(path).get("TIT2")
        if t and str(t).strip():
            # Adlibris tags the single-file MP3 as "<title> - 01": a track number on a one-track
            # file. The chapter is the book, so the suffix goes.
            return re.sub(r"\s*-\s*\d{1,2}$", "", str(t).strip())
    except Exception:
        pass
    return re.sub(r"[_-]+", " ", os.path.splitext(os.path.basename(path))[0]).strip().capitalize()


def upload(path, tok):
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    up = req("GET", "/media/transcode/audio/uploadUrl?sha256=%s&filename=%s" % (sha, os.path.basename(path)), tok)["upload"]
    if up.get("uploadUrl"):                       # absent when the server already holds this sha
        urllib.request.urlopen(urllib.request.Request(up["uploadUrl"], data=open(path, "rb").read(), method="PUT",
                               headers={"Content-Type": "audio/mpeg"}), timeout=600).read()
    for _ in range(120):
        tr = req("GET", "/media/upload/%s/transcoded?loudnorm=false" % up["uploadId"], tok).get("transcode") or {}
        if tr.get("transcodedSha256"):
            return tr
        time.sleep(2)
    sys.exit("transcode timed out for " + path)


def card_of(title_or_id, tok):
    """Resolve a library entry by cardId, exact title, or case-insensitive substring."""
    lib = req("GET", "/card/family/library", tok)
    cards = lib.get("cards") or lib
    rows = [(c.get("card") or c) for c in (cards.values() if isinstance(cards, dict) else cards)]
    q = (title_or_id or "").strip().lower()
    for r in rows:
        if r.get("cardId") == title_or_id:
            return r["cardId"]
    for pred in (lambda t: t == q, lambda t: q and q in t):
        for r in rows:
            if pred((r.get("title") or "").lower()):
                return r["cardId"]
    sys.exit("no card matching %r; have: %s" % (title_or_id, [r.get("title") for r in rows]))


def renumber(chapters):
    """Chapter keys and overlayLabels are POSITIONAL. Reordering the list without rewriting them
    leaves chapter 21 still calling itself 1, and the player's display follows the label, not the
    position -- so this is not cosmetic."""
    for i, ch in enumerate(chapters, 1):
        ch["key"] = "%02d" % i
        ch["overlayLabel"] = str(i)
        for tr in ch.get("tracks") or []:
            tr["overlayLabel"] = str(i)
    return chapters


def set_order(card_id, chapters, tok, title=None):
    """POST a new chapter order. NO AUDIO MOVES: every track already carries its own
    `trackUrl` (yoto:#<sha>), so the card is self-describing and this is pure metadata. Measured at
    0.45 s for 21 chapters, against ~115 s to rebuild the same card from local files."""
    d = req("GET", "/card/" + card_id, tok)
    card = d.get("card") or d
    chapters = renumber(chapters)
    tot_d = sum(c.get("duration") or 0 for c in chapters)
    tot_s = sum(c.get("fileSize") or 0 for c in chapters)
    req("POST", "/content", tok, {
        "cardId": card_id, "title": title or card.get("title"),
        "content": {"chapters": chapters},
        "metadata": {"media": {"duration": tot_d, "fileSize": tot_s,
                               "readableFileSize": round(tot_s / 1024 / 1024, 1)}}})
    return chapters


def chapters_of(card_id, tok):
    # /content, not /card: /card signs every trackUrl into a secure-media URL that expires within a
    # day, whereas /content returns the stored yoto:#<sha> form. Anything read here gets POSTed back
    # (copy, reorder), so it must be the permanent form.
    d = req("GET", "/content/" + card_id, tok)
    return ((d.get("card") or d).get("content") or {}).get("chapters") or []


def pick(chapters, wanted):
    """Titles named by the caller, in the order named, then everything else. Matching is
    case-insensitive substring on the chapter title, so "rymdskeppet" or "space" both land, and an
    unmatched name is reported rather than silently dropped -- a kid asking for a story that is not
    on the card should hear about it."""
    rest, first, missed = list(chapters), [], []
    for w in wanted:
        q = (w or "").strip().lower()
        # Exact title or chapter key, then whole word, THEN substring: "flygplanet" is a substring of
        # "Här kommer brandflygplanet", which sits earlier on the card, so substring-first played
        # the wrong book.
        word = re.compile(r"(?<!\w)" + re.escape(q) + r"(?!\w)") if q else None
        hit = next((c for c in rest if q and q in ((c.get("title") or "").lower(), c.get("key"))), None) \
            or next((c for c in rest if word and word.search((c.get("title") or "").lower())), None) \
            or next((c for c in rest if q and q in (c.get("title") or "").lower()), None)
        if hit is None:
            missed.append(w)
        else:
            first.append(hit); rest.remove(hit)
    return first, rest, missed


def copy(src, title, tok):
    """Create (or refresh) a second card whose chapters are the source card's, verbatim.

    NO AUDIO MOVES: chapters reference their audio by trackUrl (yoto:#<sha>), so a copy is one POST.
    Idempotent on title like a build: the new card id is remembered under YOTO_CARD_<slug>, and a
    re-run overwrites that card with the source's current chapters -- which is how the copy picks up
    books added to the source later. Exists so a card an agent reorders freely can be separate from
    one a child knows by its numbers (reordering renumbers every chapter)."""
    key = "YOTO_CARD_" + re.sub(r"[^A-Za-z0-9]+", "_", title).upper().strip("_")
    cid = yoto.env().get(key)
    chapters = renumber(json.loads(json.dumps(chapters_of(src, tok))))
    tot_d = sum(c.get("duration") or 0 for c in chapters)
    tot_s = sum(c.get("fileSize") or 0 for c in chapters)
    content = {"title": title, "content": {"chapters": chapters},
               "metadata": {"media": {"duration": tot_d, "fileSize": tot_s,
                                      "readableFileSize": round(tot_s / 1024 / 1024, 1)}}}
    if cid:
        content["cardId"] = cid
    out = req("POST", "/content", tok, content)
    new = (out.get("card") or out).get("cardId") or cid
    if new and new != cid:
        yoto._put(key, new)
    return new, len(chapters)


def twin_of(cid, tok):
    """The card that playback is reordered on, never the card itself.

    Reordering renumbers every chapter, and a physical MYO card linked to a playlist shows those
    numbers, so a playlist a child knows is never reordered. Its twin "<title> (shuffle)" is, and
    the twin is REWRITTEN from the source's chapters on every queue, so books added to the source
    appear in the twin without anyone running copy. The mapping lives in yoto.env as
    YOTO_TWIN_<cid>; a card that is itself a twin plays as itself."""
    e = yoto.env()
    if cid in {v for k, v in e.items() if k.startswith("YOTO_TWIN_")}:
        return cid, cid
    twin = e.get("YOTO_TWIN_" + cid)
    if not twin:
        d = req("GET", "/content/" + cid, tok)
        twin, _ = copy(cid, (d.get("card") or d).get("title", cid).rstrip() + " (shuffle)", tok)
        yoto._put("YOTO_TWIN_" + cid, twin)
    return cid, twin


def queue(ident, wanted=(), volume=None, sleep=2700, rest="shuffle", dry=False):
    """Bard's play routine, once, for bard and the web picker alike.

    `wanted` (titles or chapter keys) play first in the order given; `rest` is "shuffle", "order"
    or "none" for what follows them. Order is set on the twin (0.5 s, no audio moves), then one
    MQTT session sets volume BEFORE starting (a quiet request must never start loud), starts at
    chapter 01 with secondsIn so it is a start and not a resume, sets the sleep timer AFTER the
    start so it never delays the sound, and returns what the device says is actually playing.
    volume=None leaves the volume alone; sleep=0 disables the timer. dry=True resolves and orders
    but neither writes the card nor touches the player."""
    tok = yoto.token()
    src, twin = twin_of(card_of(ident, tok), tok)
    chapters = chapters_of(src, tok)
    first, others, missed = pick(chapters, list(wanted))
    if rest == "shuffle":
        random.shuffle(others)
    elif rest == "none":
        others = []
    if not first and not others:
        sys.exit("nothing to play: no chapter matched %r" % (list(wanted),))
    if dry:
        return {"source": src, "cardId": twin, "order": [c.get("title") for c in first + others],
                "not_found": missed, "volume": volume, "sleep": sleep, "dry": True}
    ordered = set_order(twin, first + others, tok)
    with yoto.Link(tok, yoto.env()["YOTO_DEVICE_ID"]) as link:
        if volume is not None:
            link.send("volume/set", {"volume": yoto.vol_cmd(volume)})
        link.send("card/start", {"uri": "https://yoto.io/" + twin, "chapterKey": "01",
                                 "trackKey": "01", "secondsIn": 0}, wait=6)
        if sleep is not None:
            link.send("sleep-timer/set", {"seconds": int(sleep)})
        now = yoto.now_playing(link)
    return {"source": src, "cardId": twin, "order": [c.get("title") for c in ordered],
            "not_found": missed, "volume": volume, "sleep": sleep, "now": now}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "queue":
        import argparse
        ap = argparse.ArgumentParser(prog="yoto queue")
        ap.add_argument("card"); ap.add_argument("titles", nargs="*")
        ap.add_argument("--volume", type=int); ap.add_argument("--sleep", type=int, default=2700)
        ap.add_argument("--rest", choices=("shuffle", "order", "none"), default="shuffle")
        ap.add_argument("--dry", action="store_true", help="resolve and order only; touch nothing")
        a = ap.parse_args(sys.argv[2:])
        print(json.dumps(queue(a.card, a.titles, a.volume, a.sleep, a.rest, a.dry), ensure_ascii=False))
        return
    # Fast paths that touch no audio. These exist because rebuilding a card from local files costs
    # ~115 s for 21 chapters while reordering the same card costs 0.45 s, and a kids-facing agent
    # asking for "shuffle but start with X" must not wait two minutes to make a sound.
    if len(sys.argv) >= 3 and sys.argv[1] in ("reorder", "shuffle"):
        mode, ident, wanted = sys.argv[1], sys.argv[2], sys.argv[3:]
        tok = yoto.token()
        cid = card_of(ident, tok)
        chapters = chapters_of(cid, tok)
        if mode == "reorder":
            if wanted and wanted[0] == "reverse":
                ordered, missed = list(reversed(chapters)), []
            else:
                first, rest, missed = pick(chapters, wanted)
                ordered = first + rest          # named first, remainder left in its current order
        else:
            first, rest, missed = pick(chapters, wanted)
            random.shuffle(rest)
            ordered = first + rest
        set_order(cid, ordered, tok)
        print(json.dumps({"cardId": cid, "mode": mode, "chapters": len(ordered),
                          "order": [c.get("title") for c in ordered],
                          "not_found": missed}, ensure_ascii=False))
        return

    if len(sys.argv) == 4 and sys.argv[1] == "copy":
        tok = yoto.token()
        src = card_of(sys.argv[2], tok)
        cid, n = copy(src, sys.argv[3], tok)
        print(json.dumps({"from": src, "cardId": cid, "title": sys.argv[3], "chapters": n}, ensure_ascii=False))
        return

    if len(sys.argv) < 3:
        sys.exit(__doc__)
    title, files = sys.argv[1], sys.argv[2:]
    tok = yoto.token()
    key = "YOTO_CARD_" + re.sub(r"[^A-Za-z0-9]+", "_", title).upper().strip("_")
    card_id = yoto.env().get(key)
    chapters, total_d, total_s = [], 0, 0
    for i, f in enumerate(files, 1):
        tr = upload(f, tok); info = tr.get("transcodedInfo") or {}
        t = title_of(f); k = "%02d" % i
        chapters.append({"key": k, "title": t, "overlayLabel": str(i),
                         "tracks": [{"key": "01", "title": t, "trackUrl": "yoto:#" + tr["transcodedSha256"],
                                     "duration": info.get("duration"), "fileSize": info.get("fileSize"),
                                     "channels": info.get("channels"), "format": info.get("format"),
                                     "type": "audio", "overlayLabel": str(i)}]})
        total_d += info.get("duration") or 0; total_s += info.get("fileSize") or 0
        print("chapter %s  %-40s %4ss" % (k, t[:40], info.get("duration")), flush=True)
    content = {"title": title, "content": {"chapters": chapters},
               "metadata": {"media": {"duration": total_d, "fileSize": total_s,
                                      "readableFileSize": round(total_s / 1024 / 1024, 1)}}}
    if card_id:
        content["cardId"] = card_id
    out = req("POST", "/content", tok, content)
    cid = (out.get("card") or out).get("cardId") or card_id
    if cid and cid != card_id:
        yoto._put(key, cid)
    print("playlist %r -> cardId %s, %d chapters, %.0f min" % (title, cid, len(chapters), total_d / 60))


if __name__ == "__main__":
    main()
