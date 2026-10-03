#!/usr/bin/env bash
# This gate deliberately WRITES to the operator's live store through the public
# edge (leg E, the shell-substitution T2 write) from a dev checkout: the
# nexus-a2qhz production-write guard needs the reason-bearing opt-in.
export NX_ALLOW_PROD_WRITE="cloud-client-path-gate: deliberate post-deploy MVV write through the public edge (nexus-a2qhz)"
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
# dimension-orphan tooling inert, and the then-live guided-upgrade
# voyage-capability check falsely fail-closed) and /health was auth-gated
# (401) while the pinned ez5.1 contract — which guided_upgrade's readiness
# gate then polled with a bare unauthenticated GET — is 200 + db=up. Three
# client features shipped green through every gate and were dead-on-arrival
# for cloud boxes. (guided_upgrade itself was deleted at RDR-155 P4b.)
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
# Legs (no config mutation; E and H write probe rows to the live store,
# which is why NX_ALLOW_PROD_WRITE is set above):
#   A  /version contract through the edge: 200, release_version parseable
#      and >= REQUIRED_ENGINE_VERSION, embedding_mode present and known,
#      embedding_models non-empty (RDR-002 + nexus-pebfx.5 contract).
#   B  edge auth contract the client relies on (Sam, 2026-09-28): an
#      UNAUTHENTICATED /health is refused by the edge (403), and the minted
#      data token the client sends is accepted on /v1 (200). Optional B3
#      (nexus-20onx fix round): when NX_EXPECTED_OWNERLESS_WRITE_MODE is set,
#      /v1/status through the edge must report that ownerless_write_mode. The old
#      authenticated-/health probe is retired: its only consumer,
#      guided_upgrade's readiness gate, was deleted at RDR-155 P4b, and the
#      static service_token it used was revoked 2026-09-28.
#   C  real-client probe: HttpVectorClient.embedding_mode() through the
#      live config resolves a mode (never None). This is the exact signal
#      the search threshold gate and dimension-orphan tooling key on.
#   D  real-client read path: list_collections + one search round-trip
#      through the edge returns without error (auth + /v1/* proxy intact).
#   F  RDR-205 tuple-space CA 3 through the edge (bead nexus-em75s.15):
#      (i) a park at the engine's default 25 s cap on an empty probe
#      subspace returns the probe result (never a 504) -- the constraint
#      CA 3 depends on; (ii) timeout_s=31 (above the cap) is asserted
#      against whatever the edge/engine ACTUALLY does -- read the result
#      rather than assume it, since the engine's synchronous
#      TimeoutTooLong (400) rejection and a genuine edge 504 are both
#      valid evidence for "a >25 s park cannot succeed end to end"; (iii)
#      registry() reports sources == ["resources"] exactly, so a stray
#      NX_TUPLE_TEMPLATE_DIR in production is a red gate, not a silent
#      second template source.
#   H  RDR-206 renew and ack-with-reply through the edge (nexus-zjzt1):
#      claim a request, renew it and assert lease_until moved forward on
#      the engine's clock; ack with a reply and assert a 64-hex reply id,
#      exactly that row at the reply address (read at n=2), and the request
#      consumed; a reply into a keys-only ledger/ target is refused as
#      SchemaViolation and the same claimant can still ack plainly. WRITES
#      two requests and one reply into fresh mailbox/ccpg-* addresses.
#
#   I  descending tuple read through the edge (nexus-kp5q3): an rd with
#      order=desc must come back with the engine's "order":"desc" and
#      "limit" echo. The echo is the client's only capability signal, so an
#      edge that strips or rewrites the JSON, or an engine deployed without
#      the read, turns `nx tuple rd --newest` back into oldest-first paging
#      that exits 3 past --max-rows, with no error anywhere else. Read-only:
#      one non-mutating rd on a fresh probe subspace. A missing echo FAILS
#      this leg; it is never skipped.
#
# Applicability: requires a CLOUD-mode box (service_url is a non-loopback
# https endpoint). On a local-mode box this gate REFUSES (exit 2) rather
# than skip-passing — a vacuous pass here would be exactly the blindness
# it exists to close (feedback_gates_scripted_not_ambient).
#
# Usage: tests/e2e/cloud-client-path-gate.sh
#   NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only tests/e2e/cloud-client-path-gate.sh
#       also asserts the LIVE engine's ownerless-write refusal mode (RDR-223
#       Phase 3 Step 2; nexus-z0o2p.24): `log-only` after the first deploy,
#       `enforce` after the flip redeploy. Nothing else in this repo reads the
#       live mode, and conexus wires the knob: a mis-wired parameter would
#       enforce on the first deploy and refuse every legacy write from the
#       hosts. The engine-release skill's Step 6.1 sets it on every cut that
#       carries the refusal. Unset, an engine that reports a mode FAILS the
#       gate (a live mode nobody asserted); an engine that reports none
#       (before P3.2) reports B3 NOT RUN and the final sentinel line says so.
# Exit 0 == CLOUD CLIENT-PATH GATE PASSED (literal sentinel on last line).
# Exit 2 == not applicable (not a cloud-mode box). Any other == FAILED.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# One interpreter >= 3.10, resolved once; never a bare python3 (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2
cd "$REPO_ROOT"

