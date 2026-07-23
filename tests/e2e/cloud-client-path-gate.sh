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
# client-visible half. EXPECTED RED until the conexus edge passes the
# engine's /version fields through and honors the ez5.1 /health contract
# (or the client's /health consumer is changed deliberately) — a red run
# here is the mechanized relay evidence, not a flake.
#
# Legs (all read-only; no writes, no config mutation, safe on a live box):
#   A  /version contract through the edge: 200, release_version parseable
#      and >= REQUIRED_ENGINE_VERSION, embedding_mode present and known,
#      embedding_models non-empty (RDR-002 + nexus-pebfx.5 contract).
#   B  /health ez5.1 contract through the edge: UNAUTHENTICATED GET ->
#      200 + body.db == "up" (exactly what guided_upgrade._health_gate
#      sends; an auth-gated edge breaks managed migrations to cloud).
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
echo "[A] /version contract"
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
if errs:
    print("  /version body:", json.dumps(body), file=sys.stderr)
    for e in errs:
        print("  VIOLATION:", e, file=sys.stderr)
    sys.exit(1)
print(f"  ok: release_version={body['release_version']} "
      f"embedding_mode={mode} models={models}")
PY

# ── Leg B: /health ez5.1 contract, unauthenticated (guided_upgrade's exact
#    probe shape) ─────────────────────────────────────────────────────────
echo "[B] /health ez5.1 contract (unauthenticated)"
HEALTH_STATUS="$(curl -sS -m 20 -o /tmp/cloud-gate-health.$$ -w "%{http_code}" "$SERVICE_URL/health" || echo 000)"
HEALTH_BODY="$(cat /tmp/cloud-gate-health.$$ 2>/dev/null; rm -f /tmp/cloud-gate-health.$$)"
if [ "$HEALTH_STATUS" != "200" ]; then
    _leg_fail "B: unauthenticated /health returned HTTP $HEALTH_STATUS (body: $HEALTH_BODY) — ez5.1 pins 200 + db=up; guided_upgrade's readiness gate polls exactly this and will time out 'service not ready' on any managed migration targeting this service"
elif ! echo "$HEALTH_BODY" | grep -q '"db"[[:space:]]*:[[:space:]]*"up"'; then
    _leg_fail "B: /health 200 but body lacks db=up (body: $HEALTH_BODY)"
else
    echo "  ok: 200 + db=up"
fi

# ── Legs C+D: real client code through the live config ───────────────────
echo "[C] client embedding_mode probe + [D] client read path"
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

if [ "$VIOLATIONS" -gt 0 ]; then
    _fail "$VIOLATIONS leg(s) violated — the public edge does not deliver the engine's pinned client contract"
fi
echo "CLOUD CLIENT-PATH GATE PASSED"
