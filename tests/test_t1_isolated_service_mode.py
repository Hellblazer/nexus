# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6 finding 13: NX_T1_ISOLATED=1 must win over service-backend
routing. Pre-fix, get_t1_database checked storage_backend_for("t1") FIRST and
returned HttpScratchStore unconditionally — the isolation escape hatch that
every T1 error message recommends was dead code in service installs
(candidate-shakeout run 6: 'scratch put (isolated)' failed with the
NX_T1_SESSION-required error DESPITE NX_T1_ISOLATED=1)."""
from __future__ import annotations

from nexus.db.t1 import T1Database, get_t1_database


def test_isolated_wins_over_service_backend(monkeypatch) -> None:
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NX_T1_ISOLATED", "1")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    t1 = get_t1_database()
    assert isinstance(t1, T1Database), (
        "NX_T1_ISOLATED=1 must return the in-process ephemeral T1Database, "
        f"got {type(t1).__name__}"
    )
    # And it must actually work end to end (put/get roundtrip, no service).
    doc_id = t1.put(content="isolated probe", tags="t")
    assert t1.get(doc_id)["content"] == "isolated probe"


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
