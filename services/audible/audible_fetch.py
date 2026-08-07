#!/usr/bin/env python3
"""Audible library fetcher — the title/metadata side of the audiobook shelf.

Mirrors polar_fetch.py / technogym_fetch.py: credentials outside every repo, output into
private-data, NO git commit here (private-data-sync.timer sweeps it), NO Telegram post (pure ETL).

Auth comes from the device registration made once by audible_bootstrap.py. Access tokens last 60
minutes and are renewed from the stored refresh token, so this runs unattended indefinitely; it
never sees the password.

WHAT IT ASKS FOR AND WHY
  /1.0/library returns almost nothing unless you name response_groups — the default payload is
  essentially asins. `product_desc` is the one carrying the synopsis (publisher_summary), which is
  the field most worth having and the easiest to forget.

SCOPE: metadata only. The same credential can download and decrypt the audio; this deliberately
does not, and nothing here should grow that ability by accident.

  --full   ignore the stored high-water mark and re-fetch the whole library
"""
import argparse
import html
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

import audible

AUTH_FILE = pathlib.Path(os.environ.get(
    "AUDIBLE_DIR", pathlib.Path.home() / ".config/claude-dev/audible")) / "auth.json"
OUT = pathlib.Path.home() / "projects/private-data/audible"
LIBRARY = OUT / "library.json"
PAGE_SIZE = 250           # NOT the documented 1000 max: a full page with synopses is a big payload
                          # and the first live run timed out. Smaller pages, more of them.
POLITE_S = 1.0

RESPONSE_GROUPS = ",".join([
    "contributors",            # authors + narrators
    "product_desc",            # publisher_summary — the synopsis
    "product_attrs",           # title, subtitle, runtime, language
    "product_extended_attrs",  # publisher, release date
    "series",
    "product_details",
    "media",                   # cover art urls
    "category_ladders",        # genre path
    "is_finished",
    "percent_complete",
])

TAGS = re.compile(r"<[^>]+>")


def _text(s):
    """Synopses come back as HTML. Store readable text — the search service would otherwise index
    markup, and every consumer would have to strip it again."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", s))).strip()


def _names(item, key):
    return [c.get("name", "") for c in (item.get(key) or []) if c.get("name")]


def _series(item):
    out = []
    for s in (item.get("series") or []):
        title = s.get("title") or ""
        seq = s.get("sequence") or ""
        out.append({"title": title, "sequence": seq} if title else None)
    return [s for s in out if s]


def _genres(item):
    names = []
    for ladder in (item.get("category_ladders") or []):
        for step in (ladder.get("ladder") or []):
            n = step.get("name")
            if n and n not in names:
                names.append(n)
    return names


def compact(item):
    """One lean record per title. Keeps what a human or an agent would search on and drops the
    rest — the raw payload is ~10x larger and almost all of it is plumbing."""
    runtime = item.get("runtime_length_min")
    return {
        "asin": item.get("asin"),
        "title": item.get("title") or "",
        "subtitle": item.get("subtitle") or "",
        "authors": _names(item, "authors"),
        "narrators": _names(item, "narrators"),
        "series": _series(item),
        "publisher": item.get("publisher_name") or "",
        "released": item.get("release_date") or item.get("issue_date") or "",
        "purchased": (item.get("purchase_date") or "")[:10],
        "runtime_min": runtime if isinstance(runtime, int) else None,
        "language": item.get("language") or "",
        "genres": _genres(item),
        "synopsis": _text(item.get("publisher_summary") or item.get("merchandising_summary")),
        "cover": ((item.get("product_images") or {}).get("500")
                  or (item.get("product_images") or {}).get("1000") or ""),
        "finished": bool(item.get("is_finished")),
        "percent_complete": item.get("percent_complete"),
    }


def fetch_all(client):
    items, page = [], 1
    while True:
        resp = client.get("1.0/library", num_results=PAGE_SIZE, page=page,
                          response_groups=RESPONSE_GROUPS, sort_by="-PurchaseDate")
        batch = resp.get("items") or []
        items.extend(batch)
        print(f"  page {page}: {len(batch)} titles", flush=True)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(POLITE_S)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="(reserved; the API has no cheap delta for this)")
    ap.parse_args()

    if not AUTH_FILE.exists():
        sys.exit(f"no device registration at {AUTH_FILE} — run audible_bootstrap.py once, interactively")

    auth = audible.Authenticator.from_file(AUTH_FILE)
    # Cheap and idempotent: the library refreshes only when the 60-minute access token has expired.
    try:
        auth.refresh_access_token()
        auth.to_file(AUTH_FILE, encryption=False)     # persist the rotated token
    except Exception as exc:                          # noqa: BLE001
        print(f"  ! token refresh failed ({type(exc).__name__}: {exc}) — trying the stored token",
              flush=True)

    # The client defaults to a 10s timeout, which a metadata-heavy page blows straight through —
    # the first live run died on exactly that, and the error ("API request timed out, please be
    # patient") reads like a server problem rather than a client setting.
    with audible.Client(auth=auth, timeout=120) as client:
        raw = fetch_all(client)

    rows = [compact(i) for i in raw]
    rows.sort(key=lambda r: (r.get("purchased") or "", r.get("title") or ""), reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(rows),
        "titles": rows,
    }
    tmp = LIBRARY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LIBRARY)          # atomic: the search service reads this file on mtime change
    with_syn = sum(1 for r in rows if r["synopsis"])
    print(f"[audible] {len(rows)} titles -> {LIBRARY} ({with_syn} with a synopsis)", flush=True)


if __name__ == "__main__":
    main()
