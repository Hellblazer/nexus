#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-152 sandbox harness — down.sh
# Gracefully stops the Java service, Postgres, and (if any) chroma child.
# Pass --purge to delete SANDBOX_HOME entirely.
set -euo pipefail

SANDBOX_HOME="${SANDBOX_HOME:-${HOME}/nexus-rdr152-sandbox}"
SANDBOX_ENV="${SANDBOX_HOME}/sandbox.env"
SERVICE_PID_FILE="${SANDBOX_HOME}/service.pid"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PURGE=0

for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE=1 ;;
        *) echo "[down] Unknown argument: ${arg}" >&2; exit 1 ;;
    esac
done

echo "[down] RDR-152 sandbox teardown"
echo "[down] SANDBOX_HOME=${SANDBOX_HOME}"

# ── STOP JAVA SERVICE ─────────────────────────────────────────────────────────
if [[ -f "${SERVICE_PID_FILE}" ]]; then
    SVC_PID="$(cat "${SERVICE_PID_FILE}")"
    if kill -0 "${SVC_PID}" 2>/dev/null; then
        echo "[down] Stopping Java service (PID ${SVC_PID})..."
        # Graceful SIGTERM first; the service installs a shutdown hook
        kill -SIGTERM "${SVC_PID}" 2>/dev/null || true
        # Wait up to 15s
        for i in $(seq 1 30); do
            if ! kill -0 "${SVC_PID}" 2>/dev/null; then
                echo "[down] Service stopped (${i}*0.5s)"
                break
            fi
            sleep 0.5
        done
        # Force kill if still alive
        if kill -0 "${SVC_PID}" 2>/dev/null; then
            echo "[down] Service did not stop gracefully; sending SIGKILL..."
            kill -SIGKILL "${SVC_PID}" 2>/dev/null || true
        fi
    else
        echo "[down] Service PID ${SVC_PID} not running"
    fi
    rm -f "${SERVICE_PID_FILE}"
else
    echo "[down] No service PID file found"
fi

# ── STOP POSTGRES ─────────────────────────────────────────────────────────────
SANDBOX_CONFIG="${SANDBOX_HOME}/.config/nexus"
CREDS_FILE="${SANDBOX_CONFIG}/pg_credentials"

if [[ -f "${CREDS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${CREDS_FILE}"
    PG_BIN_DIR="$(cd "${REPO_ROOT}" && uv run python "${SCRIPT_DIR}/sandbox_helper.py" pg-bin bin_dir 2>/dev/null | grep -v '^\[' | tr -d '\n' || echo '')"
    if [[ -n "${PG_BIN_DIR}" && -d "${PG_DATA:-}" ]]; then
        echo "[down] Stopping Postgres cluster at ${PG_DATA}..."
        "${PG_BIN_DIR}/pg_ctl" -D "${PG_DATA}" stop -m fast 2>/dev/null || true
        echo "[down] Postgres stopped"
    else
        echo "[down] pg_ctl or PG_DATA not found — skipping Postgres stop"
    fi
else
    echo "[down] No pg_credentials found — skipping Postgres stop"
fi

# ── PURGE ─────────────────────────────────────────────────────────────────────
if [[ "${PURGE}" -eq 1 ]]; then
    echo "[down] --purge: removing ${SANDBOX_HOME}..."
    rm -rf "${SANDBOX_HOME}"
    echo "[down] Purge complete"
fi

echo "[down] Sandbox teardown complete"
