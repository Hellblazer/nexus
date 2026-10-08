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
#   J  engine reaper liveness on the /v1/status body through the edge
#      (nexus-wbfpw.50, widened by the RDR-192 Phase 3 gate): the `reaper`
#      object is PRESENT, enabled, last_completed_pass_at is non-null and no
#      older than three of its own intervals plus the wall-clock budget (both
#      read from the same object), failed_passes_total is 0 (a since-boot
#      counter: a recovered blip keeps it nonzero, and the failure message says
#      so) and last_pass.tenants_errored is 0; the ok line prints the last_pass
#      counts. An edge that strips the key makes `nx doctor`'s Engine reaper row
#      read "not applicable", which looks healthy, so the gate asserts the key.
#      Tenant counts are never hardcoded. A freshly booted engine fails this leg until
#      its first pass (about a minute after start): run it a few minutes after
#      a deploy, never inside the boot window. Read-only (reuses leg B's body).
#   K  RDR-191/192 vector sweep routes through the edge (nexus-wbfpw.50): the
#      engine's own JSON (not an edge page) and the status codes the client
#      branches on, using ONLY requests that cannot move, restore, expire or
#      delete anything (each is refused by validation, answers 404, or reads an
#      unregistered collection name). Each 400 asserts a fragment of the ENGINE's
#      own message (an edge or WAF can answer a 400 with a JSON error of its own),
#      and K1 the engine's exact 404 body. The read-only property is pinned by a
#      structural allowlist audit in tests/e2e/cloud_client_path_gate_b3_test.sh,
#      itself falsified by negative tests over mutated copies of this file.
#      Read from code, NOT pinned by an engine route test: K2's 422
#      reason=unregistered_collection for quarantine-restore and K8/K11's 200 empty
#      result for an unregistered name. A first live red on K2, K8 or K11 means
#      re-check the engine's behaviour before blaming the edge.
#        K1  POST /v1/vectors/gc/<no-such-route>        404 + JSON (the client
#            reads a 404 as "engine predates the route", exit 4 of the
#            quarantine-restore verb; the real route cannot be used for this, it
#            exists on the deployed engine)
#        K2  gc/quarantine-restore, dry_run, unregistered origin   422,
#            reason=unregistered_collection (a throwaway tenant, or this one,
#            has no such origin, so the engine refuses before it looks anywhere)
#        K3  gc/quarantine-restore, no source                      400
#        K4  gc/quarantine-restore, two sources                    400
#        K5  gc/quarantine-orphans, empty body                     400
#        K6  gc/restore-rereferenced, empty body                   400
#        K7  gc/expire-quarantine, empty body                      400
#        K8  POST /v1/vectors/reapable, unregistered collection    200, empty
#            page in the engine's shape (collection, grace_seconds null,
#            returned 0, next_after null, chunks [])
#        K9  reapable, a quarantine- collection                    400
#        K10 reapable, grace_seconds=-1                            400
#        K11 POST /v1/vectors/manifest-less-census, unregistered   200, empty
#            census (all five bucket totals, scope_chunk_total 0)
#        K12 manifest-less-census, a quarantine- collection        400
#      NOT COVERED, and why (no read-only or validation-only form exists):
#        - the 200 success shape of quarantine-orphans, restore-rereferenced,
#          expire-quarantine and quarantine-restore (each moves, restores or
#          deletes; quarantine-restore's own dry run needs a REGISTERED origin,
#          which is a catalog write);
#        - the typed 503 quarantine_restore_busy (reason, Retry-After: 5,
#          nothing_moved): it fires only when a sweep gate or an index-run lock
#          is held past 2 s, which cannot be provoked from outside without
#          writes;
#        - GET /v1/vectors/count, the denominator of the reaper runbook's
#          reapable-ratio preview: no leg here or in the bead's scope, and a
#          read of an unregistered collection is not known to answer 200.
#      Every K request goes with the same bearer leg B resolved. The 200 probes
#      read a collection name no tenant has registered, so they touch no data.
#   L  the per-collection search route through the edge (nexus-tu8wp.3): POST
#      /v1/vectors/search-per-collection with two requests the engine refuses by
#      validation before it touches a collection, an embedder or the database
#      (per_collection_k 0; limit 1201). A route-serving engine
#      (engine-service-v0.1.147 and later) answers 400 with its own JSON error
#      (per_collection_k must be in 1.. / limit must be in 1..); an older engine
#      answers 404 with exactly {"error": "not found"}. Both must ARRIVE as the
#      engine's JSON through the public edge: the client falls back to the batched
#      path on a 404, an edge refusal, 403, 405 or 501 (for 10 minutes) and on a
#      500 (for 60 s), and does so quietly, so an edge that refuses the new path
#      leaves the feature inert with nothing else noticing (the nexus-bwulw class).
#      The verdict is _route_probe_verdict. The leg passes on either engine, and
#      when it sees the old engine's 404 the final sentinel line says "per-collection
#      route NOT served"; NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=served turns that
#      into a failure (run it so after the deploy of the route's engine) and =absent
#      turns a served route into one. Read-only by construction, pinned by the same
#      structural audit as leg K (tests/e2e/cloud_client_path_gate_b3_test.sh).
#   M  the engine surfaces cut after engine-service-v0.1.151 (nexus-tjyzn, nexus-mz9jv, nexus-92q1p), through
#      the edge, with the real client for the last: (M1) a response of 1 KiB or more asked for with
#      `Accept-Encoding: gzip` comes back `Content-Encoding: gzip` and decodes to the identity body (an edge
#      may strip or normalise the header, or buffer and recompress); (M2) `GET /v1/vectors/stats?fields=routing`
#      returns catalog rows (name, lifecycle_state, ...) with no count/dim/stored_count/last_write; (M3)
#      `HttpVectorClient.search_per_collection(include_embeddings=True)` over one live collection returns
#      rows with a vector, `embedding_dim` > 0 and each vector `dim * 4` bytes. Read-only: two GETs, and one
#      search (which embeds one short query). NON-VACUITY: from engine release_version >= ENGINE_SURFACES_MIN
#      (default 0.1.152, `NX_ENGINE_SURFACES_MIN` overrides) the three MUST be observed served, and a body too
#      small to exercise gzip is a failure, not a skip; below it an engine that does not serve a surface
#      passes with the sentinel line saying "new engine surfaces NOT served".
#      NX_EXPECTED_ENGINE_SURFACES=served forces the strict reading, =absent asserts the old engine.
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
#   NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=served tests/e2e/cloud-client-path-gate.sh
#       asserts leg L sees the per-collection search route SERVED (nexus-tu8wp.3):
#       set it for the run after engine-service-v0.1.147's deploy, where an old
#       engine's 404 would otherwise pass with a NOT SERVED note on the sentinel
#       line. `absent` asserts the opposite (the route not yet deployed).
#   NX_EXPECTED_ENGINE_SURFACES=served tests/e2e/cloud-client-path-gate.sh
#       asserts leg M sees gzip, the routing listing and include_embeddings SERVED regardless of the engine's
#       release_version (nexus-tjyzn / nexus-mz9jv / nexus-92q1p); `absent` asserts the opposite. Unset, the
#       engine's release_version decides (see leg M above).
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

