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
#   G  RDR-205 ledger tuple projector hook drive (nexus-g2lln pre-tag
#      proof, bead nexus-cbo4a): drives THIS CHECKOUT's real
#      conexus/hooks/scripts/subagent-{start,stop}-tuple-async.sh wrapper
#      scripts with a synthetic SubagentStart/SubagentStop payload against
#      this box's live cloud config, then polls ledger/<sid> for the two
#      tuples they are supposed to write. Closes the blind spot every
#      tuple_ledger_project.py unit test hides (each one hand-writes the
#      data-token lease the projector reads) -- no prior gate drove the
#      wrapper scripts against a real install at all. WRITES two tuples
#      to a fresh ledger/<random-uuid> subspace (like leg E's T2 write,
#      not read-only); they are not cleaned up (ledger take is disabled
#      by design -- they age out at the subspace's normal retention).
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
# EXPECTED_LEGS=6 (dated 2026-09-11): [A] /version, [B] /health
# authenticated, [C+D] client probe heredoc (one shell-side entry for the
# combined python leg), [E] T2 write body carrying shell-substitution text
# (nexus-cmzib WAF passthrough), [F] RDR-205 tuple-space CA 3 through the
# edge (nexus-em75s.15), [G] ledger tuple projector hook drive (nexus-g2lln
# pre-tag proof, nexus-cbo4a). Editing the battery means updating this
# constant in the same diff.
LEGS_RAN=0
EXPECTED_LEGS=6
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

# ── Leg G: RDR-205 ledger tuple projector hook drive (nexus-g2lln pre-tag
#    proof, bead nexus-cbo4a) ──────────────────────────────────────────────
# No prior gate drove the SubagentStart/SubagentStop ledger-projection hook
# wrappers against a REAL install: every case in
# tests/hooks/test_tuple_ledger_project.py hand-writes the data-token lease
# the projector reads, which is exactly why nexus-0zsmg (the projector dead
# on every cloud-mode box -- no endpoint resolution for the managed
# service_url leg) shipped through 7.41.0 unnoticed. This drives THIS
# CHECKOUT's own conexus/hooks/scripts/subagent-{start,stop}-tuple-
# async.sh -- the wheel does not ship them (only conexus/plans/ travels
# into the Python package; the plugin runs these from the repo/plugin
# install, never from site-packages) -- against THIS BOX's real cloud
# config and live engine, with a synthetic payload for a fresh
# ledger/<random-uuid> subspace, then polls for the two tuples they are
# supposed to write. The nexus-0zsmg endpoint fix is already on this tree
# (b24eb57c0), so this leg is expected GREEN here; it would have FAILED on
# v7.41.0 (see the counter-proof this bead's hand-back runs separately
# with the v7.41.0 projector swapped in via a temp dir).
# Read the subspace through the client library, never the `nx` binary: an
# `nx` invocation stamps last_seen_version into the operator's real
# ~/.config/nexus (tests/test_e2e_gates_isolate_home.py), and this gate has
# no sandbox HOME by design (leg G must use the box's real cloud config).
# Prints total=N, start=0|1, report=0|1 for the given session and agent.
_hook_read() {
    HOOK_READ_SID="$1" HOOK_READ_AGENT="$2" uv run python - <<'PY' 2>/dev/null || printf 'total=0\nstart=0\nreport=0\n'
import os
from nexus.db.t2.http_tuple_store import HttpTupleStore
sid = os.environ["HOOK_READ_SID"]; agent = os.environ["HOOK_READ_AGENT"]
store = HttpTupleStore()
sub = f"ledger/{sid}"
print(f"total={store.subspace_stats(sub).total}")
for kind in ("start", "report"):
    rows = store.rd(sub, keys_pattern={"agent_id": agent, "kind": kind}, n=5)
    print(f"{kind}={1 if rows else 0}")
PY
}
_leg_enter G "ledger tuple projector hook drive (SubagentStart/SubagentStop, nexus-g2lln)"
HOOK_SID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
HOOK_AGENT="cloudgate-hook-probe"
HOOK_LOG="$HOME/.local/state/nexus/orchestration/$HOOK_SID.tuple-projection.log"

