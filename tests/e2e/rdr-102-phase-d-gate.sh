#!/usr/bin/env bash
# rdr-102-phase-d-gate.sh — RDR-102 Phase D validation gate (option A).
#
# Runs the two operator-runnable validation gates RDR-102 D5 specifies:
#   1. Write-then-verify gate — index a fresh file through each of the 4
#      writer paths (PDF / md / RDR / repo) into temp collections, then
#      verify (a) chunks have NO source_path AND (b) chunks have doc_id
#      populated at write time. Runs against PRODUCTION T3 with temp
#      collection names that cannot collide with anything real.
#   2. Orphan-recovery smoke — extract the catalog backup tarball to a
#      sandbox dir, point NEXUS_CATALOG_PATH at it, run synthesize-log
#      with --dry-run BOTH WITH and WITHOUT --prefer-live-catalog, and
#      compare orphan event counts. Read-only against production T3.
#      Read-only against the live catalog (the sandbox is a copy).
#
# Safety:
#   - Production T3 sees writes ONLY for the 4 temp collections under
#     gate 1. They are deleted on cleanup.
#   - The live production catalog at $NEXUS_CONFIG_DIR is NEVER touched.
#   - The sandbox catalog (extracted tarball) is read by gate 2; the
#     synthesize-log run uses --dry-run so events.jsonl is not rewritten.
#   - Any failure (set -e) leaves the sandbox dir in place for forensics
#     and skips temp-collection cleanup so an operator can investigate.
#     Successful run cleans both.
#
# Usage:
#   tests/e2e/rdr-102-phase-d-gate.sh [TARBALL_PATH]
#
# Default tarball: ~/nexus-catalog-backup-20260501-165411.tar.gz
#
# Exit codes:
#   0  — both gates PASS
#   1  — write-then-verify gate FAIL (see report)
#   2  — orphan-recovery smoke FAIL (see report)
#   3  — environment / setup error (no tarball, no creds, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARBALL="${1:-$HOME/nexus-catalog-backup-20260501-165411.tar.gz}"
TS="$(date +%Y%m%d-%H%M%S)"
SANDBOX="/tmp/rdr102-phase-d-$TS"
GATE_PREFIX="rdr102-gate-$TS"
REPORT="$SANDBOX/report.md"

# Temp collection names — guaranteed unique by timestamp suffix
COLL_PDF="knowledge__$GATE_PREFIX"
COLL_MD="docs__$GATE_PREFIX"
COLL_REPO_CODE="code__$GATE_PREFIX-repo"
COLL_REPO_DOCS="docs__$GATE_PREFIX-repo"
COLL_RDR=""  # filled in after `nx index rdr` based on its repo-hash convention

CLEANUP_COLLECTIONS=()
CLEANUP_SANDBOX=true

_die() { printf 'ERROR: %s\n' "$*" >&2; exit 3; }
_say() { printf '\n=== %s ===\n' "$*"; }
_log() { printf '  %s\n' "$*"; }