# nexus-tu8wp.3: the optional assertion on leg L (the per-collection search route).
NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE="${NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE:-}"
case "$NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE" in
    ""|served|absent) ;;
    *) echo "FATAL: NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=$NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE is not one of: (unset), served, absent." >&2; exit 2 ;;
esac

# nexus-tjyzn / nexus-mz9jv / nexus-92q1p: leg M's expectation and the first engine release that carries the
# three surfaces (the first cut after engine-service-v0.1.151; update it if the cut is numbered differently).
NX_EXPECTED_ENGINE_SURFACES="${NX_EXPECTED_ENGINE_SURFACES:-}"
case "$NX_EXPECTED_ENGINE_SURFACES" in
    ""|served|absent) ;;
    *) echo "FATAL: NX_EXPECTED_ENGINE_SURFACES=$NX_EXPECTED_ENGINE_SURFACES is not one of: (unset), served, absent." >&2; exit 2 ;;
esac
ENGINE_SURFACES_MIN="${NX_ENGINE_SURFACES_MIN:-0.1.152}"

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
# EXPECTED_LEGS=11 (dated 2026-09-13; [B] redefined 2026-09-28; [I] added 2026-09-29;
# [J] and [K] added 2026-10-04, nexus-wbfpw.50; [L] added 2026-10-05, nexus-tu8wp.3;
# [G] deleted at cleanup step A1, nexus-0r1uz): [A] /version,
# [B] edge auth contract (unauthenticated /health refused, data token
# accepted on /v1), [C+D] client probe heredoc (one shell-side entry for the
# combined python leg), [E] T2 write body carrying shell-substitution text
# (nexus-cmzib WAF passthrough), [F] RDR-205 tuple-space CA 3 through the
# edge (nexus-em75s.15), [H] RDR-206 renew and ack-with-reply through
# the edge (nexus-zjzt1, 2026-09-13), [I] descending tuple read echo through
# the edge (nexus-kp5q3, 2026-09-29), [J] engine reaper liveness on /v1/status
# through the edge, [K] the vector sweep routes through the edge (both
# nexus-wbfpw.50, 2026-10-04), [L] the per-collection search route through the
# edge (nexus-tu8wp.3, 2026-10-05), [M] gzip, the routing listing and include_embeddings through the
# edge (nexus-tjyzn, nexus-mz9jv, nexus-92q1p, 2026-10-08). Editing the battery means updating this
# constant in the same diff.
LEGS_RAN=0
EXPECTED_LEGS=11
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

