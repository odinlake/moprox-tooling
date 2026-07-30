# private-data-sync — catch-all committer for `private-data`

`private-data` is a git repo written by many producers. Before this existed, only the mail lane ever
committed its own subtree, so everything else piled up as uncommitted work until a human noticed:

| Producer | Cadence | Commits its own subtree? |
|---|---|---|
| `email-search-categorize.timer` → `mail/` | nightly | yes |
| `backfill-msgid.py` → `mail/` | one-off | yes |
| `polar-fetch.timer` → `polar/incoming/` | every 5 min | no |
| `notif-ingest.timer` → `finance/amex-notifs.json` | every 15 min | no |
| coach / valet agents → `agents/*/*-memory.md` | ad hoc | no |
| dev sessions → anywhere | ad hoc | only when asked |

This timer sweeps hourly: if the tree is dirty it stages everything, commits as
`sync: <lanes> (<n> file(s))`, rebases on `origin/main`, and pushes. Producers that already commit
with a meaningful message keep doing so and this never sees their work — it is a backstop, not the
primary path. Prefer adding a scoped commit to a new producer over relying on the sweep.

Guards: `flock` against the mail lane and any dev session in the same tree; skips a tree left
mid-rebase/merge; refuses to stage a file over 50 MB (GitHub rejects >100 MB) rather than wedge the
push. Everything lands in `~/.local/state/private-data-sync.log`; a clean tree logs nothing.

## Install

```sh
cp services/private-data/private-data-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now private-data-sync.timer
```
