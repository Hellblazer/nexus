#!/usr/bin/env bash
# nexus-bwulw: CLOUD CLIENT-PATH GATE — assert the engine's pinned HTTP
# contracts survive the PUBLIC edge, as seen by real client code.
#
# Why this exists (2026-07-23): every automated gate stops at one of two
# boundaries — the local boundary (unit/integration/MVV/sandbox: client +
# local engine) or the engine boundary (the conexus-side cloud gate probes
# the engine DIRECTLY, inside their infra). The client -> public-edge ->
# engine path had ZERO automated coverage, and the edge silently rewrote
# both infrastructure endpoints: /version answered with a two-field stub
# (dropping embedding_mode/embedding_models -> voyage threshold gating OFF,
# dimension-orphan tooling inert, guided-upgrade voyage-capability check
# falsely fail-closed) and /health was auth-gated (401) while the pinned
# ez5.1 contract — which guided_upgrade's readiness gate polls with a bare
# unauthenticated GET — is 200 + db=up. Three client features shipped green
# through every gate and were dead-on-arrival for cloud boxes.
#
# Gate-green on the engine does NOT mean client-visible. This gate is the
# client-visible half.
#
# STATUS CHANGED 2026-08-01 — READ THIS BEFORE INTERPRETING A RED RUN.
# This gate was written EXPECTED RED, and stayed red for as long as the
# conexus edge stubbed /version and auth-gated /health. That condition is
# now MET: on the engine-service-v0.1.60 deploy, conexus reported all four
# legs passing with a TRUE EXIT 0 (the v0.1.59 run's exit code had been
# swallowed by a shell redirect, so its PASS was only the sentinel line).
# Independently confirmed from a dev box: an UNAUTHENTICATED GET of
# https://api.conexus-nexus.com/version returns release_version, embedding_mode
# and embedding_models.
#
# SO THE MEANING OF RED HAS INVERTED. A red run is no longer expected
# evidence to be relayed — it is a REGRESSION of the public edge, and it
# means the three client features named below have gone dead on arrival for
# cloud boxes again. Do not read it as the known-pending state; that reading
# is exactly what the old header would now license, which is why the header
# was changed rather than left as history.
#
# Legs (all read-only; no writes, no config mutation, safe on a live box):
#   A  /version contract through the edge: 200, release_version parseable
#      and >= REQUIRED_ENGINE_VERSION, embedding_mode present and known,
#      embedding_models non-empty (RDR-002 + nexus-pebfx.5 contract).
#   B  /health edge contract: AUTHENTICATED GET -> 200 + body.db == "up"
#      (conexus relay [21082], decision (b): the public edge auth-gates
#      /health and relays the engine's ez5.1 body verbatim to bearers —
#      exactly what guided_upgrade's health gate now sends for managed
#      targets; unauth-401 is conexus's own IT-pinned contract).
#   C  real-client probe: HttpVectorClient.embedding_mode() through the
#      live config resolves a mode (never None). This is the exact signal
#      the search threshold gate and dimension-orphan tooling key on.
#   D  real-client read path: list_collections + one search round-trip
#      through the edge returns without error (auth + /v1/* proxy intact).
#
# Applicability: requires a CLOUD-mode box (service_url is a non-loopback
# https endpoint). On a local-mode box this gate REFUSES (exit 2) rather
# than skip-passing — a vacuous pass here would be exactly the blindness
# it exists to close (feedback_gates_scripted_not_ambient).
#
# Usage: tests/e2e/cloud-client-path-gate.sh
# Exit 0 == CLOUD CLIENT-PATH GATE PASSED (literal sentinel on last line).
# Exit 2 == not applicable (not a cloud-mode box). Any other == FAILED.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Legs accumulate violations instead of fail-fast: a red run is relay
# evidence, and "which legs are broken" is the payload.
VIOLATIONS=0
_leg_fail() { echo "  LEG FAILED: $*" >&2; VIOLATIONS=$((VIOLATIONS + 1)); }
_fail() { echo "CLOUD CLIENT-PATH GATE FAILED: $*" >&2; exit 1; }

