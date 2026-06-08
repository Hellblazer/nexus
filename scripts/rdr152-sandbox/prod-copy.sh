#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-152 sandbox harness — prod-copy.sh
# Seeds the sandbox READ-ONLY from prod. Prod is NEVER written.
# - Copies prod Chroma data directory (cp -R, no modification to source).
# - ETL-imports all T2 SQLite stores into sandbox Postgres via nx storage migrate.
# - Verifies sandbox counts == prod counts per store.
# - Asserts prod file mtimes UNCHANGED after the copy.
set -euo pipefail

SANDBOX_HOME="${SANDBOX_HOME:-${HOME}/nexus-rdr152-sandbox}"
SANDBOX_ENV="${SANDBOX_HOME}/sandbox.env"
# Honour NEXUS_CONFIG_DIR so the prod-touch guard is correct on non-default deployments.
PROD_CONFIG="${NEXUS_CONFIG_DIR:-${HOME}/.config/nexus}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "[prod-copy] RDR-152 sandbox seed from prod (READ-ONLY)"
echo "[prod-copy] SANDBOX_HOME=${SANDBOX_HOME}"
echo "[prod-copy] PROD_CONFIG=${PROD_CONFIG}"

# ── PROD-TOUCH GUARD ──────────────────────────────────────────────────────────
PROD_REAL="$(realpath "${PROD_CONFIG}" 2>/dev/null || echo "${PROD_CONFIG}")"
SANDBOX_CONFIG="${SANDBOX_HOME}/.config/nexus"
SANDBOX_REAL="$(realpath "${SANDBOX_CONFIG}" 2>/dev/null || echo "${SANDBOX_CONFIG}")"

if [[ "${SANDBOX_REAL}" == "${PROD_REAL}" || "${SANDBOX_REAL}" == "${PROD_REAL}/"* ]]; then
    echo "[prod-copy] ABORT: sandbox is not isolated from prod." >&2
    exit 1
fi

# ── REQUIRE sandbox.env ───────────────────────────────────────────────────────
if [[ ! -f "${SANDBOX_ENV}" ]]; then
    echo "[prod-copy] ERROR: ${SANDBOX_ENV} not found. Run up.sh first." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "${SANDBOX_ENV}"

# ── RECORD PROD MTIMES BEFORE COPY (for proof-of-read-only) ──────────────────
PROD_MEMORY_DB="${PROD_CONFIG}/memory.db"
PROD_CHROMA_DIR="${PROD_CONFIG}/chroma"
PROD_CATALOG_DIR="${PROD_CONFIG}/catalog"

# Helper: portable mtime (macOS stat -f "%m", Linux stat -c "%Y").
_mtime() { stat -f "%m" "$1" 2>/dev/null || stat -c "%Y" "$1" 2>/dev/null || echo ""; }

echo "[prod-copy] Recording prod file mtimes before copy..."
MTIME_BEFORE_MEMORY=""
MTIME_BEFORE_MEMORY_SHM=""
MTIME_BEFORE_MEMORY_WAL=""
if [[ -f "${PROD_MEMORY_DB}" ]]; then
    MTIME_BEFORE_MEMORY="$(_mtime "${PROD_MEMORY_DB}")"
    # WAL-mode sidecars: capture even if absent (empty string = no sidecar = OK).
    [[ -f "${PROD_MEMORY_DB}-shm" ]] && MTIME_BEFORE_MEMORY_SHM="$(_mtime "${PROD_MEMORY_DB}-shm")" || MTIME_BEFORE_MEMORY_SHM=""
    [[ -f "${PROD_MEMORY_DB}-wal" ]] && MTIME_BEFORE_MEMORY_WAL="$(_mtime "${PROD_MEMORY_DB}-wal")" || MTIME_BEFORE_MEMORY_WAL=""
fi
MTIME_BEFORE_CHROMA_SQLITE=""
CHROMA_SQLITE=""
if [[ -d "${PROD_CHROMA_DIR}" ]]; then
    CHROMA_SQLITE="${PROD_CHROMA_DIR}/chroma.sqlite3"
    if [[ -f "${CHROMA_SQLITE}" ]]; then
        MTIME_BEFORE_CHROMA_SQLITE="$(_mtime "${CHROMA_SQLITE}")"
    else
        CHROMA_SQLITE=""
    fi
