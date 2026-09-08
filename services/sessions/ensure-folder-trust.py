#!/usr/bin/env python3
# Ensure this session's working directory is marked trusted in ~/.claude.json BEFORE a headless
# remote-control session launches. Claude shows an interactive "do you trust this folder?" dialog for any untrusted dir; in a
# headless `script` PTY nobody can answer it, so the session wedges ALIVE before reaching the relay and
# never appears in the app. The trust flag lives only in ~/.claude.json (no env/managed-settings knob),
# and it gets re-armed to false on version bumps and can be clobbered by other live sessions — so we
# re-assert it on every launch (run under flock; see moprox-dev@.service). Best-effort: never block
# startup if the config is momentarily unreadable.
#
# Trusts $HOME **and the current working directory**. They were the same thing while every instance
# ran from /home/mikael; coach runs in its own agent dir so its persona auto-loads, and an untrusted
# dir there would wedge that session at the trust dialog with no one able to answer it. Both are
# asserted every launch rather than one being chosen, because the cost is a dict lookup and the
# failure mode is invisible.
import json, os, sys, tempfile, time

P = os.path.expanduser("~/.claude.json")
# Project keys must match exactly: absolute, no trailing slash, symlinks resolved the way Claude
# Code sees them (it uses the cwd it was launched in).
KEYS = list(dict.fromkeys([os.path.expanduser("~"), os.getcwd()]))

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

changed = []
for key in KEYS:
    proj = d.setdefault("projects", {}).setdefault(key, {})
    if proj.get("hasTrustDialogAccepted") is not True:
        proj["hasTrustDialogAccepted"] = True
        changed.append(key)

if not changed:
    sys.exit(0)

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(P), prefix=".claude.json.", suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, P)
print("ensure-folder-trust: set hasTrustDialogAccepted=true for %s" % ", ".join(changed),
      file=sys.stderr)