# nexus-i1oh4: observed-vs-expected leg floor. The verdict used to be a
# function of VIOLATIONS alone — a leg that was skipped, guarded out, or
# removed by an edit contributed nothing, so absent work read as clean work
# (the gate could print PASSED having run nothing; unacceptable for the ONLY
# assert that the engine's pinned contracts survive the public edge,
# nexus-bwulw). Each leg group increments LEGS_RAN on ENTRY, on the SHELL
# side — a heredoc that dies mid-leg still counts as a leg that failed to
# complete, never a leg that quietly did not run.
#
# EXPECTED_LEGS=3 (dated 2026-08-18): [A] /version, [B] /health
# authenticated, [C+D] client probe heredoc (one shell-side entry for the
# combined python leg). Editing the battery means updating this constant in
# the same diff.
LEGS_RAN=0
EXPECTED_LEGS=3
_leg_enter() { LEGS_RAN=$((LEGS_RAN + 1)); echo "[$1] $2"; }

SERVICE_URL="$(uv run python - <<'PY'
from nexus.config import get_credential
print((get_credential("service_url") or "").strip())
PY
)"
[ -n "$SERVICE_URL" ] || { echo "not applicable: no service.service_url configured (local-mode box)"; exit 2; }
case "$SERVICE_URL" in
    https://*) : ;;
    *) echo "not applicable: service_url is $SERVICE_URL (not a public https edge)"; exit 2 ;;
esac
echo "Gating client path against: $SERVICE_URL"

# ── Leg A: /version contract through the edge ────────────────────────────
_leg_enter A "/version contract"
VERSION_BODY="$(curl -sS -m 20 "$SERVICE_URL/version")" || _leg_fail "A: /version unreachable"
uv run python - "$VERSION_BODY" <<'PY' || _leg_fail "A: /version contract violated (see above)"
import json, sys
from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

body = json.loads(sys.argv[1])
errs = []
parsed = parse_engine_version(body.get("release_version"))
if parsed is None:
    errs.append(f"release_version unusable: {body.get('release_version')!r}")
elif parsed < REQUIRED_ENGINE_VERSION:
    errs.append(f"release_version {parsed} below floor {REQUIRED_ENGINE_VERSION}")
mode = body.get("embedding_mode")
if mode not in ("voyage", "onnx-local"):
    errs.append(
        f"embedding_mode missing/unknown through the edge (got {mode!r}) — "
        "voyage threshold gating, doctor dimension-orphan check, and "
        "nx collection prune are all inert for every cloud client"
    )
models = body.get("embedding_models")
if not (isinstance(models, list) and models):
    errs.append(
        f"embedding_models missing/empty through the edge (got {models!r}) — "
        "guided-upgrade voyage-capability check falsely fail-closes, "
        "blocking managed migrations targeting this service"
    )
# RDR-196 .p1c (nexus-nyry9.9): nx_answer_steps_supported is a compile-time-
# constant true on any engine build carrying the handler, unconditionally
# emitted (unlike the nullable build_ref field) — the same edge-stubbing
# risk class embedding_mode/embedding_models above guards against
# (nexus-bwulw: the public edge once stubbed /version fields the engine
# itself always emitted, silently disabling client-side capability gating).
steps_supported = body.get("nx_answer_steps_supported")
if steps_supported is not True:
    errs.append(
        f"nx_answer_steps_supported missing/false through the edge "
        f"(got {steps_supported!r}) — the .p1d per-step cost/quality "
        "telemetry capability probe is inert for every cloud client"
    )
if errs:
    print("  /version body:", json.dumps(body), file=sys.stderr)
    for e in errs:
        print("  VIOLATION:", e, file=sys.stderr)
    sys.exit(1)
