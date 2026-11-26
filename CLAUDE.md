# CLAUDE.md — working in this repo

## No silent errors (hard rule)

An exception that represents an **unexpected** condition must reach the journal at **err** level.
Never `except Exception: pass`, and never substitute a plausible-looking value on failure.

```python
import errlog                                   # services/lib/errlog.py
except Exception as e: errlog.skip("file.py: parsing X", e); continue   # per-record loops
except Exception as e: errlog.err("uploading Y", e); raise              # one-off failures
errlog.die("feed X is empty: nothing to publish")                       # fatal: err level, then exit 1
```

`errlog.die(reason)` is for a fatal condition this process cannot continue past. **Not**
`sys.exit(reason)`, and **not** `raise SystemExit(reason)` — CPython writes the same unprefixed
stderr bytes for both, and journald files an un-prefixed stderr line at `PRIORITY=6`, so the only
err-level record a dead unit leaves is systemd's cause-free "Failed to start ...". Neither is
caught by the sentence above: `SystemExit` derives from `BaseException`, not `Exception`. The
mechanism and a dated measurement are in `die()`'s docstring. (A hand-run CLI path, where nobody
queries a journal, may keep either.)

One case that is not a hand-run path: a **library** function whose callers catch its exit and read
its message keeps its message-carrying `raise SystemExit(reason)`. `tg.creds()` is such a function —
`docwatch.py` and `creds-check.py` both catch `BaseException` around `tg.send` *in order to* report
the reason, and `die()` would hand them `SystemExit: 1` instead. The entry point that lets such an
exit escape to systemd is where `die()` belongs.

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