# nexus-20onx fix round: the optional live-mode assertion (leg B3).
NX_EXPECTED_OWNERLESS_WRITE_MODE="${NX_EXPECTED_OWNERLESS_WRITE_MODE:-}"
case "$NX_EXPECTED_OWNERLESS_WRITE_MODE" in
    ""|log-only|enforce) ;;
    *) echo "FATAL: NX_EXPECTED_OWNERLESS_WRITE_MODE=$NX_EXPECTED_OWNERLESS_WRITE_MODE is not one of: (unset), log-only, enforce." >&2; exit 2 ;;
esac

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
# EXPECTED_LEGS=7 (dated 2026-09-13; [B] redefined 2026-09-28; [I] added 2026-09-29;
# [G] deleted at cleanup step A1, nexus-0r1uz): [A] /version,
# [B] edge auth contract (unauthenticated /health refused, data token
# accepted on /v1), [C+D] client probe heredoc (one shell-side entry for the
# combined python leg), [E] T2 write body carrying shell-substitution text
# (nexus-cmzib WAF passthrough), [F] RDR-205 tuple-space CA 3 through the
# edge (nexus-em75s.15), [H] RDR-206 renew and ack-with-reply through
# the edge (nexus-zjzt1, 2026-09-13), [I] descending tuple read echo through
# the edge (nexus-kp5q3, 2026-09-29). Editing the battery means updating this
# constant in the same diff.
LEGS_RAN=0
EXPECTED_LEGS=7
_leg_enter() { LEGS_RAN=$((LEGS_RAN + 1)); echo "[$1] $2"; }

