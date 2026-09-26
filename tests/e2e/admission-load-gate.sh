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
# against.
#
# WHAT IT DOES: mints a throwaway knowledge collection
# (u2mlh-load-<nonce>) via the same name-resolution and registration
# functions a normal write uses, ramps concurrency (32/64/128/256
# documents by default, each 6-24KB and content-unique so the server's
# existing-chunk embed-skip can never elide one), and after each step
# re-reads the engine's live counters and checks whether either moved for
# voyage-context-3. Stops at the first step that moves either counter;
# FAILS (non-vacuity) if the whole ramp passes with neither ever moving.
# The collection is deleted in the driver's own try/finally regardless of
# outcome, via the interactive collection-delete verb's own underlying
# function — never left behind, success or failure.
#
# Applicability: requires a CLOUD-mode box (service_url is a non-loopback
# https endpoint) — refuses (exit 2) on a local-mode box, like
# tests/e2e/cloud-client-path-gate.sh.
#
# Usage:
#   tests/e2e/admission-load-gate.sh             # the real thing: real
#                                                 # Voyage cost, real
#                                                 # concurrent load on the
#                                                 # only environment
#   tests/e2e/admission-load-gate.sh --dry-run   # prints the plan; NO
#                                                 # network touched at all
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

DRY_RUN_FLAG=()
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN_FLAG=(--dry-run)
    echo "DRY RUN: plan only, no network"
fi

RESULT_FILE="$(mktemp)"
trap 'rm -f "$RESULT_FILE"' EXIT

set +e
uv run python "$LIB" run --result-file "$RESULT_FILE" "${DRY_RUN_FLAG[@]}"
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
