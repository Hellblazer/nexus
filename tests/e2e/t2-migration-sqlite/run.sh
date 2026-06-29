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

cleanup() { rm -f "$HARNESS"/conexus-*.whl; }
trap cleanup EXIT

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
