# services/queue: the planner and dev work queues

Three queues, each an `owner` on the issue board (mo.lan/issues, logview):

| queue | who works it | when |
|---|---|---|
| `planner` | `planner` agent, best model (`queues.json`) | hourly check; runs only for unseen items, 3 a day at most; `urgent` jumps the cap |
| `dev` | `devq` agent on claude-dev (push, deploy, ssh) | every 30 min, one item per run |
| `operator` | Mikael | only the planner files here, with reason `external`, `decision` or `device`; logview re-routes anything else to the planner |

Flow: analyst, triage and the loop agents file to `planner` (or straight to `dev` when the file and
fix are named). The planner plans, merges, closes, or escalates. A dev item that fails twice or sits
5 days goes back to the planner. Agent instructions: `private-data/agents/{planner,devq}/CLAUDE.md`.

Model and effort are data: `queues.json`, or per host `~/.config/moprox/queues.json` (merged over
it, no commit needed). Empty string = the CLI default.

    queue_run.py planner|dev [--dry] [--force]
    python3 services/queue/tests/test_queue.py

Install: copy the four units to /etc/systemd/system/, `systemctl enable --now queue-planner.timer queue-dev.timer`.
State: `~/.local/state/queue-{planner,dev}.json`.
