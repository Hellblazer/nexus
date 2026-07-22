#!/usr/bin/env bash
# nexus-nolqs: FRESH-INSTALL MVV — the virgin-journey release gate.
#
# Why this exists (release-process root cause, 2026-07-21): every other E2E
# release gate (rehearsal, era-hop, guided MVV, package-upgrade convergence)
# starts from a POPULATED install and tests the UPGRADE axis; the unit suite
# pins the SQLite opt-out backend; and the fresh-box bugs were silent
# best-effort skips — so an entire defect class (f1itv/e9ru2/kmo9h/r5f3c/
# 9xfx5: lost catalog registrations, divergent local substrates, voyage-env
# mode flips, vacuous pending rungs) shipped through a heavyweight release
# process untouched. This gate is the missing axis: PROVE the full data
# journey on a virgin box with the wheel under test.
#
# Journey: build wheel -> isolated venv + scrubbed env + virgin HOME ->
# NX_LOCAL init (engine download + portable PG + bge-768) -> ladder converged
# at init -> store put (catalog row asserted, manifest warning absent) ->
# index md (catalog row asserted) -> semantic search returns both -> doctor
# has zero ✗ and zero non-allowlisted warnings.
#
# Self-provisioning (feedback_gates_scripted_not_ambient): everything it
# needs it builds; nothing ambient is trusted. Env is allowlist-scrubbed —
# an exported VOYAGE_API_KEY must NOT reach the journey (the r5f3c lesson).
#
# Cost: ~2-4 min warm (engine binary + PG bundle + bge ONNX are cached under
# the sandbox only for the run; cold downloads dominate the first run).
#
# Usage: tests/e2e/fresh-install-mvv.sh
# Exit 0 == FRESH-INSTALL MVV PASSED (the literal sentinel on the last line).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d /tmp/fresh-mvv-XXXXXX)"
HOME_DIR="$WORK/home"
VENV="$WORK/venv"
LOGS="$WORK/logs"
mkdir -p "$HOME_DIR" "$LOGS"

# Optional download cache (CI cost discipline): seed the virgin HOME's
# ~/.cache/nexus (the 416MB bge ONNX) from FRESH_MVV_CACHE and save it back
# on success. The engine binary + PG bundle are still downloaded + verified
# fresh every run (that verification IS part of the journey under test).
if [ -n "${FRESH_MVV_CACHE:-}" ] && [ -d "$FRESH_MVV_CACHE/nexus" ]; then
    mkdir -p "$HOME_DIR/.cache"
    cp -R "$FRESH_MVV_CACHE/nexus" "$HOME_DIR/.cache/nexus"
    echo "  (seeded model cache from $FRESH_MVV_CACHE)"
fi

GATE_OK=0
_fail() { echo "FRESH-INSTALL MVV FAILED: $*" >&2; exit 1; }

cleanup() {
    # Stop the sandbox service + PG so nothing leaks past the gate.
    _nx daemon service stop --with-pg >/dev/null 2>&1 || true
    if [ "$GATE_OK" = 1 ]; then
        rm -rf "$WORK"
    else
        echo "FAILURE EVIDENCE PRESERVED: $LOGS (home: $HOME_DIR)" >&2
    fi
}
trap cleanup EXIT

# ── env allowlist: the ONLY ambient state the journey may see ───────────────
# Deliberately absent: VOYAGE_API_KEY (r5f3c — an ambient key must not flip
# the engine voyage-only), NX_* steering vars, XDG overrides.
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

