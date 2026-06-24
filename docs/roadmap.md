# Roadmap / deferred design

## Webhook breadcrumb relay (deferred)
Instead of `claude-dev` polling external APIs (Polar AccessLink especially) on a timer, stand up a
tiny **public serverless endpoint** (Cloudflare Workers — free tier, KV store, custom subdomain via
`odinlake.net`) that:
- receives webhooks from Polar (AccessLink natively supports new-exercise webhooks) and optionally
  Telegram, and stores a minimal **breadcrumb** (event id / timestamp) in KV;
- exposes a cheap `GET` that `claude-dev` polls frequently (no external rate limits, immune to Polar
  tightening theirs) and only triggers the real (rate-limited) data fetch when a breadcrumb appears.

Why: `claude-dev` stays inbound-isolated (only the worker is public), the relay only ever sees
"an event happened" (not HR data — privacy holds), and the heavy fetch happens only on real events.
Add the worker host to the Squid allowlist. ~30 lines of worker code + rework the Polar fetcher to
poll the breadcrumb. **Needs:** a Cloudflare account / API token.

## Agent architecture (built)
A specialised **coach agent** generates the per-type chart + commentary on each new workout
(`services/forward/polar_fetch.py`) and handles training replies; a **steward** triages all Telegram
inbound and routes to coach / dev / chat; the generic **dev agent** stays separate. The poller
(`telegram_poll.py`) only captures to the inbox; the **dispatcher** (`dispatcher.py`) does triage +
routing with a per-agent single-flight queue (an agent is never re-invoked while still running; new
messages queue). All agents run on the Max plan via `services/agents/run.py`; all outbound goes
through `tg.py`, which tags each message with the agent's `#handle`. Contexts live in
`private-data/agents/{coach,steward,dev}`.

## Context durability across a VM rebuild (future work)
If `claude-dev` is rebuilt, agent **memory** (`~/.claude/.../memory/`) and the relocated secrets
(`~/.config/claude-dev/*.env`: Polar + Telegram tokens) are lost — the agent *contexts* survive
because they're in `private-data` (git), but accumulated memory and creds don't. Plan: back these up
into `private-data` (encrypted for the secrets) and restore them from cloud-init on rebuild. Deferred
by operator decision (2026-06-24) — not a concern right now.
