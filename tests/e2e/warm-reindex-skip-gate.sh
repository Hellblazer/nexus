#!/usr/bin/env bash
# nexus-29kr0: WARM-REINDEX SKIP GATE — the RDR-181 embed-skip regression gate.
#
# Why this exists (2026-07-22): a fresh index's flush drain IS the embed cost
# (oub13 profile: 53% of a 96-minute run). A warm reindex of unchanged content
# must bypass nearly all of it via the server-side embed-skip (same text ->
# same chash -> vector present -> metadata-only UPDATE, RDR-181). The skip
# breaking silently turns minutes into hours (the stevengharris report class),
# and nothing in the unit suite exercises the client-walk-gate + server-skip
# pair against a real engine. Probe history on the bead: a --force probe is
# WRONG for this (--force threads forceReEmbed by design and measures the
# escape hatch, not the skip).
#
# Three legs against one warm sandbox:
#   A  pure warm reindex (zero changes)  -> minutes wall, ZERO server embeds
#   B  one file perturbed (new trailing section) -> its unchanged sibling
#      chunks upload but SKIP server-side (event=combined_write_embed_partition
#      skipped>=1, embedded<=2)
#   C  FALSIFICATION (non-vacuity): --force (the designed forceReEmbed
#      escape, RDR-181 step 3 — NOT the env var, which loses to the batch
#      path's explicit kwarg) -> everything uploads, ZERO chunks skipped,
#      every partition line carries force_re_embed=true. Proves leg B's
#      skip signal is real, not spuriously always-present, and that the
#      detector distinguishes skip-on from skip-off.
#
# Self-provisioning (feedback_gates_scripted_not_ambient): builds the wheel
# under test, virgin HOME, scrubbed env, synthetic deterministic corpus.
# Honors FRESH_MVV_CACHE for the bge ONNX seed (same contract as
# fresh-install-mvv.sh).
#
# Usage: tests/e2e/warm-reindex-skip-gate.sh
# Exit 0 == WARM-REINDEX SKIP GATE PASSED (literal sentinel on the last line).
#
# ENGINE FLOOR (nexus-acvi7 2026-08-10; header refreshed 2026-08-30,
# nexus-29kr0 doc-rot finding): this gate's server-side evidence,
# event=combined_write_embed_partition, ships starting engine-service
# v0.1.70 (published but never pinned by any release — see the skip note in
# src/nexus/engine_version.py — so the capability first reached installs at
# v0.1.71). Every floor since then satisfies it; the gate
# has been reaching its PASSED sentinel since 2026-08-10 (first green:
# T2 [22178]). Step 3/8's MIN_ENGINE guard remains so a box pinned to a
# pre-capability engine fails fast with a distinguishable message rather
# than letting legs B/C misdiagnose "server-side skip not firing" on an
# engine that simply cannot report it (T2 nexus/warm-reindex-gate-coupling-
# verification). Do not remove step 3/8's guard to "get to green" — that
# would silently reintroduce the exact misdiagnosis it exists to prevent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d /tmp/warm-skip-gate-XXXXXX)"
HOME_DIR="$WORK/home"
VENV="$WORK/venv"
LOGS="$WORK/logs"
CORPUS="$WORK/corpus"
mkdir -p "$HOME_DIR" "$LOGS" "$CORPUS"

if [ -n "${FRESH_MVV_CACHE:-}" ] && [ -d "$FRESH_MVV_CACHE/nexus" ]; then
    mkdir -p "$HOME_DIR/.cache"
    cp -R "$FRESH_MVV_CACHE/nexus" "$HOME_DIR/.cache/nexus"
    echo "  (seeded model cache from $FRESH_MVV_CACHE)"
fi

GATE_OK=0
_fail() { echo "WARM-REINDEX SKIP GATE FAILED: $*" >&2; exit 1; }

cleanup() {
    _nx daemon service stop --with-pg >/dev/null 2>&1 || true
    if [ "$GATE_OK" = 1 ]; then
        rm -rf "$WORK"
    else
        echo "FAILURE EVIDENCE PRESERVED: $LOGS (home: $HOME_DIR)" >&2
    fi
}
trap cleanup EXIT