# Leg B3's compare logic (nexus-20onx; nexus-i1oh4 doctrine applied to it in the
# round-3 fix): the live engine's ownerless-write mode read from /v1/status.
# Takes the status body and the expected mode (may be empty); prints one line and
# returns 0 = asserted and holds, 1 = violation, 3 = not run. A sub-check is not
# a leg, so LEGS_RAN cannot see it; this function is why an unset
# NX_EXPECTED_OWNERLESS_WRITE_MODE can never read as a clean pass:
#   - body unreadable (no JSON object with embedding_mode: curl failure, edge
#     401/403/502/WAF page), expected set or not -> 1 (never "no mode")
#   - expected set, observed equal            -> 0
#   - expected set, observed other or absent  -> 1
#   - expected UNSET, engine reports a mode   -> 1 (a mode is live and nobody
#     asserted it: the dangerous mis-wire is a valid `enforce`, which boots fine)
#   - expected UNSET, engine reports no mode  -> 3, and the final sentinel line
#     says so (B3_NOT_RUN); this is an engine that predates RDR-223 P3.2, or an
#     edge that strips the field, and the two cannot be told apart from here.
# The test (tests/e2e/cloud_client_path_gate_b3_test.sh) sources this function
# from the real script.
_ownerless_mode_verdict() {
    local body="$1" expected="${2:-}" observed
    # A served /v1/status always carries embedding_mode (StatusHandler writes it
    # first, on every engine that has the endpoint), so a body without it is not
    # a status body: a curl failure, an edge 401/403/502 page, a WAF block.
    # That reads as UNREADABLE, never as "an engine with no mode" (nexus-20onx
    # round 4, critic S4).
    observed="$(printf '%s' "$body" | "$E2E_PYTHON" -c "
import json, sys
try:
    doc = json.load(sys.stdin)
    if not isinstance(doc, dict) or 'embedding_mode' not in doc:
        raise ValueError('not a status body')
    print(doc.get('ownerless_write_mode') or '')
except Exception:
    print('@@UNREADABLE@@')
")"
    if [ "$observed" = "@@UNREADABLE@@" ]; then
        echo "B3: /v1/status through the edge returned no readable status body (a curl failure, or an edge 401/403/502/WAF page), so the live ownerless-write mode could not be read; this is a failure whether or not NX_EXPECTED_OWNERLESS_WRITE_MODE is set"
        return 1
    fi
    if [ -n "$expected" ]; then
        if [ "$observed" = "$expected" ]; then
            echo "ok [B3]: /v1/status reports ownerless_write_mode=$observed (expected $expected)"
            return 0
        fi
        echo "B3: /v1/status reports ownerless_write_mode='${observed:-(absent)}', expected '$expected' — the deployed engine lacks the refusal, or conexus wired NX_OWNERLESS_WRITE_MODE to another value (a mis-wired first deploy enforces and refuses every legacy write)"
        return 1
    fi
    if [ -n "$observed" ]; then
        echo "B3: /v1/status reports ownerless_write_mode='$observed' but NX_EXPECTED_OWNERLESS_WRITE_MODE is unset, so the live mode was not asserted — re-run with NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only (after the first deploy) or enforce (after the flip)"
        return 1
    fi
    echo "NOT RUN [B3]: NX_EXPECTED_OWNERLESS_WRITE_MODE is unset and /v1/status reports no ownerless_write_mode (an engine before RDR-223 P3.2, or an edge that strips the field), so the live mode was not asserted"
    return 3
}
B3_NOT_RUN=0

# Every python whose STDOUT is captured below configures cli logging first:
# structlog's unconfigured default writes to stdout, and a log line there
# becomes part of the captured value (leg B's first data-token run carried
# one into its Authorization header).
SERVICE_URL="$(uv run python - <<'PY'
from nexus.logging_setup import configure_logging
configure_logging("cli")
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
        "`nx service probe` and `nx init` list no embedding models "
        "for this service (nexus.db.managed_endpoint capabilities)"
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