# Leg J's compare logic (nexus-wbfpw.50): the engine reaper's liveness read from the
# `reaper` object of /v1/status through the edge. Takes the status body and, for the
# test, a fixed "now" in epoch seconds (default: the real clock). Prints one line and
# returns 0 = holds, 1 = violation. There is no not-run state: unlike the ownerless-
# write mode, this check has no unset knob, and a body without the object is exactly
# the failure the leg exists for (an edge that strips the key makes `nx doctor`'s
# Engine reaper row read "not applicable", which looks healthy).
#   - body unreadable (not a status body)                          -> 1
#   - `reaper` absent or not an object                             -> 1
#   - enabled is not true                                          -> 1
#   - interval_seconds / wall_clock_budget_seconds unusable        -> 1
#   - last_completed_pass_at null (no pass yet) or unparseable     -> 1
#   - last pass older than 3 * interval + wall_clock_budget        -> 1
#   - failed_passes_total absent, not an int, or not 0             -> 1
#     (a since-boot counter: the message says so, so a recovered blip is not read as live)
#   - last_pass absent, or its tenants_errored absent or not 0     -> 1
# The test (tests/e2e/cloud_client_path_gate_b3_test.sh) sources this function from
# the real script.
_reaper_status_verdict() {
    local body="$1" now="${2:-}"
    "$E2E_PYTHON" - "$body" "$now" <<'PY'
import json
import sys
import time
from datetime import datetime, timezone

body, now_arg = sys.argv[1], sys.argv[2]
now = float(now_arg) if now_arg else time.time()


def violation(msg):
    print("J: " + msg)
    sys.exit(1)


try:
    doc = json.loads(body)
    if not isinstance(doc, dict) or "embedding_mode" not in doc:
        raise ValueError("not a status body")
except Exception:
    violation("/v1/status through the edge returned no readable status body (a curl failure, or an edge "
              "401/403/502/WAF page), so the engine reaper's liveness could not be read")
reaper = doc.get("reaper")
if not isinstance(reaper, dict):
    violation("/v1/status carries no `reaper` object (got %r): the engine predates the reaper liveness field, or the "
              "edge strips it, and `nx doctor`'s Engine reaper row then reads not applicable, which looks healthy"
              % (reaper,))
errs = []
if reaper.get("enabled") is not True:
    errs.append("reaper.enabled is %r, expected true (NX_REAPER_ENABLED=false, or no reaper scheduled in this process)"
                % (reaper.get("enabled"),))


def whole(key, floor):
    v = reaper.get(key)
    if type(v) is not int or v < floor:
        errs.append("reaper.%s is %r, expected an integer >= %d" % (key, v, floor))
        return None
    return v


interval = whole("interval_seconds", 1)
budget = whole("wall_clock_budget_seconds", 0)
last_raw = reaper.get("last_completed_pass_at")
last = None
if last_raw is None:
    errs.append("reaper.last_completed_pass_at is null: the reaper has made no completed pass (a young engine's first "
                "pass is due about a minute after boot, so run this leg a few minutes after a deploy; later, a dead reaper)")
else:
    try:
        last = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        errs.append("reaper.last_completed_pass_at is %r, not an ISO-8601 instant" % (last_raw,))
age = limit = None
if last is not None and interval is not None and budget is not None:
    age = now - last.timestamp()
    limit = 3 * interval + budget
    if age > limit:
        errs.append("last completed pass was %ds ago, more than 3 intervals of %ds plus the %ds wall-clock budget (%ds): "
                    "the reaper may be dead" % (age, interval, budget, limit))
failed = reaper.get("failed_passes_total")
if type(failed) is not int:
    errs.append("reaper.failed_passes_total is %r, expected the integer 0" % (failed,))
elif failed != 0:
    errs.append("reaper.failed_passes_total is %d, expected 0. It is a since-boot counter: it stays nonzero after a "
                "recovered blip, so read last_completed_pass_at above and the engine log (event=reaper_pass_failed) "
                "before treating this as a live outage" % failed)
last_pass = reaper.get("last_pass")
lp_text = "last_pass absent"
if not isinstance(last_pass, dict):
    errs.append("reaper.last_pass is %r, expected an object once a pass has completed" % (last_pass,))
else:
    errored = last_pass.get("tenants_errored")
    if type(errored) is not int:
        errs.append("reaper.last_pass.tenants_errored is %r, expected the integer 0" % (errored,))
    elif errored != 0:
        errs.append("reaper.last_pass.tenants_errored is %d, expected 0: the last pass completed but could not work on "
                    "that many tenants (read the engine log for the per-tenant error)" % errored)
    lp_text = "last_pass visited=%s errored=%s refused=%s empty=%s" % tuple(
        last_pass.get(k, "absent") for k in ("tenants_visited", "tenants_errored", "tenants_refused", "tenants_empty"))
if errs:
    violation("; ".join(errs))
print("ok [J]: reaper enabled, last completed pass %ds ago (limit %ds = 3 x %ds + %ds budget), failed_passes_total=0, %s"
      % (age, limit, interval, budget, lp_text))
PY
}

# Leg L's judge (nexus-tu8wp.3): one refused request to POST
# /v1/vectors/search-per-collection, read through the edge. Arguments: label, HTTP
# status, response body, the fragment of the engine's own 400 message the probe
# expects, and the expectation (NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE: empty,
# `served` or `absent`). Prints one line; exit status:
#   0  route SERVED: 400 + the engine's own JSON error carrying the fragment
#      (engine-service-v0.1.147 and later; the request is refused by validation
#      before any collection, embedding or database is touched)
#   3  route ABSENT, unasserted: 404 + exactly the engine's {"error":"not found"}
#      (an engine before the route; the client reads this as "fall back to the
#      batched path" and remembers it for 10 minutes). The final sentinel line
#      says so, so an old engine can never read as a clean pass of the route.
#   4  route ABSENT, asserted (NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=absent)
#   1  anything else, each status named with what the client does with it:
#      403/405/501 or an edge-generated page (client falls back for 10 min, silently),
#      500 (falls back for 60 s), 429/502/503/504 (a failed model group), 200 (a
#      refused request was answered), a 404 that is not the engine's body, a 400 with
#      an edge's JSON of its own. `served` with the route absent is a violation, and
#      so is `absent` with the route served.
# The test (tests/e2e/cloud_client_path_gate_b3_test.sh) sources this function from
# the real script.
_route_probe_verdict() {
    "$E2E_PYTHON" - "$1" "$2" "$3" "$4" "${5:-}" <<'PY'
import json
import sys

label, code, body, fragment, expected = sys.argv[1:6]
head = body[:160].replace("\n", " ")


def violation(msg):
    print("%s: %s" % (label, msg))
    sys.exit(1)


if not fragment:
    violation("internal: a route probe must name the engine message fragment it expects")
try:
    doc = json.loads(body)
except ValueError:
    doc = None
if code in ("403", "405", "501"):
    violation("HTTP %s from the edge for the route (body: %r): the client reads 403/405/501 as route-absent and runs "
              "the batched path for 10 minutes, logging only a WARNING, so the feature is silently inert" % (code, head))
if code == "500":
    violation("HTTP 500 for a request the engine must refuse with 400 (body: %r): the client falls back to the batched "
              "path for 60 s on a 500" % (head,))
if code in ("429", "502", "503", "504"):
    violation("HTTP %s (body: %r): the client does not fall back on this; it reports the whole model group as failed"
              % (code, head))
if code == "404":
    if doc != {"error": "not found"}:
        violation("HTTP 404 with body %r, expected exactly the engine's {\"error\": \"not found\"}: an edge-generated 404 "
                  "(the client treats any 404 as route-absent, so this reads the same, but it is the edge, not the engine, "
                  "saying so)" % (head,))
    if expected == "served":
        violation("the route is ABSENT (404, the engine's own not-found body) but NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=served: "
                  "the deployed engine predates engine-service-v0.1.147, so every client runs the batched fallback")
    if expected == "absent":
        print("ok [%s]: route absent as expected: 404 with the engine's own JSON, which the client reads as fall back to the "
              "batched path" % label)
        sys.exit(4)
    print("NOT SERVED [%s]: 404 with the engine's own {\"error\": \"not found\"}: the engine predates the per-collection route, "
          "and the client falls back to the batched path (set NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=served to make this a "
          "failure after the deploy)" % label)
    sys.exit(3)
if code == "400":
    if not isinstance(doc, dict):
        violation("HTTP 400 body is not a JSON object (%r): an edge page or a stripped body, not the engine's answer" % (head,))
    err = doc.get("error")
    if not (isinstance(err, str) and fragment in err):
        violation("HTTP 400 'error' is %r, expected it to contain the engine's %r (an edge or WAF answers a 400 with JSON of its "
                  "own, and the request may never have reached the engine)" % (err, fragment))
    if expected == "absent":
        violation("the route is SERVED (400 with the engine's validation message) but NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE=absent")
    print("ok [%s]: route served: HTTP 400, the engine's own JSON (%r)" % (label, err))
    sys.exit(0)
violation("HTTP %s (body: %r), expected the engine's 400 (route served) or its JSON 404 (route absent): a request the engine "
          "refuses by validation was not answered like one" % (code, head))
PY
}

