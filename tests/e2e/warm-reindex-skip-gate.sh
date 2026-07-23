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
#      chunks upload but SKIP server-side (upsert_embed_skipped, embedded<=2)
#   C  FALSIFICATION (non-vacuity): --force (the designed forceReEmbed
#      escape, RDR-181 step 3 — NOT the env var, which loses to the batch
#      path's explicit kwarg) -> everything uploads, ZERO skip lines. Proves
#      leg B's skip-line signal is real, not spuriously always-present, and
#      that the detector distinguishes skip-on from skip-off.
#
# Self-provisioning (feedback_gates_scripted_not_ambient): builds the wheel
# under test, virgin HOME, scrubbed env, synthetic deterministic corpus.
# Honors FRESH_MVV_CACHE for the bge ONNX seed (same contract as
# fresh-install-mvv.sh).
#
# Usage: tests/e2e/warm-reindex-skip-gate.sh
# Exit 0 == WARM-REINDEX SKIP GATE PASSED (literal sentinel on the last line).
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

ENGLOG="$HOME_DIR/.config/nexus/logs/storage_service_native.log"

# Engine-side skip evidence: upsert_embed_skipped is the ONLY INFO-level
# vector-upsert event on the clean path (upsert_chunks_done is DEBUG;
# upsert_dedup_collapsed fires only when a batch has internal duplicates),
# so upload volume must come from the CLIENT log, not the engine log.
# Every grep stage is || true — a healthy leg can legitimately match ZERO
# lines (leg A uploads nothing), and under set -e + pipefail a bare grep's
# exit-1-on-no-match would kill the whole gate before its assertion runs.
_skip_lines_since() { tail -n +"$(( $1 + 1 ))" "$ENGLOG" 2>/dev/null | { grep -c "upsert_embed_skipped" || true; }; }
_embedded_since()   {
    tail -n +"$(( $1 + 1 ))" "$ENGLOG" 2>/dev/null \
        | { grep -oE "upsert_embed_skipped .* embedded=[0-9]+" || true; } \
        | { grep -oE "embedded=[0-9]+" || true; } | cut -d= -f2 \
        | awk '{s+=$1} END {print s+0}'
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

echo "── 1/7 Build the wheel under test ──"
( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
    || _fail "wheel build failed (see $LOGS/build.log)"
WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
echo "  $WHEEL"

echo "── 2/7 Virgin venv + local init (engine + PG + bge-768) ──"
uv venv "$VENV" >"$LOGS/venv.log" 2>&1 || _fail "venv create failed"
uv pip install --python "$VENV/bin/python" "$WHEEL" >>"$LOGS/venv.log" 2>&1 \
    || _fail "wheel install failed (see $LOGS/venv.log)"
_nx init >"$LOGS/init.log" 2>&1 || { tail -20 "$LOGS/init.log" >&2; _fail "nx init failed"; }

echo "── 3/7 Synthetic corpus (deterministic, git-tracked) ──"
# One 8-section file (the perturbation target: enough sibling chunks that
# skipped-vs-embedded is unambiguous) + 4 small files (client-walk-gate
# population). Content is fixed text — no timestamps, no randomness.
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

echo "── 4/7 COLD index ──"
_nx index repo "$CORPUS" >"$LOGS/cold.log" 2>&1 || { tail -20 "$LOGS/cold.log" >&2; _fail "cold index failed"; }
COLD_STAGED=$(_staged_in_log "$LOGS/cold.log")
echo "  cold chunks staged: $COLD_STAGED"
[ "$COLD_STAGED" -ge 10 ] || _fail "cold index staged only $COLD_STAGED chunks — corpus too small to discriminate"

echo "── 5/7 Leg A: pure warm reindex (zero changes) ──"
M1=$(_marker)
START=$(date +%s)
_nx index repo "$CORPUS" >"$LOGS/warm-a.log" 2>&1 || { tail -20 "$LOGS/warm-a.log" >&2; _fail "leg A reindex failed"; }
WALL_A=$(( $(date +%s) - START ))
STAGED_A=$(_staged_in_log "$LOGS/warm-a.log"); EMB_A=$(_embedded_since "$M1")
echo "  wall=${WALL_A}s staged=$STAGED_A embedded=$EMB_A"
[ "$WALL_A" -le 300 ] || _fail "leg A took ${WALL_A}s — warm zero-change reindex must be minutes, not hours"
[ "$STAGED_A" -le 2 ] || _fail "leg A re-uploaded $STAGED_A chunks on UNCHANGED content — client walk gate broken"
[ "$EMB_A" -le 2 ] || _fail "leg A re-embedded $EMB_A chunks on UNCHANGED content"

echo "── 6/7 Leg B: one perturbed file — sibling chunks must SKIP server-side ──"
printf '## Section 9\n\nAppended perturbation section, leg B.\n' >> "$CORPUS/target.md"
M2=$(_marker)
_nx index repo "$CORPUS" >"$LOGS/warm-b.log" 2>&1 || { tail -20 "$LOGS/warm-b.log" >&2; _fail "leg B reindex failed"; }
SKIP_B=$(_skip_lines_since "$M2"); EMB_B=$(_embedded_since "$M2")
STAGED_B=$(_staged_in_log "$LOGS/warm-b.log")
echo "  staged=$STAGED_B skip_lines=$SKIP_B embedded=$EMB_B"
[ "$STAGED_B" -ge 6 ] || _fail "leg B staged only $STAGED_B chunks — the perturbed file's siblings never uploaded, skip assertion would be vacuous"
[ "$SKIP_B" -ge 1 ] || _fail "leg B produced NO upsert_embed_skipped lines — server-side skip not firing on uploaded unchanged chunks"
[ "$EMB_B" -le 2 ] || _fail "leg B embedded $EMB_B chunks — only the appended section (~1) should embed; siblings must skip"

echo "── 7/7 Leg C: FALSIFICATION — --force (skip off by design) must show the broken-skip signature ──"
# No perturbation needed: --force defeats the client walk gate (everything
# re-uploads) AND threads force_re_embed=True into every flush (indexer.py
# RDR-181 step 3), so the server existence-partition never runs. The env
# lever NX_UPSERT_SKIP_EXISTING=0 is NOT usable here: it only applies when
# the kwarg is unset, and the batch flush always passes it explicitly.
M3=$(_marker)
_nx index repo "$CORPUS" --force >"$LOGS/warm-c.log" 2>&1 || { tail -20 "$LOGS/warm-c.log" >&2; _fail "leg C reindex failed"; }
SKIP_C=$(_skip_lines_since "$M3"); STAGED_C=$(_staged_in_log "$LOGS/warm-c.log")
echo "  staged=$STAGED_C skip_lines=$SKIP_C"
[ "$STAGED_C" -ge 10 ] || _fail "leg C staged only $STAGED_C chunks — --force did not re-upload the corpus, falsification vacuous"
[ "$SKIP_C" -eq 0 ] || _fail "leg C (--force, skip off by design) still logged skip lines — the skip-line signal is not trustworthy, leg B is vacuous"

GATE_OK=1
echo "WARM-REINDEX SKIP GATE PASSED — cold=$COLD_STAGED legA=${WALL_A}s/${STAGED_A}up legB=${STAGED_B}up/${SKIP_B}skip/${EMB_B}emb legC=${STAGED_C}forced"
