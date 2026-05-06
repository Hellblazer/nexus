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

import chromadb
import pytest


@pytest.fixture
def t1_with_agent_capture(monkeypatch: pytest.MonkeyPatch):
    """Inject a fresh T1Database with EphemeralClient so writes don't
    bleed between tests, and clear NX_AGENT env."""
    from nexus.db.t1 import T1Database
    from nexus.mcp_infra import inject_t1, reset_singletons

    reset_singletons()
    monkeypatch.delenv("NX_AGENT", raising=False)
    client = chromadb.EphemeralClient()
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
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return list(conn.execute(
            "SELECT tool, tier, agent, target_title FROM tier_writes ORDER BY id"
        ))
    finally:
        conn.close()


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
        assert target == "end-to-end"

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
