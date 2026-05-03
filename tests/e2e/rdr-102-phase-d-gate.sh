#!/usr/bin/env bash
# rdr-102-phase-d-gate.sh — RDR-102 Phase D validation gate (option A).
#
# Two operator-runnable validation gates per RDR-102 D5:
#
#   Gate 1 — write-then-verify against production T3 (temp collections)
#     Indexes a fresh PDF + md + RDR + repo through their respective
#     writer paths. For each resulting collection, paginates col.get
#     and asserts (a) NO chunk carries `source_path` (Phase B leak
#     detector — ALLOWED_TOP_LEVEL doesn't include it post-RDR-102 D2,
#     so any chunk carrying it would be a regression) and (b) every
#     chunk carries a non-empty `doc_id` (Phase A coverage detector).
#
#   Gate 2 — orphan-recovery smoke (sandbox catalog, read-only T3)
#     Extracts a production catalog backup tarball to /tmp/, points
#     NEXUS_CATALOG_PATH at it, runs `synthesize-log --force --chunks
#     --dry-run --json` BOTH WITH and WITHOUT --prefer-live-catalog,
#     and compares orphan-event counts. --dry-run skips the events.jsonl
#     write so the sandbox catalog is unchanged. Production T3 sees only
#     read traffic from the synthesizer's collection walks.
#
# Safety:
#   - Production catalog at $NEXUS_CONFIG_DIR is NEVER touched.
#   - Production T3 sees writes ONLY for the gate's temp collections;
#     they are deleted on cleanup.
#   - On any failure (gate FAIL, set -e exit, signal-induced exit) the
#     sandbox dir + temp collections are LEFT in place for forensics.
#     The trap uses an explicit SUCCESS flag set at the script's tail
#     rather than `$?` (which can be 0 for the last successful command
#     before a SIGTERM).
#
# Local-source dispatch:
#   - All `nx` invocations route through `uv run nx ...` from REPO_ROOT
#     so the local Phase A/B/C source code is exercised, not whatever
#     wheel-installed `nx` happens to be on PATH. The published PyPI
#     wheel may pre-date Phase A/B/C; running the gate against an old
#     wheel would silently fail (no doc_id, source_path leaks) without
#     this contract.
#
# Usage:
#   tests/e2e/rdr-102-phase-d-gate.sh [TARBALL_PATH]
#
# Default tarball: ~/nexus-catalog-backup-20260501-165411.tar.gz
#
# Exit codes:
#   0  — both gates PASS
#   1  — Gate 1 (write-then-verify) FAIL
#   2  — Gate 2 (orphan-recovery smoke) FAIL
#   3  — environment / setup error (no tarball, no creds, no jq, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARBALL="${1:-$HOME/nexus-catalog-backup-20260501-165411.tar.gz}"
TS="$(date +%Y%m%d-%H%M%S)"
SANDBOX="/tmp/rdr102-phase-d-$TS"
GATE_PREFIX="rdr102-gate-$TS"
REPORT="$SANDBOX/report.md"
PERMANENT_REPORT="$HOME/rdr-102-phase-d-report-$TS.md"

# Local-source nx wrapper. cd to REPO_ROOT so uv resolves the project's
# own pyproject + src/, not whatever happens to be installed globally.
nx() { (cd "$REPO_ROOT" && uv run --quiet nx "$@"); }

CLEANUP_COLLECTIONS=()
SUCCESS=false  # set true at the very end of the happy path

_die() { printf 'ERROR: %s\n' "$*" >&2; exit 3; }
_say() { printf '\n=== %s ===\n' "$*"; }
_log() { printf '  %s\n' "$*"; }

