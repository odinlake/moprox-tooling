# Conversation-thread archive + search

Every Claude Code session's transcript, kept durably and made searchable on demand — so when you
realise weeks later that you need something discussed in an old thread, an agent can find it. This is
**separate from the agent memory** ([`../memory/`](../memory/)): nothing here is ever loaded into a
session's startup context. It's a cold archive you query explicitly.

## Why it's needed
Claude Code stores transcripts as JSONL under `~/.claude/projects/<proj>/<session>.jsonl` but **prunes
them after `cleanupPeriodDays` (default 30)**. So "something I need later" is gone in a month unless we
copy it out. `archive.py` (hourly timer) flattens each session to a readable `.md` (user/assistant
turns + a one-line trace of tool actions — no thinking, no tool-output noise) in a durable dir.

## Privacy
Transcripts contain everything discussed, including secrets. The archive is **local only**
(`~/.local/share/moprox/threads/`, outside any git repo) — it is NEVER committed or pushed. Only the
code lives in this repo.

## Search
```bash
thread-search "halifax floor"             # AND of all terms, ranked by hits, with snippets + path
thread-search --since 2026-05-01 "totolo embeddings"
thread-search -n 5 "sudo ask rule"
```
Prints `title · date · project · #hits · path`; then `Read` the path for the full thread. Agents run
this via Bash when you ask "didn't we discuss X?". Keyword (ripgrep) for now — fast, no dependencies;
for semantic recall the `email-search` engine (Tantivy + MiniLM) can later index this same dir.

## Install (on claude-dev, as root)
```bash
cp services/threads/systemd/thread-archive.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now thread-archive.timer
# agent/operator-callable search wrapper:
ln -sf /home/mikael/projects/moprox-tooling/services/threads/thread-search /home/mikael/.local/bin/thread-search
```
Optionally raise retention so the *source* sticks around longer too (defence in depth):
`~/.claude/settings.json` → `"cleanupPeriodDays": 90`.

Run the archiver once by hand any time: `python3 services/threads/archive.py`.
