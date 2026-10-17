#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# cc-validation, container half (nexus-ueztp). run.sh bind-mounts the staged
# harness at /stage (read-only) and an evidence directory at /evidence; this
# copies the harness somewhere writable, adds the one thing runner.sh insists on
# (a .venv/bin/python, which only the MCP scenarios use), and runs the named
# scenario(s). The automation token is already in this process's environment
# (docker run -e CLAUDE_CODE_OAUTH_TOKEN, under claude_credentials.py run --);
# it is checked for presence here and never printed.
set -euo pipefail

scenario="${1:?usage: entrypoint_in_container.sh <scenario-number-or-comma-list>}"

if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    echo "CLAUDE_CODE_OAUTH_TOKEN did not reach the container's environment" >&2
    exit 2
fi
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ANTHROPIC_API_KEY is set in the container; it outranks the automation token" >&2
    exit 2
fi

REPO=/home/nexus/repo
rm -rf "$REPO"; mkdir -p "$REPO"
cp -R /stage/. "$REPO"/
mkdir -p "$REPO/.venv/bin"
ln -sf "$(command -v python3)" "$REPO/.venv/bin/python"

claude_version_out=$(claude --version 2>&1)
printf '%s\n' "${claude_version_out%%$'\n'*}" > /evidence/claude-version.txt
echo "[container] claude $(cat /evidence/claude-version.txt) (pinned: ${CC_VAL_EXPECTED_CLAUDE_VERSION:-unset})"
echo "[container] tmux $(tmux -V)"
echo "[container] current plugin $(python3 -c 'import json;print(json.load(open("'"$REPO"'/conexus/.claude-plugin/plugin.json"))["version"])')"
if [[ -d /control ]]; then
    export CC_VAL_CONTROL_PLUGIN_ROOT=/control/conexus
    echo "[container] control plugin $(python3 -c 'import json;print(json.load(open("/control/conexus/.claude-plugin/plugin.json"))["version"])')"
fi

export NX_CC_VAL_IN_CONTAINER=1
export CC_VAL_EVIDENCE_DIR=/evidence
export NX_NO_TELEMETRY=1
export TMPDIR=/tmp

set +e
"$REPO/tests/cc-validation/runner.sh" --scenario "$scenario"
rc=$?
set -e
chmod -R a+rwX /evidence 2>/dev/null || true
exit $rc
