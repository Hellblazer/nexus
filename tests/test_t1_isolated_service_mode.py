# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6 finding 13 established that isolation must win over
service-backend routing (pre-fix, get_t1_database checked
storage_backend_for("t1") FIRST and returned HttpScratchStore
unconditionally — the isolation escape hatch every T1 error message
recommended was dead code in service installs).

nexus-4lkmz (Hal determination 2026-07-28: "T1 exists in PG only. The
need for an isolated, ephemeral T1 has been eliminated.") retires the
escape hatch itself: NX_T1_ISOLATED=1 no longer selects an in-process
T1Database — it hard-fails. This file now verifies the checked-first
POSITION nexus-h8rf6 established survives the retirement (isolation
still outranks backend routing — it just fails loud instead of
redirecting)."""
from __future__ import annotations

import pytest

from nexus.db.t1 import T1IsolatedLegRetiredError, get_t1_database


def test_isolated_hard_fails_before_service_backend_routing(monkeypatch) -> None:
    """NX_T1_ISOLATED=1 must still be checked FIRST, ahead of backend
    routing — but now raises instead of returning an in-process store."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NX_T1_ISOLATED", "1")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    with pytest.raises(T1IsolatedLegRetiredError, match="NX_T1_ISOLATED"):
        get_t1_database()


def test_legacy_alias_removed_no_longer_wins(monkeypatch) -> None:
    """NEXUS_SKIP_T1 was removed at 6.5.2 (promised gone in 5.0): the stale
    alias must be INERT — service routing proceeds as if it were unset."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NEXUS_SKIP_T1", "1")
    monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
    monkeypatch.setenv("NX_T1_SESSION", "route-check")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("NX_SERVICE_PORT", "1")
    t1 = get_t1_database()
    assert type(t1).__name__ == "HttpScratchStore", (
        "the removed alias must not divert service routing to T1Database"
    )


def test_service_backend_still_routes_without_isolation(monkeypatch) -> None:
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
    monkeypatch.setenv("NX_T1_SESSION", "route-check")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("NX_SERVICE_PORT", "1")
    t1 = get_t1_database()
    assert type(t1).__name__ == "HttpScratchStore"
