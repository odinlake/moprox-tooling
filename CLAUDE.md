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
