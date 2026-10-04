#!/usr/bin/env python3
"""Rebuild a reader playlist's card image from its books: the chapter covers in a grid inside the
orange frame, with the reader's badge (silhouette holding an open book) over the bottom.

    reader_cover.py <card> [--dry out.jpg]

The Daddy Reads and Akiko Läser images were first drawn by one-off scripts with the books typed in
(card-covers/daddy_reads_cover.py, akiko_cover.py), so every new recording left the playlist image a
book behind. This reads the card's chapters and uses whatever cover the picker has for each
(~/.local/share/moprox/yoto/covers/<chapter title>.jpg, which bard fills in curate mode), so it can
run after every new recording. Chapters without a cover are left out rather than drawn blank. More
than MAX books: the newest MAX, since the newest is the one the family will be looking for.

Each book's reader badge is NOT baked into its cover: the picker crops covers to 5:6 tiles, so a
baked-in corner landed somewhere different on every book (tried 2026-10-04, reverted the same day).
The picker draws it from READER_OF instead, pinned to the tile's corner.
"""
import math, os, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covers, myo, yoto

SHARE = Path.home() / ".local/share/moprox/yoto"
COVERS, ART = SHARE / "covers", SHARE / "card-covers"
W, H = covers.W, covers.H
ORANGE, NAVY = (255, 139, 2), (31, 42, 68)          # Pappa läser's frame, sampled 2026-09-28
G, MAX = 12, 6
# cardId -> the reader's white silhouette (from private-data/family/icons), its crop box, and its
# width/top as fractions of the badge. Values are the ones the hand-made covers were approved with.
MIKAEL = ("mikael-white-1024.png", (204, 93, 820, 939), 0.60, 0.13)
AKIKO = ("akiko-white-1024.png", (213, 119, 828, 939), 0.52, 0.17)
READERS = {"ckpmj": MIKAEL,     # Daddy Reads
           "gKv8S": AKIKO,      # Akiko Läser
           "6jwsz": MIKAEL}     # Pappa läser little tiger books: its card image is hand-made, so
CARD_IMAGE = {"ckpmj", "gKv8S"}  # it is never rebuilt here; listed for the picker's badge only
READER_OF = {cid: r[0].split("-")[0] for cid, r in READERS.items()}   # cardId -> "mikael" | "akiko"


