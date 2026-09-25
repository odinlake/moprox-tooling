#!/usr/bin/env python3
"""Build or update one MYO playlist on the family's Yoto account from local audio files.

    myo.py <playlist title> <file.mp3> [file.mp3 ...]

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
import hashlib, json, os, re, sys, time, urllib.request, urllib.error
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
            return str(t).strip()
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


def main():
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