# ── Leg B: the edge auth contract (Sam, 2026-09-28) ──────────────────────
#    B1: an UNAUTHENTICATED /health is refused by the edge (measured 403). An
#        access-control pin on the edge; no client calls cloud /health today.
#    B2: the minted data token the client sends is accepted on /v1 (200). This
#        is the dependency every cloud client has. It curls the route directly
#        rather than leaning on leg D, whose list_collections degrades an
#        error to an empty list and hits a different route.
#    The authenticated-/health probe this leg used to make is retired: its
#    consumer (guided_upgrade) was deleted at RDR-155 P4b, the static
#    service_token it sent was revoked 2026-09-28, and the edge's /health gate
#    does not admit the minted data token (401, T2 [23517] separates the two
#    credential classes). A different /health status is reported as a change
#    in conexus's gate, not as an engine fault.
_leg_enter B "edge auth contract (unauthenticated /health refused, data token accepted on /v1)"
NOAUTH_STATUS="$(curl -sS -m 20 -o /dev/null -w "%{http_code}" "$SERVICE_URL/health" || echo 000)"
case "$NOAUTH_STATUS" in
    403) echo "  ok [B1]: unauthenticated /health refused by the edge (403)" ;;
    200) _leg_fail "B1: unauthenticated /health returned 200 — the edge no longer gates /health (conexus changed relay [21082] decision (b)); confirm with conexus and update this pin" ;;
    401) _leg_fail "B1: unauthenticated /health returned 401, not the 403 pinned 2026-09-28 — conexus changed how its edge gates /health; confirm with conexus and update this pin" ;;
    *)   _leg_fail "B1: unauthenticated /health returned HTTP $NOAUTH_STATUS (pinned: 403) — the edge is unreachable or its /health gate changed" ;;
esac

# The bearer the client itself sends: the minted data token (the pass-through
# engine refuses a static token), resolved by DataTokenManager.bearer_for as
# catalog.factory and nx doctor do; the static service_token only when no
# mint_token is configured. The header travels through a mode-600 temp file,
# never stdout (structlog writes its info lines there, and the first live run
# carried one into the Authorization header), argv, or a shell variable; curl
# reads it with -H @file. Which kind was used goes to stderr.
BEARER_FILE="$(mktemp)"
chmod 600 "$BEARER_FILE"
BEARER_RC=0
SERVICE_URL="$SERVICE_URL" BEARER_FILE="$BEARER_FILE" uv run python - <<'PY' || BEARER_RC=$?
import os
import sys

from nexus.logging_setup import configure_logging
configure_logging("cli")
from nexus.config import get_credential
from nexus.db.data_token import DataTokenMintError, get_data_token_manager
from nexus.db.t2._refreshable_client import DEFAULT_TENANT

try:
    token = get_data_token_manager().bearer_for(os.environ["SERVICE_URL"], DEFAULT_TENANT)
except DataTokenMintError as exc:
    print(f"  B2: data-token mint failed: {exc}", file=sys.stderr)
    sys.exit(0)
if token:
    print("  B2: bearer = minted data token", file=sys.stderr)
else:
    token = (get_credential("service_token") or "").strip()
    if token:
        print("  B2: bearer = static service_token (no mint_token configured)", file=sys.stderr)
if token:
    with open(os.environ["BEARER_FILE"], "w") as fh:
        fh.write(f"Authorization: Bearer {token}\n")
PY
if [ "$BEARER_RC" -ne 0 ]; then
    _leg_fail "B2: resolving the bearer crashed (exit $BEARER_RC, see above)"
elif [ ! -s "$BEARER_FILE" ]; then
    _leg_fail "B2: no bearer — neither a mint_token (minted data token) nor a service_token credential is usable"
else
    V1_STATUS="$(curl -sS -m 20 -H @"$BEARER_FILE" -o /dev/null -w "%{http_code}" "$SERVICE_URL/v1/catalog/collections/list" || echo 000)"
    if [ "$V1_STATUS" = "200" ]; then
        echo "  ok [B2]: /v1 accepts the client's bearer (200)"
    else
        _leg_fail "B2: /v1/catalog/collections/list returned HTTP $V1_STATUS with the client's bearer (pinned: 200) — every cloud client's reads and writes go through this"
    fi
    # B3 (nexus-20onx fix round): the live engine's ownerless-write mode, read
    # from /v1/status through the edge with the same bearer. Read-only.
    STATUS_BODY="$(curl -sS -m 20 -H @"$BEARER_FILE" "$SERVICE_URL/v1/status" || echo "")"
    B3_RC=0
    B3_LINE="$(_ownerless_mode_verdict "$STATUS_BODY" "$NX_EXPECTED_OWNERLESS_WRITE_MODE")" || B3_RC=$?
    case "$B3_RC" in
        0) echo "  $B3_LINE" ;;
        3) echo "  $B3_LINE"; B3_NOT_RUN=1 ;;
        *) _leg_fail "$B3_LINE" ;;
    esac