# Env allowlist — identical philosophy to fresh-install-mvv.sh: no ambient
# VOYAGE_API_KEY, no NX_* steering vars.
_nx() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$VENV/bin:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$VENV/bin/nx" "$@"
}
# Same allowlist, python instead of the nx CLI — used only by the
# engine-version guard (step 3/8) to read the live storage-service lease
# and probe /version. Must share HOME with `_nx` above: the lease file the
# guard reads is written under $HOME_DIR/.config/nexus by the `nx init`
# this same scrubbed HOME just ran.
_py() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$VENV/bin:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$VENV/bin/python" "$@"
}

ENGLOG="$HOME_DIR/.config/nexus/logs/storage_service_native.log"

# Engine-side skip evidence (nexus-acvi7 item 2 repoint, 2026-08-10): the
# ChunkBatcher flush path (indexer.py's `_batch_flush`, wired as
# `ChunkBatcher(flush=_batch_flush, ...)`) never reaches the OLD two-call
# upsert path (PgVectorRepository.java:527, `upsert_embed_skipped`) —
# `nx index repo` posts `chunks` on the combined-write manifest call, which
# CatalogHandler routes to CombinedWriteService.writeManyCombined
# (CatalogHandler.java's `rawChunks != null` branch), the ONLY engine path
# this gate's corpus actually exercises. That method emits ONE INFO line
# per call, UNCONDITIONALLY (unlike the retired line, which fired only
# when skipped>0):
#   event=combined_write_embed_partition collection=... deduped=... skipped=... embedded=... force_re_embed=...
# Confirmed emitted on the ChunkBatcher path: (a) statically —
# ChunkBatcher's `flush=_batch_flush` -> `_build_combined_write_payload` ->
# `cat.write_manifest_many(..., chunks=chunks_payload, ...)` ->
# `http_catalog_client.write_manifest_many` POSTs `chunks` to
# `/manifest/write_many` -> CatalogHandler's `rawChunks != null` branch ->
# `combinedWriteService.writeManyCombined(...)`, the sole call site of
# `writeManyCombined`; (b) empirically — this gate's own leg B/C runs, see
# the RED/GREEN falsification record in T2
# `nexus/acvi7-warm-reindex-gate-repointed`.
#
# One line is emitted PER CALL (i.e. per flush), not per chunk, so a
# leg with several flushes produces several lines — SUM the `skipped=`/
# `embedded=` fields across all matching lines since the marker, do NOT
# `grep -c` (line count conflates "one call, N skips" with "N calls, 1
# skip each", and would also undercount when the field name changes).
# Every grep/awk stage is || true — a healthy leg can legitimately match
# ZERO lines or ZERO count (leg A uploads nothing), and under set -e +
# pipefail a bare grep's exit-1-on-no-match would kill the whole gate
# before its assertion runs.
_partition_lines_since() {
    tail -n +"$(( $1 + 1 ))" "$ENGLOG" 2>/dev/null \
        | { grep "event=combined_write_embed_partition" || true; }
}
_partition_line_count_since() {
    _partition_lines_since "$1" | wc -l | tr -d ' '
}
_sum_field_since() {
    _partition_lines_since "$1" \
        | { grep -oE "$2=[0-9]+" || true; } | cut -d= -f2 \
        | awk '{s+=$1} END {print s+0}'
}
_skipped_since()   { _sum_field_since "$1" "skipped"; }
_embedded_since()  { _sum_field_since "$1" "embedded"; }
# Leg C non-vacuity: every matching line in the window must actually carry
# force_re_embed=true — otherwise a zero-skip result could just mean no
# partition lines fired at all, not that forceReEmbed disabled the skip.
_all_forced_since() {
    local total unforced
    total=$(_partition_lines_since "$1" | wc -l | tr -d ' ')
    [ "$total" -gt 0 ] || return 1
    unforced=$(_partition_lines_since "$1" | { grep -vc "force_re_embed=true" || true; })
    [ "$unforced" -eq 0 ]
}
# Chunks the client actually uploaded in one run: sum of "Flushing N staged
# chunks" lines in that run's client log. Under forceReEmbed (leg C) every
# staged chunk IS re-embedded server-side, so this doubles as the forced-
# re-embed count there.
_staged_in_log() {
    { grep -oE "Flushing [0-9]+ staged chunks" "$1" 2>/dev/null || true; } \
        | { grep -oE "[0-9]+" || true; } | awk '{s+=$1} END {print s+0}'
}
_marker() { wc -l < "$ENGLOG" 2>/dev/null || echo 0; }