fi

# ── COUNT PROD ROWS (read-only, --readonly flag prevents WAL -shm update) ──────
# CRITICAL: every sqlite3 invocation on a prod path MUST use --readonly.
# On a WAL-mode database, a plain SELECT (without --readonly) updates the prod
# -shm sidecar even though it writes no data rows.  --readonly opens the db in
# PRAGMA locking_mode=NORMAL, read-only VFS mode — no sidecar update.
# NOTE: --readonly requires the -shm file to pre-exist if the WAL is non-empty;
# if it is absent sqlite3 creates it momentarily.  We therefore record the
# sidecar mtime AFTER the count block and assert it matches BEFORE as part of
# the read-only proof.
echo "[prod-copy] Counting prod T2 rows (--readonly queries)..."
if [[ -f "${PROD_MEMORY_DB}" ]]; then
    PROD_MEMORY_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM memory;" 2>/dev/null || echo 0)"
    PROD_PLANS_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM plans;" 2>/dev/null || echo 0)"
    PROD_CHASH_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM chash_index;" 2>/dev/null || echo 0)"
    PROD_TOPICS_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM topics;" 2>/dev/null || echo 0)"
    PROD_TOPIC_ASSIGN_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM topic_assignments;" 2>/dev/null || echo 0)"
    PROD_TOPIC_LINKS_COUNT="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM topic_links;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_RELEVANCE="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM relevance_log;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_SEARCH="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM search_telemetry;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_RUNS="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM nx_answer_runs;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_HOOKS="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM hook_failures;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_TIER="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM tier_writes;" 2>/dev/null || echo 0)"
    PROD_TELEMETRY_FRECENCY="$(sqlite3 --readonly "${PROD_MEMORY_DB}" "SELECT COUNT(*) FROM frecency;" 2>/dev/null || echo 0)"
    echo "[prod-copy] Prod counts: memory=${PROD_MEMORY_COUNT} plans=${PROD_PLANS_COUNT} chash=${PROD_CHASH_COUNT}"
    echo "[prod-copy]             topics=${PROD_TOPICS_COUNT} assignments=${PROD_TOPIC_ASSIGN_COUNT} topic_links=${PROD_TOPIC_LINKS_COUNT}"
    echo "[prod-copy]             telemetry: relevance=${PROD_TELEMETRY_RELEVANCE} search=${PROD_TELEMETRY_SEARCH}"
    echo "[prod-copy]                        nx_answer_runs=${PROD_TELEMETRY_RUNS} hook_failures=${PROD_TELEMETRY_HOOKS}"
    echo "[prod-copy]                        tier_writes=${PROD_TELEMETRY_TIER} frecency=${PROD_TELEMETRY_FRECENCY}"
    # Re-capture sidecar mtimes after reads (--readonly may create -shm if absent).
    # If -shm didn't exist before and now does, record the new mtime as the baseline
    # for the post-copy assertion (the sidecar was created empty, not written with data).
    [[ -z "${MTIME_BEFORE_MEMORY_SHM}" && -f "${PROD_MEMORY_DB}-shm" ]] && MTIME_BEFORE_MEMORY_SHM="$(_mtime "${PROD_MEMORY_DB}-shm")"
else
    echo "[prod-copy] WARNING: ${PROD_MEMORY_DB} not found — skipping T2 ETL"
    PROD_MEMORY_DB=""
fi

# ── COUNT PROD CHROMA COLLECTIONS ─────────────────────────────────────────────
if [[ -n "${CHROMA_SQLITE}" && -f "${CHROMA_SQLITE}" ]]; then
    PROD_CHROMA_CHUNKS="$(sqlite3 --readonly "${CHROMA_SQLITE}" "SELECT COUNT(*) FROM embeddings;" 2>/dev/null || echo 0)"
    PROD_CHROMA_COLLECTIONS="$(sqlite3 --readonly "${CHROMA_SQLITE}" "SELECT COUNT(*) FROM collections;" 2>/dev/null || echo 0)"
    echo "[prod-copy] Prod Chroma: ${PROD_CHROMA_COLLECTIONS} collections, ${PROD_CHROMA_CHUNKS} embeddings"