echo "── 1/8 Build the wheel under test ──"
( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
    || _fail "wheel build failed (see $LOGS/build.log)"
WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
echo "  $WHEEL"

echo "── 2/8 Virgin venv + install ──"
uv venv --python 3.12 -q "$VENV"
uv pip install -q --python "$VENV/bin/python" "$WHEEL"
_nx --version

echo "── 3/8 nx init (local mode, virgin HOME, scrubbed env) ──"
_nx init -y --no-autostart 2>&1 | tee "$LOGS/init.log"
grep -Eq "the service backend is serving" "$LOGS/init.log" \
    || _fail "init did not confirm a serving backend"
# nexus-9xfx5: init converges the ladder — a virgin box must not boot with a
# vacuous pending rung. ("converged and verified" is the runner's literal
# success line — upgrade.py's LadderRunner output.)
grep -q "converged and verified" "$LOGS/init.log" \
    || _fail "init did not converge the upgrade ladder (9xfx5 regression)"
# Save the model cache as soon as it exists — a later-step failure must not
# force the next run to re-download 416 MB.
if [ -n "${FRESH_MVV_CACHE:-}" ] && [ -d "$HOME_DIR/.cache/nexus" ]; then
    mkdir -p "$FRESH_MVV_CACHE"
    rm -rf "$FRESH_MVV_CACHE/nexus"
    cp -R "$HOME_DIR/.cache/nexus" "$FRESH_MVV_CACHE/nexus" 2>/dev/null || true
fi

echo "── 4/8 store put: catalog row + manifest (the f1itv assertions) ──"
SENTINEL="fresh-mvv-sentinel: portable pgvector never ships the builder ISA"
echo "$SENTINEL" | _nx store put - --title "fresh-mvv-sentinel" \
    >"$LOGS/store.log" 2>&1 || _fail "store put failed (see $LOGS/store.log)"
grep -Eq "Stored: [0-9a-f]{64}" "$LOGS/store.log" \
    || _fail "store put did not emit a full-digest doc id (RDR-180 shape)"
if grep -q "manifest_hook_batch_missing_doc_identity" "$LOGS/store.log"; then
    _fail "manifest skipped — catalog doc identity missing on a fresh box (nexus-f1itv class)"
fi
_nx catalog search "fresh-mvv-sentinel" >"$LOGS/catalog-store.log" 2>&1 || true
grep -q "fresh-mvv-sentinel" "$LOGS/catalog-store.log" \
    || _fail "store put did not register in the engine catalog (nexus-f1itv class)"

echo "── 5/8 index md: catalog registration (the e9ru2 assertions) ──"
# File stem == frontmatter title on purpose: the pre-flight registration
# titles the catalog row by STEM, and the post-hook's update branch does not
# overwrite title from frontmatter (recorded as nexus bead — see gate docs);
# the assertion must not depend on which one wins.
cat > "$WORK/fresh-mvv-markdown-note.md" <<'EOF'
---
title: fresh-mvv-markdown-note
---
The era-32 wire re-id recomputes during re-embed, and fresh boxes register
markdown in the service catalog.
EOF
_nx index md "$WORK/fresh-mvv-markdown-note.md" --corpus fresh-mvv \
    >"$LOGS/index.log" 2>&1 || _fail "index md failed (see $LOGS/index.log)"
if grep -q "manifest_hook_batch_missing_doc_identity" "$LOGS/index.log"; then
    _fail "markdown manifest skipped on a fresh box (nexus-e9ru2 class)"
fi
_nx catalog search "fresh-mvv-markdown-note" >"$LOGS/catalog-md.log" 2>&1 || true
grep -q "fresh-mvv-markdown-note" "$LOGS/catalog-md.log" \
    || _fail "index md did not register in the engine catalog (nexus-e9ru2 class)"

echo "── 6/8 semantic search returns both ──"
_nx search "portable pgvector builder ISA" >"$LOGS/search1.log" 2>&1 || true
grep -q "fresh-mvv-sentinel" "$LOGS/search1.log" \
    || _fail "search did not return the stored sentinel"
_nx search "era-32 wire re-id re-embed" >"$LOGS/search2.log" 2>&1 || true
grep -q "fresh-mvv" "$LOGS/search2.log" \
    || _fail "search did not return the indexed markdown"

echo "── 7/8 doctor: zero ✗, zero ⚠, warnings allowlisted ──"
_nx doctor >"$LOGS/doctor.log" 2>&1 || _fail "doctor exited non-zero"
if grep -q "✗" "$LOGS/doctor.log"; then
    grep "✗" "$LOGS/doctor.log" >&2
    _fail "doctor shows red ✗ on a virgin box (9xfx5 class)"
fi
# Warnings allowlist — EMPTY by design. Every new fresh-box warning is a
# decision: fix it or allowlist it HERE with a rationale + bead reference.
# Covers BOTH channels (critic-3modes M-H): structlog lines
# (level='warning') AND doctor's human-facing soft-warn rows (⚠ — the
# format_health_for_cli warn=True render, which contains no literal
# "warning" text and was invisible to the structlog grep alone).
ALLOWLIST_REGEX='^$'  # no allowed warnings
if grep -E "level='warning'|\[warning|⚠" "$LOGS/doctor.log" \
        | grep -Ev "$ALLOWLIST_REGEX" | grep -q .; then
    grep -E "level='warning'|\[warning|⚠" "$LOGS/doctor.log" >&2
    _fail "non-allowlisted warnings in a virgin box's doctor (add a fix or an allowlist entry with rationale)"
fi

echo "── 8/8 non-vacuity ──"
# The gate must never skip-pass: prove the substantive legs actually ran.
for f in init.log store.log index.log doctor.log; do
    [ -s "$LOGS/$f" ] || _fail "leg log $f is empty — a journey leg silently skipped"
done

GATE_OK=1
echo "FRESH-INSTALL MVV PASSED — conexus $(_nx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