fi
rm -f "$BEARER_FILE"

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

# ── Leg E: T2 write body carrying shell-substitution text (nexus-cmzib) ──
# Measured 2026-08-20: the edge WAF (KnownBadInputs Log4JRCE_BODY) 403'd any
# T2/T1 write whose JSON body contained shell-substitution syntax, so review
# records and shell-guard notes could not be persisted verbatim. The edge
# half was fixed 2026-08-23 (conexus PR #230 / conexus-04dp: the managed
# rule group is split — the three _BODY sub-rules COUNT on /v1/*, enforce
# elsewhere), verified live 2026-08-31 (this leg green, first run). The leg
# stays as the REGRESSION TRIPWIRE for the recurrence class (conexus-5jm5:
# the managed group grows new _BODY signatures over time): a leg-E-only red
# means a NEW signature is blocking /v1 writes again — classify on the
# response Server header (awselb/2.0 = WAF, absent/nginx = app) and relay
# to conexus; the structured client error it prints (edge_refusal.py) names
# the cause. Heredoc is quoted: the substitution text below is DATA.
_leg_enter E "T2 write with shell-substitution body (WAF passthrough, nexus-cmzib)"
uv run python - <<'PY' || _leg_fail "E: shell-substitution T2 write refused or mangled (see above)"
import sys
from nexus.db.t2.http_memory_store import HttpMemoryStore

title = "cloud-gate-waf-probe"
content = "waf probe: $(echo a) and ${x:-i} must persist verbatim"
store = HttpMemoryStore()
try:
    store.put("nexus_gate_probes", title, content, tags="cloud-gate,nexus-cmzib", ttl=1)
    row = store.get("nexus_gate_probes", title)
    got = (row or {}).get("content", "")
    if content not in got:
        print(f"  VIOLATION [E]: probe stored but read back mangled: {got!r}", file=sys.stderr)
        sys.exit(1)
    print("  ok [E]: substitution-bearing body stored and read back verbatim")
finally:
    try:
        store.delete("nexus_gate_probes", title)
    except Exception:  # noqa: BLE001 — best-effort cleanup; ttl=1 reaps leftovers
        pass
PY

# ── Leg F: RDR-205 tuple-space CA 3 through the edge (nexus-em75s.15) ────
# Both calls below are non-mutating (`rd` passes mutates=False; `registry`
# is a GET) so neither routes through the nexus-a2qhz production-write
# guard -- nothing is written, nothing needs to be taken back.
#
# CA 3 (docs/rdr/rdr-205-linda-tuple-space-over-postgres.md): the control
# plane times out a response that has not started within 30 s, so
# timeout_s is capped at 25 s engine-side. Read against the ENGINE source
# (service/src/main/java/dev/nexus/service/db/TupleRepository.java
# validateTimeout): a timeout_s above the cap is rejected SYNCHRONOUSLY
# with a 400 TimeoutTooLong error before the request ever parks -- there
# is no server-side clamp to 25 s. That is the observed contract this
# leg pins: a client cannot make the engine park past the cap through
# this route, so a genuine edge 504 for an over-cap tuple park is
# structurally unreachable, not merely avoided by convention. The probe
# still checks for an actual 504 rather than assuming the 400, in case
# the engine's behavior has changed since.
_leg_enter F "RDR-205 tuple-space CA 3 (park cap, over-cap rejection, registry sources)"
uv run python - <<'PY' || _leg_fail "F: tuple-space CA 3 probe failed (see above)"
import sys
import time
import uuid
import httpx
from nexus.db.t2.http_tuple_store import HttpTupleStore, TimeoutTooLongError