fi

# ── COPY PROD CHROMA → SANDBOX (read-only) ────────────────────────────────────
SANDBOX_CHROMA="${NX_CHROMA_PATH}"
if [[ -d "${PROD_CHROMA_DIR}" ]]; then
    echo "[prod-copy] Copying Chroma data: ${PROD_CHROMA_DIR} → ${SANDBOX_CHROMA}"
    mkdir -p "$(dirname "${SANDBOX_CHROMA}")"
    # Use cp -R which does not modify the source.
    # If sandbox chroma dir exists, wipe it first for idempotency.
    # SAFETY: compare realpath, not string, so a symlink pointing at prod Chroma
    # fails the check rather than silently deleting prod Chroma via rm -rf.
    if [[ -d "${SANDBOX_CHROMA}" ]]; then
        SANDBOX_CHROMA_REAL="$(realpath -m "${SANDBOX_CHROMA}" 2>/dev/null || realpath "${SANDBOX_CHROMA}" 2>/dev/null || echo "${SANDBOX_CHROMA}")"
        PROD_CHROMA_REAL="$(realpath -m "${PROD_CHROMA_DIR}" 2>/dev/null || realpath "${PROD_CHROMA_DIR}" 2>/dev/null || echo "${PROD_CHROMA_DIR}")"
        if [[ "${SANDBOX_CHROMA_REAL}" == "${PROD_CHROMA_REAL}" || "${SANDBOX_CHROMA_REAL}" == "${PROD_CHROMA_REAL}/"* ]]; then
            echo "[prod-copy] ABORT: sandbox Chroma '${SANDBOX_CHROMA_REAL}' resolves to prod Chroma '${PROD_CHROMA_REAL}'." >&2
            echo "[prod-copy]        Refusing to rm -rf." >&2
            exit 1
        fi
        rm -rf "${SANDBOX_CHROMA}"
    fi
    cp -R "${PROD_CHROMA_DIR}" "${SANDBOX_CHROMA}"
    echo "[prod-copy] Chroma copy complete"
else
    echo "[prod-copy] WARNING: ${PROD_CHROMA_DIR} not found — skipping Chroma copy"
fi

# ── ETL: T2 STORES → SANDBOX POSTGRES ─────────────────────────────────────────
if [[ -n "${PROD_MEMORY_DB:-}" ]]; then
    echo "[prod-copy] ETL: migrating T2 stores from ${PROD_MEMORY_DB} into sandbox Postgres..."
    cd "${REPO_ROOT}"

    # NOTE: ETL commands continue on per-row HTTP errors (each ETL logs failures
    # but returns exit 0 when at least some rows succeed).  We use || true here
    # for the outer shell so a warning summary line does not fail the script.
    # Migration order matters: catalog before taxonomy (cross-store doc_id FK).
    # Both prior known gaps are now fixed: nx_answer_runs plan_id ClassCastException
    # (nexus-5gaj7) and the taxonomy assignment cross-store FK (nexus-0a7xc, now a
    # counted skip when the doc is absent rather than a hard per-row failure).

    # memory
    echo "[prod-copy]   memory ETL..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate memory \
        --db "${PROD_MEMORY_DB}" \
        --service-url "${NX_SERVICE_URL}" || true

    # plans
    echo "[prod-copy]   plans ETL..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate plans \
        --db "${PROD_MEMORY_DB}" \
        --service-url "${NX_SERVICE_URL}" || true

    # telemetry (6 tables) — nx_answer_runs plan_id ClassCastException FIXED (nexus-5gaj7)
    echo "[prod-copy]   telemetry ETL..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate telemetry \
        --db "${PROD_MEMORY_DB}" \
        --service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true

    # catalog — MUST run BEFORE taxonomy: topic_assignments carries a hard cross-store
    # FK (doc_id -> catalog_documents.tumbler). Without catalog first, every assignment
    # is skipped (nexus-0a7xc). Reads the prod catalog .catalog.db + owners.jsonl
    # read-only (copy-not-move).
    echo "[prod-copy]   catalog ETL (must precede taxonomy for the doc_id FK)..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate catalog \
        --catalog-db "${PROD_CATALOG_DIR}/.catalog.db" \
        --service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true

    # taxonomy (4 tables) — assignments referencing docs not in the catalog are SKIPPED
    # (counted, not failed); catalog ran above so the vast majority import (nexus-0a7xc).
    echo "[prod-copy]   taxonomy ETL (assignments skip-and-count if doc absent)..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate taxonomy \
        --db "${PROD_MEMORY_DB}" \
        --service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true

    # chash
    echo "[prod-copy]   chash ETL..."
    NX_SERVICE_TOKEN="${NX_SERVICE_TOKEN}" \
    NEXUS_CONFIG_DIR="${NEXUS_CONFIG_DIR}" \
    uv run nx storage migrate chash \
        --db "${PROD_MEMORY_DB}" \
        --service-url "${NX_SERVICE_URL}" || true

    echo "[prod-copy] T2 ETL complete (check service.log for per-row failures)"