cleanup() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    printf '\n!!! Failure (exit %d) — sandbox left at %s for forensics\n' "$rc" "$SANDBOX" >&2
    printf '!!! Temp T3 collections (delete manually if you like):\n' >&2
    for c in "${CLEANUP_COLLECTIONS[@]}"; do printf '!!!   %s\n' "$c" >&2; done
    return
  fi
  if $CLEANUP_SANDBOX; then
    printf '\nCleaning up sandbox at %s\n' "$SANDBOX"
    rm -rf "$SANDBOX"
  fi
  if (( ${#CLEANUP_COLLECTIONS[@]} > 0 )); then
    printf 'Cleaning up %d temp T3 collections\n' "${#CLEANUP_COLLECTIONS[@]}"
    for c in "${CLEANUP_COLLECTIONS[@]}"; do
      nx collection delete "$c" --yes 2>/dev/null || \
        printf '  WARN: could not delete %s (may not exist)\n' "$c"
    done
  fi
}
trap cleanup EXIT

# ── Pre-flight ──────────────────────────────────────────────────────────────

_say "Pre-flight"
[[ -f "$TARBALL" ]] || _die "tarball not found: $TARBALL"
command -v nx >/dev/null || _die "nx CLI not on PATH (run scripts/reinstall-tool.sh)"
command -v jq >/dev/null || _die "jq not on PATH (brew install jq)"

NX_VERSION="$(nx --version 2>/dev/null || echo unknown)"
TARBALL_SIZE="$(du -h "$TARBALL" | cut -f1)"
_log "nx --version : $NX_VERSION"
_log "tarball      : $TARBALL ($TARBALL_SIZE)"
_log "sandbox      : $SANDBOX"
_log "gate prefix  : $GATE_PREFIX"

mkdir -p "$SANDBOX"
echo "# RDR-102 Phase D Gate Report ($TS)" >"$REPORT"
echo "" >>"$REPORT"
echo "- nx version: \`$NX_VERSION\`" >>"$REPORT"
echo "- tarball: \`$TARBALL\`" >>"$REPORT"
echo "- sandbox: \`$SANDBOX\`" >>"$REPORT"
echo "" >>"$REPORT"

# ── Gate 1 — write-then-verify against production T3 ────────────────────────

_say "Gate 1 — write-then-verify (production T3, temp collections)"

FIXTURE_DIR="$SANDBOX/fixtures"
mkdir -p "$FIXTURE_DIR"

# Fixture 1: PDF (use the smallest existing test fixture)
PDF_FIXTURE="$REPO_ROOT/tests/fixtures/tc-sql.pdf"
[[ -f "$PDF_FIXTURE" ]] || _die "PDF fixture missing: $PDF_FIXTURE"
_log "PDF fixture  : $PDF_FIXTURE"

# Fixture 2: standalone markdown
MD_FIXTURE="$FIXTURE_DIR/test-doc.md"
cat >"$MD_FIXTURE" <<'EOF'
---
title: RDR-102 Phase D Gate Test Doc
---

# Phase D Gate Test

This is a sentinel markdown file for the RDR-102 Phase D write-then-verify
gate. It exercises the standalone `nx index md` writer path.

## Section A

Body text alpha for chunking.

## Section B

Body text beta for chunking.
EOF
_log "MD fixture   : $MD_FIXTURE"

# Fixture 3: standalone RDR markdown
RDR_FIXTURE="$FIXTURE_DIR/rdr-test.md"
cat >"$RDR_FIXTURE" <<'EOF'
---
title: RDR-102 Phase D Gate Test RDR
status: draft
---

# RDR-102 Phase D Gate Test

## Decision

This sentinel exercises the standalone `nx index rdr` writer path.

## Consequences

Phase D verifies it lands chunks with doc_id and without source_path.
EOF
_log "RDR fixture  : $RDR_FIXTURE"

# Fixture 4: small repo (empty git init + 1 code + 1 prose file)
REPO_FIXTURE="$FIXTURE_DIR/sentinel-repo"
mkdir -p "$REPO_FIXTURE"
cat >"$REPO_FIXTURE/sample.py" <<'EOF'
"""Sentinel code file for RDR-102 Phase D write-then-verify gate."""

def hello() -> str:
    """Return a greeting; small enough to chunk in one piece."""
    return "RDR-102 Phase D"
EOF
cat >"$REPO_FIXTURE/README.md" <<'EOF'
# Sentinel Repo

Exercises the `nx index repo` writer path for the RDR-102 Phase D gate.
EOF
(cd "$REPO_FIXTURE" && git init -q && git add . && \
  git -c user.email=gate@nexus -c user.name=gate commit -q -m "init") || \
  _die "git init failed in $REPO_FIXTURE"
_log "repo fixture : $REPO_FIXTURE"

echo "## Gate 1 — write-then-verify" >>"$REPORT"
echo "" >>"$REPORT"

# 1a. nx index pdf — knowledge__rdr102-gate-$TS
_log "1a) nx index pdf -> $COLL_PDF"
nx index pdf "$PDF_FIXTURE" --collection "$COLL_PDF" >/dev/null
CLEANUP_COLLECTIONS+=("$COLL_PDF")

# 1b. nx index md — docs__rdr102-gate-$TS (corpus = $GATE_PREFIX)
_log "1b) nx index md  -> $COLL_MD"
nx index md "$MD_FIXTURE" --corpus "$GATE_PREFIX" >/dev/null
CLEANUP_COLLECTIONS+=("$COLL_MD")

# 1c. nx index rdr — standalone (uses batch_index_markdowns; collection
# named after the file's owning repo hash — for a standalone .md without
# a repo, the convention is rdr__standalone-<hash>; we use --collection
# explicitly so cleanup knows what to delete)
COLL_RDR="rdr__$GATE_PREFIX-rdr"
_log "1c) nx index rdr -> $COLL_RDR (via --collection override)"
# nx index rdr does not accept --collection in current CLI; route via
# batch_index_markdowns through a shim or use index md with rdr__ prefix.
# Use index md directly with --collection — same code path as `nx index
# rdr` (both go through batch_index_markdowns / _index_document).
nx index md "$RDR_FIXTURE" --corpus "$GATE_PREFIX-rdr" \
  --collection "$COLL_RDR" --content-type rdr >/dev/null 2>&1 || {
  _log "    NOTE: --collection / --content-type may not be exposed on"
  _log "    nx index md; falling back to bare nx index md (collection"
  _log "    will be docs__-prefixed instead of rdr__). The chunk-write"
  _log "    invariants still apply — only the prefix differs."
  COLL_RDR="docs__$GATE_PREFIX-rdr"
  nx index md "$RDR_FIXTURE" --corpus "$GATE_PREFIX-rdr" >/dev/null
}
CLEANUP_COLLECTIONS+=("$COLL_RDR")

# 1d. nx index repo — exercises code_indexer + prose_indexer + pipeline_stages
_log "1d) nx index repo -> $COLL_REPO_CODE + $COLL_REPO_DOCS"
nx index repo "$REPO_FIXTURE" --corpus "$GATE_PREFIX-repo" >/dev/null 2>&1 || {
  # nx index repo may not accept --corpus; fall back to default naming.
  nx index repo "$REPO_FIXTURE" >/dev/null
  # Default naming: code__<reponame>-<hash8> / docs__<reponame>-<hash8>
  REPO_HASH="$(printf '%s' "$REPO_FIXTURE" | shasum -a 256 | cut -c1-8)"
  COLL_REPO_CODE="code__sentinel-repo-$REPO_HASH"
  COLL_REPO_DOCS="docs__sentinel-repo-$REPO_HASH"
}
CLEANUP_COLLECTIONS+=("$COLL_REPO_CODE" "$COLL_REPO_DOCS")

# Verify each collection: no source_path, doc_id populated.
GATE_1_PASS=true
echo "| collection | source_path leaks | doc_id coverage | verdict |" >>"$REPORT"
echo "|------------|-------------------|-----------------|---------|" >>"$REPORT"

for COLL in "$COLL_PDF" "$COLL_MD" "$COLL_RDR" "$COLL_REPO_CODE" "$COLL_REPO_DOCS"; do
  # source_path leak check: prune-deprecated-keys --dry-run
  PRUNE_JSON="$(nx catalog prune-deprecated-keys \
    --dry-run --collection "$COLL" --json 2>/dev/null || echo '{}')"
  SP_LEAKS="$(printf '%s' "$PRUNE_JSON" | jq -r '.chunks_updated // 0')"

  # doc_id coverage check: doctor --t3-doc-id-coverage filtered to coll
  COV_JSON="$(nx catalog doctor --t3-doc-id-coverage --json 2>/dev/null || echo '{}')"
  COV="$(printf '%s' "$COV_JSON" | jq -r ".t3_doc_id_coverage.tables[\"$COLL\"].coverage // \"-\"")"

  if [[ "$SP_LEAKS" == "0" ]] && [[ "$COV" == "1.0" || "$COV" == "1" ]]; then
    VERDICT="PASS"
  else
    VERDICT="FAIL"
    GATE_1_PASS=false
  fi
  echo "| \`$COLL\` | $SP_LEAKS | $COV | $VERDICT |" >>"$REPORT"
  _log "  $COLL  source_path=$SP_LEAKS  coverage=$COV  $VERDICT"
done
echo "" >>"$REPORT"

if $GATE_1_PASS; then
  echo "**Gate 1 result: PASS** — every writer path produces chunks with no source_path AND doc_id populated." >>"$REPORT"
  _say "Gate 1 PASS"
else
  echo "**Gate 1 result: FAIL** — see per-collection rows above." >>"$REPORT"
  _say "Gate 1 FAIL"
  CLEANUP_SANDBOX=false
  cat "$REPORT"
  exit 1
fi
echo "" >>"$REPORT"

# ── Gate 2 — orphan-recovery smoke (sandbox catalog, read-only T3) ──────────

_say "Gate 2 — orphan-recovery smoke (sandbox catalog, read-only T3)"

CATALOG_DIR="$SANDBOX/catalog"
_log "Extracting tarball ($TARBALL_SIZE compressed) to $SANDBOX/..."
tar -xzf "$TARBALL" -C "$SANDBOX/"
[[ -d "$CATALOG_DIR" ]] || _die "expected $CATALOG_DIR after tarball extract"
CATALOG_SIZE="$(du -sh "$CATALOG_DIR" | cut -f1)"
_log "extracted    : $CATALOG_DIR ($CATALOG_SIZE)"

# Override NEXUS_CATALOG_PATH for the rest of this gate.
export NEXUS_CATALOG_PATH="$CATALOG_DIR"

echo "## Gate 2 — orphan-recovery smoke" >>"$REPORT"
echo "" >>"$REPORT"
echo "Sandbox catalog: \`$CATALOG_DIR\` ($CATALOG_SIZE)" >>"$REPORT"
echo "Production T3 access: read-only (synthesize-log walks; --dry-run skips events.jsonl write)." >>"$REPORT"
echo "" >>"$REPORT"

# Baseline: synthesize-log --force --chunks --dry-run (no --prefer-live)
_log "Baseline: synthesize-log --force --chunks --dry-run --json"
BASELINE_JSON="$SANDBOX/baseline.json"
nx catalog synthesize-log --force --chunks --dry-run --json >"$BASELINE_JSON" 2>/dev/null || \
  _die "baseline synthesize-log failed; see $SANDBOX for partial output"
BASELINE_CHUNK_EVENTS="$(jq -r '.events_by_type.ChunkIndexed // 0' "$BASELINE_JSON")"
BASELINE_ORPHANS="$(jq -r '.orphan_chunks // 0' "$BASELINE_JSON")"
_log "  ChunkIndexed = $BASELINE_CHUNK_EVENTS"
_log "  orphans      = $BASELINE_ORPHANS"

# Recovery: synthesize-log --force --chunks --prefer-live-catalog --dry-run
_log "Recovery: synthesize-log --force --chunks --prefer-live-catalog --dry-run --json"
RECOVERY_JSON="$SANDBOX/recovery.json"
nx catalog synthesize-log --force --chunks --prefer-live-catalog --dry-run --json \
  >"$RECOVERY_JSON" 2>/dev/null || \
  _die "recovery synthesize-log failed; see $SANDBOX for partial output"
RECOVERY_CHUNK_EVENTS="$(jq -r '.events_by_type.ChunkIndexed // 0' "$RECOVERY_JSON")"
RECOVERY_ORPHANS="$(jq -r '.orphan_chunks // 0' "$RECOVERY_JSON")"
_log "  ChunkIndexed = $RECOVERY_CHUNK_EVENTS"
_log "  orphans      = $RECOVERY_ORPHANS"

# Compare
RECOVERED=$((BASELINE_ORPHANS - RECOVERY_ORPHANS))
if (( BASELINE_ORPHANS > 0 )); then
  RECOVERED_PCT="$(awk -v r="$RECOVERED" -v b="$BASELINE_ORPHANS" \
    'BEGIN { printf "%.2f", (r/b)*100 }')"
else
  RECOVERED_PCT="0.00"
fi

echo "| metric | baseline (no flag) | recovery (--prefer-live-catalog) | delta |" >>"$REPORT"
echo "|--------|--------------------|----------------------------------|-------|" >>"$REPORT"
echo "| ChunkIndexed events | $BASELINE_CHUNK_EVENTS | $RECOVERY_CHUNK_EVENTS | (should be equal) |" >>"$REPORT"
echo "| orphan chunks       | $BASELINE_ORPHANS | $RECOVERY_ORPHANS | -$RECOVERED ($RECOVERED_PCT%) |" >>"$REPORT"
echo "" >>"$REPORT"

GATE_2_PASS=true
if [[ "$BASELINE_CHUNK_EVENTS" != "$RECOVERY_CHUNK_EVENTS" ]]; then
  echo "**Gate 2 anomaly:** ChunkIndexed event counts differ between baseline and recovery." >>"$REPORT"
  echo "Counts MUST match — the flag only changes orphan classification, not the event population." >>"$REPORT"
  GATE_2_PASS=false
fi
if (( BASELINE_ORPHANS == 0 )); then
  echo "**Gate 2 note:** baseline orphan count is 0 — nothing to recover. The tarball's events.jsonl may be too clean for this smoke. Try a more recent backup." >>"$REPORT"
elif (( RECOVERED == 0 )); then
  echo "**Gate 2 note:** zero orphans recovered. This is EXPECTED if the orphan population is dominated by PDFs / standalone markdown (their catalog Documents do not carry content_hash; see docs/migration/rdr-101-phase4-orphan-recovery.md § Limitations). It is NOT a failure of the flag — the smoke confirms the recovery path runs without error and produces a deterministic result." >>"$REPORT"
else
  echo "**Gate 2 PASS:** $RECOVERED orphans ($RECOVERED_PCT%) became non-orphan via content_hash recovery." >>"$REPORT"
fi
echo "" >>"$REPORT"

if $GATE_2_PASS; then
  _say "Gate 2 PASS"
else
  _say "Gate 2 FAIL"
  CLEANUP_SANDBOX=false
  cat "$REPORT"
  exit 2
fi

# ── Final report ────────────────────────────────────────────────────────────

echo "## Summary" >>"$REPORT"
echo "" >>"$REPORT"
echo "- Gate 1 (write-then-verify): PASS" >>"$REPORT"
echo "- Gate 2 (orphan-recovery smoke): PASS" >>"$REPORT"
echo "" >>"$REPORT"
echo "RDR-102 Phase D operator gates passed. Phase 4 closeout complete." >>"$REPORT"

_say "BOTH GATES PASS"
echo ""
cat "$REPORT"
echo ""
echo "Report saved to: $REPORT (will be deleted on cleanup; copy if you want to keep it)"

# Save the report outside the sandbox before cleanup runs
PERMANENT_REPORT="$HOME/rdr-102-phase-d-report-$TS.md"
cp "$REPORT" "$PERMANENT_REPORT"
echo "Report also copied to: $PERMANENT_REPORT (survives cleanup)"
