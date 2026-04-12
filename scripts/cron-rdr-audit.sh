#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# rdr-audit cron/launchd wrapper
#
# Invokes `claude -p '/nx:rdr-audit <PROJECT>'` in a headless Claude Code
# session, rotates the output log, and writes all output to
# ~/.local/state/rdr-audit/<PROJECT>.log.
#
# Called by:
#   - scripts/launchd/com.nexus.rdr-audit.PROJECT.plist (macOS)
#   - scripts/cron/rdr-audit.crontab (Linux)
#
# Requires:
#   PROJECT env var — the target project name (e.g. ART, nexus)
#
# Exits non-zero if PROJECT is unset so install errors surface early.

set -euo pipefail

if [[ -z "${PROJECT:-}" ]]; then
  echo "ERROR: PROJECT env var is required" >&2
  echo "       Set it in your crontab line or launchd EnvironmentVariables block." >&2
  exit 1
fi

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo /usr/local/bin/claude)}"

if [[ ! -x "${CLAUDE_BIN}" ]]; then
  echo "ERROR: claude binary not found or not executable at ${CLAUDE_BIN}" >&2
  echo "       Set CLAUDE_BIN env var to the absolute path of the claude CLI." >&2
  exit 1
fi

LOG_DIR="${HOME}/.local/state/rdr-audit"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/${PROJECT}.log"

# Rotate log if it exceeds 10MB. Keep one previous file as .1.
if [[ -f "${LOG_FILE}" ]]; then
  size=$(wc -c < "${LOG_FILE}" | tr -d '[:space:]')
  if [[ "${size}" -gt 10485760 ]]; then
    mv "${LOG_FILE}" "${LOG_FILE}.1"
  fi
fi

{
  echo "=== rdr-audit run: $(date -u +%Y-%m-%dT%H:%M:%SZ) project=${PROJECT} ==="
  exec "${CLAUDE_BIN}" -p "/nx:rdr-audit ${PROJECT}"
} >> "${LOG_FILE}" 2>&1
