#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PRE-TAG CLOUD LEG. The release candidate, in a scratch HOME, through the real public edge, against the
# dedicated cloud TEST TENANT, driving the write paths the release adds, compared with the same steps on a
# local engine. A step FAILS when the cloud run returns a 4xx/5xx the local run does not (the 8.5.0 faults:
# the CDN stripped X-Nexus-Identity-Token -> 403 identity_token_required; the data credential could not
# create seats). A red blocks the tag. Logic: tests/e2e/lib/cloud_leg.py; sandbox: lib/candidate_sandbox.sh.
#
# USAGE
#   tests/e2e/cloud-leg.sh --wheel <candidate.whl> --window <id> [--ledger docs/wire-contract-pending.md]
#                          [--release <vX.Y.Z>] [--core-only] [--op-item <title>] [--op-vault AI] [--work-dir <dir>] [--keep]
#   The release additions are the wire ledger's `## Unshipped` entries (or the named --release's `## Shipped`
#   ones), each mapped to a client call or listed SKIPPED. --core-only runs the fixed core alone: use it against an
#   engine that has not deployed the release's routes yet (they would answer 404 in the cloud and 200 locally).
#   tests/e2e/cloud-leg.sh --local-vs-local --wheel <candidate.whl>     # plumbing self-test, no cloud, no window
#
# REFUSES (exit 2, nothing written, before any sandbox exists) unless
#   (a) an announced cloud window is in effect: a marker file <window dir>/<id>.window written by the releaser
#       after the announcement (format and check: lib/cloud_leg.py, "WINDOW"); the window dir is
#       $NX_CLOUD_WINDOW_DIR or ~/.config/nexus-cloud-windows;
#   (b) the credential is the test tenant's: the 1Password item (default "nexus cloud test tenant (release-test)",
#       vault AI, read with op-auto) exists, its title says "test", its mint_tenant is exactly "release-test", and
#       its token is not the operator's own ambient credential (compared by hash; nothing is printed);
#   (c) its service_url is the public https edge.
# The credential reaches the client only through the sandbox runner's environment (NX_SERVICE_URL, NX_MINT_TOKEN,
# NX_MINT_TENANT; NX_SERVICE_TOKEN unset), read from 0600 files; it is never printed or put on a command line.
# CLOUD RUNS NEED AN ANNOUNCED WINDOW. A local-vs-local run touches nothing outside this machine.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
# shellcheck source=lib/candidate_sandbox.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/candidate_sandbox.sh"
e2e_python_resolve 11 || exit 2
LEG="$REPO_ROOT/tests/e2e/lib/cloud_leg.py"

CORE_ONLY=0 WHEEL="" WINDOW="" LEDGER="$REPO_ROOT/docs/wire-contract-pending.md" RELEASE="" KEEP=0 LOCAL_VS_LOCAL=0
OP_ITEM="${NX_CLOUDLEG_OP_ITEM:-nexus cloud test tenant (release-test)}" OP_VAULT="${NX_CLOUDLEG_OP_VAULT:-AI}"
OP_BIN="${NX_CLOUDLEG_OP:-op-auto}" WORK="" RUN_ID="" WORK_AUTO=0
while [ $# -gt 0 ]; do
    case "$1" in
        --wheel) WHEEL="${2:?}"; shift 2 ;;
        --window) WINDOW="${2:?}"; shift 2 ;;
        --ledger) LEDGER="${2:?}"; shift 2 ;;
        --release) RELEASE="${2:?}"; shift 2 ;;
        --op-item) OP_ITEM="${2:?}"; shift 2 ;;
        --op-vault) OP_VAULT="${2:?}"; shift 2 ;;
        --work-dir) WORK="${2:?}"; shift 2 ;;
        --run-id) RUN_ID="${2:?}"; shift 2 ;;
        --core-only) CORE_ONLY=1; shift ;;
        --keep) KEEP=1; shift ;;
        --local-vs-local) LOCAL_VS_LOCAL=1; shift ;;
        -h|--help) sed -n 2,26p "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "cloud-leg: unknown argument: $1" >&2; exit 2 ;;
    esac
done

