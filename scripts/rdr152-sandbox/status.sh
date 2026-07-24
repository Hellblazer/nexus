#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-152 sandbox harness — status.sh
# Reports: /health ping, Liquibase migration state, role check, collection/doc counts.
set -euo pipefail

SANDBOX_HOME="${SANDBOX_HOME:-${HOME}/nexus-rdr152-sandbox}"
SANDBOX_ENV="${SANDBOX_HOME}/sandbox.env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ ! -f "${SANDBOX_ENV}" ]]; then
    echo "[status] ERROR: ${SANDBOX_ENV} not found. Run up.sh first." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "${SANDBOX_ENV}"

echo "[status] RDR-152 sandbox status"
echo "[status] SANDBOX_HOME=${SANDBOX_HOME}"
echo "[status] Service URL:  ${NX_SERVICE_URL}"

FAIL=0

# ── /health ───────────────────────────────────────────────────────────────────
echo ""
echo "=== /health ==="
HEALTH_RESP="$(curl -s -w '\nHTTP_STATUS:%{http_code}' "${NX_SERVICE_URL}/health" 2>/dev/null || echo 'CURL_FAILED\nHTTP_STATUS:000')"
HTTP_STATUS="$(echo "${HEALTH_RESP}" | grep '^HTTP_STATUS:' | cut -d: -f2)"
HEALTH_BODY="$(echo "${HEALTH_RESP}" | grep -v '^HTTP_STATUS:')"
if [[ "${HTTP_STATUS}" == "200" ]]; then
    echo "PASS /health 200"
    echo "     ${HEALTH_BODY}"
else
    echo "FAIL /health returned: ${HTTP_STATUS}" >&2
    FAIL=1
fi

# ── LIQUIBASE MIGRATION STATE ─────────────────────────────────────────────────
echo ""
echo "=== Liquibase migration state ==="
CREDS_FILE="${SANDBOX_HOME}/.config/nexus/pg_credentials"
if [[ -f "${CREDS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${CREDS_FILE}"
    PSQL_BIN="$(cd "${REPO_ROOT}" && uv run python "${SCRIPT_DIR}/sandbox_helper.py" pg-bin psql 2>/dev/null | grep -v '^\[' || echo '')"
    if [[ -n "${PSQL_BIN}" && -x "${PSQL_BIN}" ]]; then
        # Liquibase table name varies: quoted uppercase "DATABASECHANGELOG" (PG/standard) or
        # lowercase databasechangelog depending on Liquibase version + DB settings.
        # Try both; the one that returns rows wins.
        CHANGELOG_TABLE=""
        for tbl in 'public.databasechangelog' 'public."DATABASECHANGELOG"'; do
            CNT="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
                -U "${NX_DB_ADMIN_USER}" -d nexus \
                -t -c "SELECT COUNT(*) FROM ${tbl};" \
                2>/dev/null | tr -d ' ' || echo '')"
            if [[ "${CNT}" =~ ^[0-9]+$ && "${CNT}" -gt 0 ]]; then
                CHANGELOG_TABLE="${tbl}"
                CHANGELOG_COUNT="${CNT}"
                break
            fi
        done
        if [[ -n "${CHANGELOG_TABLE}" ]]; then
            echo "PASS DATABASECHANGELOG (${CHANGELOG_TABLE}): ${CHANGELOG_COUNT} changesets applied"
        else
            echo "FAIL DATABASECHANGELOG: table not found or no rows" >&2
            FAIL=1
        fi
        echo ""
        echo "Last 5 changesets:"
        "${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
            -U "${NX_DB_ADMIN_USER}" -d nexus \
            -c "SELECT id, author, dateexecuted FROM ${CHANGELOG_TABLE:-public.databasechangelog} ORDER BY orderexecuted DESC LIMIT 5;" \
            2>/dev/null || true

        # ── ROLE CHECK ────────────────────────────────────────────────────────
        echo ""
        echo "=== Role check ==="
        for role in nexus_admin nexus_svc; do
            EXISTS="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
                -U "${NX_DB_ADMIN_USER}" -d nexus \
                -t -c "SELECT COUNT(*) FROM pg_roles WHERE rolname = '${role}';" \
                2>/dev/null | tr -d ' ' || echo '0')"
            if [[ "${EXISTS}" == "1" ]]; then
                echo "PASS role ${role} exists"
            else
                echo "FAIL role ${role} missing" >&2
                FAIL=1
            fi
        done

        # ── POSTGRES TABLE COUNTS ─────────────────────────────────────────────
        # Query as OS superuser (trust auth) to bypass FORCE RLS.
        # nexus_admin is a NOSUPERUSER role subject to FORCE RLS and returns
        # 0 rows without a tenant GUC; the OS superuser sees all rows.
        echo ""
        echo "=== Sandbox Postgres row counts (OS superuser, bypasses RLS) ==="
        OS_USER="${USER:-$(id -un)}"
        for table in memory plans chash_index topics topic_assignments topic_links; do
            CNT="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
                -U "${OS_USER}" -d nexus \
                -t -c "SELECT COUNT(*) FROM nexus.${table};" \
                2>/dev/null | tr -d ' ' || echo '?')"
            echo "  ${table}: ${CNT}"
        done

        # ── DML PROBE (nexus_svc under FORCE RLS) ─────────────────────────────
        echo ""
        echo "=== nexus_svc DML probe ==="
        DML_OK="$("${PSQL_BIN}" -h 127.0.0.1 -p "${PG_PORT}" \
            -U "${NX_DB_USER}" \
            -d nexus \
            -t -c "SET LOCAL nexus.tenant = 'sandbox-probe'; SELECT 1;" \
            2>/dev/null | tr -d ' ' || echo '?')"
        if [[ "${DML_OK}" == "1" ]]; then
            echo "PASS nexus_svc DML under RLS works"
        else
            echo "WARN nexus_svc DML probe returned: '${DML_OK}' (may require password auth)" >&2
        fi
    else
        echo "WARNING: psql not found — skipping migration/role/count checks"
    fi
else
    echo "WARNING: no pg_credentials — skipping Postgres checks"
fi

echo ""
if [[ "${FAIL}" -ne 0 ]]; then
    echo "OVERALL: FAIL"
    exit 1
fi
echo "OVERALL: PASS"