def badge(D, reader, sc=2):
    png, box, wf, tf = reader
    d = D * sc
    lay = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    g = ImageDraw.Draw(lay)
    ring = max(6, d // 36)
    g.ellipse((0, 0, d - 1, d - 1), fill=(255, 255, 255, 255))
    g.ellipse((ring, ring, d - 1 - ring, d - 1 - ring), fill=NAVY + (255,))
    inner = Image.new("L", (d, d), 0)
    ImageDraw.Draw(inner).ellipse((ring, ring, d - 1 - ring, d - 1 - ring), fill=255)
    sil = Image.open(ART / png).crop(box)
    sw = round(d * wf); sh = round(sil.height * sw / sil.width)
    sil = sil.resize((sw, sh), Image.LANCZOS)
    person = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    person.alpha_composite(sil, ((d - sw) // 2, round(d * tf)))
    person.putalpha(Image.composite(person.getchannel("A"), Image.new("L", (d, d), 0), inner))
    lay.alpha_composite(person)
    cx = d / 2; bw = d * 0.50; bh = d * 0.21; top = d * 0.64; bot = top + bh; e = d * 0.022
    book = Image.new("RGBA", (d, d), (0, 0, 0, 0)); b = ImageDraw.Draw(book)
    b.polygon([(cx, top + bh * .10 + e), (cx - bw / 2 - e, top - e), (cx - bw / 2 - e, bot - bh * .06 + e),
               (cx, bot + e * 1.6), (cx + bw / 2 + e, bot - bh * .06 + e), (cx + bw / 2 + e, top - e)],
              fill=ORANGE + (255,))
    ow = max(3, d // 110)
    for s in (-1, 1):
        pg = [(cx, top + bh * .10), (cx + s * bw / 2, top), (cx + s * bw / 2, bot - bh * .06), (cx, bot)]
        b.polygon(pg, fill=(255, 247, 232, 255), outline=NAVY + (255,), width=ow)
        for k in (0.30, 0.50, 0.70):
            y0 = top + bh * k
            b.line([(cx + s * bw * 0.08, y0 + bh * .06 * (1 - k)), (cx + s * bw * 0.40, y0 - bh * .02)],
                   fill=NAVY + (150,), width=max(2, ow - 1))
    b.line([(cx, top + bh * .10), (cx, bot)], fill=NAVY + (255,), width=ow)
    book.putalpha(Image.composite(book.getchannel("A"), Image.new("L", (d, d), 0), inner))
    lay.alpha_composite(book)
    return lay.resize((D, D), Image.LANCZOS)


def collage(paths):
    """1 book: full width. 2: stacked. 3+: two columns. Each cell cover-cropped from the TOP, where
    the titles are."""
    base = Image.new("RGB", (W, H), ORANGE)
    n = len(paths)
    cols = 1 if n <= 2 else 2
    rows = math.ceil(n / cols)
    cw = (W - G * (cols + 1)) // cols
    ch = (H - G * (rows + 1)) // rows
    for i, p in enumerate(paths):
        bk = Image.open(p).convert("RGB")
        sc = max(cw / bk.width, ch / bk.height)
        bk = bk.resize((round(bk.width * sc), round(bk.height * sc)), Image.LANCZOS)
        x0 = (bk.width - cw) // 2
        r, c = divmod(i, cols)
        if n % cols and i == n - 1:                 # odd last book: centre it in its row
            x = (W - cw) // 2
        else:
            x = G + c * (cw + G)
        base.paste(bk.crop((x0, 0, x0 + cw, ch)), (x, G + r * (ch + G)))
    return base


def build(card, tok):
    cid = myo.card_of(card, tok)
    if cid not in READERS:
        sys.exit("%s is not a reader playlist (%s)" % (cid, ", ".join(READERS)))
    titles = [c.get("title") for c in myo.chapters_of(cid, tok)]
    have = [COVERS / ("%s.jpg" % t) for t in titles if (COVERS / ("%s.jpg" % t)).exists()]
    missing = [t for t in titles if not (COVERS / ("%s.jpg" % t)).exists()]
    if cid not in CARD_IMAGE:
        return cid, None, [p.stem for p in have], missing
    if not have:
        sys.exit("no chapter on %s has a cover yet" % cid)
    have = have[-MAX:]
    im = collage(have).convert("RGBA")
    n = len(have)
    D, cy = (320, 840) if n == 1 else (270, 865) if n == 2 else (250, H // 2 if n <= 4 else 880)
    cx = W // 2
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((cx - D // 2, cy - D // 2 + 10, cx + D // 2, cy + D // 2 + 10), fill=(0, 0, 0, 130))
    im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(14)))
    im.alpha_composite(badge(D, READERS[cid]), (cx - D // 2, cy - D // 2))
    return cid, im.convert("RGB"), [p.stem for p in have], missing


def badge_png(reader, out, D=192):
    """The picker's corner badge as a transparent PNG (web/reader-<name>.png)."""
    badge(D, reader).save(out)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--badges":
        for r in (MIKAEL, AKIKO):
            badge_png(r, os.path.join(sys.argv[2], "reader-%s.png" % r[0].split("-")[0]))
        return
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tok = yoto.token()
    cid, im, used, missing = build(sys.argv[1], tok)
    import json
    if im is None:                                  # hand-made card image (see CARD_IMAGE)
        print(json.dumps({"card": cid, "books": used, "no_cover": missing, "card_image": False},
                         ensure_ascii=False))
        return
    out = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--dry" else str(ART / ("card-%s.jpg" % cid))
    im.save(out, quality=92)
    res = {"card": cid, "image": out, "books": used, "no_cover": missing}
    if "--dry" not in sys.argv:
        res["cover"] = myo.set_cover(cid, out, tok)["cover"]
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
