#!/usr/bin/env bash
# ADMISSION LOAD GATE (nexus-u2mlh.9) — deliberately WRITES to the
# operator's live engine through the public edge: drives concurrent CCE
# embed load into a throwaway knowledge collection until the engine's
# admission control (admission_refusals_total) or its request-deadline
# abort (deadline_aborts_total) fires. This is the close check for
# nexus-u2mlh.2 (`bd show nexus-u2mlh.2`) and its epic nexus-u2mlh.
export NX_ALLOW_PROD_WRITE="admission-load-gate: deliberate concurrent embed load against the live engine to exercise admission control (nexus-u2mlh.9)"
#
# WHY THIS EXISTS (2026-09-26): nexus-u2mlh.2 added per-request admission
# ahead of the engine-wide CCE semaphore — 503 + Retry-After instead of
# queueing work the edge's own deadline will abandon anyway — but nothing
# had ever driven concurrency high enough, in the cloud, through the
# public edge, to prove either counter actually moves. Sam runs the real
# load by hand behind a narrow settings.json allow rule (see the bottom of
# this file); this script is what he runs.
#
# HOW ADMISSION FIRES (read CceEmbedder.admit, ~line 715, before touching
# any number below): the engine refuses a request when
# ceil((held + queued + mine) / thread_width) waves of the EWMA per-call
# time would overrun the request's remaining deadline. thread_width is 12
# today; the public edge clamps X-Nexus-Request-Deadline-Ms to ~50s on
# this route regardless of what the client sends (conexus-26b5). That
# puts the refusal threshold at roughly 12 * (50s / per-call-time) queued
# batches — on the order of 70-200 concurrent document-sized upserts, far
# more than an ordinary manual indexing run drives.
#
# WHY THE PYTHON DRIVER SPEAKS RAW HTTP rather than the ordinary write
# client: that client's own layered retry silently retries a plain 503
# refusal several times before any caller code ever sees it, and even the
# one 503 shape that propagates immediately loses its headers by the time
# it reaches the caller. Observing the raw status code, Retry-After, and
# deadline-outcome header per attempt needs one un-retried POST per
# request — see tests/e2e/lib/admission_load.py's own header for the
# full read and the exact functions/line numbers this was verified
# against. Auth reuses the real write path's own bearer resolution (a
# self-minted data token when configured, else the static service
# token) rather than always sending the static token.
#
# PASS CRITERION: admission_refusals_total (voyage-context-3) must move
# AND this driver must have observed at least one of its OWN raw 503
# responses in that same step carrying X-Nexus-Deadline-Outcome=refused.
# deadline_aborts_total is a genuinely distinct counter at a distinct
# call site (nexus-u2mlh.3's mechanism, already closed) -- reported per
# step for corroboration only, never sufficient on its own. A step where
# admission moved but no refused 503 was observed fails the whole run
# immediately as UNATTRIBUTABLE (the movement cannot be pinned on this
# run's own load).
#
# WHAT IT DOES: before any write, confirms GET /v1/status is readable
# with these credentials and that voyage-context-3 is idle
# (active=false, queue_depth<=0) across two reads a few seconds apart --
# refusing to start otherwise, so this driver never piles onto real
# traffic. Reports (never deletes) any pre-existing u2mlh-load-* orphan
# collection left by a prior killed run. Mints a throwaway knowledge
# collection (u2mlh-load-<nonce>) via the same name-resolution and
# registration functions a normal write uses, then ramps concurrency
# (32/64/128/256 documents by default, each 6-24KB and content-unique so
# the server's existing-chunk embed-skip can never elide one) against a
# connection pool sized to the largest step, stopping at the first step
# that satisfies the pass criterion above, or once an overall wall-clock
# cap (default 360s / 6 minutes, --wall-clock-cap-s overridable) is
# reached, whichever comes first. A step whose responses are mostly
# transport errors is marked invalid and never counted as evidence
# either way. The collection is deleted in the driver's own try/finally
# on every exit path this process can control, including Ctrl-C.
#
# Applicability: requires a CLOUD-mode box (service_url is a non-loopback
# https endpoint) — refuses (exit 2) on a local-mode box, like
# tests/e2e/cloud-client-path-gate.sh.
#
# Usage:
#   tests/e2e/admission-load-gate.sh                     # the real thing:
#                                                         # real Voyage
#                                                         # cost, real
#                                                         # concurrent load
#                                                         # on the only
#                                                         # environment
#   tests/e2e/admission-load-gate.sh --dry-run           # prints the
#                                                         # plan; NO
#                                                         # network
#                                                         # touched at all
#   tests/e2e/admission-load-gate.sh --ramp-steps 64,128,256,384
#                                                         # override the
#                                                         # default
#                                                         # 32,64,128,256
#                                                         # ramp (strictly
#                                                         # increasing)
#   tests/e2e/admission-load-gate.sh --wall-clock-cap-s 180
#                                                         # override the
#                                                         # default 360s
#                                                         # (6 min) overall
#                                                         # time budget
# Flags combine freely, in any order.
#
# Exit 0 == ADMISSION LOAD GATE PASSED (literal sentinel on the last line).
# Exit 2 == not applicable (not a cloud-mode box).
# Any other == FAILED (literal "ADMISSION LOAD GATE FAILED: <why>").
#
# settings.json allow rule (read-only suggestion below; this script never
# writes settings.json — Sam adds it himself):
#   "Bash(tests/e2e/admission-load-gate.sh:*)"
# repo-relative, matching how every command-position invocation in this
# repo's own settings.json allowlist is written (e.g. "Bash(git:*)"). Add
# the absolute-path form too if the invoking cwd is ever outside the repo
# checkout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LIB="$REPO_ROOT/tests/e2e/lib/admission_load.py"

_fail() { echo "ADMISSION LOAD GATE FAILED: $*" >&2; exit 1; }

# ── Mode detection (mirrors cloud-client-path-gate.sh exactly) ──────────
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
echo "Driving admission load against: $SERVICE_URL"

# ── Flag parsing: --dry-run, --ramp-steps <csv>, --wall-clock-cap-s <n> ──
DRIVER_ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRIVER_ARGS+=(--dry-run)
            echo "DRY RUN: plan only, no network"
            shift
            ;;
        --ramp-steps)
            [ -n "${2:-}" ] || { echo "ADMISSION LOAD GATE FAILED: --ramp-steps needs a value" >&2; exit 1; }
            DRIVER_ARGS+=(--ramp-steps "$2")
            shift 2
            ;;
        --wall-clock-cap-s)
            [ -n "${2:-}" ] || { echo "ADMISSION LOAD GATE FAILED: --wall-clock-cap-s needs a value" >&2; exit 1; }
            DRIVER_ARGS+=(--wall-clock-cap-s "$2")
            shift 2
            ;;
        *)
            echo "ADMISSION LOAD GATE FAILED: unrecognized argument: $1" >&2
            exit 1
            ;;
    esac
done

RESULT_FILE="$(mktemp)"
trap 'rm -f "$RESULT_FILE"' EXIT

set +e
uv run python "$LIB" run --result-file "$RESULT_FILE" "${DRIVER_ARGS[@]}"
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
    WHY="$(uv run python - "$RESULT_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    print(data.get("reason", "no reason recorded"))
except Exception as exc:  # noqa: BLE001 — best-effort: the result file may not exist if the driver crashed before writing it
    print(f"driver produced no readable result file ({exc})")
PY
)"
    _fail "$WHY"
fi

echo "ADMISSION LOAD GATE PASSED"
