#!/usr/bin/env python3
"""Idempotent patch for workspace-mcp's granular permission ladder.

workspace-mcp ships permission levels that are CUMULATIVE per service, so the
stock `gmail:drafts` level pulls in `organize` (gmail.modify, which can trash
mail) underneath it. We want "read broad, write narrow": draft-compose WITHOUT
modify/trash, and calendar event-write WITHOUT full calendar ACL control.

Two reorderings of auth/permissions.py achieve that against the installed copy:

  gmail:    readonly -> drafts(compose) -> organize(labels+modify) -> send -> full
            (drafts now sits BELOW organize, so `gmail:drafts` = readonly+compose only)
  calendar: readonly -> events(calendar.events) -> full(calendar)
            (new intermediate `events` level = create/edit events, no full calendar scope)

Run after any `pip install/upgrade workspace-mcp` (it rewrites the file). Safe to
re-run: it detects the patched shape and no-ops.

  /opt/mcp-google/venv/bin/python permissions-patch.py
"""
import sys
from pathlib import Path

TARGET_HINT = "/opt/mcp-google/venv/lib/python3.13/site-packages/auth/permissions.py"

GMAIL_STOCK = '''    "gmail": [
        ("readonly", [GMAIL_READONLY_SCOPE]),
        ("organize", [GMAIL_LABELS_SCOPE, GMAIL_MODIFY_SCOPE]),
        ("drafts", [GMAIL_COMPOSE_SCOPE]),
        ("send", [GMAIL_SEND_SCOPE]),
        ("full", [GMAIL_SETTINGS_BASIC_SCOPE]),
    ],'''
GMAIL_PATCHED = '''    "gmail": [
        ("readonly", [GMAIL_READONLY_SCOPE]),
        ("drafts", [GMAIL_COMPOSE_SCOPE]),
        ("organize", [GMAIL_LABELS_SCOPE, GMAIL_MODIFY_SCOPE]),
        ("send", [GMAIL_SEND_SCOPE]),
        ("full", [GMAIL_SETTINGS_BASIC_SCOPE]),
    ],'''

CAL_STOCK = '''    "calendar": [
        ("readonly", [CALENDAR_READONLY_SCOPE]),
        ("full", [CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE]),
    ],'''
CAL_PATCHED = '''    "calendar": [
        ("readonly", [CALENDAR_READONLY_SCOPE]),
        ("events", [CALENDAR_EVENTS_SCOPE]),
        ("full", [CALENDAR_SCOPE]),
    ],'''


def locate():
    p = Path(TARGET_HINT)
    if p.exists():
        return p
    # fall back to importing the installed module
    try:
        import auth.permissions as ap  # noqa
        return Path(ap.__file__)
    except Exception:
        sys.exit("could not locate auth/permissions.py — run with the venv python")


def main():
    path = locate()
    text = path.read_text()
    changed = False
    for stock, patched, label in ((GMAIL_STOCK, GMAIL_PATCHED, "gmail"),
                                  (CAL_STOCK, CAL_PATCHED, "calendar")):
        if patched in text:
            print(f"[skip] {label}: already patched")
        elif stock in text:
            text = text.replace(stock, patched)
            changed = True
            print(f"[ok]   {label}: reordered")
        else:
            print(f"[WARN] {label}: neither stock nor patched block found "
                  f"(package layout changed?) — review {path}")
    if changed:
        path.write_text(text)
        print(f"wrote {path}")
    else:
        print("no changes needed")


if __name__ == "__main__":
    main()