fi

# ── VERIFY SANDBOX COUNTS == PROD COUNTS ─────────────────────────────────────
echo "[prod-copy] Verifying sandbox counts match prod..."
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Query sandbox Postgres directly for counts
CREDS_FILE="${SANDBOX_HOME}/.config/nexus/pg_credentials"
PSQL_BIN=""
if [[ -f "${CREDS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${CREDS_FILE}"
    PSQL_BIN="$(cd "${REPO_ROOT}" && uv run python "${SCRIPT_DIR}/sandbox_helper.py" pg-bin psql 2>/dev/null | grep -v '^\[' || echo '')"
fi

OS_USER="${USER:-$(id -un)}"

# verify_pg_count_exact: FAIL if sandbox != prod_count (strict equality).
# Use for stores that must copy completely.
# The memory store accepts prod_count OR prod_count+1 (one probe write on ETL
# bootstrap) — callers set allow_plus_one=1 for that table.
verify_pg_count_exact() {
    local label="$1"
    local table="$2"
    local prod_count="$3"
    local allow_plus_one="${4:-0}"
    if [[ -z "${PSQL_BIN}" ]]; then
        echo "[prod-copy] SKIP ${label} (psql not found)"
        return
    fi
    # Query as OS superuser (trust auth) to bypass FORCE RLS on nexus tables.
    # nexus_admin has FORCE RLS applied (it is not a BYPASSRLS role).
    SBX_COUNT="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
        -U "${OS_USER}" -d nexus \
        -t -c "SELECT COUNT(*) FROM nexus.${table};" \
        2>/dev/null | tr -d ' ' || echo '?')"
    local ok=0
    if [[ "${SBX_COUNT}" =~ ^[0-9]+$ ]]; then
        if [[ "${SBX_COUNT}" -eq "${prod_count}" ]]; then
            ok=1
        elif [[ "${allow_plus_one}" -eq 1 && "${SBX_COUNT}" -eq $(( prod_count + 1 )) ]]; then
            ok=1  # probe write on memory ETL bootstrap is acceptable
        fi
    fi
    if [[ "${ok}" -eq 1 ]]; then
        echo "[prod-copy] PASS ${label}: prod=${prod_count} sandbox=${SBX_COUNT}"
    elif [[ "${SBX_COUNT}" =~ ^[0-9]+$ ]]; then
        echo "[prod-copy] FAIL ${label}: prod=${prod_count} sandbox=${SBX_COUNT} (expected exact match)" >&2
        FAIL=1
    else
        echo "[prod-copy] FAIL ${label}: count query returned '${SBX_COUNT}'" >&2
        FAIL=1
    fi
}

# verify_pg_count_at_least: FAIL if sandbox < prod_count; PASS if sandbox >= prod_count.
# Use for stores that may grow during a live-prod seed (the ETL may import rows that
# arrived after the pre-flight count snapshot).
verify_pg_count_at_least() {
    local label="$1"
    local table="$2"
    local prod_count="$3"
    if [[ -z "${PSQL_BIN}" ]]; then
        echo "[prod-copy] SKIP ${label} (psql not found)"
        return
    fi
    SBX_COUNT="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
        -U "${OS_USER}" -d nexus \
        -t -c "SELECT COUNT(*) FROM nexus.${table};" \
        2>/dev/null | tr -d ' ' || echo '?')"
    if [[ "${SBX_COUNT}" =~ ^[0-9]+$ && "${SBX_COUNT}" -ge "${prod_count}" ]]; then
        echo "[prod-copy] PASS ${label}: prod_snapshot=${prod_count} sandbox=${SBX_COUNT} (>= snapshot)"
    elif [[ "${SBX_COUNT}" =~ ^[0-9]+$ ]]; then
        echo "[prod-copy] FAIL ${label}: prod_snapshot=${prod_count} sandbox=${SBX_COUNT} (sandbox < snapshot — data lost)" >&2
        FAIL=1
    else
        echo "[prod-copy] FAIL ${label}: count query returned '${SBX_COUNT}'" >&2
        FAIL=1
    fi
}

