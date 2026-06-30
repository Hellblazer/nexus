# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 2 (Gap 4) — migration route-coverage go-live gate.

The managed edge (nginx) must let every migration endpoint through to the
service for a tenant bearer. The 6.0.0 dogfood found the T2 import routes 403'd
at the WAF, so a migration that passed locally was silently blocked at the edge.

This gate enumerates the canonical allowlist (RDR Research-2 §D) as the single
source of truth and probes each route, FAILING on any that the edge rejects
(403/404 — blocked before reaching the app). Run against the live cloud edge it
is the xr7.8.9-style go-live check; here we pin the gate logic with an injected
probe, failing-first on a simulated current-edge 403 (bead nexus-t9rmg.9).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.migration.route_coverage import (
    MIGRATION_ROUTES,
    RouteCoverageResult,
    check_route_coverage,
)

# The full enumerated allowlist, expanded from RDR Research-2 §D. Frozen here so
# a route added to a store without edge coverage trips the equality assertion.
_EXPECTED_ROUTES = (
    "/v1/memory/import",
    "/v1/memory/import_batch",
    "/v1/plans/import",
    "/v1/telemetry/import",
    "/v1/taxonomy/import/topic",
    "/v1/taxonomy/import/assignment",
    "/v1/taxonomy/import/link",
    "/v1/taxonomy/import/meta",
    "/v1/aspects/import",
    "/v1/aspects/highlights/import",
    "/v1/aspects/promotion/import",
    "/v1/aspects/queue/import",
    "/v1/chash/import",
    "/v1/chash/upsert_many",
    "/v1/catalog/import/owner",
    "/v1/catalog/import/document",
    "/v1/catalog/import/link",
    "/v1/catalog/import/chunk",
    "/v1/catalog/import/collection",
    "/v1/catalog/verify/relation-counts",
    "/v1/vectors/upsert-chunks",
    "/v1/vectors/collections",
)

# The T2/catalog import routes the dogfood found 403'd at the edge.
_EDGE_BLOCKED = frozenset(
    r for r in _EXPECTED_ROUTES if "/import" in r or r.endswith("relation-counts")
)


def test_allowlist_matches_the_frozen_enumerated_set() -> None:
    """Edit-guard: a change to MIGRATION_ROUTES must be deliberate (update this
    frozen copy too). This does NOT detect an OMISSION — a new ETL route absent
    from both lists passes; that gap is covered by code review and the
    real-endpoint check below."""
    assert tuple(MIGRATION_ROUTES) == _EXPECTED_ROUTES


def test_every_route_is_a_real_endpoint_in_the_source_tree() -> None:
    """Non-tautological completeness check: every allowlisted route must appear
    as a literal somewhere in the nexus client OR the Java service handlers —
    so a typo'd or fictional route in MIGRATION_ROUTES is caught (the tautology
    test alone could not). The probe can only ever check routes it is given, so
    a route that exists nowhere is dead allowlist weight that would false-alarm
    the go-live gate forever."""

    repo_root = Path(__file__).resolve().parents[2]
    missing: list[str] = []
    for route in MIGRATION_ROUTES:
        # Search client + service source; exclude the allowlist module + tests
        # so we are matching real call sites / handler registrations, not the
        # enumeration itself.
        hit = subprocess.run(
            ["grep", "-rqF", "--include=*.py", "--include=*.java",
             "--exclude=route_coverage.py", route,
             str(repo_root / "src" / "nexus"), str(repo_root / "service" / "src")],
            capture_output=True,
        )
        if hit.returncode != 0:
            missing.append(route)
    assert missing == [], f"routes not found as real endpoints in source: {missing}"


def test_gate_fails_on_current_edge_config() -> None:
    """Simulate the 6.0.0 edge: T2/catalog import routes 403, vectors pass.
    The gate must FAIL and name every blocked route."""

    def probe(route: str) -> int:
        return 403 if route in _EDGE_BLOCKED else 200

    result = check_route_coverage(probe)
    assert isinstance(result, RouteCoverageResult)
    assert result.ok is False
    assert set(result.unreachable) == set(_EDGE_BLOCKED)


def test_gate_passes_when_every_route_reachable() -> None:
    """A route is 'reachable' when the edge does not block it — any status the
    APP returns (200/400/422) counts; only 403/404 mean edge-blocked."""

    def probe(route: str) -> int:
        return 422  # app reached, validation rejected the empty probe body

    result = check_route_coverage(probe)
    assert result.ok is True
    assert result.unreachable == ()


def test_gate_treats_404_as_edge_blocked() -> None:
    def probe(route: str) -> int:
        return 404 if route == "/v1/vectors/collections" else 200

    result = check_route_coverage(probe)
    assert result.ok is False
    assert result.unreachable == ("/v1/vectors/collections",)
