#!/usr/bin/env python3
"""Ingest HA-captured notifications into private-data (month-partitioned, redacted, deduped).

Source: ~/ha-notif/notif.jsonl (Syncthing receive-only mirror of HA /config/notif).
Dest:   private-data/notifications/notif-YYYY-MM.jsonl
Second-pass redaction mirrors the HA automation's deny-list (belt-and-braces).
Dedupe key: (app, post_time). Commits to private-data when new lines land.
"""
import json, re, subprocess, sys
from pathlib import Path

SRC = Path.home() / "ha-notif/notif.jsonl"
DEST = Path.home() / "projects/private-data/notifications"

DENY_PKGS = {
    "io.homeassistant.companion.android",
    "com.google.android.apps.authenticator2", "com.authy.authy",
    "com.beemdevelopment.aegis", "com.azure.authenticator",
    "com.x8bit.bitwarden", "com.bitwarden.authenticator",
    "com.google.android.apps.messaging", "com.android.messaging",
}
OTP = re.compile(
    r"(one[- ]?time|verification|security|login|access)\s*code|\botp\b"
    r"|(code|passcode)\D{0,15}\d{3,8}|\d{3,8}\D{0,15}(code|passcode)|password", re.I)


def seen_keys():
    keys = set()
    for f in DEST.glob("notif-*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                keys.add((e.get("app"), e.get("post_time")))
    return keys


def main():
    if not SRC.exists():
        sys.exit(0)
    seen = seen_keys()
    added, dropped = 0, 0
    for line in SRC.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        key = (e.get("app"), e.get("post_time"))
        if key in seen:
            continue
        seen.add(key)
        body = f"{e.get('title') or ''} {e.get('text') or ''}"
        if e.get("app") in DENY_PKGS or OTP.search(body):
            dropped += 1
            continue
        month = (e.get("ts") or "")[:7] or "unknown"
        with open(DEST / f"notif-{month}.jsonl", "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        added += 1
    if added:
        subprocess.run(["git", "-C", str(DEST.parent), "add", "notifications"], check=True)
        subprocess.run(["git", "-C", str(DEST.parent), "commit", "-q", "-m",
                        f"notifications: ingest {added} new"], check=True)
    print(f"added={added} dropped={dropped}")


if __name__ == "__main__":
    main()