bad = False
store = HttpTupleStore()
addr = f"ccpg-probe-{int(time.time())}-{uuid.uuid4().hex[:6]}"
subspace = f"mailbox/{addr}"
pattern = {"to": addr}

# [F1] a park at the 25 s cap on a subspace with nothing in it returns the
# probe result (empty) without raising -- no 504, no typed error.
t0 = time.monotonic()
try:
    rows = store.rd(subspace, pattern, timeout_s=25)
    elapsed = time.monotonic() - t0
    if rows != []:
        print(f"  VIOLATION [F1]: expected an empty result on a fresh probe "
              f"subspace, got {rows!r}", file=sys.stderr)
        bad = True
    elif elapsed < 20:
        print(f"  VIOLATION [F1]: returned in {elapsed:.1f}s, well under the "
              "25s cap -- this did not exercise an actual park", file=sys.stderr)
        bad = True
    else:
        print(f"  ok [F1]: 25s park on an empty subspace returned the probe "
              f"result (empty) in {elapsed:.1f}s, no 504")
except Exception as exc:
    elapsed = time.monotonic() - t0
    print(f"  VIOLATION [F1]: 25s park raised after {elapsed:.1f}s instead of "
          f"returning the probe result: {exc!r}", file=sys.stderr)
    bad = True

# [F2] timeout_s=31 (above the 25s cap) -- assert the OBSERVED contract,
# not an assumed one: a fast synchronous TimeoutTooLong rejection, an
# actual edge 504, or (if the engine ever changes to clamp) a bounded
# non-error response are all read here rather than presumed.
t0 = time.monotonic()
try:
    rows = store.rd(subspace, pattern, timeout_s=31)
    elapsed = time.monotonic() - t0
    if elapsed >= 28:
        print(f"  VIOLATION [F2]: an unrejected timeout_s=31 request took "
              f"{elapsed:.1f}s -- indistinguishable from a park that would "
              "reach the edge's 30s window", file=sys.stderr)
        bad = True
    else:
        print(f"  ok [F2]: engine accepted timeout_s=31 without error in "
              f"{elapsed:.1f}s (rows={rows!r}) -- apparently clamped below "
              "the edge's 30s window; the >25s case still could not reach it")
except TimeoutTooLongError as exc:
    elapsed = time.monotonic() - t0
    if elapsed >= 10:
        print(f"  VIOLATION [F2]: TimeoutTooLong took {elapsed:.1f}s to "
              "arrive -- not the fast synchronous rejection the cap's "
              "justification depends on", file=sys.stderr)
        bad = True
    else:
        print(f"  ok [F2]: timeout_s=31 (> 25s cap) rejected SYNCHRONOUSLY "
              f"as TimeoutTooLong in {elapsed:.1f}s ({exc}) -- the engine "
              "refuses the request before ever parking, so a >25s park can "
              "never reach the edge's 30s window through this route; this "
              "is the contract that justifies the cap, not an edge 504")
except httpx.HTTPStatusError as exc:
    elapsed = time.monotonic() - t0
    status = exc.response.status_code
    if status == 504:
        print(f"  ok [F2]: timeout_s=31 (> 25s cap) hit the edge's 504 "
              f"after {elapsed:.1f}s -- the control-plane cutoff CA 3 "
              "names, pinned directly")
    else:
        print(f"  VIOLATION [F2]: timeout_s=31 request failed with "
              f"unexpected HTTP {status} after {elapsed:.1f}s: {exc}",
              file=sys.stderr)
        bad = True
except Exception as exc:
    elapsed = time.monotonic() - t0
    print(f"  VIOLATION [F2]: timeout_s=31 request failed unexpectedly "
          f"after {elapsed:.1f}s: {exc!r}", file=sys.stderr)
    bad = True

