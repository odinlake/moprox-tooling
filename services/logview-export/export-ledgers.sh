#!/usr/bin/env bash
# Copy the estate's INVESTIGATION RECORD off the monitoring box into private-data.
#
# /var/lib/logview/{issues,acks}.jsonl is where every open issue, every ack verdict and every "we
# ruled this out" note lives — the whole reason nobody re-derives a finding. It exists in exactly
# one place, on a box that can be rebuilt, and nothing exported it. The ack ledger's own docstring
# calls itself "the investigation record"; a record with no copy is a draft.
#
# Append-only files, so a plain overwrite is safe: the remote copy is always a superset of ours.
# private-data-sync commits whatever lands here, so this script does no git of its own.
set -Eeuo pipefail
SRC="${SRC:-agent@logview.lan}"
KEY="${KEY:-$HOME/.ssh/claude-dev-ops}"
DEST="${DEST:-$HOME/projects/private-data/estate/logview}"
mkdir -p "$DEST"
for f in issues.jsonl acks.jsonl; do
  tmp="$DEST/.$f.tmp"
  if ssh -o BatchMode=yes -i "$KEY" "$SRC" "sudo -n cat /var/lib/logview/$f" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    # Never let a truncated read replace a good copy: these only ever grow.
    if [ -f "$DEST/$f" ] && [ "$(wc -c < "$tmp")" -lt "$(wc -c < "$DEST/$f")" ]; then
      echo "<3>logview-export: $f came back SHORTER than the local copy ($(wc -c < "$tmp") < $(wc -c < "$DEST/$f")) — keeping ours" >&2
      rm -f "$tmp"; continue
    fi
    mv "$tmp" "$DEST/$f"
    echo "$f: $(wc -l < "$DEST/$f") records"
  else
    rm -f "$tmp"
    echo "<3>logview-export: could not read $f from $SRC — the investigation record is NOT backed up today" >&2
    exit 1
  fi
done