echo "── 1/8 Build the wheel under test ──"
( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
    || _fail "wheel build failed (see $LOGS/build.log)"
WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
echo "  $WHEEL"

echo "── 2/8 Virgin venv + local init (engine + PG + bge-768) ──"
uv venv "$VENV" >"$LOGS/venv.log" 2>&1 || _fail "venv create failed"
uv pip install --python "$VENV/bin/python" "$WHEEL" >>"$LOGS/venv.log" 2>&1 \
    || _fail "wheel install failed (see $LOGS/venv.log)"
_nx init >"$LOGS/init.log" 2>&1 || { tail -20 "$LOGS/init.log" >&2; _fail "nx init failed"; }

echo "── 3/8 Engine-version guard (nexus-acvi7 item a) ──"
# event=combined_write_embed_partition — the ONLY server-side evidence legs
# B/C key on — was added by engine commit 2b0c5908 and ships starting
# v0.1.70. Deliberately NOT gated on REQUIRED_ENGINE_VERSION
# (src/nexus/engine_version.py): that floor legitimately stays at (0, 1, 69)
# until the paired client release bumps it (AGENTS.md § "Engine-service
# release" paired-release choreography), and a REQUIRED_ENGINE_VERSION-based
# check would then silently start PASSING a comparison that says nothing
# about whether THIS gate's event actually exists on the running engine.
# Probe the LIVE local engine `nx init` just started (its lease is
# discoverable the same way every T2/T3 HTTP client resolves it —
# nexus.db.service_endpoint.discover_lease, ServiceRegistry tier=
# "storage_service") rather than assume a version — an assumption is
# exactly the class of bug this whole bead exists to fix (nexus-b32rx: the
# tree/engine pairing confound). Hard-fail with a distinguishing message;
# a silent skip here would be success-shaped emptiness, the class this
# repo is actively removing.
ENGINE_CHECK="$(_py - <<'PY'
from nexus.db.service_endpoint import discover_lease
from nexus.engine_version import parse_engine_version

# This gate's floor, not REQUIRED_ENGINE_VERSION — see the shell comment
# immediately above this heredoc for why the two must not be conflated.
MIN_ENGINE = (0, 1, 70)
MIN_ENGINE_STR = "0.1.70"

base, _token = discover_lease()
if not base:
    print("NO_LEASE")
    raise SystemExit(0)

import httpx  # noqa: PLC0415 — local to the probe, mirrors the guard's own scope

try:
    resp = httpx.get(f"{base}/version", timeout=10)
    resp.raise_for_status()
    body = resp.json()
except Exception as exc:  # noqa: BLE001 — reported to the shell caller, not swallowed
    print(f"PROBE_FAILED {exc}")
    raise SystemExit(0)

raw = body.get("release_version")
parsed = parse_engine_version(raw)
if parsed is None:
    print(f"UNPARSEABLE {raw!r}")
elif parsed < MIN_ENGINE:
    print(f"BELOW_FLOOR {'.'.join(str(p) for p in parsed)} {MIN_ENGINE_STR}")
else:
    print(f"OK {'.'.join(str(p) for p in parsed)}")
PY
)"
ENGINE_STATUS="${ENGINE_CHECK%% *}"
ENGINE_REST="${ENGINE_CHECK#* }"
case "$ENGINE_STATUS" in
    OK)
        echo "  engine release_version=$ENGINE_REST (>= 0.1.70, guard satisfied)"
        ;;
    BELOW_FLOOR)
        ENGINE_ACTUAL="${ENGINE_REST%% *}"
        ENGINE_REQUIRED="${ENGINE_REST#* }"
        _fail "requires engine >= v$ENGINE_REQUIRED (event=combined_write_embed_partition added by 2b0c5908); this engine reports v$ENGINE_ACTUAL"
        ;;
    NO_LEASE)
        _fail "could not discover a live storage-service lease after nx init — cannot verify the engine version this gate requires (>= v0.1.70, event=combined_write_embed_partition added by 2b0c5908)"
        ;;
    PROBE_FAILED)
        _fail "GET /version failed against the local storage service: $ENGINE_REST"
        ;;
    UNPARSEABLE)
        _fail "local storage service /version returned an unparseable release_version: $ENGINE_REST"
        ;;
    *)
        _fail "engine-version guard produced an unrecognized result: $ENGINE_CHECK"
        ;;