print(f"  ok: release_version={body['release_version']} "
      f"embedding_mode={mode} models={models} "
      f"nx_answer_steps_supported={steps_supported}")
PY

# ── Leg B: /health edge contract, AUTHENTICATED (guided_upgrade's managed-
#    target probe shape per conexus relay [21082], decision (b)) ──────────
_leg_enter B "/health edge contract (authenticated bearer)"
SERVICE_TOKEN="$(uv run python - <<'PY'
from nexus.config import get_credential
print((get_credential("service_token") or "").strip())
PY
)"
if [ -z "$SERVICE_TOKEN" ]; then
    _leg_fail "B: no service_token credential configured — cannot probe the auth-gated edge /health"
else
    HEALTH_STATUS="$(curl -sS -m 20 -H "Authorization: Bearer $SERVICE_TOKEN" -o /tmp/cloud-gate-health.$$ -w "%{http_code}" "$SERVICE_URL/health" || echo 000)"
    HEALTH_BODY="$(cat /tmp/cloud-gate-health.$$ 2>/dev/null; rm -f /tmp/cloud-gate-health.$$)"
    if [ "$HEALTH_STATUS" != "200" ]; then
        _leg_fail "B: authenticated /health returned HTTP $HEALTH_STATUS (body: $HEALTH_BODY) — the edge contract (conexus [21082]) is 200 + verbatim engine {status, db} for bearers; guided_upgrade's managed-target readiness gate will time out 'service not ready'"
    # Pipe-free (nexus-i66g4/wbeyi class): match the already-captured
    # variable directly instead of `echo ... | grep -q ...` -- under this
    # script's `set -o pipefail`, a still-writing echo closed early by
    # grep risks its SIGPIPE getting promoted over grep's own (successful)
    # exit status.
    elif ! [[ "$HEALTH_BODY" =~ \"db\"[[:space:]]*:[[:space:]]*\"up\" ]]; then
        _leg_fail "B: authenticated /health 200 but body lacks db=up (body: $HEALTH_BODY)"
    else
        echo "  ok: 200 + db=up (authenticated)"
    fi
fi

# ── Legs C+D: real client code through the live config ───────────────────
_leg_enter C+D "client embedding_mode probe + client read path"
uv run python - <<'PY' || _leg_fail "C/D: client-path probe failed (see above)"
import sys
from nexus.db import make_t3

bad = False
t3 = make_t3()
mode = t3.embedding_mode()
if mode is None:
    print("  VIOLATION [C]: HttpVectorClient.embedding_mode() -> None through "
          "the edge; search threshold gating is OFF for this box", file=sys.stderr)
    bad = True
else:
    print(f"  ok [C]: client resolves embedding_mode={mode}")

try:
    colls = t3.list_collections()
    if not colls:
        raise RuntimeError("list_collections returned no collections")
    name = colls[0]["name"]
    t3.search("smoke test query", [name], n_results=1)
    print(f"  ok [D]: {len(colls)} collection(s); search round-trip on "
          f"{name!r} returned without error")
except Exception as exc:
    print(f"  VIOLATION [D]: client read path failed through the edge: {exc}",
          file=sys.stderr)
    bad = True

sys.exit(1 if bad else 0)
PY

if [ "$LEGS_RAN" -ne "$EXPECTED_LEGS" ]; then
    # Distinct from a violation: "the gate did not run its full battery" is
    # a different fact from "the edge is broken", and the relay must be able
    # to tell them apart (nexus-i1oh4).
    _fail "battery shortfall: only $LEGS_RAN of $EXPECTED_LEGS leg(s) ran — this run proves nothing about the legs that never executed"
fi
if [ "$VIOLATIONS" -gt 0 ]; then
    _fail "$VIOLATIONS leg(s) violated — the public edge does not deliver the engine's pinned client contract"
fi
echo "CLOUD CLIENT-PATH GATE PASSED — legs=$LEGS_RAN/$EXPECTED_LEGS violations=0"
