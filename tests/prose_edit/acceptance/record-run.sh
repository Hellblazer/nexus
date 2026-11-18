#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: record-run.sh OUT_DIR NAME
#
# What a batch needs to be traced to one tree and one set of bytes (nexus-ger02.7): the HEAD sha of the tree the
# runner sits in, in NAME.head, and the sha256 of the transcript, in NAME.sha256 as "<hex>  NAME.jsonl".
# run-scenario.sh calls it after every session, so every runner records both. Fails when the transcript is
# missing: a run that left no transcript has nothing to record, and saying so beats an empty hash.
#
# Also NAME.model (the session's model, from the stream-json init event; "unknown" when the transcript has none)
# and NAME.status (`git status --porcelain` of the tree when the run ended). HEAD alone hides a dirty tree, and the
# model is part of the evidence: a haiku orchestrator and an opus editor are a different claim than one model.
# And NAME.app: `standin` when the session ran against the stand-in MarkupEditor server (NAME.mcp.json exists, which
# run-scenario.sh writes only then), else `none`. A stand-in run is a harness measurement, not a run in the app, and the
# verdict scripts say so in their output (nexus-scmcs.3).
# The status lists the runner's own untracked scenario copies while it runs, so it is read, not scored.
set -u
OUT="${1:?out dir}"
NAME="${2:?name}"
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
[ -f "$OUT/$NAME.jsonl" ] || { echo "record-run.sh: no transcript $OUT/$NAME.jsonl" >&2; exit 1; }
git -C "$WT" rev-parse HEAD > "$OUT/$NAME.head" || exit 1
python3 -c 'import hashlib, sys
digest = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
sys.stdout.write(digest + "  " + sys.argv[2] + "\n")' "$OUT/$NAME.jsonl" "$NAME.jsonl" > "$OUT/$NAME.sha256" || exit 1
git -C "$WT" status --porcelain > "$OUT/$NAME.status" || exit 1
python3 -c 'import json, sys
model = "unknown"
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init" and event.get("model"):
        model = str(event["model"])
        break
sys.stdout.write(model + "\n")' "$OUT/$NAME.jsonl" > "$OUT/$NAME.model" || exit 1
if [ -f "$OUT/$NAME.mcp.json" ]; then echo standin > "$OUT/$NAME.app"; else echo none > "$OUT/$NAME.app"; fi
