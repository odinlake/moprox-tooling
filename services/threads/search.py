#!/usr/bin/env python3
"""Search the archived conversation transcripts (built by archive.py). For agents to run on demand —
NOT loaded into any context automatically. AND-of-terms across each thread, ranked by hit count.

Usage:
  thread-search "totolo embedding dimensions"      # all terms must appear in the thread
  thread-search --since 2026-05-01 "halifax floor" # only threads ending on/after a date
  thread-search -n 5 "sudo ask rule"               # cap results
Prints each hit: title · date · project · #hits · path, with a couple of matching snippets.
Then Read the path for the full thread.
"""
import os, sys, subprocess, glob, re

THREADS_DIR = os.environ.get("THREADS_DIR", os.path.expanduser("~/.local/share/moprox/threads"))

def main(argv):
    since = None; limit = 10; terms = []
    it = iter(argv)
    for a in it:
        if a == "--since": since = next(it, None)
        elif a in ("-n", "--limit"): limit = int(next(it, "10"))
        else: terms.append(a)
    if len(terms) == 1: terms = terms[0].split()   # allow one quoted phrase OR bare words
    if not terms:
        print("usage: thread-search [--since YYYY-MM-DD] [-n N] <terms...>"); return 2
    files = set(glob.glob(os.path.join(THREADS_DIR, "*", "*.md")))
    # AND: keep only files containing every term (case-insensitive, fixed-string)
    for t in terms:
        r = subprocess.run(["rg", "-il", "-F", t, *files], capture_output=True, text=True)
        files &= set(r.stdout.split("\n")) - {""}
        if not files: break
    if not files:
        print(f"no threads match all of: {' '.join(terms)}"); return 0
    scored = []
    for f in files:
        head = open(f, errors="ignore").readline().lstrip("# ").strip()
        meta = ""
        with open(f, errors="ignore") as fh:
            fh.readline(); meta = fh.readline().strip()
        end = (re.search(r"–(\d{4}-\d{2}-\d{2})", meta) or re.search(r"(\d{4}-\d{2}-\d{2})", meta))
        end = end.group(1) if end else "0000-00-00"
        if since and end < since: continue
        hits = sum(len(subprocess.run(["rg", "-o", "-F", "-i", t, f], capture_output=True, text=True).stdout.split("\n")) - 1 for t in terms)
        scored.append((hits, end, head, meta, f))
    scored.sort(reverse=True)
    if not scored:
        print(f"no threads match all of: {' '.join(terms)}" + (f" since {since}" if since else "")); return 0
    print(f"{len(scored)} thread(s) matching: {' '.join(terms)}\n")
    for hits, end, head, meta, f in scored[:limit]:
        proj = (re.search(r"project: (\S+)", meta) or [None, "?"])[1]
        print(f"━ {head}")
        print(f"  {end} · {proj} · {hits} hits · {f}")
        snip = subprocess.run(["rg", "-i", "-F", "-m2", "--no-line-number", "-N", terms[0], f],
                              capture_output=True, text=True).stdout.strip().split("\n")
        for s in snip[:2]:
            print(f"    … {s.strip()[:160]}")
        print()
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
