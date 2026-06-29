# Shared-memory protocol (moprox dev one/two/three)

The three `moprox dev` sessions share ONE memory dir. You are agent **`$AGENT_ID`** (`one`/`two`/`three`).
Follow this so the deterministic reconciler (`services/memory/reconcile.py`, runs every 10 min) and the
steward can keep the shared memory clean, provenance-tagged, and bounded.

## When you save a durable fact (as usual), also:
1. **Tag the file's frontmatter** under `metadata:` with:
   - `agents: [$AGENT_ID]` — provenance (the reconciler grows this list; never remove names).
   - `scope: global` for cross-cutting facts (user/feedback/how-the-estate-works), or
     `scope: project:<name>` for facts specific to one project (e.g. `project:finance`).
   - `salience: high|normal|low` — `high` = belongs in every session's hot index; `low` = niche.
   - optional `supersedes: <slug>` when this fact replaces an older one (the old one is archived).
   - optional `expires: YYYY-MM-DD` for time-bound facts (auto-archived after).
2. **Append one line to `journals/$AGENT_ID.jsonl`** (this is the audit trail + cross-pollination feed):
   `{"ts":"<today>","agent":"$AGENT_ID","slug":"<fact-slug>","action":"add|update|supersede","scope":"...","salience":"...","note":"<5-8 word what/why>"}`

Do **not** hand-edit `MEMORY.md`, `CHANGES.md`, `CONFLICTS.md`, or `.reconcile-state.json` — the reconciler
owns those. Just drop fact files + journal lines; the index is rebuilt from them (which also recovers
from any concurrent-write race).

## At the start of each task (the "active read"):
- Read **`CHANGES.md`** — what the other two sessions learned/changed recently. If something there
  affects your task, read that fact file before proceeding (don't make the operator re-explain it).
- Track your own cursor: remember the latest `ts` you've already absorbed; only re-read newer lines.

## Conflicts
If you and another session both `update` the same fact within ~2 days, the reconciler last-write-wins it
and lists it in `CONFLICTS.md` for the **steward** to merge — so prefer `supersedes`/new slugs over
silently overwriting a shared fact, and if you knowingly change another agent's fact, say why in the note.

## What to write (unchanged from normal memory hygiene)
Durable, cross-session, non-derivable facts only — not what the repo/git/CLAUDE.md already records, not
conversation-only detail. Keep facts fact-shaped ("X is at Y, decided Z because W"), not narrative —
provenance tells the others *that* you built something; the code/commits carry *how*.