CREDD=""
# shellcheck disable=SC2329  # invoked through the EXIT trap
cleanup() {
    local rc=$?
    set +e
    cand_engine_stop
    if [ "$KEEP" = 0 ]; then
        [ -n "${WORK:-}" ] && [ -f "$WORK/local/env.sh" ] && cand_sandbox_destroy "$WORK/local" >/dev/null 2>&1
        [ -n "${WORK:-}" ] && [ -f "$WORK/cloud/env.sh" ] && cand_sandbox_destroy "$WORK/cloud" >/dev/null 2>&1
    fi
    [ -n "$CREDD" ] && rm -rf "$CREDD"
    [ "$KEEP" = 0 ] && [ "$WORK_AUTO" = 1 ] && [ "$rc" = 0 ] && [ -n "${WORK:-}" ] && rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT

[ -n "$RUN_ID" ] || RUN_ID="r$(date -u +%y%m%d%H%M%S)$(printf '%04x' "$RANDOM")"

# ── refusals (cloud mode only), all before any sandbox or write ────────────────
if [ "$LOCAL_VS_LOCAL" = 0 ]; then
    "$E2E_PYTHON" "$LEG" check-window --window "$WINDOW" || exit 2
    CREDD="$(mktemp -d "${TMPDIR:-/tmp}/cloudleg-cred.XXXXXX")" || exit 2
    chmod 700 "$CREDD"
    command -v "$OP_BIN" >/dev/null 2>&1 || { echo "REFUSED: $OP_BIN is not on PATH (the credential is read through it)" >&2; exit 2; }
    for pair in $CAND_CLOUD_FIELD_MAP; do
        field="${pair%%:*}"; optional=0
        case "$field" in *\?) optional=1; field="${field%\?}" ;; esac
        # the 1Password item field; "token" is concealed, so --reveal. Stdout goes straight to a 0600 file.
        if ! ( umask 077; "$OP_BIN" item get "$OP_ITEM" --vault "$OP_VAULT" --fields "label=$field" --reveal >"$CREDD/$field" 2>/dev/null ) \
           || [ ! -s "$CREDD/$field" ]; then
            rm -f "$CREDD/$field"
            if [ "$optional" = 0 ]; then
                echo "REFUSED: 1Password item '$OP_ITEM' (vault $OP_VAULT) has no readable field '$field'" >&2
                exit 2
            fi
        fi
    done
    "$E2E_PYTHON" "$LEG" check-target --cred-dir "$CREDD" --item-title "$OP_ITEM" || exit 2
fi
[ -f "$WHEEL" ] || { echo "REFUSED: --wheel <candidate wheel> is required and must exist" >&2; exit 2; }

# ── plan, sandboxes, slots ─────────────────────────────────────────────────────
[ -n "$WORK" ] || { WORK="$(mktemp -d "${TMPDIR:-/tmp}/cloudleg-work.XXXXXX")"; WORK_AUTO=1; }
mkdir -p "$WORK"; chmod 700 "$WORK"
LEDGER_ARGS=()
if [ "$CORE_ONLY" = 0 ]; then
    [ -f "$LEDGER" ] && LEDGER_ARGS=(--ledger "$LEDGER")
    [ -z "$RELEASE" ] || LEDGER_ARGS+=(--release "$RELEASE")
fi
"$E2E_PYTHON" "$LEG" plan --run "$RUN_ID" "${LEDGER_ARGS[@]}" --out "$WORK/plan.json"

tenants=1; [ "$LOCAL_VS_LOCAL" = 1 ] && tenants=2
cand_engine_start "$REPO_ROOT" "$WORK/engine" "$tenants"
cand_sandbox_create "$WORK/local" >/dev/null
cand_sandbox_install "$WHEEL"
cand_sandbox_config local "$CAND_ENGINE_JSON" 0
cand_sandbox_create "$WORK/cloud" >/dev/null
cand_sandbox_install "$WHEEL"
if [ "$LOCAL_VS_LOCAL" = 1 ]; then
    cand_sandbox_config local "$CAND_ENGINE_JSON" 1
    CLOUD_HOST="127.0.0.1"
else
    cand_sandbox_config cloud "$CREDD"
    CLOUD_HOST="$("$E2E_PYTHON" -c 'import sys; print(sys.argv[1].split("//",1)[1].split("/",1)[0].rsplit("@",1)[-1].split(":",1)[0])' "$(head -n 1 "$CREDD/service_url")")"
fi

echo "cloud-leg: run $RUN_ID, label cloudleg-$RUN_ID, $([ "$LOCAL_VS_LOCAL" = 1 ] && echo 'LOCAL vs LOCAL (plumbing self-test)' || echo "cloud host $CLOUD_HOST")"
"$E2E_PYTHON" "$LEG" run-slot --plan "$WORK/plan.json" --slot local --sandbox "$WORK/local" --host 127.0.0.1 \
    --workdir "$WORK/taps" --out "$WORK/local.json"
"$E2E_PYTHON" "$LEG" run-slot --plan "$WORK/plan.json" --slot cloud --sandbox "$WORK/cloud" --host "$CLOUD_HOST" \
    --workdir "$WORK/taps" --out "$WORK/cloud.json"
rc=0
"$E2E_PYTHON" "$LEG" compare --plan "$WORK/plan.json" --local "$WORK/local.json" --cloud "$WORK/cloud.json" | tee "$WORK/report.txt" || rc=$?
echo "cloud-leg: left behind (cannot be deleted by the client): seat cloudleg-$RUN_ID-seat and the ephemeral identity labelled cloudleg-$RUN_ID"
exit "$rc"
