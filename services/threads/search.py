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
import os, sys, subprocess, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import errlog  # noqa: E402

THREADS_DIR = os.environ.get("THREADS_DIR", os.path.expanduser("~/.local/share/moprox/threads"))

def counts(term):
    """{path: matches} for every thread containing `term` (case-insensitive, fixed-string).

    rg walks THREADS_DIR itself. This used to glob every thread and pass them all as arguments,
    which broke on 2026-07-06 when 30k docindex batch runs were archived: the list passed ARG_MAX
    and every search died with "Argument list too long" for four months, unnoticed."""
    r = subprocess.run(["rg", "--count-matches", "-i", "-F", "--glob", "*.md", "-e", term, THREADS_DIR],
                       capture_output=True, text=True)
    if r.returncode > 1:                            # 0 = hits, 1 = no hits, 2 = rg itself failed
        raise RuntimeError(f"rg failed ({r.returncode}): {r.stderr.strip()[:300]}")
    out = {}
    for line in r.stdout.splitlines():
        path, _, n = line.rpartition(":")
        if path: out[path] = int(n)
    return out

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
    try:
        per_term = [counts(t) for t in terms]
    except Exception as e:
        errlog.err("thread-search: search failed", e); return 1
    files = set.intersection(*(set(c) for c in per_term))
    if not files:
        print(f"no threads match all of: {' '.join(terms)}"); return 0
    scored = []
    for f in files:
        with open(f, errors="ignore") as fh:
            head = fh.readline().lstrip("# ").strip(); meta = fh.readline().strip()
        end = (re.search(r"–(\d{4}-\d{2}-\d{2})", meta) or re.search(r"(\d{4}-\d{2}-\d{2})", meta))
        end = end.group(1) if end else "0000-00-00"
        if since and end < since: continue
        hits = sum(c[f] for c in per_term)
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