# Leg M's mode (nexus-tjyzn / nexus-mz9jv / nexus-92q1p). Arguments: the /version body, the expectation
# (NX_EXPECTED_ENGINE_SURFACES: empty, `served` or `absent`), the first engine release that carries the
# surfaces. Prints one word: `strict` (the surfaces MUST be served: asserted, or the engine's release_version is at
# or above the minimum), `absent` (asserted not served) or `auto-old` (an engine below the minimum, nothing asserted:
# a surface not served passes with a note). A /version body whose release_version cannot be parsed is `strict`
# when nothing is asserted: an engine that cannot say what it is does not get the old-engine allowance.
_surfaces_mode() {
    "$E2E_PYTHON" - "$1" "${2:-}" "$3" <<'PY'
import json
import re
import sys

body, expected, minimum = sys.argv[1:4]


def parse(raw):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (raw or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


if expected in ("served", "absent"):
    print("strict" if expected == "served" else "absent")
    sys.exit(0)
try:
    version = parse(json.loads(body).get("release_version"))
except (ValueError, AttributeError):
    version = None
floor = parse(minimum)
print("auto-old" if (version is not None and floor is not None and version < floor) else "strict")
PY
}

# Leg M1 and M2's judge (nexus-tjyzn, nexus-mz9jv): the routing listing read twice through the edge, once with
# `Accept-Encoding: identity` and once with `gzip`. Arguments: which check (`gzip` or `routing`), the identity
# body file, the gzip-request body file, the gzip-request response-headers file (curl -D), the mode
# (_surfaces_mode). Prints one line; exit status: 0 served and correct, 3 NOT served (allowed only in `auto-old` and
# `absent`), 1 a violation. `strict` turns every "not served" into a violation, so once the engine is new enough
# the leg cannot pass by observing nothing.
_surface_verdict() {
    "$E2E_PYTHON" - "$1" "$2" "$3" "$4" "$5" <<'PY'
import gzip
import json
import sys

which, ident_path, gz_path, hdr_path, mode = sys.argv[1:6]
label = {"gzip": "M1", "routing": "M2"}[which]
GZIP_MIN_BYTES = 1024   # HttpUtil.GZIP_MIN_BYTES


def violation(msg):
    print("%s: %s" % (label, msg))
    sys.exit(1)


def not_served(msg):
    if mode == "strict":
        violation(msg + " -- the engine must serve this (release_version at or above the minimum, or NX_EXPECTED_ENGINE_SURFACES=served)")
    print("NOT SERVED [%s]: %s" % (label, msg))
    sys.exit(3)


def served(msg):
    if mode == "absent":
        violation("served, but NX_EXPECTED_ENGINE_SURFACES=absent: " + msg)
    print("ok [%s]: %s" % (label, msg))
    sys.exit(0)


ident = open(ident_path, "rb").read()
try:
    rows = json.loads(ident)
except ValueError:
    rows = None
if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and isinstance(r.get("name"), str) for r in rows):
    violation("GET /v1/vectors/stats?fields=routing did not answer a non-empty JSON list of rows with names through the edge "
              "(body: %r): an edge page, a stripped body, or a tenant with no collections" % (ident[:160],))

if which == "routing":
    counted = [r["name"] for r in rows if any(k in r for k in ("count", "dim", "stored_count", "last_write"))]
    if counted:
        not_served("the listing still carries counts or a dimension (e.g. %s): the engine ignored fields=routing and answered "
                   "the full stats" % counted[0])
    if not any(isinstance(r.get("lifecycle_state"), str) for r in rows):
        violation("no row carries lifecycle_state: the routing rows must carry the registry state the router filters on")
    served("%d catalog rows, name and registry attributes, no counts" % len(rows))

# which == "gzip"
raw_headers = open(hdr_path, "rb").read().decode("latin-1")
blocks = [b for b in raw_headers.replace("\r\n", "\n").split("\n\n") if b.strip()]
last = blocks[-1] if blocks else ""
encodings = [ln.split(":", 1)[1].strip().lower() for ln in last.split("\n")
             if ln.lower().startswith("content-encoding:")]
if len(ident) < GZIP_MIN_BYTES:
    not_served("the routing listing is %d bytes, below the engine's %d-byte compression threshold, so no read-only route "
               "exercises gzip for this tenant" % (len(ident), GZIP_MIN_BYTES))
if "gzip" not in encodings:
    not_served("a %d-byte response asked for with Accept-Encoding: gzip came back without Content-Encoding: gzip "
               "(Content-Encoding: %s): the engine does not compress, or the edge strips the header" % (len(ident), encodings or "absent"))
packed = open(gz_path, "rb").read()
try:
    decoded = json.loads(gzip.decompress(packed))
except (OSError, EOFError, ValueError) as exc:
    violation("Content-Encoding: gzip but the body does not gunzip to JSON (%s): the edge re-encoded or truncated it" % exc)
if decoded != rows:
    violation("the gzip response decodes to a different listing than the identity response (%d vs %d rows): re-run, and "
              "if it persists the edge is altering the body" % (len(decoded) if isinstance(decoded, list) else -1, len(rows)))
served("%d bytes identity, %d bytes gzip (%.1f to 1), decodes to the same %d rows" % (len(ident), len(packed), len(ident) / max(1, len(packed)), len(rows)))
PY
}

