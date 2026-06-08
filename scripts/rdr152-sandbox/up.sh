#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-152 sandbox harness — up.sh
# Provisions a fully isolated Postgres + Chroma + Java service sandbox.
# NEVER touches prod ~/.config/nexus.
#
# Usage:
#   SANDBOX_HOME=/path/to/sandbox ./up.sh
#
# Environment:
#   SANDBOX_HOME  Default: ~/nexus-rdr152-sandbox
set -euo pipefail

SANDBOX_HOME="${SANDBOX_HOME:-${HOME}/nexus-rdr152-sandbox}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_CONFIG="${HOME}/.config/nexus"
SANDBOX_CONFIG="${SANDBOX_HOME}/.config/nexus"
SANDBOX_ENV="${SANDBOX_HOME}/sandbox.env"
SERVICE_PID_FILE="${SANDBOX_HOME}/service.pid"

echo "[up] RDR-152 sandbox harness"
echo "[up] SANDBOX_HOME=${SANDBOX_HOME}"
echo "[up] REPO_ROOT=${REPO_ROOT}"

# ── HARD PROD-TOUCH GUARD ─────────────────────────────────────────────────────
# Resolve both to realpaths; abort if sandbox config is equal to or under prod.
mkdir -p "${SANDBOX_CONFIG}"
PROD_REAL="$(realpath "${PROD_CONFIG}" 2>/dev/null || echo "${PROD_CONFIG}")"
SANDBOX_REAL="$(realpath "${SANDBOX_CONFIG}")"

if [[ "${SANDBOX_REAL}" == "${PROD_REAL}" || "${SANDBOX_REAL}" == "${PROD_REAL}/"* ]]; then
    echo ""
    echo "  ABORT: sandbox config dir '${SANDBOX_REAL}'" >&2
    echo "         is equal to or under prod '${PROD_REAL}'." >&2
    echo "  Set SANDBOX_HOME to a path outside ~/.config/nexus." >&2
    echo ""
    exit 1
fi
echo "[up] Prod-touch guard PASSED"
echo "[up]   sandbox=${SANDBOX_REAL}"
echo "[up]   prod   =${PROD_REAL}"

# ── REDIRECT ALL NX CONFIG PATHS INTO SANDBOX ─────────────────────────────────
export NEXUS_CONFIG_DIR="${SANDBOX_CONFIG}"
export XDG_CONFIG_HOME="${SANDBOX_HOME}/.config"
export NX_CONFIG_HOME="${SANDBOX_HOME}/.config"
export NX_DB_PATH="${SANDBOX_CONFIG}/memory.db"

# ── PROVISION ISOLATED POSTGRES ───────────────────────────────────────────────
echo "[up] Provisioning isolated Postgres cluster..."
CREDS_FILE="${SANDBOX_CONFIG}/pg_credentials"

# Helper prints structlog to stderr, JSON to stdout — capture separately.
PROVISION_JSON="$(cd "${REPO_ROOT}" && uv run python "${SCRIPT_DIR}/sandbox_helper.py" provision \
    --config-dir "${SANDBOX_CONFIG}" 2>/dev/null)"
if [[ -z "${PROVISION_JSON}" ]]; then
    echo "[up] ERROR: pg_provision returned no JSON (check stderr for details):" >&2
    cd "${REPO_ROOT}" && uv run python "${SCRIPT_DIR}/sandbox_helper.py" provision \
        --config-dir "${SANDBOX_CONFIG}" >&2 || true
    exit 1
fi
echo "[up] pg_provision result: ${PROVISION_JSON}"

PG_PORT="$(echo "${PROVISION_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['port'])")"
echo "[up] Postgres port: ${PG_PORT}"

if [[ ! -f "${CREDS_FILE}" ]]; then
    echo "[up] ERROR: pg_credentials not found at ${CREDS_FILE}" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "${CREDS_FILE}"
echo "[up] Postgres ready: PG_PORT=${PG_PORT} PG_DATA=${PG_DATA}"

# ── BUILD JAR ─────────────────────────────────────────────────────────────────
JAR="${REPO_ROOT}/service/target/nexus-service-1.0-SNAPSHOT.jar"
if [[ ! -f "${JAR}" ]]; then
    echo "[up] Building Java service jar (may take ~60s)..."
    (cd "${REPO_ROOT}/service" && mvn -q package -DskipTests)
    echo "[up] Jar built: ${JAR}"
else
    echo "[up] Using existing jar: ${JAR}"
fi

# ── FIND FREE PORTS ───────────────────────────────────────────────────────────
find_free_port() {
    python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); p=s.getsockname()[1]; s.close(); print(p)"
}
SVC_PORT="$(find_free_port)"
CHROMA_PORT="$(find_free_port)"
echo "[up] Service port:  ${SVC_PORT}"
echo "[up] Chroma port:   ${CHROMA_PORT}"

# ── CHROMA DATA DIR ────────────────────────────────────────────────────────────
CHROMA_PATH="${SANDBOX_CONFIG}/chroma"
mkdir -p "${CHROMA_PATH}"

# ── GENERATE SANDBOX TOKEN ────────────────────────────────────────────────────
SANDBOX_TOKEN="sandbox-rdr152-$(openssl rand -hex 8)"