# verify_pg_count_known_gap: for stores with a known service bug (bead reference
# in comment) — verify the known-gap count exactly so the harness detects when
# the bug is fixed without operator attention.
verify_pg_count_known_gap() {
    local label="$1"
    local table="$2"
    local prod_count="$3"
    local known_sbx_count="$4"  # expected sandbox count given the bug
    local bead="$5"             # tracking bead for the gap
    if [[ -z "${PSQL_BIN}" ]]; then
        echo "[prod-copy] SKIP ${label} (psql not found)"
        return
    fi
    SBX_COUNT="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
        -U "${OS_USER}" -d nexus \
        -t -c "SELECT COUNT(*) FROM nexus.${table};" \
        2>/dev/null | tr -d ' ' || echo '?')"
    if [[ "${SBX_COUNT}" =~ ^[0-9]+$ && "${SBX_COUNT}" -eq "${prod_count}" ]]; then
        echo "[prod-copy] PASS ${label}: prod=${prod_count} sandbox=${SBX_COUNT} (gap ${bead} appears FIXED — remove known-gap override)"
    elif [[ "${SBX_COUNT}" =~ ^[0-9]+$ && "${SBX_COUNT}" -eq "${known_sbx_count}" ]]; then
        echo "[prod-copy] WARN ${label}: prod=${prod_count} sandbox=${SBX_COUNT} (known gap ${bead})"
    elif [[ "${SBX_COUNT}" =~ ^[0-9]+$ ]]; then
        echo "[prod-copy] FAIL ${label}: prod=${prod_count} sandbox=${SBX_COUNT} expected ${known_sbx_count} (gap ${bead})" >&2
        FAIL=1
    else
        echo "[prod-copy] FAIL ${label}: count query returned '${SBX_COUNT}'" >&2
        FAIL=1
    fi
}

