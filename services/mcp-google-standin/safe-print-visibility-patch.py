#!/usr/bin/env python3
"""Idempotent patch: safe_print() throws away every fatal reason under systemd.

workspace-mcp 1.22.0's `main.safe_print()` is the ONLY channel its startup path has for a
fatal reason -- `safe_print("<reason>")` followed by `sys.exit(1)`, at a dozen sites in
main.py (bad --tool-tier, single-user/OAuth21 conflict, service-account mode without
USER_GOOGLE_EMAIL, ...). But it opens with

    if not sys.stderr.isatty():
        logger.debug(f"[MCP Server] {text}")
        return

and under systemd stderr is a journal socket, never a tty, while the root logger is at
INFO (main.py:97, core/server.py:46). So every one of those reasons is written as a DEBUG
record and dropped. mcp-google.service restarted 10 times on 2026-11-26 09:07:40..09:08:23Z,
each run 1.4 s then `status=1/FAILURE`, and the journal holds not one byte of cause -- the
last line of every failed run is "OAuth 2.0 legacy mode ..." and then nothing. Same shape
for the 9 restarts after the 2026-11-09 OOM. No `[MCP Server]` record has ever reached the
fleet journal from this host.

The guard protects nothing. Its comment says it is there to keep stderr clean for a stdio
MCP peer, but the package logs INFO to stderr unconditionally all through startup (every
`[REGISTRY]`/`[AUTH]` line in the journal arrives that way), so suppressing safe_print to
DEBUG buys no protection and costs the whole fatal-diagnostic surface. This raises only the
level: no control flow, no new output channel, and the interactive path is untouched.

Run after any `pip install/upgrade workspace-mcp` (it rewrites the file), then restart
mcp-google. Safe to re-run: detects the patched shape and no-ops. Fails loudly, without
writing, if the stock line is not found exactly once.

  sudo /opt/mcp-google/venv/bin/python safe-print-visibility-patch.py [path/to/main.py]

Stock line verified against workspace_mcp-1.22.0-py3-none-any.whl
sha256 c17daecb5b3050f7e89019a1f364cf20e950ecadca9a705c1627387ebc987b21.
"""
import sys
from pathlib import Path

TARGET_HINT = "/opt/mcp-google/venv/lib/python3.13/site-packages/main.py"

STOCK = '        logger.debug(f"[MCP Server] {text}")'
PATCHED = '        logger.info(f"[MCP Server] {text}")  # safe-print-visibility-patch'


def locate():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    p = Path(TARGET_HINT)
    if p.exists():
        return p
    try:
        import main  # noqa
        return Path(main.__file__)
    except Exception as e:
        sys.exit(f"could not locate workspace-mcp's main.py ({e}) — run with the venv python")


def main():
    path = locate()
    text = path.read_text()
    if PATCHED in text:
        print("[skip] safe_print level: already patched")
        return
    n = text.count(STOCK)
    if n != 1:
        sys.exit(f"[FAIL] expected the stock safe_print debug line once, found {n} "
                 f"(package layout changed?) — review {path}")
    path.write_text(text.replace(STOCK, PATCHED))
    print(f"[ok]   safe_print non-tty path raised debug -> info; wrote {path}")


if __name__ == "__main__":
    main()