# Leg M3's judge (nexus-92q1p): the summary the real client's search_per_collection(include_embeddings=True) produced,
# as JSON {"collection", "rows", "embedding_dim", "vector_lens"}. Arguments: the summary, the mode. Exit status as
# _surface_verdict.
_embeddings_verdict() {
    "$E2E_PYTHON" - "$1" "$2" <<'PY'
import json
import sys

raw, mode = sys.argv[1:3]
label = "M3"


def violation(msg):
    print("%s: %s" % (label, msg))
    sys.exit(1)


try:
    s = json.loads(raw)
except ValueError:
    violation("the client probe printed no JSON summary (%r)" % (raw[:160],))
rows, dim, lens = s.get("rows"), s.get("embedding_dim"), s.get("vector_lens")
carried = [n for n in (lens or []) if n is not None]
if not isinstance(rows, int) or rows < 1:
    violation("the search over %r returned %r rows: nothing to carry a vector (a live collection must return rows)" % (s.get("collection"), rows))
if not carried and not dim:
    if mode == "strict":
        violation("rows came back with no vector and no embedding_dim echo: the engine must serve include_embeddings "
                  "(release_version at or above the minimum, or NX_EXPECTED_ENGINE_SURFACES=served)")
    print("NOT SERVED [M3]: %d rows, no vector and no embedding_dim echo: the engine predates include_embeddings, and the "
          "client fetches the vectors by id" % rows)
    sys.exit(3)
if mode == "absent":
    violation("include_embeddings is served, but NX_EXPECTED_ENGINE_SURFACES=absent")
if not isinstance(dim, int) or dim <= 0:
    violation("vectors came back but embedding_dim is %r" % (dim,))
if len(carried) != rows:
    violation("%d of %d rows carry a vector (the engine omits a row only when its vector is unreadable)" % (len(carried), rows))
bad = [n for n in carried if n != dim * 4]
if bad:
    violation("a vector is %d bytes, expected embedding_dim * 4 = %d" % (bad[0], dim * 4))
print("ok [M3]: %d rows, each with a %d-byte vector (embedding_dim=%d), read through the edge by the real client" % (rows, dim * 4, dim))
PY
}

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
# curl -w prints 000 itself when the request fails, so the fallback ASSIGNS 000 rather than
# appending a second one (an `|| echo 000` inside the substitution read "000000").
NOAUTH_STATUS="$(curl -sS -m 20 -o /dev/null -w "%{http_code}" "$SERVICE_URL/health")" || NOAUTH_STATUS=000
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
# Leg K reuses the bearer, so it outlives leg B; the trap removes it on every exit, a
# _fail included, and is installed BEFORE mktemp so a kill between the two cannot leak it.
trap 'rm -f "${BEARER_FILE:-}"; rm -rf "${M_DIR:-}"' EXIT
BEARER_FILE="$(mktemp)"
chmod 600 "$BEARER_FILE"
STATUS_BODY=""
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
    V1_STATUS="$(curl -sS -m 20 -H @"$BEARER_FILE" -o /dev/null -w "%{http_code}" "$SERVICE_URL/v1/catalog/collections/list")" || V1_STATUS=000
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

# ── Leg J: engine reaper liveness through the edge (nexus-wbfpw.50) ──────
# Judged on the /v1/status body leg B already fetched with the client's bearer;
# no second request. The compare logic is _reaper_status_verdict, above, and is
# unit-tested in tests/e2e/cloud_client_path_gate_b3_test.sh.
_leg_enter J "engine reaper liveness on /v1/status through the edge (present, enabled, fresh, no failed pass)"
J_RC=0
J_LINE="$(_reaper_status_verdict "$STATUS_BODY")" || J_RC=$?
if [ "$J_RC" -eq 0 ]; then
    echo "  $J_LINE"
else
    _leg_fail "$J_LINE"
fi

