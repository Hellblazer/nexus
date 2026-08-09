# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1B: memory_put MCP API accepts agent + session kwargs (nexus-9clx).

Schema columns ``agent`` and ``session`` on the ``memory`` table have
existed since the project's first commit but were never wired through
the MCP layer. 1012 of 1012 production rows have both NULL because
``memory_put`` MCP signature did not accept them.

This test suite locks the new signature in:
- Default behaviour: when neither kwarg is passed, falls back to
  ``NX_AGENT`` env and ``_read_session_id()``.
- Explicit kwargs: subagent role + caller-supplied session win.
- Empty string vs None: MCP optional kwargs use ``""`` defaults; the
  helper translates empty to None so the MemoryStore's own fall-back
  chain takes over.
- Tier-write attribution: when memory_put records to ``tier_writes``,
  the same ``agent`` value is stamped there too.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests._t2_fixture_ops import memory_row


@pytest.fixture
def isolated_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect default_db_path so t2_ctx() opens a tmp DB.

    Patching ``nexus.mcp_infra.default_db_path`` (rather than
    ``t2_ctx`` itself) is the right hook because ``t2_ctx`` references
    ``default_db_path`` at call time. Patching ``t2_ctx`` would only
    affect callers that lazy-imported it; ``core._t2_ctx`` is bound at
    module-import time and would not pick up the override. The recorder
    helper uses lazy imports so it's covered too.
    """
    import nexus.mcp_infra as infra
    db = tmp_path / "t.db"
    monkeypatch.setattr(infra, "default_db_path", lambda: db)
    monkeypatch.delenv("NX_AGENT", raising=False)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    # Default: no claude session file present (prevents the real
    # ``~/.config/nexus/current_session`` from leaking into the session
    # resolution).
    #
    # nexus-aqbrk: patches the CANONICAL function, not the old
    # ``memory_store._read_session_id`` alias. The fallback chain now has one
    # owner shared by the SQLite and service stores
    # (``nexus.db.t2._attribution.resolve_attribution``), which resolves the
    # name at CALL time — so the alias is gone and patching it would have
    # silently stopped intercepting anything, leaving these assertions to
    # pass only by accident of the isolated config dir having no session file.
    import nexus.session as _sess
    monkeypatch.setattr(_sess, "read_claude_session_id", lambda: None)
    return db


def _read_memory(db: Path, project: str, title: str) -> tuple | None:
    """``(agent, session, project, title)`` for a row, on either substrate.

    nexus-aqbrk: was a raw ``sqlite3.connect`` read-back, which under the
    engine substrate opened an empty tmp file the service-backed writer had
    never touched ("no such table: memory"). ``get_all`` returns the same
    column set on both stores and — unlike ``get``/``search`` — tracks no
    access, so it observes attribution without perturbing it.
    """
    from nexus.db.t2 import T2Database

    with T2Database(db) as t2:
        row = memory_row(t2, project, title)
    if row is None:
        return None
    return (row["agent"], row["session"], row["project"], row["title"])


def _read_tier_writes(db: Path) -> list[tuple]:
    """Tier-write rows as ``(tool, tier, agent, project, target_title)``.

    nexus-onjvy gap 4 RESOLVED: ``tier_writes.target_title`` was WRITE-ONLY in
    service mode — accepted and stored by ``recordTierWrite`` /
    ``importTierWritesBatch``, but the only read route,
    ``query_tier_writes`` (``GET /v1/telemetry/tier_writes/query``), is an
    AGGREGATE shaped ``(tool, tier, agent, project, count)`` with no target
    slot. ``list_tier_writes`` (``GET /v1/telemetry/tier_writes/list``) is
    the unaggregated per-row read route that closes the gap: one row per
    write, ``target_title`` included.

    Called with zero filters, relying on the isolated per-test tmp DB (a
    fresh tenant per test, via ``isolated_t2``) rather than a server-side
    "all rows" guarantee: ``list_tier_writes`` is capped at its default
    ``limit`` (100, review finding 2026-08-08 — the route used to return
    every tier_writes row ever recorded for the tenant, unbounded) and this
    module's tests never write anywhere near that many rows per DB, so the
    default page is never actually truncating anything here. Discards
    ``total`` — this helper only cares about the rows themselves.
    """

    from nexus.db.t2 import T2Database

    with T2Database(db) as t2:
        result = t2.telemetry.list_tier_writes()
    return list(result["rows"])


def _telemetry_store(db: Path):
    from nexus.db.t2 import T2Database

    with T2Database(db) as t2:
        return t2.telemetry


def _assert_target_title(got: str | None, expected: str) -> None:
    """Assert ``target_title`` at its real value (nexus-onjvy gap 4 resolved).

    Restored to an unconditional strict check now that ``list_tier_writes``
    exists (see :func:`_read_tier_writes`) — no more substrate-asymmetric
    branch to carry.
    """
    assert got == expected


class TestSignatureAcceptsKwargs:
    def test_explicit_agent_and_session_persisted(
        self, isolated_t2: Path,
    ) -> None:
        from nexus.mcp.core import memory_put

        result = memory_put(
            content="finding from developer",
            project="nexus",
            title="bug-investigation-2026-05-06",
            agent="developer",
            session="sess-explicit",
        )
        assert "Stored:" in result, result

        row = _read_memory(isolated_t2, "nexus", "bug-investigation-2026-05-06")
        assert row is not None
        agent, session, project, title = row
        assert agent == "developer"
        assert session == "sess-explicit"
        assert project == "nexus"
        assert title == "bug-investigation-2026-05-06"

    def test_default_kwargs_use_env_fallback(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the MCP caller omits agent/session (or passes empty
        strings), the MemoryStore fall-back chain kicks in:
        NX_AGENT env → None, NX_SESSION_ID env → claude file → None."""
        from nexus.mcp.core import memory_put

        monkeypatch.setenv("NX_AGENT", "code-review-expert")
        monkeypatch.setenv("NX_SESSION_ID", "sess-from-env")

        memory_put(
            content="review notes",
            project="nexus",
            title="env-fallback-test",
        )

        row = _read_memory(isolated_t2, "nexus", "env-fallback-test")
        assert row is not None
        agent, session, *_ = row
        assert agent == "code-review-expert"
        assert session == "sess-from-env"

    def test_empty_strings_treated_as_unspecified(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MCP defaults to '' (FastMCP serialises None weirdly).
        Empty string MUST translate to 'unspecified' so the
        MemoryStore fall-backs run, NOT persist as a literal empty
        string in the agent column."""
        from nexus.mcp.core import memory_put

        monkeypatch.setenv("NX_AGENT", "fallback-agent")

        memory_put(
            content="empty string test",
            project="nexus",
            title="empty-translation",
            agent="",
            session="",
        )

        row = _read_memory(isolated_t2, "nexus", "empty-translation")
        assert row is not None
        agent, session, *_ = row
        # Empty string DID NOT win — the env fallback did.
        assert agent == "fallback-agent"
        # Session falls all the way through to None (no env, no file).
        assert session is None

    def test_agent_appears_in_tier_writes_telemetry(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """memory_put records to BOTH ``memory`` and ``tier_writes``;
        the agent attribution must propagate so ``nx tier-status``
        can slice by agent."""
        from nexus.mcp.core import memory_put
        monkeypatch.setenv("NX_SESSION_ID", "sess-tier-attr")

        memory_put(
            content="attribution end-to-end",
            project="nexus",
            title="attribution-test",
            agent="substantive-critic",
        )

        rows = _read_tier_writes(isolated_t2)
        agent_rows = [r for r in rows if r[0] == "memory_put"]
        assert len(agent_rows) == 1
        tool, tier, agent, project, target = agent_rows[0]
        assert tool == "memory_put"
        assert tier == "T2"
        assert agent == "substantive-critic"
        assert project == "nexus"
        _assert_target_title(target, "attribution-test")

    def test_no_kwargs_still_works_backward_compat(
        self, isolated_t2: Path,
    ) -> None:
        """Existing callers that pass only the original five params
        must continue to work unchanged. No agent/session ⇒ NULL in
        both columns."""
        from nexus.mcp.core import memory_put

        result = memory_put(
            content="legacy call",
            project="nexus",
            title="legacy-shape",
            tags="x,y",
            ttl=30,
        )
        assert "Stored:" in result, result

        row = _read_memory(isolated_t2, "nexus", "legacy-shape")
        assert row is not None
        agent, session, *_ = row
        assert agent is None
        assert session is None
