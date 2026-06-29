# Shared agent memory (moprox dev one/two/three)

The three [`moprox dev`](../sessions/) sessions share **one** memory dir
(`~/.claude/projects/-home-mikael/memory/`). This gives them shared context (so you don't re-explain
what another built) while letting you work three projects in parallel. The design follows current
(2026) agent-memory practice — OS-style tiering, a scoped blackboard, background reconciliation, and
provenance — adapted to Claude Code's file memory and kept deliberately simple (no vector DB, no
external service, no per-write LLM pass).

## How it works
- **Agents write fact files normally + a journal line** (see [`PROTOCOL.md`](./PROTOCOL.md)). Fact
  files rarely collide (one slug each); the always-loaded `MEMORY.md` index is *not* hand-edited.
- **`reconcile.py`** (timer, every 10 min, deterministic, no tokens) rebuilds `MEMORY.md`
  authoritatively from the fact files — which also **recovers from any index write-race** — and does
  the housekeeping: provenance-preserving merge, supersession, TTL expiry → `archive/`, conflict
  detection → `CONFLICTS.md`, and a `CHANGES.md` activity feed.
- **`steward.sh`** (timer, daily, bounded `claude -p`, self-skips when idle) does the *semantic* work
  the script can't: merge flagged conflicts, roll up related facts, demote stale, write `DIGEST.md`.

## The four design choices worth knowing (from the 2026 best-practice review)
1. **Bound the HOT set, not total memory.** `MEMORY.md` is capped (`MEMORY_BUDGET`, ~9 KB); overflow
   is evicted to a "Cold" pointer (the fact file stays, searchable). Total memory grows freely.
2. **Stable hot set, not just small.** The index is ordered alpha within scope, and membership uses
   **hysteresis** (already-hot facts get a keep-bonus) — so it changes only on real change, protecting
   prompt-caching and attention. We optimise for *quiet diffs*, not just size.
3. **Provenance never collapses.** `agents:` lists only grow; merges keep every origin. The index
   shows who learned each fact (`①②③`).
4. **Reconcile in the background, never silently delete.** Conflicts are flagged for the steward, not
   auto-resolved; superseded/expired facts are *archived*, not deleted. (The field's trend — e.g.
   Mem0 v3 dropping its per-write LLM pass — is away from silent rewrites; this matches it.)

## Files (in the memory dir, not this repo)
`*.md` facts · `MEMORY.md` (built index) · `journals/<id>.jsonl` (append-only, per agent) ·
`CHANGES.md` (active-read feed) · `CONFLICTS.md` (steward queue) · `DIGEST.md` (periodic summary) ·
`archive/` (superseded/expired/evicted, kept) · `.reconcile-state.json` (hysteresis state).

## Scope & semantic recall
Facts are scoped `global` or `project:<name>`; the single shared index sections by scope so every
session sees the same headlines while details stay cold. For meaning-based recall of cold/archived
facts, point the `email-search` engine (Tantivy + MiniLM) at the memory dir — same infra, no new
dependency (future enhancement).

## Install (on claude-dev, as root)
```bash
cp services/memory/systemd/agent-memory-*.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agent-memory-reconcile.timer agent-memory-steward.timer
```
Test deterministically without touching the live store: `MEMORY_DIR=/tmp/copy reconcile.py --dry-run`.
