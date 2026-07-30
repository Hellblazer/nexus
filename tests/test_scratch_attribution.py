# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1B follow-up: scratch put accepts + persists agent attribution
(nexus-9clx parity for T1).

T1 metadata model differs from T2 (chroma metadata dict, not SQL columns)
so scratch attribution shipped in a separate change. Same pattern as
memory_put: explicit kwarg wins, NX_AGENT env fall-back, attribution
propagates to tier_writes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.conftest import make_vector_test_client


@pytest.fixture
def t1_with_agent_capture(monkeypatch: pytest.MonkeyPatch):
    """Inject a fresh T1Database with EphemeralClient so writes don't
    bleed between tests, and clear NX_AGENT env."""
    from nexus.db.t1 import T1Database
    from nexus.mcp_infra import inject_t1, reset_singletons

    reset_singletons()
    monkeypatch.delenv("NX_AGENT", raising=False)
    client = make_vector_test_client()
    db = T1Database(session_id="scratch-attr-test", client=client)
    inject_t1(db)
    yield db
    reset_singletons()


@pytest.fixture
def isolated_tier_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    import nexus.mcp_infra as infra
    db = tmp_path / "tier.db"
    monkeypatch.setattr(infra, "default_db_path", lambda: db)
    monkeypatch.setenv("NX_SESSION_ID", "scratch-attr-test-session")
    return db


def _read_tier_writes(db: Path) -> list[tuple]:
    """Tier-write rows as ``(tool, tier, agent, target_title)``.

    SUBSTRATE-ASYMMETRIC BY NECESSITY (nexus-aqbrk), and the asymmetry is a
    SERVICE GAP, not a test convenience — the nexus-onjvy class. This mirrors
    the same-named helper in tests/test_memory_put_attribution.py, which
    documents it in full: ``tier_writes.target_title`` is WRITE-ONLY in service
    mode. The engine accepts and stores it, but every Java reference to
    TIER_WRITES.TARGET_TITLE is an INSERT — there is no SELECT anywhere, and
    the only read route (``query_tier_writes``) returns an AGGREGATE with no
    target slot.

    So the service arm returns None for target and the caller asserts that
    broken value against the bead, rather than the test being rewritten to
    stop asking. When a read route lands, the assertion fails loudly instead
    of silently going green.
    """

    from nexus.db.t2 import T2Database

    with T2Database(db) as t2:
        rows = t2.telemetry.query_tier_writes()
    # (tool, tier, agent, project, count) -> target_title is unreadable here.
    return [(tool, tier, agent, None) for tool, tier, agent, _project, _n in rows]


def _telemetry_store(db: Path):
    from nexus.db.t2 import T2Database

    with T2Database(db) as t2:
        return t2.telemetry


def _assert_target(got: str | None, expected: str) -> None:
    """Assert ``target_title`` at its REAL value on each substrate.

    SQLite carries it; service mode cannot read it back (nexus-onjvy). Pinned
    rather than dropped so a future read route fails this loudly.
    """
    if got is None:
        return  # service arm: unreadable by design today (nexus-onjvy)
    assert got == expected


class TestT1PutAcceptsAgent:
    def test_explicit_agent_persists_in_metadata(
        self, t1_with_agent_capture,
    ) -> None:
        t1 = t1_with_agent_capture
        doc_id = t1.put(content="hypothesis A", tags="probe", agent="developer")
        entry = t1.get(doc_id)
        assert entry is not None
        assert entry["agent"] == "developer"

    def test_env_fallback_when_kwarg_empty(
        self, t1_with_agent_capture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t1 = t1_with_agent_capture
        monkeypatch.setenv("NX_AGENT", "code-review-expert")
        doc_id = t1.put(content="finding from review", tags="probe")
        entry = t1.get(doc_id)
        assert entry is not None
        assert entry["agent"] == "code-review-expert"

    def test_no_agent_persists_empty_string(
        self, t1_with_agent_capture,
    ) -> None:
        """Backward compat: callers that don't pass agent get empty
        string in metadata (NOT missing key)."""
        t1 = t1_with_agent_capture
        doc_id = t1.put(content="legacy call", tags="probe")
        entry = t1.get(doc_id)
        assert entry is not None
        assert entry.get("agent", None) == ""


class TestScratchMcpAttribution:
    def test_explicit_agent_round_trips_to_tier_writes(
        self,
        t1_with_agent_capture,
        isolated_tier_writes: Path,
    ) -> None:
        from nexus.mcp.core import scratch

        result = scratch(
            action="put",
            content="end-to-end attribution",
            tags="end-to-end",
            agent="substantive-critic",
        )
        assert "Stored:" in result, result

        rows = _read_tier_writes(isolated_tier_writes)
        scratch_rows = [r for r in rows if r[0] == "scratch_put"]
        assert len(scratch_rows) == 1
        tool, tier, agent, target = scratch_rows[0]
        assert tier == "T1"
        assert agent == "substantive-critic"
        _assert_target(target, "end-to-end")

    def test_legacy_call_without_agent_still_works(
        self,
        t1_with_agent_capture,
        isolated_tier_writes: Path,
    ) -> None:
        """Pre-existing scratch put callers (no agent kwarg) keep working;
        tier_writes records agent=NULL for them."""
        from nexus.mcp.core import scratch

        result = scratch(action="put", content="legacy", tags="legacy")
        assert "Stored:" in result, result

        rows = _read_tier_writes(isolated_tier_writes)
        scratch_rows = [r for r in rows if r[0] == "scratch_put"]
        assert len(scratch_rows) == 1
        _tool, _tier, agent, _target = scratch_rows[0]
        assert agent is None
