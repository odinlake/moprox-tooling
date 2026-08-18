# CLAUDE.md — working in this repo

## No silent errors (hard rule)

An exception that represents an **unexpected** condition must reach the journal at **err** level.
Never `except Exception: pass`, and never substitute a plausible-looking value on failure.

```python
import errlog                                   # services/lib/errlog.py
except Exception as e: errlog.skip("file.py: parsing X", e); continue   # per-record loops
except Exception as e: errlog.err("uploading Y", e); raise              # one-off failures
```

`errlog` prints a `<3>`/`<4>` syslog prefix, which journald turns into a real `PRIORITY` — so it
reaches the fleet sink and `search_logs(priority=3)` with no logging config. `skip()` aggregates and
reports **once at exit**, and escalates to err when *every* record was unusable.

**Why this is a rule and not a preference:** localnews' classifier scored ~1,200 posts as
significance 0 across 21 days because `except Exception:` turned "the CLI is missing" into a
plausible value. Unit green, nothing logged, data quietly wrong, three weeks lost. Scoring 0 on
failure is indistinguishable from a genuine "this is noise" — which is precisely what made it
invisible.

If a swallow really is benign (best-effort cleanup, a kill on a process already dead), say so in a
comment. Silence must be a decision, not a default.


## Deploying — and never `sudo git`

Production runs from `/opt/moprox-tooling`, a clone written **only** by `tooling-pull.service`,
which runs as `mikael`. **`tooling-pull.timer` already pulls every 5 minutes**, so a pushed commit
reaches production on its own. To deploy immediately: `sudo systemctl start tooling-pull.service`.

**Never run git as root there.** `sudo git pull` creates object fanout dirs owned by root, and the
next fetch that needs one of those 256 dirs dies with `insufficient permission for adding an
object` — typically hours or DAYS later, in someone else's session, looking unrelated. Three times
so far (2026-08-08, 08-11, 08-17), each a deploy freeze; the third froze production for 21 h and 10
deploys ran fine in between, which is why the author never connects cause to effect. The script now
detects and repairs it and shouts at err level, but repair is not permission.

This rule is in the repo on purpose. When wave 3 happened it was already a high-salience memory fact
AND in `tooling-pull.sh`'s header, and neither reached the session that did it: that session had
been running since three days before the fact was written, and nobody opens the deploy script when
all they want is their commit live. A rule in CLAUDE.md is read by whoever touches the repo, however
old their session — that is the only channel that does not depend on when the session started.
