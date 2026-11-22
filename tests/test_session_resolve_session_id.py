# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nexus.session.resolve_session_id``: the one session-id chain, after RDR-253 Phase 2 (nexus-hczdo.2.1).

The chain is an explicit argument, ``NX_SESSION_ID``, ``CLAUDE_CODE_SESSION_ID``, then this process's own token file.
The machine-wide ``current_session`` file it used to end with is gone: a file there is never read, so a second session
on the box can no longer hand a process its scratch. Every caller that used to resolve a session id routes through this
function, so the chain lives in one place (issue #594, nexus-9e9a) and the T1 and tier-write fallbacks stay ``"unknown"``.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import nexus.session as session

_REAL_TOKEN_TIER = session._token_file_session_id


@dataclass(frozen=True)
class _Record:
    session_id: str | None


@pytest.fixture
def token_session(monkeypatch: pytest.MonkeyPatch):
    """Restore the real token-file tier (conftest stubs it) over a fake ``token_header.current``."""
    from nexus.identity import token_header

    monkeypatch.setattr(session, "_token_file_session_id", _REAL_TOKEN_TIER)

    def _set(value):
        if isinstance(value, Exception):
            def _raise():
                raise value
            monkeypatch.setattr(token_header, "current", lambda **_: _raise())
        else:
            monkeypatch.setattr(token_header, "current", lambda **_: value)
    return _set


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


class TestTheChain:
    def test_an_explicit_arg_wins(self, monkeypatch, token_session):
        monkeypatch.setenv("NX_SESSION_ID", "env-1")
        token_session(_Record("tok-1"))
        assert session.resolve_session_id("arg-1") == "arg-1"

    def test_nx_session_id_wins_over_claude_code_session_id(self, monkeypatch, token_session):
        monkeypatch.setenv("NX_SESSION_ID", "env-1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")
        token_session(_Record("tok-1"))
        assert session.resolve_session_id() == "env-1"

    def test_claude_code_session_id_wins_over_the_token_file(self, monkeypatch, token_session):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")
        token_session(_Record("tok-1"))
        assert session.resolve_session_id() == "cc-1"

    def test_the_token_file_answers_when_the_env_is_empty(self, token_session):
        token_session(_Record("tok-1"))
        assert session.resolve_session_id() == "tok-1"

    @pytest.mark.parametrize("arg", ["", "   ", None])
    def test_a_blank_arg_falls_through(self, arg, token_session):
        token_session(_Record("tok-1"))
        assert session.resolve_session_id(arg) == "tok-1"

    def test_blank_env_counts_as_unset(self, monkeypatch, token_session):
        monkeypatch.setenv("NX_SESSION_ID", "  ")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        token_session(_Record("tok-1"))
        assert session.resolve_session_id() == "tok-1"

    @pytest.mark.parametrize("record", [None, _Record(None), _Record("  ")])
    def test_no_token_session_resolves_nothing(self, record, token_session):
        token_session(record)
        assert session.resolve_session_id() is None

    def test_a_token_file_that_cannot_be_read_resolves_nothing(self, token_session):
        token_session(OSError("gone"))
        assert session.resolve_session_id() is None

    def test_the_explicit_variant_never_reads_the_token_file(self, token_session):
        token_session(_Record("tok-1"))
        assert session.resolve_explicit_session_id() is None


class TestTheRetiredFileIsNeverRead:
    def test_a_current_session_file_names_nothing(self, tmp_path: Path, monkeypatch, token_session):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "current_session").write_text("file-session-1", encoding="utf-8")
        token_session(None)
        assert session.resolve_session_id() is None

    @pytest.mark.parametrize("name", ["resolve_active_session_id", "read_claude_session_id", "write_claude_session_id",
                                      "claude_session_file", "CLAUDE_SESSION_FILE", "mark_mcp_server_process"])
    def test_the_retired_names_are_gone(self, name):
        assert not hasattr(session, name)

    @pytest.mark.t2_tenant  # session_start reaches the engine-backed stores; the storage-touch guard wants the lease
    def test_session_start_writes_no_current_session_file(self, tmp_path: Path, monkeypatch):
        from nexus import hooks

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_WORK_REAP_DISABLED", "1")
        try:
            hooks.session_start(claude_session_id="sess-start-1")
        except Exception:  # noqa: BLE001 — the rest of SessionStart may need services this test has none of
            pass
        assert not (tmp_path / "current_session").exists()


class TestCallersRouteThroughTheChain:
    """T1 metadata and tier-write rows take the chain's answer, and ``"unknown"`` when it has none."""

    def test_t1_takes_the_chains_answer(self, monkeypatch):
        from nexus.db import t1

        monkeypatch.setattr(t1, "resolve_session_id", lambda arg=None: "CHAIN-1", raising=False)
        monkeypatch.setattr(session, "resolve_session_id", lambda arg=None: "CHAIN-1")
        assert t1.T1Database._resolve_session_id(None) == "CHAIN-1"

    def test_t1_falls_back_to_unknown(self):
        from nexus.db.t1 import T1Database

        assert T1Database._resolve_session_id(None) == "unknown"

    @staticmethod
    def _capture_tier_write(monkeypatch) -> dict[str, str]:
        observed: dict[str, str] = {}

        class _Telemetry:
            def record_tier_write(self, *, session_id, **_):
                observed["session_id"] = session_id

        class _T2:
            telemetry = _Telemetry()

        @contextmanager
        def _ctx():
            yield _T2()

        monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _ctx)
        return observed

    def test_tier_write_takes_the_chains_answer(self, monkeypatch):
        from nexus.mcp.core import _record_tier_write

        monkeypatch.setattr(session, "resolve_session_id", lambda arg=None: "CHAIN-2")
        observed = self._capture_tier_write(monkeypatch)
        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "CHAIN-2"

    def test_tier_write_falls_back_to_unknown(self, monkeypatch):
        from nexus.mcp.core import _record_tier_write

        observed = self._capture_tier_write(monkeypatch)
        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "unknown"
