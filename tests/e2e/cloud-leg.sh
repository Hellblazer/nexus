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
#   tests/e2e/cloud-leg.sh --wheel <candidate.whl> --window <id> [--release v<X.Y.Z>] [--core-only | --require-deployed]
#        [--expect-red <step>@<status>[,<status>]:<reason>]... [--accept-skip "<route>:<reason>"]... [--no-ledger-entries-ok "<reason>"]
#        [--ledger <file>] [--op-item <title>] [--op-vault AI] [--work-dir <dir>] [--keep] [--plan-only]
#   The release additions are the wire ledger's entries (docs/wire-contract-pending.md): `## Unshipped`, or with
#   --release v<X.Y.Z> that release's `## Shipped` entries (the release skill's Step 4b has moved them there). Each
#   route maps to a client call; an unmapped route FAILS unless --accept-skip names it. A full run with zero ledger
#   entries FAILS unless --no-ledger-entries-ok gives a reason. --core-only runs the fixed core alone and reports how many
#   ledger routes it did not drive; it is not needed before a deploy: a cloud run reads the engine release the cloud runs
#   (GET /version, after every local refusal) and SKIPS each ledger route whose engine tag is later ("engine tag not
#   deployed"; the verdict then reads NOT A RELEASE PASS); the gating rerun after the deploy passes --require-deployed, which
#   fails on any such skip. --expect-red
#   <step>@<status>:<reason> turns that step's cloud-only failure into EXPECTED-RED only when the cloud returned exactly
#   the pinned status (a step the cut itself fixes, e.g. identity-seat-create@403);
#   any other failure on it stays FAIL, and a PASS on it fails the run. --plan-only prints the plan and stops (a cloud
#   run has read GET /version for it, the only cloud contact).
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

CORE_ONLY=0 WHEEL="" WINDOW="" LEDGER="$REPO_ROOT/docs/wire-contract-pending.md" LEDGER_SET=0 RELEASE="" KEEP=0 LOCAL_VS_LOCAL=0
PLAN_ONLY=0 NO_LEDGER_OK="" EXPECT_RED=() ACCEPT_SKIP=()
OP_ITEM="${NX_CLOUDLEG_OP_ITEM:-nexus cloud test tenant (release-test)}" OP_VAULT="${NX_CLOUDLEG_OP_VAULT:-AI}"
OP_BIN="${NX_CLOUDLEG_OP:-op-auto}" WORK="" RUN_ID="" WORK_AUTO=0 CLOUD_ENGINE="" REQUIRE_DEPLOYED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --wheel) WHEEL="${2:?}"; shift 2 ;;
        --window) WINDOW="${2:?}"; shift 2 ;;
        --ledger) LEDGER="${2:?}"; LEDGER_SET=1; shift 2 ;;
        --expect-red) EXPECT_RED+=("${2:?}"); shift 2 ;;
        --accept-skip) ACCEPT_SKIP+=("${2:?}"); shift 2 ;;
        --no-ledger-entries-ok) NO_LEDGER_OK="${2:?}"; shift 2 ;;
        --plan-only) PLAN_ONLY=1; shift ;;
        --release) RELEASE="${2:?}"; shift 2 ;;
        --op-item) OP_ITEM="${2:?}"; shift 2 ;;
        --op-vault) OP_VAULT="${2:?}"; shift 2 ;;
        --work-dir) WORK="${2:?}"; shift 2 ;;
        --run-id) RUN_ID="${2:?}"; shift 2 ;;
        --core-only) CORE_ONLY=1; shift ;;
        --require-deployed) REQUIRE_DEPLOYED=1; shift ;;
        --keep) KEEP=1; shift ;;
        --local-vs-local) LOCAL_VS_LOCAL=1; shift ;;
        -h|--help) sed -n 2,31p "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "cloud-leg: unknown argument: $1" >&2; exit 2 ;;
    esac
done

