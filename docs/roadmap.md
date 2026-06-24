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

## Agent architecture (under discussion)
A specialised **training agent** (own context: goals, history, session-type playbooks) generates the
per-type chart + commentary on each new workout and handles training replies; a **steward/triage
agent** receives all Telegram inbound (`telegram_poll.py` inbox) and routes to training vs dev vs
other. The generic dev agent (Claude Code on claude-dev) stays separate. Pattern + persistence/
durability decision pending — see the session discussion.