esac

echo "── 4/8 Synthetic corpus (deterministic, git-tracked) ──"
# One 8-section file (the perturbation target: enough sibling chunks that
# skipped-vs-embedded is unambiguous) + 4 small files (client-walk-gate
# population). Content is fixed text — no timestamps, no randomness.
#
# CAP-COUPLING TRAP (nexus-acvi7 item c, T2 nexus/warm-reindex-gate-
# coupling-verification A5): target.md's 8 sections chunk 1:1 to 8 chunks
# cold, 9 in leg B after the appended Section 9 (empirically measured).
# The onnx-local per-collection chunk cap this gate always runs under —
# `_serving_embedding_mode()=="onnx-local"` forces
# `_ONNX_LOCAL_UPSERT_CHUNK_CAP`=16 for EVERY prefix, including docs__
# (src/nexus/db/http_vector_client.py:730-731) — is the TIGHTEST of the
# three caps, so headroom here is only 16 - 9 = 7 chunks.
# `ChunkBatcher.add` refuses (returns False, falls through to the legacy
# per-file `upsert_embed_skipped` path — NOT combined-write) the instant
# `len(ids) > cap` (src/nexus/chunk_batcher.py:253-260, strict `>`).
# Enlarging target.md by ~7 more sections is the single most natural way
# someone would try to "strengthen" this gate's discriminating power — and
# it would silently flip the file onto the legacy path, producing the
# SAME misleading "leg B produced NO skipped chunks" RED this bead exists
# to fix, for an unrelated reason. If the corpus ever needs to grow, grow
# it via MORE small satellite files (doc-a.md..doc-d.md below), never by
# adding sections to target.md past a combined per-file chunk count of 16.
for s in 1 2 3 4 5 6 7 8; do
    printf '## Section %s\n\nDeterministic corpus body for section %s. %s\n\n' \
        "$s" "$s" \
        "$(printf 'The quick brown fox jumps over the lazy dog. %.0s' 1 2 3 4 5 6 7 8)"
done > "$CORPUS/target.md"
for f in a b c d; do
    printf '# Doc %s\n\nStable satellite document %s for the walk-gate population.\n' \
        "$f" "$f" > "$CORPUS/doc-$f.md"
done
( cd "$CORPUS" && git init -q && git add -A && git -c user.email=gate@e2e -c user.name=gate commit -qm corpus )

echo "── 5/8 COLD index ──"
_nx index repo "$CORPUS" >"$LOGS/cold.log" 2>&1 || { tail -20 "$LOGS/cold.log" >&2; _fail "cold index failed"; }
COLD_STAGED=$(_staged_in_log "$LOGS/cold.log")
echo "  cold chunks staged: $COLD_STAGED"
[ "$COLD_STAGED" -ge 10 ] || _fail "cold index staged only $COLD_STAGED chunks — corpus too small to discriminate"

echo "── 6/8 Leg A: pure warm reindex (zero changes) ──"
M1=$(_marker)
START=$(date +%s)
_nx index repo "$CORPUS" >"$LOGS/warm-a.log" 2>&1 || { tail -20 "$LOGS/warm-a.log" >&2; _fail "leg A reindex failed"; }
WALL_A=$(( $(date +%s) - START ))
STAGED_A=$(_staged_in_log "$LOGS/warm-a.log"); EMB_A=$(_embedded_since "$M1")
echo "  wall=${WALL_A}s staged=$STAGED_A embedded=$EMB_A"
[ "$WALL_A" -le 300 ] || _fail "leg A took ${WALL_A}s — warm zero-change reindex must be minutes, not hours"
[ "$STAGED_A" -le 2 ] || _fail "leg A re-uploaded $STAGED_A chunks on UNCHANGED content — client walk gate broken"
[ "$EMB_A" -le 2 ] || _fail "leg A re-embedded $EMB_A chunks on UNCHANGED content"

