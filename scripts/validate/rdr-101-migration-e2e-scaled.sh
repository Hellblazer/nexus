#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RDR-101 Phase 3 scaled-soak migration validator (nexus-o6aa.9.11).
#
# Heavier-weight companion to ``rdr-101-migration-e2e.sh``. The lightweight
# harness validates correctness at 3 docs in under a minute; this one
# validates the linear-scaling claim at N=200 docs and reports
# per-stage wall-clock so regressions in synthesize-log /
# t3-backfill-doc-id show up loudly.
#
# Run on demand, not as part of routine validation. Expect 2–5 minutes
# wall-clock depending on machine.
#
# Usage:
#   ./scripts/validate/rdr-101-migration-e2e-scaled.sh             # default N=200
#   N=1000 ./scripts/validate/rdr-101-migration-e2e-scaled.sh      # crank up
#
# Self-cleaning sandbox on success; preserved on failure.

set -uo pipefail

N="${N:-200}"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/nexus-rdr101-scaled-XXXXXX")"
ORIG_HOME="$HOME"
export HOME="$SANDBOX"
export NX_LOCAL=1
export NX_LOCAL_CHROMA_PATH="$SANDBOX/.local/share/nexus/chroma"
export NEXUS_CONFIG_DIR="$SANDBOX/.config/nexus"
export NEXUS_CATALOG_PATH="$SANDBOX/.config/nexus/catalog"
mkdir -p "$NEXUS_CONFIG_DIR" "$NX_LOCAL_CHROMA_PATH" "$SANDBOX/.cache" "$SANDBOX/repo"
TRANSCRIPT="$SANDBOX/.cache/transcript.log"