# ── Leg K: vector sweep routes through the edge (nexus-wbfpw.50) ─────────
# The routes under /v1/vectors/gc/*, POST /v1/vectors/reapable and POST
# /v1/vectors/manifest-less-census, reached through the PUBLIC edge with the
# client's own bearer. What is asserted is what the client reads: the status
# code, and a body that is the engine's own JSON rather than an edge page. The
# restore verb's exit 4 ("the engine predates the route") is a 404, the typed
# refusals are 400/422, and nothing else notices an edge that rewrites any of
# them (nexus-bwulw).
#
# READ-ONLY BY CONSTRUCTION. This runs against the live engine, so every request
# below is one the engine refuses or answers without touching data: an empty or
# source-less body (400 before any repository call), a quarantine- collection
# (400), a route that does not exist (404), a dry run naming an UNREGISTERED
# origin (422, refused before the engine looks anywhere), and two reads of an
# unregistered collection name (200 with an empty result). Nothing is moved,
# restored, expired or deleted. The three older sweep routes (quarantine-orphans,
# restore-rereferenced, expire-quarantine) have no dry-run form, so only their
# validation refusal is reachable; their 200 shapes, and quarantine-restore's own
# 200 shape (its dry run needs a REGISTERED origin, a catalog write), are
# UNCOVERED here. So is the typed 503 quarantine_restore_busy (reason,
# Retry-After: 5, nothing_moved): it needs a sweep gate or an index-run lock held
# past 2 s, which cannot be provoked from outside without writes.
_leg_enter K "vector sweep routes through the edge (gc/* refusals + 404, reapable, manifest-less-census; read-only)"
K_STAMP="$(date +%s)-$RANDOM"
K_COLLECTION="knowledge__ccpg-unregistered-${K_STAMP}__voyage-context-3__v1"
K_QUARANTINE="quarantine-${K_COLLECTION}"
K_CHASH="0000000000000000000000000000000000000000000000000000000000000000"
EDGE_CODE=""
EDGE_BODY=""
# POST a JSON body through the edge with the leg-B bearer: sets EDGE_CODE and EDGE_BODY.
_edge_post() {
    local path="$1" data="$2" out
    out="$(mktemp)"
    EDGE_CODE="$(curl -sS -m 30 -X POST -H @"$BEARER_FILE" -H 'Content-Type: application/json' \
        --data "$data" -o "$out" -w '%{http_code}' "$SERVICE_URL$path")" || EDGE_CODE=000
    EDGE_BODY="$(cat "$out")"
    rm -f "$out"
}
# Judge the last _edge_post: the status code first, then a body that is a JSON
# object of the kind named. kinds: notfound (the engine's exact 404 body,
# {"error":"not found"}), error (the "error" string CONTAINS the fourth argument, a
# fragment of the engine's own message: an edge or WAF can answer a 400 with a JSON
# {"error": ...} of its own, and only the engine's wording proves the request got
# there), unregistered (error + reason unregistered_collection), reapable_empty,
# census_empty.
_edge_expect() {
    local label="$1" want="$2" kind="$3" fragment="${4:-}"
    "$E2E_PYTHON" - "$label" "$want" "$EDGE_CODE" "$kind" "$K_COLLECTION" "$EDGE_BODY" "$fragment" <<'PY'
import json
import sys

label, want, got, kind, coll, body, fragment = sys.argv[1:8]
head = body[:160].replace("\n", " ")


def violation(msg):
    print("  VIOLATION [%s]: %s" % (label, msg), file=sys.stderr)
    sys.exit(1)


if got != want:
    violation("HTTP %s, expected %s (body: %r) -- the client branches on this status" % (got, want, head))
try:
    doc = json.loads(body)
except ValueError:
    doc = None
if not isinstance(doc, dict):
    violation("HTTP %s body is not a JSON object (%r): an edge page or a stripped body, not the engine's answer" % (got, head))
errs = []
if kind == "notfound" and doc != {"error": "not found"}:
    errs.append("body is %r, expected exactly the engine's {\"error\": \"not found\"}" % (head,))
if kind == "error":
    if not fragment:
        errs.append("internal: an 'error' probe must name the engine message fragment it expects")
    elif not (isinstance(doc.get("error"), str) and fragment in doc["error"]):
        errs.append("'error' is %r, expected it to contain the engine's %r (an edge or WAF answer reads differently)"
                    % (doc.get("error"), fragment))
if kind == "unregistered" and not (isinstance(doc.get("error"), str) and doc["error"]):
    errs.append("no non-empty 'error' string in %r" % (head,))
if kind == "unregistered" and doc.get("reason") != "unregistered_collection":
    errs.append("reason is %r, expected 'unregistered_collection' (the key clients branch on)" % (doc.get("reason"),))
if kind == "reapable_empty":
    if doc.get("collection") != coll:
        errs.append("collection echo is %r, expected %r" % (doc.get("collection"), coll))
    if "grace_seconds" not in doc or doc["grace_seconds"] is not None:
        errs.append("grace_seconds is %r, expected an echoed null (the engine default)" % (doc.get("grace_seconds", "<absent>"),))
    if type(doc.get("returned")) is not int or doc["returned"] != 0:
        errs.append("returned is %r, expected 0 for an unregistered collection" % (doc.get("returned"),))
    if doc.get("chunks") != []:
        errs.append("chunks is %r, expected []" % (doc.get("chunks"),))
    if "next_after" not in doc or doc["next_after"] is not None:
        errs.append("next_after is %r, expected null" % (doc.get("next_after", "<absent>"),))
if kind == "census_empty":
    buckets = ("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified")
    if doc.get("collection") != coll:
        errs.append("collection echo is %r, expected %r" % (doc.get("collection"), coll))
    totals = doc.get("totals")
    if not isinstance(totals, dict) or any(type(totals.get(b)) is not int for b in buckets):
        errs.append("totals is %r, expected an integer for each of %s" % (totals, ", ".join(buckets)))
    if type(doc.get("scope_chunk_total")) is not int or doc["scope_chunk_total"] != 0:
        errs.append("scope_chunk_total is %r, expected 0 for an unregistered collection" % (doc.get("scope_chunk_total"),))
    if type(doc.get("returned")) is not int or doc["returned"] != 0:
        errs.append("returned is %r, expected 0" % (doc.get("returned"),))
    for key in ("chashes", "owners"):
        if not isinstance(doc.get(key), dict):
            errs.append("%s is %r, expected an object" % (key, doc.get(key)))
if errs:
    violation("; ".join(errs))
print("  ok [%s]: HTTP %s, the engine's own JSON (%s)" % (label, got, kind))
PY
}

if [ ! -s "$BEARER_FILE" ]; then
    _leg_fail "K: no bearer (leg B could not resolve one), so the sweep routes could not be reached"