cleanup() {
  if ! $SUCCESS; then
    printf '\n!!! Run did not complete successfully — sandbox left at %s for forensics\n' "$SANDBOX" >&2
    if [[ -f "$REPORT" ]]; then
      cp "$REPORT" "$PERMANENT_REPORT" 2>/dev/null && \
        printf '!!! Partial report copied to %s\n' "$PERMANENT_REPORT" >&2
    fi
    if (( ${#CLEANUP_COLLECTIONS[@]} > 0 )); then
      printf '!!! Temp T3 collections (delete manually):\n' >&2
      for c in "${CLEANUP_COLLECTIONS[@]}"; do
        printf '!!!   nx collection delete %s --yes\n' "$c" >&2
      done
    fi
    return
  fi
  printf '\nCleaning up sandbox at %s\n' "$SANDBOX"
  rm -rf "$SANDBOX"
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
command -v jq >/dev/null || _die "jq not on PATH (brew install jq)"
[[ -d "$REPO_ROOT/src/nexus" ]] || _die "REPO_ROOT does not look like the nexus source tree: $REPO_ROOT"

NX_VERSION="$(nx --version 2>/dev/null | tail -1 || echo unknown)"
TARBALL_SIZE="$(du -h "$TARBALL" | cut -f1)"
_log "nx (uv run) : $NX_VERSION"
_log "REPO_ROOT   : $REPO_ROOT (uv run picks up local source)"
_log "tarball     : $TARBALL ($TARBALL_SIZE)"
_log "sandbox     : $SANDBOX"
_log "gate prefix : $GATE_PREFIX"

mkdir -p "$SANDBOX"
cat >"$REPORT" <<EOF
# RDR-102 Phase D Gate Report ($TS)

- nx (uv run): \`$NX_VERSION\` (sourced from \`$REPO_ROOT\`)
- tarball: \`$TARBALL\`
- sandbox: \`$SANDBOX\`

EOF

# ── Fixtures ────────────────────────────────────────────────────────────────

FIXTURE_DIR="$SANDBOX/fixtures"
mkdir -p "$FIXTURE_DIR"

PDF_FIXTURE="$REPO_ROOT/tests/fixtures/tc-sql.pdf"
[[ -f "$PDF_FIXTURE" ]] || _die "PDF fixture missing: $PDF_FIXTURE"

MD_FIXTURE="$FIXTURE_DIR/test-doc.md"
cat >"$MD_FIXTURE" <<'EOF'
---
title: RDR-102 Phase D Gate Test Doc
---

# Phase D Gate Test

Sentinel for the standalone `nx index md` writer path.

## Section A

Body alpha.

## Section B

Body beta.
EOF

# Sentinel git repo: holds both the code/prose for `nx index repo`
# AND the docs/rdr/ entry that `nx index rdr <repo>` discovers. The
# resulting collection names are derived from this repo's path hash
# so we list collections after each step to capture them.
REPO_FIXTURE="$FIXTURE_DIR/sentinel-repo"
mkdir -p "$REPO_FIXTURE/docs/rdr"
cat >"$REPO_FIXTURE/sample.py" <<'EOF'
"""Sentinel code file for RDR-102 Phase D write-then-verify gate."""

def hello() -> str:
    """Greeting."""
    return "RDR-102 Phase D"
EOF
cat >"$REPO_FIXTURE/README.md" <<'EOF'
# Sentinel Repo

Exercises `nx index repo` for the RDR-102 Phase D gate.
EOF
cat >"$REPO_FIXTURE/docs/rdr/RDR-D-GATE.md" <<'EOF'
---
title: RDR-102 Phase D Gate Sentinel RDR
status: draft
---

# Phase D Gate Sentinel RDR

## Decision

Exercises `nx index rdr` for the RDR-102 Phase D gate.

## Consequences

Phase D verifies it lands chunks with doc_id and without source_path.
EOF
(cd "$REPO_FIXTURE" && git init -q && git add . && \
  git -c user.email=gate@nexus -c user.name=gate commit -q -m "init") || \
  _die "git init failed in $REPO_FIXTURE"

# Snapshot existing collections so we can detect the new ones the gate creates.
_collections_now() {
  nx collection list 2>/dev/null | awk 'NR>1 {print $1}' | grep -v '^$' || true
}
PRE_GATE_COLLECTIONS="$(_collections_now | sort -u)"

# Helper: diff post vs pre to find newly-created collections.
_new_collections_since() {
  local pre="$1" post
  post="$(_collections_now | sort -u)"
  comm -13 <(printf '%s\n' "$pre") <(printf '%s\n' "$post")
}

# ── Gate 1 — write-then-verify ──────────────────────────────────────────────

_say "Gate 1 — write-then-verify (production T3, temp collections)"

# 1a — nx index pdf into a uniquely-named knowledge__ collection.
COLL_PDF="knowledge__$GATE_PREFIX"
_log "1a) nx index pdf -> $COLL_PDF"
nx index pdf "$PDF_FIXTURE" --collection "$COLL_PDF" >/dev/null
CLEANUP_COLLECTIONS+=("$COLL_PDF")

# 1b — nx index md (standalone). docs__$CORPUS naming convention.
_log "1b) nx index md  -> docs__$GATE_PREFIX (corpus=$GATE_PREFIX)"
nx index md "$MD_FIXTURE" --corpus "$GATE_PREFIX" >/dev/null
COLL_MD="docs__$GATE_PREFIX"
CLEANUP_COLLECTIONS+=("$COLL_MD")

# 1c — nx index rdr discovers RDR file inside sentinel-repo. Collection
# name comes from _rdr_collection_name(repo_root) which uses the path
# hash. We don't predict it; we list collections before/after.
_log "1c) nx index rdr <sentinel-repo>"
PRE_RDR="$(_collections_now | sort -u)"
nx index rdr "$REPO_FIXTURE" >/dev/null
RDR_NEW="$(_new_collections_since "$PRE_RDR" | grep -E '^rdr__' | head -1)"
[[ -n "$RDR_NEW" ]] || _die "could not find new rdr__ collection after nx index rdr"
COLL_RDR="$RDR_NEW"
CLEANUP_COLLECTIONS+=("$COLL_RDR")
_log "    detected: $COLL_RDR"

# 1d — nx index repo creates code__ and docs__ for the sentinel repo.
_log "1d) nx index repo <sentinel-repo>"
PRE_REPO="$(_collections_now | sort -u)"
nx index repo "$REPO_FIXTURE" >/dev/null
REPO_NEW="$(_new_collections_since "$PRE_REPO")"
COLL_REPO_CODE="$(printf '%s\n' "$REPO_NEW" | grep -E '^code__' | head -1 || true)"
COLL_REPO_DOCS="$(printf '%s\n' "$REPO_NEW" | grep -E '^docs__' | head -1 || true)"
[[ -n "$COLL_REPO_CODE" ]] || _die "could not find new code__ collection after nx index repo"
[[ -n "$COLL_REPO_DOCS" ]] || _die "could not find new docs__ collection after nx index repo"
CLEANUP_COLLECTIONS+=("$COLL_REPO_CODE" "$COLL_REPO_DOCS")
_log "    detected: $COLL_REPO_CODE + $COLL_REPO_DOCS"

# Verify each collection: no source_path, doc_id populated. Direct
# chunk inspection via uv run python — bypasses doctor coverage
# (which only sees collections with non-orphan ChunkIndexed events
# in events.jsonl, and fresh writes haven't been synthesize-log'd
# yet) and prune-deprecated-keys (which requires the
# --i-have-completed-the-reader-migration acknowledgement).
{
  echo "## Gate 1 — write-then-verify"
  echo ""
  echo "| collection | total chunks | source_path leaks | missing doc_id | verdict |"
  echo "|------------|--------------|-------------------|----------------|---------|"
} >>"$REPORT"

GATE_1_PASS=true
for COLL in "$COLL_PDF" "$COLL_MD" "$COLL_RDR" "$COLL_REPO_CODE" "$COLL_REPO_DOCS"; do
  CHECK_JSON="$(cd "$REPO_ROOT" && uv run --quiet python -c "
import json
import sys
from nexus.db import make_t3
t3 = make_t3()
try:
    col = t3._client.get_collection(name='$COLL')
except Exception as exc:
    print(json.dumps({'error': f'open: {exc}'}))
    sys.exit(0)
total, sp_leaks, missing_docid = 0, 0, 0
offset = 0
while True:
    page = col.get(limit=300, offset=offset, include=['metadatas'])
    ids = page.get('ids') or []
    metas = page.get('metadatas') or []
    if not ids:
        break
    for m in metas:
        m = m or {}
        total += 1
        if 'source_path' in m:
            sp_leaks += 1
        if not m.get('doc_id'):
            missing_docid += 1
    if len(ids) < 300:
        break
    offset += 300
print(json.dumps({'total': total, 'source_path_leaks': sp_leaks, 'missing_doc_id': missing_docid}))
" 2>&1 | tail -1)"

  if printf '%s' "$CHECK_JSON" | jq -e '.error' >/dev/null 2>&1; then
    TOTAL="-"; SP_LEAKS="-"; MISSING="-"
    VERDICT="ERROR: $(printf '%s' "$CHECK_JSON" | jq -r '.error')"
    GATE_1_PASS=false
  else
    TOTAL="$(printf '%s' "$CHECK_JSON" | jq -r '.total // "-"')"
    SP_LEAKS="$(printf '%s' "$CHECK_JSON" | jq -r '.source_path_leaks // "-"')"
    MISSING="$(printf '%s' "$CHECK_JSON" | jq -r '.missing_doc_id // "-"')"
    if [[ "$TOTAL" != "0" && "$TOTAL" != "-" ]] && \
       [[ "$SP_LEAKS" == "0" ]] && [[ "$MISSING" == "0" ]]; then
      VERDICT="PASS"
    else
      VERDICT="FAIL"
      GATE_1_PASS=false
    fi
  fi
  echo "| \`$COLL\` | $TOTAL | $SP_LEAKS | $MISSING | $VERDICT |" >>"$REPORT"
  _log "  $COLL  total=$TOTAL  source_path_leaks=$SP_LEAKS  missing_doc_id=$MISSING  $VERDICT"
done
echo "" >>"$REPORT"

if $GATE_1_PASS; then
  echo "**Gate 1 result: PASS** — every writer path produces chunks with no source_path AND doc_id populated." >>"$REPORT"
  echo "" >>"$REPORT"
  _say "Gate 1 PASS"
else
  echo "**Gate 1 result: FAIL** — see per-collection rows above." >>"$REPORT"
  echo "" >>"$REPORT"
  _say "Gate 1 FAIL"
  cat "$REPORT"
  exit 1
fi

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

{
  echo "## Gate 2 — orphan-recovery smoke"
  echo ""
  echo "Sandbox catalog: \`$CATALOG_DIR\` ($CATALOG_SIZE)"
  echo "Production T3 access: read-only (synthesize-log walks; --dry-run skips events.jsonl write)."
  echo ""
} >>"$REPORT"

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

{
  echo "| metric | baseline (no flag) | recovery (--prefer-live-catalog) | delta |"
  echo "|--------|--------------------|----------------------------------|-------|"
  echo "| ChunkIndexed events | $BASELINE_CHUNK_EVENTS | $RECOVERY_CHUNK_EVENTS | (should be equal) |"
  echo "| orphan chunks       | $BASELINE_ORPHANS | $RECOVERY_ORPHANS | -$RECOVERED ($RECOVERED_PCT%) |"
  echo ""
} >>"$REPORT"

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
  cat "$REPORT"
  exit 2
fi

# ── Final report ────────────────────────────────────────────────────────────

{
  echo "## Summary"
  echo ""
  echo "- Gate 1 (write-then-verify): PASS"
  echo "- Gate 2 (orphan-recovery smoke): PASS"
  echo ""
  echo "RDR-102 Phase D operator gates passed. Phase 4 closeout complete."
} >>"$REPORT"

_say "BOTH GATES PASS"
echo ""
cat "$REPORT"
echo ""

# Save the report outside the sandbox before cleanup runs.
cp "$REPORT" "$PERMANENT_REPORT"
echo "Report copied to: $PERMANENT_REPORT (survives cleanup)"

# Mark success so the EXIT trap performs cleanup.
SUCCESS=true