# ── START JAVA SERVICE ─────────────────────────────────────────────────────────
# The service runs Liquibase at startup (net63 contract):
#   NX_DB_ADMIN_* = nexus_admin (DDL) from pg_credentials
#   NX_DB_*       = nexus_svc   (DML) from pg_credentials
echo "[up] Starting Java service (Liquibase self-migrates on first boot)..."

env \
    NX_SERVICE_PORT="${SVC_PORT}" \
    NX_SERVICE_TOKEN="${SANDBOX_TOKEN}" \
    NX_DB_ADMIN_URL="${NX_DB_ADMIN_URL}" \
    NX_DB_ADMIN_USER="${NX_DB_ADMIN_USER}" \
    NX_DB_ADMIN_PASS="${NX_DB_ADMIN_PASS}" \
    NX_DB_URL="${NX_DB_URL}" \
    NX_DB_USER="${NX_DB_USER}" \
    NX_DB_PASS="${NX_DB_PASS}" \
    NX_POOL_SIZE=4 \
    NX_CHROMA_MODE=local \
    NX_CHROMA_PATH="${CHROMA_PATH}" \
    NX_CHROMA_HTTP_PORT="${CHROMA_PORT}" \
    java -jar "${JAR}" \
    > "${SANDBOX_HOME}/service.log" 2>&1 &

SVC_PID=$!
echo "${SVC_PID}" > "${SERVICE_PID_FILE}"
echo "[up] Service PID: ${SVC_PID} (log: ${SANDBOX_HOME}/service.log)"

# ── WAIT FOR /health 200 ───────────────────────────────────────────────────────
echo "[up] Waiting for /health 200 (up to 120s)..."
HEALTH_URL="http://127.0.0.1:${SVC_PORT}/health"
DEADLINE=$(( $(date +%s) + 120 ))
HTTP_STATUS="000"
while [[ $(date +%s) -lt ${DEADLINE} ]]; do
    HTTP_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "${HEALTH_URL}" 2>/dev/null || echo 000)"
    if [[ "${HTTP_STATUS}" == "200" ]]; then
        break
    fi
    if ! kill -0 "${SVC_PID}" 2>/dev/null; then
        echo "[up] ERROR: Service process (PID ${SVC_PID}) exited unexpectedly." >&2
        echo "[up] Last 50 lines of service.log:" >&2
        tail -50 "${SANDBOX_HOME}/service.log" >&2
        exit 1
    fi
    sleep 1
done
if [[ "${HTTP_STATUS}" != "200" ]]; then
    echo "[up] ERROR: Service did not become healthy within 120s (last status: ${HTTP_STATUS})." >&2
    echo "[up] Last 50 lines of service.log:" >&2
    tail -50 "${SANDBOX_HOME}/service.log" >&2
    exit 1
fi
echo "[up] Service healthy at ${HEALTH_URL}"

# ── WRITE sandbox.env ─────────────────────────────────────────────────────────
cat > "${SANDBOX_ENV}" <<ENV
# RDR-152 sandbox environment. Source before using nx in sandbox mode.
# Generated by up.sh. Isolated from prod.
export SANDBOX_HOME="${SANDBOX_HOME}"
export NEXUS_CONFIG_DIR="${SANDBOX_CONFIG}"
export XDG_CONFIG_HOME="${SANDBOX_HOME}/.config"
export NX_CONFIG_HOME="${SANDBOX_HOME}/.config"
export NX_DB_PATH="${SANDBOX_CONFIG}/memory.db"

# Service
export NX_STORAGE_BACKEND=service
export NX_STORAGE_BACKEND_MEMORY=service
export NX_STORAGE_BACKEND_PLANS=service
export NX_STORAGE_BACKEND_TELEMETRY=service
export NX_STORAGE_BACKEND_TAXONOMY=service
export NX_STORAGE_BACKEND_CHASH=service
export NX_STORAGE_BACKEND_CATALOG=service
export NX_SERVICE_URL=http://127.0.0.1:${SVC_PORT}
export NX_SERVICE_PORT=${SVC_PORT}
export NX_SERVICE_TOKEN=${SANDBOX_TOKEN}

# Postgres (sandbox cluster)
export NX_DB_ADMIN_URL="${NX_DB_ADMIN_URL}"
export NX_DB_ADMIN_USER="${NX_DB_ADMIN_USER}"
export NX_DB_ADMIN_PASS="${NX_DB_ADMIN_PASS}"
export NX_DB_URL="${NX_DB_URL}"
export NX_DB_USER="${NX_DB_USER}"
export NX_DB_PASS="${NX_DB_PASS}"
export PG_PORT="${PG_PORT}"
export PG_DATA="${PG_DATA}"

# Chroma (sandbox)
export NX_CHROMA_MODE=local
export NX_CHROMA_PATH="${CHROMA_PATH}"
export NX_CHROMA_HTTP_PORT=${CHROMA_PORT}
ENV

chmod 600 "${SANDBOX_ENV}"
echo "[up] Wrote ${SANDBOX_ENV}"
echo "[up]"
echo "[up] Sandbox ready."
echo "[up]   source ${SANDBOX_ENV}"
echo "[up]   ./prod-copy.sh     # seed from prod (optional)"
echo "[up]   ./status.sh        # verify"
echo "[up]   ./down.sh          # stop"
echo "[up]   ./down.sh --purge  # stop + delete"