else
    K_BAD=0
    _edge_post "/v1/vectors/gc/this-route-does-not-exist-${K_STAMP}" '{}'
    _edge_expect K1 404 notfound || K_BAD=1
    _edge_post "/v1/vectors/gc/quarantine-restore" \
        "{\"origin_collection\":\"$K_COLLECTION\",\"chashes\":[\"$K_CHASH\"],\"dry_run\":true}"
    _edge_expect K2 422 unregistered || K_BAD=1
    _edge_post "/v1/vectors/gc/quarantine-restore" "{\"origin_collection\":\"$K_COLLECTION\",\"dry_run\":true}"
    _edge_expect K3 400 error "name exactly one source" || K_BAD=1
    # audit_id -1 can never name a gc_audit row; the engine counts any non-null audit_id as a
    # source and refuses the two-source request before it parses the value.
    _edge_post "/v1/vectors/gc/quarantine-restore" \
        "{\"origin_collection\":\"$K_COLLECTION\",\"chashes\":[\"$K_CHASH\"],\"audit_id\":-1,\"dry_run\":true}"
    _edge_expect K4 400 error "name exactly one source" || K_BAD=1
    _edge_post "/v1/vectors/gc/quarantine-orphans" '{}'
    _edge_expect K5 400 error "missing required field: collection" || K_BAD=1
    _edge_post "/v1/vectors/gc/restore-rereferenced" '{}'
    _edge_expect K6 400 error "missing required field: quarantine_collection" || K_BAD=1
    _edge_post "/v1/vectors/gc/expire-quarantine" '{}'
    _edge_expect K7 400 error "missing required field: quarantine_collection" || K_BAD=1
    _edge_post "/v1/vectors/reapable" "{\"collection\":\"$K_COLLECTION\",\"limit\":1}"
    _edge_expect K8 200 reapable_empty || K_BAD=1
    _edge_post "/v1/vectors/reapable" "{\"collection\":\"$K_QUARANTINE\"}"
    _edge_expect K9 400 error "is a quarantine collection" || K_BAD=1
    _edge_post "/v1/vectors/reapable" "{\"collection\":\"$K_COLLECTION\",\"grace_seconds\":-1}"
    _edge_expect K10 400 error "field 'grace_seconds' must be between 0 and" || K_BAD=1
    _edge_post "/v1/vectors/manifest-less-census" "{\"collection\":\"$K_COLLECTION\",\"limit\":1}"
    _edge_expect K11 200 census_empty || K_BAD=1
    _edge_post "/v1/vectors/manifest-less-census" "{\"collection\":\"$K_QUARANTINE\"}"
    _edge_expect K12 400 error "is a quarantine collection" || K_BAD=1
    [ "$K_BAD" -eq 0 ] || _leg_fail "K: the sweep routes through the edge did not answer as the engine does (see above)"
fi

# ── Leg L: the per-collection search route through the edge (nexus-tu8wp.3) ──
# Two requests to POST /v1/vectors/search-per-collection that the engine refuses by
# validation, before it resolves a collection, embeds the query or opens a database
# transaction (VectorHandler.handleSearchPerCollection and PgVectorRepository.
# searchPerCollection validate per_collection_k and limit first): per_collection_k 0
# and limit 1201. K_COLLECTION is leg K's unregistered name; the engine never looks
# it up. Nothing is searched, written or embedded. An engine that predates the route
# answers both with its generic 404. The same _edge_post helper as leg K, so the one
# POST curl stays the one the audit pins; the compare logic is _route_probe_verdict,
# above, unit-tested in tests/e2e/cloud_client_path_gate_b3_test.sh.
_leg_enter L "per-collection search route through the edge (engine's 400 JSON when served, its 404 JSON when absent; read-only)"
L_SERVED=0
L_ABSENT=0
L_ROUTE_NOT_SERVED=0
# Judge the last _edge_post as a route probe: one line to stdout (stderr for a violation),
# and count which state it saw.
_route_probe() {
    local label="$1" fragment="$2" line rc=0
    line="$(_route_probe_verdict "$label" "$EDGE_CODE" "$EDGE_BODY" "$fragment" "$NX_EXPECTED_SEARCH_PER_COLLECTION_ROUTE")" || rc=$?
    case "$rc" in
        0) echo "  $line"; L_SERVED=$((L_SERVED + 1)) ;;
        3) echo "  $line"; L_ABSENT=$((L_ABSENT + 1)); L_ROUTE_NOT_SERVED=1 ;;
        4) echo "  $line"; L_ABSENT=$((L_ABSENT + 1)) ;;
        *) echo "  VIOLATION [${line%%:*}]:${line#*:}" >&2; return 1 ;;
    esac
}
if [ ! -s "$BEARER_FILE" ]; then
    _leg_fail "L: no bearer (leg B could not resolve one), so the per-collection route could not be reached"
else
    L_BAD=0
    _edge_post "/v1/vectors/search-per-collection" "{\"query\":\"ccpg route probe\",\"collections\":[\"$K_COLLECTION\"],\"per_collection_k\":0,\"limit\":1}"
    _route_probe L1 "per_collection_k must be in 1.." || L_BAD=1
    _edge_post "/v1/vectors/search-per-collection" "{\"query\":\"ccpg route probe\",\"collections\":[\"$K_COLLECTION\"],\"per_collection_k\":1,\"limit\":1201}"
    _route_probe L2 "limit must be in 1.." || L_BAD=1
    if [ "$L_SERVED" -gt 0 ] && [ "$L_ABSENT" -gt 0 ]; then
        echo "  VIOLATION [L]: the two probes disagree ($L_SERVED saw the route served, $L_ABSENT saw it absent): an edge or a rolling deploy answers this path two ways" >&2
        L_BAD=1
    fi
    [ "$L_BAD" -eq 0 ] || _leg_fail "L: the per-collection route through the edge did not answer as the engine does (see above)"
fi