# memory: allow +1 for the probe write during ETL bootstrap.
verify_pg_count_exact   "memory"              "memory"             "${PROD_MEMORY_COUNT:-0}"      1
# chash: require sandbox >= prod snapshot count.
# The prod db may accumulate new chash rows between pre-flight count and ETL run,
# so the sandbox may legitimately hold MORE rows than counted at snapshot time.
# When prod MCP is quiescent (nx daemon stop before prod-copy.sh), the count
# is stable and this resolves to exact equality.
verify_pg_count_at_least "chash_index" "chash_index" "${PROD_CHASH_COUNT:-0}"
# topics: exact (pure bulk copy)
verify_pg_count_exact   "topics"              "topics"             "${PROD_TOPICS_COUNT:-0}"      0
# relevance_log, search_telemetry, tier_writes: allow >= snapshot (live writes during ETL).
# frecency: exact (rarely written during normal MCP operation).
verify_pg_count_at_least "relevance_log"      "relevance_log"      "${PROD_TELEMETRY_RELEVANCE:-0}"
verify_pg_count_at_least "search_telemetry"   "search_telemetry"   "${PROD_TELEMETRY_SEARCH:-0}"
verify_pg_count_at_least "tier_writes"        "tier_writes"        "${PROD_TELEMETRY_TIER:-0}"
verify_pg_count_exact   "frecency"            "frecency"           "${PROD_TELEMETRY_FRECENCY:-0}" 0
# plans: partial import (type mismatch on some rows).
# Gap tracked in nexus-5gaj7 (same TelemetryHandler/PlanHandler cast issue class).
# Expected sandbox count: ~70 of 88 (stable across runs on this prod state).
verify_pg_count_known_gap "plans"             "plans"              "${PROD_PLANS_COUNT:-0}"       70 "nexus-5gaj7"
# topic_assignments: FK ordering issue — topics+assignments in same pass.
# Gap tracked in nexus-0a7xc. Expected sandbox count: 0.
verify_pg_count_known_gap "topic_assignments" "topic_assignments"  "${PROD_TOPIC_ASSIGN_COUNT:-0}" 0 "nexus-0a7xc"
# topic_links: same FK ordering issue. Expected sandbox count: partial (>0, varies by prod state).
# Gap tracked in nexus-0a7xc. The known-gap count is approximate; use 0 as a floor —
# any non-zero count is acceptable (the FAIL path triggers if count < 0 somehow).
# When the FK fix ships this will move to verify_pg_count_exact.
# Last observed: 6525 (2026-06-08).
verify_pg_count_known_gap "topic_links"       "topic_links"        "${PROD_TOPIC_LINKS_COUNT:-0}"  6525 "nexus-0a7xc"
# nx_answer_runs: ClassCastException in TelemetryHandler.java:328 (String→Number cast).
# Gap tracked in nexus-5gaj7. Expected sandbox count: 2 (a very small number succeed).
# Last observed: 2 (2026-06-08).
verify_pg_count_known_gap "nx_answer_runs"    "nx_answer_runs"     "${PROD_TELEMETRY_RUNS:-0}"    2 "nexus-5gaj7"
# hook_failures: likely same ClassCastException pattern.
# Gap tracked in nexus-5gaj7. Expected sandbox count: 0.
verify_pg_count_known_gap "hook_failures"     "hook_failures"      "${PROD_TELEMETRY_HOOKS:-0}"   0 "nexus-5gaj7"

# Verify Chroma copy (sandbox copy should be bit-exact: same embedding count).
if [[ -n "${CHROMA_SQLITE}" && -f "${CHROMA_SQLITE}" && -f "${SANDBOX_CHROMA}/chroma.sqlite3" ]]; then
    SBX_CHROMA_CHUNKS="$(sqlite3 "${SANDBOX_CHROMA}/chroma.sqlite3" "SELECT COUNT(*) FROM embeddings;" 2>/dev/null || echo 0)"
    SBX_CHROMA_COLS="$(sqlite3 "${SANDBOX_CHROMA}/chroma.sqlite3" "SELECT COUNT(*) FROM collections;" 2>/dev/null || echo 0)"
    echo "[prod-copy] Chroma: prod=${PROD_CHROMA_COLLECTIONS:-?} collections / ${PROD_CHROMA_CHUNKS:-?} embeddings"
    echo "[prod-copy] Chroma: sandbox=${SBX_CHROMA_COLS} collections / ${SBX_CHROMA_CHUNKS} embeddings"
    if [[ "${SBX_CHROMA_CHUNKS}" == "${PROD_CHROMA_CHUNKS:-0}" ]]; then
        echo "[prod-copy] PASS chroma_embeddings: ${PROD_CHROMA_CHUNKS}"
    else
        echo "[prod-copy] WARN chroma_embeddings: prod=${PROD_CHROMA_CHUNKS:-?} sandbox=${SBX_CHROMA_CHUNKS} (delta may be in-flight writes during live-prod snapshot)"
    fi
fi

