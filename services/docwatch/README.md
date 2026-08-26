# docwatch — file and index documents dropped in Drive root

**LIVE since 2026-08-26.** Every 10 minutes, anything newly parented in Drive **root** gets read,
indexed, and filed as a renamed copy into the right folder — then a Telegram digest says what was
done and what the document turned out to be.

Root is the operator's inbox: scans and downloads land there and get dealt with later. The 2026-07
reorg sorted ~3k of them in one batch (`~/docindex`). This is the incremental half — the batch pass
is what happened once, this is what happens from now on.

## Install
```bash
services/docwatch/install.sh          # on claude-dev; idempotent, uses sudo for the units
```
The **first run files nothing**: it records a Drive `startPageToken` as the baseline, because
everything already sitting in root predates the watcher and belongs to the batch pass.

## What happens to a new file
1. **Extract** — via `~/docindex/pipeline.py`'s extractors (pdftotext, docx/xlsx/pptx, Google
   export, whisper for audio, and the file itself handed to `claude -p` for scans and images).
2. **Decide** — ONE `claude -p` call returns both the index fields (summary, tags, entities,
   doc_date, category) and the filing decision (proposed name, destination folder, confidence).
   One call rather than three because they read the same document, and two calls disagreeing about
   what it is would be worse than either answer alone.
3. **File** — a server-side `files.copy` puts the document, under its proposed name, in that folder.
4. **Index** — upserted into `docindex.db`, so `mo.lan/docs` finds it. The embedding and FTS row are
   left to the next pipeline run rather than half-duplicated here.
5. **Notify** — one Telegram digest per run, `#docwatch`, listing each file as
   `old name → new name → folder` with its summary, and flagging anything filed at less than high
   confidence.

## The naming convention is not defined here
It is **quoted verbatim** from `~/docindex/rename2.py`, which was tuned against operator corrections
over 3k files (`<date>_<subject>_<doctype-and-descriptors>.<ext>`, and the rule that dropping a
meaning-bearing token — "passport photo" → "passport" — is the worst error). Editing the copy in
`docwatch.py` without editing that one silently forks the archive into two conventions. If the
convention changes, change it in both, in the same commit.

The destination list is read **fresh from Drive on every run**, two levels under root — the operator
adds folders by hand and a cached taxonomy would file into a tree that no longer exists. Nothing
outside that list can be chosen, and `00 Inbox/_needs-review` is the escape hatch for "not sure".

## What it deliberately does NOT do

**It does not touch the original.** That is the workflow, not a shortfall: you read the digest,
check the filed copy, and delete the original from root yourself — so the digest links to *both* the
copy and the original, one tap each. Deleting an original after filing is safe, because dedup keys
on the source `drive_id` recorded in `filed.jsonl`, which outlives the file.

(For the record, so nobody re-tests it: the service account could not rename the original in any
case — measured 2026-08-26, the full `drive` scope returns `unauthorized_client`, and `drive.file`
covers only files the app itself created. Noted as a fact about the credential, **not** as a gap to
close. Nothing here wants that scope.)

**It does not file more than 25 files in one run.** A run that suddenly wants to reorganise hundreds
of files is a restore, a sync loop, or a bad page token — not a busy day. Over the cap it files
nothing and raises at err level. Raise `DOCWATCH_MAX_PER_RUN` deliberately for a genuine bulk import.

**It does not move or delete anything.** Every action is additive: a copy, an index row, a log line.
Nothing the operator dropped is touched, so a bad classification costs a stray copy, not a lost
document.

## Operating it
```bash
systemctl list-timers docwatch.timer
journalctl -u docwatch.service -n 50
cat ~/.local/state/docwatch/filed.jsonl        # one line per filed document, append-only
```
`filed.jsonl` is also the dedup key: a `drive_id` already in it is never filed twice, so the log can
be trimmed but not reordered, and deleting it would re-file everything the page token still covers.

**Dependency note:** the unit runs under `~/docindex/venv` (that is where the Google client and the
extractors live). `tg.py` needs `telegramify-markdown` in *that* interpreter — installed
2026-08-26. Without it the run still files correctly and only the notification is lost, at err
level; that is how the first live test failed.
