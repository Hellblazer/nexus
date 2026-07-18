#!/usr/bin/env bash
# Host orchestrator for the fully-isolated SQLite-T2 migration E2E.
#
#   tests/e2e/t2-migration-sqlite/run.sh
#
# Builds the develop wheel on the host, bakes it into a clean python:3.12-slim
# image (no source, no service stack), and runs the migration scenarios in a
# throwaway container. Zero contact with the host's ~/.config/nexus or its uv
# tool venv — safe to run alongside a live install.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
IMAGE="nexus-t2-sqlite-e2e"
HARNESS="tests/e2e/t2-migration-sqlite"

# RDR-184 P0 guard-surface gap (nexus-ccs9v.4/.5 review): fixed docker
# image tag (nexus-t2-sqlite-e2e) + shared dist/*.whl mutation, same
# shape as migration-rehearsal (the harness that motivated this whole
# Phase). Own lockdir (this is a distinct resource from the other
# guarded harnesses, not a shared one). Acquired here, before the first
# mutation (the wheel build/dist/*.whl cleanup below); the EXIT trap is
# chained with the pre-existing wheel cleanup so both fire on any exit.
# shellcheck source=../lib/lock.sh disable=SC1091
source "tests/e2e/lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/t2-migration-sqlite.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
trap 'rm -f "$HARNESS"/conexus-*.whl; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2/.4, nexus-ccs9v.2/.4): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (wheel build, docker build/run). No-op
# in normal use.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

echo "== building develop wheel =="
rm -f dist/*.whl
uv build --wheel 2>&1 | tail -2
WHL="$(ls dist/*.whl)"
echo "   wheel: $WHL  (git $(git rev-parse --short HEAD))"

# Stage the wheel into the build context (repo .dockerignore excludes dist/),
# PRESERVING its PEP 427 filename so pip accepts it.
cp "$WHL" "$HARNESS/"

echo "== docker build (isolated image: wheel only, context=$HARNESS) =="
docker build -f "$HARNESS/Dockerfile" -t "$IMAGE" "$HARNESS"

echo "== running isolated SQLite-T2 migration E2E (throwaway container) =="
docker run --rm "$IMAGE"
