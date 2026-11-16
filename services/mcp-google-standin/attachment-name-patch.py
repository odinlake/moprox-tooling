#!/usr/bin/env python3
"""Idempotent patch: get_gmail_attachment_content names attachments from the wrong part.

workspace-mcp 1.22.0 re-fetches message metadata to name the saved attachment, with a
`fields` mask that lists only ONE level of `parts`. Attachments nested deeper (images in a
multipart/related inside multipart/mixed) are invisible to it, so its "only one attachment
left, use its name" fallback labels every nested part with the one top-level attachment's
name: a PNG saved as "<the pdf>.pdf" (issue i-20261105-204051). This widens the mask to
nested parts so the attachmentId / size matches can see every part.

Run after any `pip install/upgrade workspace-mcp` (it rewrites the file), then restart
mcp-google. Safe to re-run: detects the patched shape and no-ops.

  sudo /opt/mcp-google/venv/bin/python attachment-name-patch.py
"""
import sys
from pathlib import Path

TARGET_HINT = "/opt/mcp-google/venv/lib/python3.13/site-packages/gmail/gmail_tools.py"

STOCK = ('fields="payload(parts(filename,mimeType,body(attachmentId,size)),'
         'body(attachmentId,size),filename,mimeType)",')
LEAF = "filename,mimeType,body(attachmentId,size)"
MASK = LEAF
for _ in range(6):                      # six MIME levels below the payload
    MASK = f"{LEAF},parts({MASK})"
PATCHED = f'fields="payload({MASK})",  # attachment-name-patch'


def locate():
    p = Path(TARGET_HINT)
    if p.exists():
        return p
    try:
        import gmail.gmail_tools as gt  # noqa
        return Path(gt.__file__)
    except Exception as e:
        sys.exit(f"could not locate gmail/gmail_tools.py ({e}) — run with the venv python")


def main():
    path = locate()
    text = path.read_text()
    if PATCHED in text:
        print("[skip] attachment fields mask: already patched")
        return
    n = text.count(STOCK)
    if n != 1:
        sys.exit(f"[FAIL] expected the stock fields mask once, found {n} "
                 f"(package layout changed?) — review {path}")
    path.write_text(text.replace(STOCK, PATCHED))
    print(f"[ok]   attachment fields mask widened to nested parts; wrote {path}")


if __name__ == "__main__":
    main()