printf '{"session_id":"%s","agent_id":"%s","agent_type":"Explore","hook_event_name":"SubagentStart"}' \
    "$HOOK_SID" "$HOOK_AGENT" | bash "$REPO_ROOT/conexus/hooks/scripts/subagent-start-tuple-async.sh"
printf '{"session_id":"%s","agent_id":"%s","hook_event_name":"SubagentStop"}' \
    "$HOOK_SID" "$HOOK_AGENT" | bash "$REPO_ROOT/conexus/hooks/scripts/subagent-stop-tuple-async.sh"

# Both wrappers detach a background subshell and return in milliseconds
# (see their own headers) -- the actual resolve+POST can take up to the
# projector's 5s per-call timeout, so poll rather than assume completion.
HOOK_DEADLINE=$(( $(date +%s) + 30 ))
HOOK_TOTAL=0
HOOK_STATS=""
while :; do
    HOOK_STATS="$(_hook_read "$HOOK_SID" "$HOOK_AGENT")"
    HOOK_TOTAL="$(printf '%s\n' "$HOOK_STATS" | sed -n 's/^total=//p')"
    [ -n "$HOOK_TOTAL" ] || HOOK_TOTAL=0
    if [ "$HOOK_TOTAL" -ge 2 ] 2>/dev/null; then
        break
    fi
    HOOK_SKIP_LINES=0
    if [ -f "$HOOK_LOG" ]; then
        HOOK_SKIP_LINES="$(grep -c 'SKIP' "$HOOK_LOG" 2>/dev/null || echo 0)"
    fi
    # Both kinds have already given up -- no further wait will change that.
    if [ "$HOOK_SKIP_LINES" -ge 2 ] 2>/dev/null; then
        break
    fi
    [ "$(date +%s)" -lt "$HOOK_DEADLINE" ] || break
    sleep 1
done

HOOK_STATS="$(_hook_read "$HOOK_SID" "$HOOK_AGENT")"
HOOK_START_ROWS="$(printf '%s\n' "$HOOK_STATS" | sed -n 's/^start=//p')"
HOOK_REPORT_ROWS="$(printf '%s\n' "$HOOK_STATS" | sed -n 's/^report=//p')"
HOOK_LOG_CONTENT=""
[ -f "$HOOK_LOG" ] && HOOK_LOG_CONTENT="$(cat "$HOOK_LOG")"

HOOK_FAIL=""
if ! [ "$HOOK_TOTAL" -ge 2 ] 2>/dev/null; then
    HOOK_FAIL="ledger/$HOOK_SID total=$HOOK_TOTAL (want >=2) after 30s"
fi
[ "$HOOK_START_ROWS" = "1" ] || HOOK_FAIL="${HOOK_FAIL:+$HOOK_FAIL; }no kind=start row for agent_id=$HOOK_AGENT"
[ "$HOOK_REPORT_ROWS" = "1" ] || HOOK_FAIL="${HOOK_FAIL:+$HOOK_FAIL; }no kind=report row for agent_id=$HOOK_AGENT"
case "$HOOK_LOG_CONTENT" in
    *SKIP*) HOOK_FAIL="${HOOK_FAIL:+$HOOK_FAIL; }projection log carries a SKIP line" ;;
esac

if [ -n "$HOOK_FAIL" ]; then
    echo "  tuple stats: $HOOK_STATS" >&2
    echo "  kind=start rows: $HOOK_START_ROWS" >&2
    echo "  kind=report rows: $HOOK_REPORT_ROWS" >&2
    echo "  projection log ($HOOK_LOG):" >&2
    printf '%s\n' "$HOOK_LOG_CONTENT" >&2
    _leg_fail "G: tuple ledger projector hook drive: $HOOK_FAIL"
else
    echo "  ok [G]: ledger/$HOOK_SID total=$HOOK_TOTAL, kind=start and kind=report rows present for $HOOK_AGENT, no SKIP in the projection log"
fi

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
