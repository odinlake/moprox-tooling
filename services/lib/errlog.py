"""errlog — make swallowed exceptions impossible to miss.

THE FAILURE THIS EXISTS TO PREVENT
  localnews' classifier scored ~1,200 posts as significance 0 across 21 days because a
  `except Exception: a = {...significance: 0...}` turned "the CLI is missing" into a plausible
  value. Nothing logged, unit green, data quietly wrong. The cost was three weeks.

  The same shape is everywhere a per-record loop does `except Exception: continue`. Skipping one
  corrupt line is correct; skipping ALL of them silently is indistinguishable from an empty file.

WHY `<3>` PREFIXES AND NOT logging.basicConfig
  Every guest ships journald to the fleet sink, and journald reads a `<N>` syslog-level prefix off
  stdout/stderr. So a bare print with `<3>` becomes a genuine PRIORITY=3 record — findable by
  `search_logs(priority=3)` across the whole estate, with no logging config, no handlers, and no
  dependency. Anything heavier would have to be configured per service to get the same result.

USE
    from errlog import err, Skips

    skips = Skips("parsing exercise json")          # per loop, per file
    for line in f:
        try: rows.append(json.loads(line))
        except Exception as exc: skips.add(exc); continue
    skips.report(total=len(lines))                  # err-level IF anything was skipped

    err("upload failed", exc)                       # one-off unexpected error
"""
import atexit
import os
import sys
import traceback

_TAG = os.path.basename(sys.argv[0]) or "python"


def _emit(level, msg):
    # <3>=err <4>=warning. journald strips the prefix and sets PRIORITY accordingly; a terminal
    # shows it verbatim, which is ugly but honest and only happens when run by hand.
    print(f"<{level}>{_TAG}: {msg}", file=sys.stderr, flush=True)


def err(context, exc=None, *, trace=False):
    """An unexpected error. ALWAYS reaches the journal at err level."""
    detail = f": {type(exc).__name__}: {exc}" if exc is not None else ""
    _emit(3, f"{context}{detail}")
    if trace and exc is not None:
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for sub in line.rstrip().splitlines():
                _emit(3, "  " + sub)


def warn(context, exc=None):
    """Expected-but-notable. Visible, but does not imply something is broken."""
    detail = f": {type(exc).__name__}: {exc}" if exc is not None else ""
    _emit(4, f"{context}{detail}")


class Skips:
    """Counts records skipped in a loop and reports ONCE at the end.

    Per-record logging would drown a big file; reporting nothing is how corruption hides. So:
    aggregate, then speak up if anything was dropped — and shout if EVERYTHING was, because that
    is a broken input, not a stray bad line."""

    def __init__(self, context):
        self.context = context
        self.n = 0
        self.first = None

    def add(self, exc=None):
        self.n += 1
        if self.first is None and exc is not None:
            self.first = exc

    def report(self, total=None):
        if not self.n:
            return False
        d = f" of {total}" if total else ""
        first = f" (first: {type(self.first).__name__}: {self.first})" if self.first else ""
        if total and self.n >= total:
            err(f"{self.context}: ALL {self.n}{d} records unusable — treat the input as broken{first}")
        else:
            warn(f"{self.context}: skipped {self.n}{d} unusable record(s){first}")
        return True


# ---- one-line call sites -----------------------------------------------------------------------
# A per-loop Skips object is the tidiest API but needs a variable threaded through the loop, which
# makes retrofitting ~20 existing sites risky. skip() keys an aggregator by context string instead,
# so a swallow becomes a ONE-LINE change and the summary still arrives once, at exit.
_registry = {}


def skip(context, exc=None):
    """Record a skipped record. Reported once at process exit, never per-record."""
    s = _registry.get(context)
    if s is None:
        s = _registry[context] = Skips(context)
    s.add(exc)


@atexit.register
def _report_all():
    for s in _registry.values():
        s.report()