# [F3] registry() reports the resources source ONLY -- an
# NX_TUPLE_TEMPLATE_DIR set in production would append a second entry
# and must fail this leg, not pass silently.
try:
    reg = store.registry()
    sources = reg.get("sources")
    if sources != ["resources"]:
        print(f"  VIOLATION [F3]: registry() sources={sources!r}, expected "
              "exactly ['resources'] -- a second entry means "
              "NX_TUPLE_TEMPLATE_DIR is set in production", file=sys.stderr)
        bad = True
    else:
        print(f"  ok [F3]: registry() sources={sources!r} (resources only)")
except Exception as exc:
    print(f"  VIOLATION [F3]: registry() call failed: {exc!r}", file=sys.stderr)
    bad = True

sys.exit(1 if bad else 0)
PY

# ── Leg H: RDR-206 renew and ack-with-reply through the edge (nexus-zjzt1) ─
# conexus's STEP-6 gate reaches none of /v1/tuples, and leg F covers
# RDR-205 only, so without this leg the two RDR-206 routes are live in
# production with no proof they work through the public edge (the
# nexus-bwulw class). WRITES: a request and its reply into two fresh
# mailbox/<ccpg-...> addresses in the live tenant, both consumed or aging out
# at the template's 7-day retention; a refused reply writes nothing.
_leg_enter H "RDR-206 renew and ack-with-reply (lease moves, reply lands, keys-only target refused)"
uv run python - <<'PY' || _leg_fail "H: RDR-206 renew/ack-with-reply probe failed (see above)"
import sys
import time
import uuid
from datetime import datetime
from nexus.db.t2.http_tuple_store import (
    HttpTupleStore,
    ReplySpec,
    SchemaViolationError,
)

bad = False
store = HttpTupleStore()
stamp = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
asker, answerer = f"ccpg-asker-{stamp}", f"ccpg-answerer-{stamp}"
req_sub, reply_sub = f"mailbox/{answerer}", f"mailbox/{asker}"


def _claim(tag: str) -> tuple[str, str] | None:
    store.out(req_sub, {"to": answerer}, {"from": asker, "kind": "request"},
              f"cloud gate {tag}", nonce=f"{stamp}-{tag}")
    got = store.inp(req_sub, {"to": answerer}, claimant=answerer, lease_s=60)
    if got is None:
        print(f"  VIOLATION [H]: the {tag} request was not claimable", file=sys.stderr)
        return None
    row, claim_id = got
    return row.lease_until or "", claim_id


try:
    # [H1] renew moves the lease forward by the engine's own clock.
    first = _claim("renew")
    if first is None:
        bad = True
    else:
        before, claim_id = first
        after = store.renew(claim_id, answerer, 600)
        prior = datetime.fromisoformat(before.replace("Z", "+00:00"))
        if (after - prior).total_seconds() < 300:
            print(f"  VIOLATION [H1]: renew 600s moved the lease {prior} -> {after}, "
                  "less than 300s forward", file=sys.stderr)
            bad = True
        else:
            print(f"  ok [H1]: renew moved lease_until {prior.isoformat()} -> {after.isoformat()}")

        # [H2] ack with a reply returns a hex reply id and the reply is readable.
        reply_id = store.ack(claim_id, answerer, reply=ReplySpec(
            subspace=reply_sub, keys={"to": asker},
            dims={"from": answerer, "kind": "ack"}, body="cloud gate reply"))
        rows = store.rdp(reply_sub, {"to": asker}, n=2)
        census = store.subspace_stats(req_sub)
        if not (reply_id and len(reply_id) == 64):
            print(f"  VIOLATION [H2]: ack with reply returned {reply_id!r}, not a 64-hex id",
                  file=sys.stderr)
            bad = True
        elif [r.id for r in rows] != [reply_id]:
            print(f"  VIOLATION [H2]: reply address holds {[r.id for r in rows]}, "
                  f"expected exactly [{reply_id}]", file=sys.stderr)
            bad = True
        elif census.consumed < 1 or census.claimed != 0:
            print(f"  VIOLATION [H2]: request census after ack {census}", file=sys.stderr)
            bad = True
        else:
            print(f"  ok [H2]: ack with reply returned {reply_id[:12]}..., exactly that row "
                  "at the reply address, request consumed")
        # Consume the reply, so the live tenant keeps no unclaimed gate mail.
        # Left in place, it turned the doctor's tuples.oldest_unclaimed red an
        # hour after every run (shakedown 2026-09-15).
        drained = store.inp(reply_sub, {"to": asker}, claimant=asker, lease_s=60)
        if drained is None:
            print("  VIOLATION [H2]: the reply could not be claimed for cleanup",
                  file=sys.stderr)
            bad = True
        else:
            store.ack(drained[1], asker)

    # [H3] a reply into a keys-only template (the ledger) is refused before
    # anything is consumed, so the same claimant can still ack plainly.
    second = _claim("refused")
    if second is None:
        bad = True
    else:
        _, claim_id = second
        try:
            store.ack(claim_id, answerer, reply=ReplySpec(
                subspace=f"ledger/ccpg-{stamp}", keys={"agent_id": "ccpg", "kind": "report"}))
            print("  VIOLATION [H3]: a reply into ledger/ was accepted", file=sys.stderr)
            bad = True
        except SchemaViolationError as exc:
            if store.ack(claim_id, answerer) is not None:
                print("  VIOLATION [H3]: the follow-up plain ack returned a reply id",
                      file=sys.stderr)
                bad = True
            else:
                print(f"  ok [H3]: keys-only reply target refused ({exc}); the request "
                      "stayed claimed and a plain ack then consumed it")
