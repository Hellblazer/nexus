# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.session.resolve_active_session_id (issue #594, nexus-9e9a).

The session_id resolution chain (NX_SESSION_ID env -> read_claude_session_id ->
fallback) must live in exactly one place and be reused by all three callsites:

  1. T1Database._init_new_discovery       (every Path A/B/C/client branch)
  2. _record_tier_write (mcp/core.py)     (telemetry insert)
  3. _print_tier_status_summary (_session_end_launcher.py)
                                          (session-end summary)

Pre-PR these were three open-coded copies with three divergent fallbacks
(uuid4(), "unknown", and no fallback respectively). The drift class that
produced PR #590 (nexus-h8ge) was a divergence between two of these copies.
This file's tests exist so any future change to the chain has to be made
in exactly one place or it surfaces here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helper: write the on-disk current_session pointer.
# ─────────────────────────────────────────────────────────────────────────────


def _write_current_session(tmp_path: Path, sid: str) -> None:
    target = tmp_path / "current_session"
    target.write_text(sid)
    target.chmod(0o600)


# ─────────────────────────────────────────────────────────────────────────────
# resolve_active_session_id chain semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveActiveSessionIdChain:
    """The chain: arg > NX_SESSION_ID env > read_claude_session_id() > None."""

    def test_explicit_arg_wins_over_env_and_file(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id("from-arg") == "from-arg"

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-env"

    def test_file_wins_when_env_empty(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "canonical-uuid")

        assert resolve_active_session_id() == "canonical-uuid"

    def test_returns_none_when_nothing_resolves(self, tmp_path, monkeypatch):
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No current_session file written.

        assert resolve_active_session_id() is None

    def test_blank_env_treated_as_unset(self, tmp_path, monkeypatch):
        """Whitespace-only NX_SESSION_ID falls through, matching the
        pre-PR behaviour every callsite implemented via .strip()."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "   ")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id() == "from-file"

    def test_blank_arg_falls_through(self, tmp_path, monkeypatch):
        """Empty string arg behaves the same as None: chain continues."""
        from nexus.session import resolve_active_session_id

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        _write_current_session(tmp_path, "from-file")

        assert resolve_active_session_id("") == "from-env"


# ─────────────────────────────────────────────────────────────────────────────
# Callsite unification: each of the three sites routes through the helper.
# ─────────────────────────────────────────────────────────────────────────────


class TestT1DatabaseRoutesThroughHelper:
    """T1Database._resolve_session_id MUST delegate to
    nexus.session.resolve_active_session_id with the explicit ctor arg."""

    def test_t1_propagates_helper_value(self, tmp_path, monkeypatch):
        from nexus.db.t1 import T1Database

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # Patch the helper to return a recognisable sentinel.
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-1234",
        )
        # Also patch the alias in nexus.db.t1's import namespace, since
        # `from nexus.session import resolve_active_session_id` would
        # bind the symbol at import time.
        monkeypatch.setattr(
            "nexus.db.t1.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-1234",
            raising=False,
        )
        assert T1Database._resolve_session_id(None) == "HELPER-SENTINEL-1234"

    def test_t1_uses_unknown_fallback_when_helper_returns_none(
        self, tmp_path, monkeypatch
    ):
        """Per-entry T1 metadata can never be empty (it's the filter
        key). When the helper returns None, T1 must substitute
        ``"unknown"`` so the audit log and chunk store agree on
        attribution."""
        from nexus.db.t1 import T1Database

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No current_session file -> helper returns None -> T1 uses "unknown".
        assert T1Database._resolve_session_id(None) == "unknown"


class TestRecordTierWriteRoutesThroughHelper:
    """_record_tier_write MUST delegate to resolve_active_session_id and
    use ``"unknown"`` as the per-row last-resort sentinel."""

    def test_tier_write_uses_helper(self, tmp_path, monkeypatch):
        """Patch the helper, invoke the function, assert the row landed
        with the helper's value as session_id."""
        from nexus.mcp.core import _record_tier_write

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: "HELPER-SENTINEL-9876",
        )

        observed: dict[str, str] = {}

        class _FakeTelemetry:
            def record_tier_write(self, **kw):
                observed["session_id"] = kw["session_id"]

        class _FakeT2:
            def __init__(self):
                self.telemetry = _FakeTelemetry()

        from contextlib import contextmanager

        @contextmanager
        def _fake_t2_ctx():
            yield _FakeT2()

        monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)

        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "HELPER-SENTINEL-9876"

    def test_tier_write_unknown_fallback_when_helper_returns_none(
        self, tmp_path, monkeypatch
    ):
        from nexus.mcp.core import _record_tier_write

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No file -> helper returns None -> tier-write uses "unknown".

        observed: dict[str, str] = {}

        class _FakeTelemetry:
            def record_tier_write(self, **kw):
                observed["session_id"] = kw["session_id"]

        class _FakeT2:
            def __init__(self):
                self.telemetry = _FakeTelemetry()

        from contextlib import contextmanager

        @contextmanager
        def _fake_t2_ctx():
            yield _FakeT2()

        monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)

        _record_tier_write(tool="t", tier="T1")
        assert observed.get("session_id") == "unknown"