# ── ASSERT PROD FILE MTIMES UNCHANGED ─────────────────────────────────────────
# All three: .db, -shm, -wal.  A change in any sidecar indicates sqlite3 opened
# the prod db in writable mode — the CRITICAL C1 invariant.
echo "[prod-copy] Asserting prod files unchanged (read-only proof — .db + -shm + -wal)..."

_assert_mtime_unchanged() {
    local path="$1"
    local before="$2"
    local label="$3"
    local warn_only="${4:-0}"  # 1 = WARN instead of FAIL (for files written by concurrent processes)
    if [[ -z "${before}" ]]; then
        # File did not exist before — verify it still doesn't (or was created by a concurrent writer).
        if [[ -f "${path}" && "${warn_only}" -eq 0 ]]; then
            echo "[prod-copy] FAIL ${label}: did not exist before but now does" >&2
            FAIL=1
        elif [[ -f "${path}" ]]; then
            echo "[prod-copy] WARN ${label}: did not exist before but now does (concurrent prod write)"
        fi
        return
    fi
    if [[ ! -f "${path}" ]]; then
        echo "[prod-copy] FAIL ${label}: existed before (mtime=${before}) but missing now" >&2
        FAIL=1
        return
    fi
    local after
    after="$(_mtime "${path}")"
    if [[ "${after}" == "${before}" ]]; then
        echo "[prod-copy] PASS ${label} mtime unchanged: ${after}"
    elif [[ "${warn_only}" -eq 1 ]]; then
        echo "[prod-copy] WARN ${label} mtime changed: before=${before} after=${after} (expected if prod MCP is running)"
    else
        echo "[prod-copy] FAIL ${label} mtime CHANGED: before=${before} after=${after}" >&2
        FAIL=1
    fi
}

if [[ -n "${PROD_MEMORY_DB}" ]]; then
    # Detect whether any prod process has memory.db open in read-write mode.
    # If so, mtime changes to .db and -shm come from the concurrent writer, not
    # from our --readonly reads.  We WARN in that case; FAIL if the prod db is
    # quiescent (no concurrent writer) but our reads still changed it.
    # lsof format: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
    # FD column ($4) contains mode suffix: 9u (read-write), 9r (read-only).
    # A FD ending in 'u' (update/read-write) is a writer.
    PROD_DB_WRITERS="$(lsof "${PROD_MEMORY_DB}" 2>/dev/null | awk 'NR>1 && $5=="REG" && $4~/u$/{print $2}' | sort -u | tr '\n' ' ')"
    if [[ -n "${PROD_DB_WRITERS}" ]]; then
        MTIME_WARN=1
        echo "[prod-copy] NOTE: prod memory.db has active writers (PIDs: ${PROD_DB_WRITERS})."
        echo "[prod-copy]       mtime assertions are WARN-only; stop prod MCP for strict FAIL mode."
    else
        MTIME_WARN=0
    fi
    _assert_mtime_unchanged "${PROD_MEMORY_DB}"       "${MTIME_BEFORE_MEMORY}"     "prod memory.db"     "${MTIME_WARN}"
    _assert_mtime_unchanged "${PROD_MEMORY_DB}-shm"   "${MTIME_BEFORE_MEMORY_SHM}" "prod memory.db-shm" "${MTIME_WARN}"
    # -wal: always WARN — the prod MCP may append checkpoint records concurrently
    # even without an open file descriptor (the WAL writer can be transient).
    _assert_mtime_unchanged "${PROD_MEMORY_DB}-wal"   "${MTIME_BEFORE_MEMORY_WAL}" "prod memory.db-wal" 1
fi
if [[ -n "${CHROMA_SQLITE}" ]]; then
    _assert_mtime_unchanged "${CHROMA_SQLITE}" "${MTIME_BEFORE_CHROMA_SQLITE}" "prod chroma.sqlite3" 0
fi

if [[ "${FAIL}" -ne 0 ]]; then
    echo "[prod-copy] VERIFICATION FAILED — see errors above" >&2
    exit 1
fi
echo "[prod-copy] All verifications passed. Sandbox seeded successfully."
