"""Cover art for the (shuffle) twins: the playlist's own cover with a shuffle mark on it.

A twin is what plays when bard or mo.lan/yoto reorders a playlist, and it wears its source's cover
so the child recognises it -- but in the Yoto app the two sit side by side and must be told apart
at a glance. So the twin gets the same art with the standard shuffle glyph large in the middle.

The glyph is Material Design's "shuffle" (Apache-2.0), whose path is straight lines only, so it is
drawn here as three polygons and needs no SVG rasteriser (claude-dev has none).
"""
import io, urllib.request
from PIL import Image, ImageDraw, ImageFilter

# Material "shuffle", 24x24 viewBox, resolved to absolute points.
SHUFFLE = [
    [(10.59, 9.17), (5.41, 4), (4, 5.41), (9.17, 10.58)],
    [(14.5, 4), (16.54, 6.04), (4, 18.59), (5.41, 20), (17.96, 7.46), (20, 9.5), (20, 4)],
    [(14.83, 13.41), (13.42, 14.82), (16.55, 17.95), (14.5, 20), (20, 20), (20, 14.5),
     (17.96, 16.54)],
]
W, H = 638, 1011                                   # Yoto's default cover size


def glyph(size, color=(255, 255, 255, 255)):
    """The shuffle glyph on a transparent square, drawn 4x and downsampled for clean edges."""
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for poly in SHUFFLE:
        d.polygon([(x / 24 * s, y / 24 * s) for x, y in poly], fill=color)
    return im.resize((size, size), Image.LANCZOS)


def fit(im):
    """Cover-crop to Yoto's portrait card shape."""
    im = im.convert("RGB")
    r = W / H
    if im.width / im.height > r:
        w = round(im.height * r); x = (im.width - w) // 2; im = im.crop((x, 0, x + w, im.height))
    else:
        h = round(im.width / r); im = im.crop((0, 0, im.width, h))
    return im.resize((W, H), Image.LANCZOS)


def shuffle_badge(im, scale=0.46, dim=0.22, ring=(255, 255, 255), disc=(255, 90, 54)):
    """Variant A: the cover, slightly dimmed, with a big shuffle disc dead centre."""
    im = fit(im)
    if dim:
        im = Image.blend(im, Image.new("RGB", im.size, (0, 0, 0)), dim)
    D = round(W * scale)
    cx, cy = W // 2, H // 2
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((cx - D // 2, cy - D // 2 + 10, cx + D // 2, cy + D // 2 + 10), fill=(0, 0, 0, 120))
    lay = Image.alpha_composite(lay, sh.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(lay)
    b = max(6, D // 22)
    d.ellipse((cx - D // 2, cy - D // 2, cx + D // 2, cy + D // 2), fill=ring + (255,))
    d.ellipse((cx - D // 2 + b, cy - D // 2 + b, cx + D // 2 - b, cy + D // 2 - b), fill=disc + (255,))
    g = glyph(round(D * 0.62))
    lay.alpha_composite(g, (cx - g.width // 2, cy - g.height // 2))
    return Image.alpha_composite(im.convert("RGBA"), lay).convert("RGB")


def jpeg(im):
    b = io.BytesIO(); im.save(b, "JPEG", quality=92); return b.getvalue()


def fetch(url):
    return Image.open(io.BytesIO(urllib.request.urlopen(url, timeout=60).read()))