echo "── 7/8 Leg B: one perturbed file — sibling chunks must SKIP server-side ──"
printf '## Section 9\n\nAppended perturbation section, leg B.\n' >> "$CORPUS/target.md"
M2=$(_marker)
_nx index repo "$CORPUS" >"$LOGS/warm-b.log" 2>&1 || { tail -20 "$LOGS/warm-b.log" >&2; _fail "leg B reindex failed"; }
SKIP_B=$(_skipped_since "$M2"); EMB_B=$(_embedded_since "$M2")
STAGED_B=$(_staged_in_log "$LOGS/warm-b.log")
PARTITION_LINES_B=$(_partition_line_count_since "$M2")
echo "  staged=$STAGED_B partition_lines=$PARTITION_LINES_B skipped=$SKIP_B embedded=$EMB_B"
[ "$STAGED_B" -ge 6 ] || _fail "leg B staged only $STAGED_B chunks — the perturbed file's siblings never uploaded, skip assertion would be vacuous"
# Non-vacuity precondition (nexus-acvi7 item b — the highest-value fix here):
# distinguish "no partition events fired at all" from "partition events
# fired but reported zero skips". These are DIFFERENT failures with
# DIFFERENT causes — an engine too old to emit the event (see step 3/8),
# or the corpus overflowing the chunk cap onto the legacy path (see the
# cap-coupling comment at step 4/8), both produce the FIRST message here,
# not "server-side skip not firing". Conflating them into one message is
# exactly what misdiagnosed this bead's own repointed gate on 2026-08-10
# (T2 nexus/warm-reindex-gate-coupling-verification B3).
[ "$PARTITION_LINES_B" -ge 1 ] || _fail "leg B: no event=combined_write_embed_partition lines emitted at all since the marker — either the engine is too old to emit them (see the step 3/8 engine-version guard) or the perturbed corpus overflowed the collection's onnx-local chunk cap (16) onto the legacy per-file upsert path, which never emits this event (see the cap-coupling comment at step 4/8)"
[ "$SKIP_B" -ge 1 ] || _fail "leg B: partition lines ARE present ($PARTITION_LINES_B) but skipped=0 summed across them — server-side skip not firing on uploaded unchanged chunks"
[ "$EMB_B" -le 2 ] || _fail "leg B embedded $EMB_B chunks — only the appended section (~1) should embed; siblings must skip"

echo "── 8/8 Leg C: FALSIFICATION — --force (skip off by design) must show the broken-skip signature ──"
# No perturbation needed: --force defeats the client walk gate (everything
# re-uploads) AND threads force_re_embed=True into every flush (indexer.py
# RDR-181 step 3), so the server existence-partition never runs. The env
# lever NX_UPSERT_SKIP_EXISTING=0 is NOT usable here: it only applies when
# the kwarg is unset, and the batch flush always passes it explicitly.
M3=$(_marker)
_nx index repo "$CORPUS" --force >"$LOGS/warm-c.log" 2>&1 || { tail -20 "$LOGS/warm-c.log" >&2; _fail "leg C reindex failed"; }
SKIP_C=$(_skipped_since "$M3"); STAGED_C=$(_staged_in_log "$LOGS/warm-c.log")
echo "  staged=$STAGED_C skipped=$SKIP_C"
[ "$STAGED_C" -ge 10 ] || _fail "leg C staged only $STAGED_C chunks — --force did not re-upload the corpus, falsification vacuous"
[ "$SKIP_C" -eq 0 ] || _fail "leg C (--force, skip off by design) still reported skipped chunks — the skip signal is not trustworthy, leg B is vacuous"
_all_forced_since "$M3" || _fail "leg C event=combined_write_embed_partition lines did not all carry force_re_embed=true — either no partition events fired (vacuous) or --force did not thread through to the combined write"

GATE_OK=1
echo "WARM-REINDEX SKIP GATE PASSED — cold=$COLD_STAGED legA=${WALL_A}s/${STAGED_A}up legB=${STAGED_B}up/${SKIP_B}skip/${EMB_B}emb legC=${STAGED_C}forced"
