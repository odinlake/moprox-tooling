#!/usr/bin/env python3
"""strength_sheet.py — the dashboard's strength charts as a one-page A4 PDF and a PNG.

  strength_sheet.py <strength.json> <out_dir>    writes strength-sheet.pdf + strength-sheet.png

The charts are not redrawn here. strengthProgress() is lifted verbatim from the dashboard's
index.html and run by headless Chromium on the same strength.json, so the sheet always matches the
panel, including every change made to the panel later. Layout: portrait A4, 6 mm margins, two
charts across (operator 2026-10-16: "closely replicated on a single page with small margins and
spacing"). The dashboard's strength panel links both files.

Chromium is Playwright's chrome-headless-shell under ~/.cache/ms-playwright, driven by its own
--print-to-pdf / --screenshot flags, so no Python or Node Playwright package is needed.
"""
import glob, html, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "components/dashboard/web/index.html"
W_PX, H_PX = 794, 1123            # A4 at 96 dpi: the PNG is the same page the PDF prints

CSS = """
@page{size:A4 portrait;margin:6mm}
:root{--bg:#fff;--panel:#fff;--bd:#d0d7de;--fg:#1f2328;--mut:#59636e;--grid:#e9edf1;--accent:#0969da}
html,body{margin:0;background:#fff;color:var(--fg);font-family:-apple-system,system-ui,"Segoe UI",Roboto,"DejaVu Sans",sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}
@media screen{body{padding:6mm;box-sizing:border-box;width:%dpx}}
h1{font-size:12pt;margin:0 0 1mm} h1 span{font-weight:400;color:var(--mut);font-size:8.5pt}
svg{display:block;width:100%%;height:auto}
.axis{fill:var(--mut);font-size:10px}.grid line{stroke:var(--grid)}
.muted{color:var(--mut)}
#c > div:first-child{grid-template-columns:1fr 1fr !important;gap:2mm 4mm !important;margin:0 0 1mm !important}
#c > div.muted{font-size:7.5pt !important;margin:0 !important}
""" % W_PX


def chromium():
    hits = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    if not hits:
        sys.exit("strength_sheet: no chrome-headless-shell under ~/.cache/ms-playwright")
    return hits[-1]


def chart_function():
    src = INDEX.read_text()
    m = re.search(r"^function strengthProgress\(d\)\{.*?^\}", src, re.S | re.M)
    if not m:
        sys.exit("strength_sheet: strengthProgress() not found in " + str(INDEX))
    return m.group(0)


def page(feed):
    gen = feed.get("generated")
    asof = time.strftime("%-d %b %Y", time.localtime(gen / 1000 if gen and gen > 1e11 else gen)) if gen else "?"
    data = json.dumps(feed, ensure_ascii=False).replace("</", "<\\/")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Strength progress</title>"
            f"<style>{CSS}</style></head><body>"
            f"<h1>Strength progress <span>· training dashboard · data as of {html.escape(asof)}</span></h1>"
            f"<div id='c'></div><script>{chart_function()}\n"
            f"document.getElementById('c').innerHTML = strengthProgress({data});</script></body></html>")


def run(*args):
    r = subprocess.run([chromium(), "--no-sandbox", "--disable-gpu", "--hide-scrollbars", *args],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError("chrome-headless-shell exit %d: %s" % (r.returncode, r.stderr.strip()[-300:]))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    feed = json.loads(Path(sys.argv[1]).read_text())
    if not feed.get("progress"):
        print("strength_sheet: feed has no progress section; nothing drawn")
        return
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "sheet.html"
        src.write_text(page(feed))
        pdf, png = Path(tmp) / "s.pdf", Path(tmp) / "s.png"
        run("--no-pdf-header-footer", f"--print-to-pdf={pdf}", src.as_uri())
        run(f"--window-size={W_PX},{H_PX}", "--force-device-scale-factor=2", f"--screenshot={png}", src.as_uri())
        for f in (pdf, png):
            if not f.exists() or f.stat().st_size < 1000:
                raise RuntimeError(f"chrome-headless-shell wrote no usable {f.suffix}")
        os.replace(pdf, out / "strength-sheet.pdf")
        os.replace(png, out / "strength-sheet.png")
    print(f"strength_sheet: wrote {out}/strength-sheet.pdf + .png")


if __name__ == "__main__":
    main()
