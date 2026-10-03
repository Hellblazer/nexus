#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: record-run.sh OUT_DIR NAME
#
# What a batch needs to be traced to one tree and one set of bytes (nexus-ger02.7): the checkout's HEAD sha, in
# NAME.head, and the sha256 of the transcript, in NAME.sha256 as "<hex>  NAME.jsonl". run-scenario.sh calls it
# after every session, so every runner records both. Fails when the transcript is missing: a run that left no
# transcript has nothing to record, and saying so beats an empty hash.
set -u
OUT="${1:?out dir}"
NAME="${2:?name}"
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
[ -f "$OUT/$NAME.jsonl" ] || { echo "record-run.sh: no transcript $OUT/$NAME.jsonl" >&2; exit 1; }
git -C "$WT" rev-parse HEAD > "$OUT/$NAME.head" || exit 1
python3 -c 'import hashlib, sys
digest = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
sys.stdout.write(digest + "  " + sys.argv[2] + "\n")' "$OUT/$NAME.jsonl" "$NAME.jsonl" > "$OUT/$NAME.sha256" || exit 1