CREDD=""
# shellcheck disable=SC2329  # invoked through the EXIT trap
cleanup() {
    local rc=$?
    set +e
    cand_engine_stop
    # EVERY exit path, --keep included: no credential, identity token or engine token stays in a kept work dir.
    if [ -n "${WORK:-}" ]; then
        local d
        for d in "$WORK/local" "$WORK/cloud"; do
            [ -f "$d/env.sh" ] && cand_sandbox_scrub "$d" >/dev/null 2>&1
        done
        rm -f "$WORK/engine/engine.json" 2>/dev/null
    fi
    if [ "$KEEP" = 0 ]; then
        [ -n "${WORK:-}" ] && [ -f "$WORK/local/env.sh" ] && cand_sandbox_destroy "$WORK/local" >/dev/null 2>&1
        [ -n "${WORK:-}" ] && [ -f "$WORK/cloud/env.sh" ] && cand_sandbox_destroy "$WORK/cloud" >/dev/null 2>&1
    fi
    [ -n "$CREDD" ] && rm -rf "$CREDD"
    [ "$KEEP" = 0 ] && [ "$WORK_AUTO" = 1 ] && { [ "$rc" = 0 ] || [ "$rc" = 2 ]; } && [ -n "${WORK:-}" ] && rm -rf "$WORK"
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
    # the item's OWN title as the vault returned it, not the argument string
    if ! "$OP_BIN" item get "$OP_ITEM" --vault "$OP_VAULT" --format json 2>/dev/null | "$E2E_PYTHON" "$LEG" item-title >"$CREDD/_title" 2>/dev/null \
       || [ ! -s "$CREDD/_title" ]; then
        echo "REFUSED: 1Password item '$OP_ITEM' (vault $OP_VAULT) answered no title" >&2
        exit 2
    fi
    # one reader of every credential value: the same normalised text is checked and sent
    "$E2E_PYTHON" "$LEG" normalize-cred --cred-dir "$CREDD" || exit 2
    "$E2E_PYTHON" "$LEG" check-target --cred-dir "$CREDD" --item-title-file "$CREDD/_title" || exit 2
fi
if [ "$REQUIRE_DEPLOYED" = 1 ] && { [ "$LOCAL_VS_LOCAL" = 1 ] || [ "$CORE_ONLY" = 1 ]; }; then
    echo "REFUSED: --require-deployed needs a cloud run that drives the ledger (not --local-vs-local, not --core-only)" >&2
    exit 2
fi
if [ "$LEDGER_SET" = 1 ] && [ ! -f "$LEDGER" ]; then
    echo "REFUSED: --ledger $LEDGER does not exist" >&2
    exit 2
fi

# ── plan, sandboxes, slots ─────────────────────────────────────────────────────
[ -n "$WORK" ] || { WORK="$(mktemp -d "${TMPDIR:-/tmp}/cloudleg-work.XXXXXX")"; WORK_AUTO=1; }
mkdir -p "$WORK"; chmod 700 "$WORK"
PLAN_ARGS=(--ledger "$LEDGER")
[ -z "$RELEASE" ] || PLAN_ARGS+=(--release "$RELEASE")
[ "$CORE_ONLY" = 0 ] || PLAN_ARGS+=(--core-only)
[ -z "$NO_LEDGER_OK" ] || PLAN_ARGS+=(--no-ledger-entries-ok "$NO_LEDGER_OK")
for x in ${ACCEPT_SKIP[@]+"${ACCEPT_SKIP[@]}"}; do PLAN_ARGS+=(--accept-skip "$x"); done
"$E2E_PYTHON" "$LEG" plan --run "$RUN_ID" "${PLAN_ARGS[@]}" --out "$WORK/plan.json" || exit 2
# a plan with failures, or an --expect-red naming no step or lacking its @status pin, stops here: no sandbox, no cloud contact
CHECK_ARGS=(--plan "$WORK/plan.json")
for x in ${EXPECT_RED[@]+"${EXPECT_RED[@]}"}; do CHECK_ARGS+=(--expect-red "$x"); done
"$E2E_PYTHON" "$LEG" check-plan "${CHECK_ARGS[@]}" || exit 2
if [ "$PLAN_ONLY" = 0 ] && [ ! -f "$WHEEL" ]; then echo "REFUSED: --wheel <candidate wheel> is required and must exist" >&2; exit 2; fi
if [ "$LOCAL_VS_LOCAL" = 0 ]; then
    # every local refusal has run; the first cloud contact is the one unauthenticated read, GET /version. A ledger route
    # whose engine tag is later than the cloud's engine is SKIPPED, not driven (nexus-h4hge); the plan is rebuilt with it.
    CLOUD_ENGINE="$("$E2E_PYTHON" "$LEG" cloud-engine --cred-dir "$CREDD")" || exit 2
    PLAN_ARGS+=(--cloud-engine "$CLOUD_ENGINE")
    [ "$REQUIRE_DEPLOYED" = 0 ] || PLAN_ARGS+=(--require-deployed)
    "$E2E_PYTHON" "$LEG" plan --run "$RUN_ID" "${PLAN_ARGS[@]}" --out "$WORK/plan.json" || exit 2
    "$E2E_PYTHON" "$LEG" check-plan "${CHECK_ARGS[@]}" || exit 2
fi
if [ "$PLAN_ONLY" = 1 ]; then exit 0; fi

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
    CLOUD_HOST="$("$E2E_PYTHON" -c 'import sys; print(sys.argv[1].split("//",1)[1].split("/",1)[0].rsplit("@",1)[-1].split(":",1)[0].lower())' "$(head -n 1 "$CREDD/service_url")")"
fi

echo "cloud-leg: run $RUN_ID, label cloudleg-$RUN_ID, $([ "$LOCAL_VS_LOCAL" = 1 ] && echo 'LOCAL vs LOCAL (plumbing self-test)' || echo "cloud host $CLOUD_HOST")"
"$E2E_PYTHON" "$LEG" run-slot --plan "$WORK/plan.json" --slot local --sandbox "$WORK/local" --host 127.0.0.1 \
    --workdir "$WORK/taps" --out "$WORK/local.json"
"$E2E_PYTHON" "$LEG" run-slot --plan "$WORK/plan.json" --slot cloud --sandbox "$WORK/cloud" --host "$CLOUD_HOST" \
    --workdir "$WORK/taps" --out "$WORK/cloud.json"
CMP_ARGS=(--window "${WINDOW:--}" --edge-host "$CLOUD_HOST")
for x in ${EXPECT_RED[@]+"${EXPECT_RED[@]}"}; do CMP_ARGS+=(--expect-red "$x"); done
rc=0
"$E2E_PYTHON" "$LEG" compare --plan "$WORK/plan.json" --local "$WORK/local.json" --cloud "$WORK/cloud.json" "${CMP_ARGS[@]}" | tee "$WORK/report.txt" || rc=${PIPESTATUS[0]}
echo "cloud-leg: left behind (the client cannot delete these): seat cloudleg-$RUN_ID-seat, and the now-empty collection knowledge__cloud-leg-probe__* in the tenant. The ephemeral identity cloudleg-$RUN_ID was ended."
echo "cloud-leg: paste the report from the 'examined' line to the verdict line onto the cut bead."
exit "$rc"