except Exception as exc:
    print(f"  VIOLATION [H]: RDR-206 probe raised {exc!r}", file=sys.stderr)
    bad = True

sys.exit(1 if bad else 0)
PY

# ── Leg I: descending tuple read through the edge (nexus-kp5q3) ──────────
# `nx tuple rd --newest` asks for order=desc and trusts only a response that
# echoes "order":"desc" and the "limit" it ran with. Nothing else notices if
# that echo is lost between the engine and the client: the CLI just pages
# oldest-first again. So this leg asserts the echo itself, on a probe subspace
# with no rows (the engine echoes regardless of rows; nothing is written).
_leg_enter I "descending tuple read echo (order=desc, limit) through the edge (nexus-kp5q3)"
uv run python - <<'PY' || _leg_fail "I: descending tuple read probe failed (see above)"
import sys
import time
import uuid

from nexus.db.t2.http_tuple_store import DescendingReadUnsupportedError, HttpTupleStore

store = HttpTupleStore()
addr = f"ccpg-desc-{int(time.time())}-{uuid.uuid4().hex[:6]}"
try:
    read = store.rd_newest(f"mailbox/{addr}", {"to": addr}, n=1)
except DescendingReadUnsupportedError as exc:
    print(f"  VIOLATION [I]: the descending read came back without its echo ({exc}) -- "
          "the engine predates order=desc or the edge stripped order/limit from the "
          "response; `nx tuple rd --newest` silently reverts to oldest-first paging",
          file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"  VIOLATION [I]: descending tuple read raised {exc!r}", file=sys.stderr)
    sys.exit(1)
finally:
    store.close()
if read.rows != [] or read.limit != 1:
    print(f"  VIOLATION [I]: expected an empty page with limit 1 on a fresh probe "
          f"subspace, got rows={read.rows!r} limit={read.limit!r}", file=sys.stderr)
    sys.exit(1)
print("  ok [I]: descending rd echoed order=desc and limit=1 through the edge")
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
if [ "$B3_NOT_RUN" = 1 ]; then
    echo "CLOUD CLIENT-PATH GATE PASSED — legs=$LEGS_RAN/$EXPECTED_LEGS violations=0 (ownerless-write mode NOT asserted: B3 not run)"
else
    echo "CLOUD CLIENT-PATH GATE PASSED — legs=$LEGS_RAN/$EXPECTED_LEGS violations=0"
fi
