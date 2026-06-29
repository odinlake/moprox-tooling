#!/usr/bin/env python3
# Ensure /home/mikael is marked trusted in ~/.claude.json BEFORE a headless remote-control session
# launches. Claude shows an interactive "do you trust this folder?" dialog for any untrusted dir; in a
# headless `script` PTY nobody can answer it, so the session wedges ALIVE before reaching the relay and
# never appears in the app. The trust flag lives only in ~/.claude.json (no env/managed-settings knob),
# and it gets re-armed to false on version bumps and can be clobbered by other live sessions — so we
# re-assert it on every launch (run under flock; see moprox-dev@.service). Best-effort: never block
# startup if the config is momentarily unreadable.
import json, os, sys, tempfile, time

P = os.path.expanduser("~/.claude.json")
KEY = os.path.expanduser("~")  # "/home/mikael" — must match the project key exactly (no trailing slash)

d = None
for _ in range(5):
    try:
        with open(P) as f:
            d = json.load(f)
        break
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        time.sleep(0.3)
if d is None:
    sys.exit(0)  # don't gate startup on a transient read; worst case claude prompts

proj = d.setdefault("projects", {}).setdefault(KEY, {})
if proj.get("hasTrustDialogAccepted") is True:
    sys.exit(0)

proj["hasTrustDialogAccepted"] = True
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(P), prefix=".claude.json.", suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, P)
print(f"ensure-folder-trust: set hasTrustDialogAccepted=true for {KEY}", file=sys.stderr)
