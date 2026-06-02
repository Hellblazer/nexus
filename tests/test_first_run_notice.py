# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P5b: user-visible embedder notice for plugin/Desktop/Cowork-first
users (no Claude Code SessionStart hook).

The MCP server cannot print to stdout (that is the JSON-RPC channel). The
verified surfacing mechanism (P5b spike) is the server-level ``instructions``
string: written to the low-level server before ``mcp.run()``, it is delivered
to the client at ``initialize`` and surfaced to the agent as the
"MCP Server Instructions" block.

These tests exercise the pure notice builder and the server-application
helper without standing up a live MCP server.
"""
from __future__ import annotations

import pytest

from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL
from nexus.mcp._first_run import apply_embedder_notice, embedder_startup_notice


def _pin(monkeypatch, *, local: bool, choice, active):
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: local)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: choice)
    monkeypatch.setattr(
        "nexus.db.local_ef._resolve_local_model", lambda *, warn: active
    )


class TestEmbedderStartupNotice:
    def test_cloud_mode_no_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin(monkeypatch, local=False, choice=None, active=_TIER0_MODEL)
        assert embedder_startup_notice() is None

    def test_bge_active_no_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin(monkeypatch, local=True, choice=_TIER1_MODEL, active=_TIER1_MODEL)
        assert embedder_startup_notice() is None

    def test_default_384_notice_points_at_nx_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # State 1: no choice recorded, 384 active.
        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)
        notice = embedder_startup_notice()
        assert notice is not None
        assert "nx init" in notice
        assert "384" in notice

    def test_degraded_bge_notice_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # State 2: chose bge but extra missing -> resolver fell back to 384.
        _pin(monkeypatch, local=True, choice=_TIER1_MODEL, active=_TIER0_MODEL)
        notice = embedder_startup_notice()
        assert notice is not None
        # fix_suggestions[0] for State 2 is the nx init line — assert it
        # directly rather than a disjunction that obscures the behavior.
        assert "nx init" in notice
        assert "384" in notice

    def test_notice_is_single_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Server instructions notice must be compact — one line, no newlines.
        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)
        notice = embedder_startup_notice()
        assert notice is not None
        assert "\n" not in notice


class _FakeLowLevel:
    def __init__(self, instructions=None) -> None:
        self.instructions = instructions


class _FakeServer:
    def __init__(self, instructions=None) -> None:
        self._mcp_server = _FakeLowLevel(instructions)


class TestApplyEmbedderNotice:
    def test_sets_instructions_when_notice_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)
        srv = _FakeServer(instructions=None)

        applied = apply_embedder_notice(srv)

        assert applied is True
        assert srv._mcp_server.instructions is not None
        assert "nx init" in srv._mcp_server.instructions

    def test_appends_to_existing_instructions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)
        srv = _FakeServer(instructions="EXISTING CAPABILITIES")

        apply_embedder_notice(srv)

        # existing content preserved, notice appended (not clobbered)
        assert srv._mcp_server.instructions.startswith("EXISTING CAPABILITIES")
        assert "nx init" in srv._mcp_server.instructions

    def test_no_change_when_no_notice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin(monkeypatch, local=True, choice=_TIER1_MODEL, active=_TIER1_MODEL)
        srv = _FakeServer(instructions=None)

        applied = apply_embedder_notice(srv)

        assert applied is False
        assert srv._mcp_server.instructions is None

    def test_best_effort_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A startup advisory must never break MCP boot. A malformed server
        # (no _mcp_server) returns False, not an exception.
        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)

        class _Broken:
            pass

        assert apply_embedder_notice(_Broken()) is False


class TestCoreMainWiring:
    def test_main_applies_embedder_notice_before_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """core.main() must wire the notice onto the live server before serving
        — proven by stopping at mcp.run and inspecting the instructions."""
        import nexus.mcp.core as core

        _pin(monkeypatch, local=True, choice=None, active=_TIER0_MODEL)
        monkeypatch.setattr(core, "_t1_chroma_shutdown", lambda: None)
        monkeypatch.setattr(
            "nexus.mcp._first_run.ensure_installed_and_running", lambda: None
        )
        monkeypatch.setattr(
            "nexus.mcp_infra.check_version_compatibility", lambda: None
        )
        # reset any instructions a prior test left on the shared module server
        core.mcp._mcp_server.instructions = None

        class _Stop(Exception):
            pass

        def _stop_run(*a, **k):
            raise _Stop

        monkeypatch.setattr(core.mcp, "run", _stop_run)

        with pytest.raises(_Stop):
            core.main()

        assert core.mcp._mcp_server.instructions is not None
        assert "nx init" in core.mcp._mcp_server.instructions

        # leave the shared server clean for other tests
        core.mcp._mcp_server.instructions = None
