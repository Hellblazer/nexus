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


def test_allowlist_is_the_canonical_enumerated_set() -> None:
    assert tuple(MIGRATION_ROUTES) == _EXPECTED_ROUTES


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