# ── Leg M: gzip, the routing listing and include_embeddings through the edge ─────────────────────────────
# nexus-tjyzn, nexus-mz9jv, nexus-92q1p. The review of the branch that added them found no gate leg through the
# public edge for any of the three. M1 and M2 read GET /v1/vectors/stats?fields=routing twice (identity, then
# gzip), so both checks ride on the same read-only route; M3 is the real client's search over one live collection
# with include_embeddings. The mode (_surfaces_mode) is what makes the leg non-vacuous: from the first engine
# release that carries the surfaces they must be observed served.
_leg_enter M "gzip, the routing listing and include_embeddings through the edge (served from the engine release that carries them; read-only)"
M_DIR="$(mktemp -d)"
M_NOT_SERVED=0
M_MODE="$(_surfaces_mode "$VERSION_BODY" "$NX_EXPECTED_ENGINE_SURFACES" "$ENGINE_SURFACES_MIN")"
echo "  mode: $M_MODE (engine release_version floor for the surfaces: $ENGINE_SURFACES_MIN)"
_surface_judge() {
    local rc=0 line
    line="$("$@")" || rc=$?
    case "$rc" in
        0) echo "  $line" ;;
        3) echo "  $line"; M_NOT_SERVED=1 ;;
        *) echo "  VIOLATION [${line%%:*}]:${line#*:}" >&2; return 1 ;;
    esac
}
if [ ! -s "$BEARER_FILE" ]; then
    _leg_fail "M: no bearer (leg B could not resolve one), so the engine surfaces could not be reached"
else
    M_BAD=0
    M_IDENT_CODE="$(curl -sS -m 30 -H @"$BEARER_FILE" -H 'Accept-Encoding: identity' -o "$M_DIR/ident.body" -w '%{http_code}' "$SERVICE_URL/v1/vectors/stats?fields=routing")" || M_IDENT_CODE=000
    M_GZ_CODE="$(curl -sS -m 30 -H @"$BEARER_FILE" -H 'Accept-Encoding: gzip' -D "$M_DIR/gz.headers" -o "$M_DIR/gz.body" -w '%{http_code}' "$SERVICE_URL/v1/vectors/stats?fields=routing")" || M_GZ_CODE=000
    if [ "$M_IDENT_CODE" != 200 ] || [ "$M_GZ_CODE" != 200 ]; then
        echo "  VIOLATION [M1]: GET /v1/vectors/stats?fields=routing answered HTTP $M_IDENT_CODE (identity) and $M_GZ_CODE (gzip), expected 200 for both" >&2
        M_BAD=1
    else
        _surface_judge _surface_verdict gzip "$M_DIR/ident.body" "$M_DIR/gz.body" "$M_DIR/gz.headers" "$M_MODE" || M_BAD=1
        _surface_judge _surface_verdict routing "$M_DIR/ident.body" "$M_DIR/gz.body" "$M_DIR/gz.headers" "$M_MODE" || M_BAD=1
        M_COLLECTION="$("$E2E_PYTHON" - "$M_DIR/ident.body" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
live = [r["name"] for r in rows if r.get("lifecycle_state", "live") == "live" and "__" in r["name"]]
print(live[0] if live else "")
PY
)"
        if [ -z "$M_COLLECTION" ]; then
            echo "  VIOLATION [M3]: the routing listing names no live collection to search" >&2
            M_BAD=1
        else
            M_SUMMARY="$(M_COLLECTION="$M_COLLECTION" uv run python - <<'PY'
import json
import os
from nexus.logging_setup import configure_logging
configure_logging("cli")
from nexus.db import make_t3

name = os.environ["M_COLLECTION"]
t3 = make_t3()
env = t3.search_per_collection("ccpg include_embeddings probe", [name], per_collection_k=2, limit=2, include_embeddings=True)
if not env:
    print(json.dumps({"collection": name, "rows": 0, "embedding_dim": None, "vector_lens": []}))
else:
    vecs = env.get("result_embeddings") or [None] * len(env.get("results") or [])
    print(json.dumps({
        "collection": name,
        "rows": len(env.get("results") or []),
        "embedding_dim": env.get("embedding_dim"),
        "vector_lens": [len(v) if v is not None else None for v in vecs],
    }))
PY
)" || { echo "  VIOLATION [M3]: the client probe crashed (see above)" >&2; M_BAD=1; M_SUMMARY=""; }
            if [ -n "$M_SUMMARY" ]; then
                _surface_judge _embeddings_verdict "$M_SUMMARY" "$M_MODE" || M_BAD=1
            fi
        fi
    fi
    [ "$M_BAD" -eq 0 ] || _leg_fail "M: the engine surfaces through the edge did not answer as the engine does (see above)"
fi
rm -rf "$M_DIR"

if [ "$LEGS_RAN" -ne "$EXPECTED_LEGS" ]; then
    # Distinct from a violation: "the gate did not run its full battery" is
    # a different fact from "the edge is broken", and the relay must be able
    # to tell them apart (nexus-i1oh4).
    _fail "battery shortfall: only $LEGS_RAN of $EXPECTED_LEGS leg(s) ran — this run proves nothing about the legs that never executed"
fi
if [ "$VIOLATIONS" -gt 0 ]; then
    _fail "$VIOLATIONS leg(s) violated — the public edge does not deliver the engine's pinned client contract"
fi
# Every unasserted state reaches the sentinel line, so a pass never reads as more than it proved.
PASS_NOTE=""
if [ "$B3_NOT_RUN" = 1 ]; then
    PASS_NOTE="ownerless-write mode NOT asserted: B3 not run"
fi
if [ "$L_ROUTE_NOT_SERVED" = 1 ]; then
    PASS_NOTE="${PASS_NOTE:+$PASS_NOTE; }per-collection route NOT served: L saw the old engine's JSON 404"
fi
if [ "$M_NOT_SERVED" = 1 ]; then
    PASS_NOTE="${PASS_NOTE:+$PASS_NOTE; }new engine surfaces NOT served: M saw an engine without gzip, the routing listing or include_embeddings"
fi
if [ -n "$PASS_NOTE" ]; then
    echo "CLOUD CLIENT-PATH GATE PASSED — legs=$LEGS_RAN/$EXPECTED_LEGS violations=0 ($PASS_NOTE)"
else
    echo "CLOUD CLIENT-PATH GATE PASSED — legs=$LEGS_RAN/$EXPECTED_LEGS violations=0"
fi