class TestSessionEndLauncherRoutesThroughHelper:
    """_print_tier_status_summary MUST delegate to resolve_active_session_id and
    short-circuit when the helper returns None (no useful summary
    without a bound session)."""

    def test_launcher_short_circuits_when_helper_returns_none(
        self, tmp_path, monkeypatch
    ):
        """If the helper returns None the launcher must NOT query for
        ``WHERE session_id = "unknown"`` (that would leak rows from
        unrelated invocations into the user-facing summary)."""
        from nexus._session_end_launcher import _print_tier_status_summary

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: None,
        )
        # Also patch the launcher's import alias if applicable.
        monkeypatch.setattr(
            "nexus._session_end_launcher.resolve_active_session_id",
            lambda arg=None: None,
            raising=False,
        )

        # Should return without raising and without attempting any DB
        # connect. We assert this by ensuring sqlite3.connect is NOT
        # called.
        called = {"connect": False}

        def _spy_connect(*a, **kw):
            called["connect"] = True
            raise AssertionError("launcher must short-circuit")

        monkeypatch.setattr("sqlite3.connect", _spy_connect)
        _print_tier_status_summary()
        assert called["connect"] is False

    def test_launcher_uses_helper_value_for_query(
        self, tmp_path, monkeypatch
    ):
        """When the helper returns a session_id, the launcher must use
        it as the WHERE-clause value verbatim."""
        import sqlite3

        from nexus._session_end_launcher import _print_tier_status_summary

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "nexus.session.resolve_active_session_id",
            lambda arg=None: "LAUNCHER-SENTINEL",
        )
        monkeypatch.setattr(
            "nexus._session_end_launcher.resolve_active_session_id",
            lambda arg=None: "LAUNCHER-SENTINEL",
            raising=False,
        )

        # Build a minimal SQLite db with the tier_writes table + a row.
        db_path = tmp_path / "t2.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE tier_writes "
            "(session_id TEXT, ts TEXT, tool TEXT, tier TEXT, "
            "agent TEXT, project TEXT, target_title TEXT)"
        )
        conn.execute(
            "INSERT INTO tier_writes VALUES "
            "('LAUNCHER-SENTINEL', '2026-05-08T00:00:00', 't', 'T1', "
            "'', '', '')"
        )
        conn.execute(
            "INSERT INTO tier_writes VALUES "
            "('OTHER-SESSION', '2026-05-08T00:00:00', 't', 'T1', "
            "'', '', '')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "nexus.config.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path", lambda: db_path,
        )

        # Capture stderr so we can assert the launcher only counted the
        # one row matching LAUNCHER-SENTINEL, not the OTHER-SESSION row.
        import io
        import sys

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", buf)
        _print_tier_status_summary()
        out = buf.getvalue()
        assert "total=1" in out, out
        assert "LAUNCHER" in out  # truncated session_id[:8] in the line