cleanup() {
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        rm -rf "$SANDBOX"
    else
        printf "\nSandbox preserved at %s (transcript: %s)\n" \
            "$SANDBOX" "$TRANSCRIPT" >&2
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

PASS=0
FAIL=0
FAILED_CASES=()

ts()   { date +"%H:%M:%S"; }
step() { printf "\n[%s] ═══ %s ═══\n" "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; }
info() { printf "[%s]    %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; }
pass() { printf "[%s]  ✓ %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; PASS=$((PASS+1)); }
fail() { printf "[%s]  ✗ %s\n"        "$(ts)" "$*" | tee -a "$TRANSCRIPT" >&2; FAIL=$((FAIL+1)); FAILED_CASES+=("$*"); }

now_ms() { python3 -c 'import time; print(int(time.monotonic()*1000))'; }

step "Sandbox: N=$N"
info "Sandbox:  $SANDBOX"
info "Catalog:  $NEXUS_CATALOG_PATH"
info "Chroma:   $NX_LOCAL_CHROMA_PATH"
info "Transcript: $TRANSCRIPT"

# ── Step 1: Generate N markdown docs ──────────────────────────────────────

step "1. Generate $N markdown docs in $SANDBOX/repo"
GEN_START=$(now_ms)
for i in $(seq 1 "$N"); do
    cat >"$SANDBOX/repo/doc-$i.md" <<EOF
# Document $i

Sandbox-generated content for the RDR-101 Phase 3 scaled-soak validator.
Lorem ipsum line A.
Lorem ipsum line B with a few more words to add some embedding signal.
A reference to document number $i. Cross-document signal, slot $i / $N.
EOF
done
GEN_END=$(now_ms)
GEN_MS=$((GEN_END - GEN_START))
info "Generated $N docs in ${GEN_MS}ms"
pass "doc generation"

# ── Step 2: Initialize catalog and index under legacy mode ────────────────

step "2. Initialize catalog and index $N docs under NEXUS_EVENT_SOURCED=0"
uv run nx catalog init >"$SANDBOX/.cache/init.out" 2>&1 || true
INIT_LINE=$(tail -1 "$SANDBOX/.cache/init.out")
info "init: $INIT_LINE"

export NEXUS_EVENT_SOURCED=0
INDEX_START=$(now_ms)
# Batch the indexing — hammering nx in a tight bash loop has Python
# startup cost dominating; one nx invocation per doc is realistic for
# a real upgrade scenario but slow at N=200. Use the markdown-batch
# verb (`nx index md` accepts a single file at a time, so loop, but
# stream stdout/stderr to a sink to keep the transcript readable).
INDEX_FAILS=0
for f in "$SANDBOX/repo/"doc-*.md; do
    if ! uv run nx index md "$f" --corpus scaled \
        >>"$SANDBOX/.cache/index.out" 2>&1; then
        INDEX_FAILS=$((INDEX_FAILS+1))
    fi
done
INDEX_END=$(now_ms)
INDEX_MS=$((INDEX_END - INDEX_START))
INDEX_PER_DOC=$((INDEX_MS / N))
info "Indexed $N docs in ${INDEX_MS}ms (${INDEX_PER_DOC}ms/doc, $INDEX_FAILS failures)"

if [[ "$INDEX_FAILS" -eq 0 ]]; then
    pass "all $N docs indexed under legacy mode"
else
    fail "$INDEX_FAILS of $N indexings failed under legacy mode"
fi

# Confirm legacy JSONL state.
DOCS_JSONL="$NEXUS_CATALOG_PATH/documents.jsonl"
EVENTS_JSONL="$NEXUS_CATALOG_PATH/events.jsonl"
DOC_LINES=$(wc -l <"$DOCS_JSONL" 2>/dev/null || echo 0)
info "documents.jsonl: $DOC_LINES lines"

if [[ "$DOC_LINES" -ge "$N" ]]; then
    pass "documents.jsonl scaled correctly ($DOC_LINES ≥ $N)"
else
    fail "documents.jsonl undersized ($DOC_LINES < $N)"
fi

if [[ ! -s "$EVENTS_JSONL" ]]; then
    pass "events.jsonl empty (legacy bootstrap shape)"
else
    fail "events.jsonl unexpectedly populated under NEXUS_EVENT_SOURCED=0"
fi

# ── Step 3: ES default-on with empty events.jsonl: synthesizer PASS ──────

step "3. ES default-on: doctor uses synthesizer path (empty event log)"
unset NEXUS_EVENT_SOURCED

DOC0_START=$(now_ms)
uv run nx catalog doctor --replay-equality >"$SANDBOX/.cache/doctor0.out" 2>&1
DOC0_RC=$?
DOC0_END=$(now_ms)
DOC0_MS=$((DOC0_END - DOC0_START))
info "doctor (empty-events synthesizer) wall: ${DOC0_MS}ms  exit: $DOC0_RC"

if [[ $DOC0_RC -eq 0 ]]; then
    pass "doctor PASSes via synthesizer at scale (empty events.jsonl)"
else
    fail "doctor fails on empty-events catalog at scale (rc=$DOC0_RC)"
    tail -20 "$SANDBOX/.cache/doctor0.out" >&2
fi

# ── Step 4: Migrate; measure synthesize-log + t3-backfill ─────────────────

step "4. nx catalog synthesize-log --force (timed)"
# This harness uses the underlying primitive rather than the
# composed ``nx catalog migrate`` verb so it can run against any
# branch carrying the ``.9.10`` harness, regardless of whether the
# ``.9.9`` migrate verb has merged yet.
MIG_START=$(now_ms)
uv run nx catalog synthesize-log --force >"$SANDBOX/.cache/migrate.out" 2>&1
MIG_RC=$?
MIG_END=$(now_ms)
MIG_MS=$((MIG_END - MIG_START))
info "synthesize-log wall: ${MIG_MS}ms  exit: $MIG_RC"
info "synthesize-log per-doc cost: $((MIG_MS / N))ms/doc"

if [[ $MIG_RC -eq 0 ]]; then
    pass "synthesize-log --force succeeds at scale"
else
    fail "synthesize-log --force fails at scale (rc=$MIG_RC)"
    tail -30 "$SANDBOX/.cache/migrate.out" >&2
fi

EVT_LINES=$(wc -l <"$EVENTS_JSONL" 2>/dev/null || echo 0)
info "events.jsonl: $EVT_LINES lines"

# Expected: at least N DocumentRegistered events + 1 OwnerRegistered.
# May include LinkCreated events too if the legacy state had links.
if [[ "$EVT_LINES" -ge "$N" ]]; then
    pass "events.jsonl populated to scale ($EVT_LINES ≥ $N)"
else
    fail "events.jsonl undersized post-migration ($EVT_LINES < $N)"
fi

# ── Step 5: Doctor PASSes post-migration ──────────────────────────────────

step "5. Doctor PASSes post-migration (timed)"
DOC1_START=$(now_ms)
uv run nx catalog doctor --replay-equality >"$SANDBOX/.cache/doctor1.out" 2>&1
DOC1_RC=$?
DOC1_END=$(now_ms)
DOC1_MS=$((DOC1_END - DOC1_START))
info "doctor (post-migration replay) wall: ${DOC1_MS}ms  exit: $DOC1_RC"

if [[ $DOC1_RC -eq 0 ]]; then
    pass "doctor PASSes post-migration at scale"
else
    fail "doctor fails post-migration at scale (rc=$DOC1_RC)"
    tail -20 "$SANDBOX/.cache/doctor1.out" >&2
fi

# ── Step 6: Performance ratios ────────────────────────────────────────────

step "6. Performance baseline at N=$N"
info "doc generation:        ${GEN_MS}ms"
info "legacy index:          ${INDEX_MS}ms (${INDEX_PER_DOC}ms/doc)"
info "doctor synthesizer:    ${DOC0_MS}ms"
info "migrate (synth-log):   ${MIG_MS}ms ($((MIG_MS / N))ms/doc)"
info "doctor post-migration: ${DOC1_MS}ms"

# Soft thresholds — flag a heads-up for regression but don't hard-fail.
# These are wall-clock budgets that suit a 200-doc sandbox; revisit if
# scaling further or running on slower CI.
if [[ "$DOC1_MS" -lt 30000 ]]; then  # 30s budget for post-migration doctor
    pass "post-migration doctor wall under 30s ($((DOC1_MS / 1000))s)"
else
    fail "post-migration doctor wall exceeded 30s soft budget ($((DOC1_MS / 1000))s)"
fi

# ── Summary ───────────────────────────────────────────────────────────────

step "Summary"
info "PASS: $PASS"
info "FAIL: $FAIL"
if (( FAIL > 0 )); then
    info "Failed cases:"
    for c in "${FAILED_CASES[@]}"; do
        info "  - $c"
    done
    exit 1
fi
info "All scaled-soak validation steps passed at N=$N."
exit 0
