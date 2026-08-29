# private-data-sync — catch-all committer for `private-data`

`private-data` is a git repo written by many producers. Before this existed, only the mail lane ever
committed its own subtree, so everything else piled up as uncommitted work until a human noticed:

| Producer | Cadence | Commits its own subtree? |
|---|---|---|
| `email-search-categorize.timer` → `mail/` | nightly | yes |
| `backfill-msgid.py` → `mail/` | one-off | yes |
| `polar-fetch.timer` → `polar/incoming/` | every 5 min | no |
| `notif-ingest.timer` → `notifications/` (`ingest.py`) | every 15 min | yes, under the lock below |
| `notif-ingest.timer` → `finance/amex-notifs.json` (`amex.py`) | every 15 min | no |
| coach / valet agents → `agents/*/*-memory.md` | ad hoc | no |
| dev sessions → anywhere | ad hoc | only when asked |

This timer sweeps hourly: if the tree is dirty it stages everything, commits as
`sync: <lanes> (<n> file(s))`, rebases on `origin/main`, and pushes. Producers that already commit
with a meaningful message keep doing so and this never sees their work — it is a backstop, not the
primary path. Prefer adding a scoped commit to a new producer over relying on the sweep.

Guards: `flock` on `/run/lock/private-data-sync.lock` (falling back to `/tmp` when `/run/lock` is not
writable); skips a tree left
mid-rebase/merge; refuses to stage a file over 50 MB (GitHub rejects >100 MB) rather than wedge the
push. Everything lands in `~/.local/state/private-data-sync.log`; a clean tree logs nothing.

**Who actually holds that lock.** For a long time: nobody but this script, so it serialised the sweep
against other sweeps and against no producer at all. On 2026-08-29T08:01:01Z the sweeper and
`ingest.py` started in the same second, the sweeper's commit held `.git/index.lock`, ingest died
exit 128 on it, and its already-appended line then made the sweeper's `pull --rebase` refuse a dirty
tree — two unit-failed incidents from one collision. `ingest.py` now takes the same lock around its
git calls and treats contention as "skip, retry next run", so the pair is real. Any **new** producer
that commits into this tree must do the same; the lock name is derived from the repo basename in both
places so they agree. The mail-lane committers (`categorize-nightly.sh`, `backfill-msgid.py`) are not
in this repo and have **not** been checked — do not assume they hold it.

## Install

```sh
cp services/private-data/private-data-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now private-data-sync.timer
```
