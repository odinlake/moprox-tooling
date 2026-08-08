# loop — the harness for continuous agents

**The model proposes; the harness disposes.** An agent emits a claim *and a machine-checkable
verifier*; this code runs the verifier and keeps the claim only if it holds.

```sh
python3 harness.py analyst --dry-run     # exercise everything except the model call
systemctl enable --now loop@analyst.timer
touch ~/.local/share/moprox/loop/analyst/STOP    # kill switch, effective next wake
```

## Why a timer and not `while True:`

One cycle per wake, then exit. That inherits journald, restart backoff, `Persistent=true` and
`systemctl stop` as a kill switch. A hot loop would reimplement all four, worse — and each wake
being a fresh bounded call is also what stops context rot.

## Failure mode → mechanism

| failure mode | mechanism |
|---|---|
| grades its own homework | model emits `verify`; **the harness runs it**, claim survives only if output contains `expect` |
| drifts, re-derives | durable ledger, `novelty_key`s seeded into every prompt |
| context rot | fresh bounded call per wake |
| busywork / spinning | 3 dry cycles → back off |
| runaway cost | budget gate before the call, from the real `agent-usage.jsonl` |
| unsafe outward action | `DENY` patterns on the verifier + propose-don't-dispose |
| quota bursts, unattributable interleaving | `flock` — loop agents are mutually exclusive |

## The sharp edge

The harness executes **model-authored shell commands**. That is inherent to having an external
objective function, and it is the reason this belongs on the contained loop guest and **not on
claude-dev**, which holds data credentials. Defence is layered and none of it is a sandbox:
containment (Squid egress, no data creds, no SSH out), `DENY` for the classes git cannot undo, a
timeout, and an output cap.

## Two things testing changed

- **Budget is per-agent.** The first cut summed the whole estate, so a busy interactive day (2.97M
  tokens, none of them the loop's) starved an agent that had spent nothing. A budget must meter the
  thing it restrains.
- **`PATH` is set explicitly in the unit.** `~/.local/bin` is not on systemd's default PATH and the
  claude CLI lives there. Omitting it is precisely what scored 1,200 local-news posts as 0.

## Not done yet

D2 concurrency beyond `flock`, D3 the analyst's real objective (`open_incidents()` is the obvious
first one), D4 the burndown, and the adversarial second pass — currently the verifier is the only
check, so a claim whose verifier is *technically* satisfiable but vacuous still passes.
