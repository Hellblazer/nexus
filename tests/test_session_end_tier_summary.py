# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1C: SessionEnd launcher tier-write summary (nexus-a52i).

LOCAL mode prints BEFORE the fork (fast sqlite read; the launcher
detaches stdio in the child). SERVICE mode (nexus-ov13k) prints AFTER
the fork dispatch, from the parent — a network read must never sit
ahead of the cleanup dispatch (review Critical: the retrying transport
has a 20-50s worst case), so the service twin uses a pinned-endpoint,
single-attempt read instead. This test suite locks in:

- Sessions with writes get a one-line stderr summary at close (both modes).
- Sessions with zero writes are silent (no noise on transactional runs).
- Failures (missing table, unreadable DB, no session resolvable,
  service unreachable) are swallowed — telemetry must never break
  session close.
- The pre-fork printer NEVER touches the network in service mode, and
  main() orders prefork → fork dispatch → service summary.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _seed(db_path: Path, rows: list[tuple]) -> None:
    """Insert tier_writes rows. Each row: (session_id, tool, tier)."""
    from nexus.db.migrations import migrate_tier_writes

    conn = sqlite3.connect(str(db_path))
    try:
        migrate_tier_writes(conn)
        ts = datetime.now(timezone.utc).isoformat()
        for sid, tool, tier in rows:
            conn.execute(
                "INSERT INTO tier_writes "
                "(session_id, ts, tool, tier) VALUES (?, ?, ?, ?)",
                (sid, ts, tool, tier),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def isolated_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    from nexus.commands import _helpers
    db = tmp_path / "t.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    import nexus.session
    monkeypatch.setattr(nexus.session, "read_claude_session_id", lambda: None)
    return db


class TestPrintTierStatusSummary:
    def test_prints_summary_when_session_has_writes(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus._session_end_launcher import _print_tier_status_summary

        sid = "abc12345-1111-2222-3333-1234567890ab"
        monkeypatch.setenv("NX_SESSION_ID", sid)
        _seed(isolated_t2, [
            (sid, "memory_put", "T2"),
            (sid, "memory_put", "T2"),
            (sid, "scratch_put", "T1"),
            (sid, "store_put", "T3"),
        ])

        _print_tier_status_summary()

        out = capsys.readouterr().err
        assert "nx tier writes" in out
        assert sid[:8] in out
        assert "total=4" in out
        assert "T1=1" in out
        assert "T2=2" in out
        assert "T3=1" in out
        # plan not in this run, must be omitted
        assert "plan=" not in out

    def test_silent_when_zero_writes(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero writes ⇒ no output. Transactional sessions don't need
        the noise."""
        from nexus._session_end_launcher import _print_tier_status_summary

        sid = "empty-session-no-writes-zzzzzzzz"
        monkeypatch.setenv("NX_SESSION_ID", sid)
        _seed(isolated_t2, [
            ("other-session", "memory_put", "T2"),  # for someone else
        ])

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_prefork_summary_is_silent_in_service_mode(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-ov13k review Critical: the PRE-fork printer must never touch
        the network in service mode — any wait there sits ahead of the
        cleanup dispatch (the module's historical SIGTERM race)."""
        from nexus._session_end_launcher import _print_tier_status_summary

        monkeypatch.setenv("NX_SESSION_ID", "svc-pre-1111-2222-3333-abcdefabcdef")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        def _boom(**_kw):
            raise AssertionError("pre-fork path must not construct the HTTP store")

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", _boom,
        )

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_service_summary_prints_from_service(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-ov13k: the POST-fork service printer reads the engine via
        the single-attempt variant and prints the same format."""
        from nexus._session_end_launcher import _print_service_tier_summary

        sid = "svc12345-1111-2222-3333-1234567890ab"
        monkeypatch.setenv("NX_SESSION_ID", sid)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            lambda **_kw: ("http://svc.test", "tok"),
        )

        class _FakeStore:
            def query_tier_writes_once(self, *, session_id=None, timeout=None):
                assert session_id == sid
                assert timeout == 2.0
                return [
                    ("memory_put", "T2", None, None, 2),
                    ("scratch_put", "T1", None, None, 1),
                ]

            def close(self):
                pass

        def _make_store(**kw):
            # Both halves must be PINNED (round-2 critique: an unpinned
            # constructor can wait 12s in the evidence-gated resolve).
            assert kw.get("base_url") == "http://svc.test"
            assert kw.get("_token") == "tok"
            return _FakeStore()

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", _make_store,
        )

        _print_service_tier_summary()

        out = capsys.readouterr().err
        assert "nx tier writes" in out
        assert "total=3" in out
        assert "T2=2" in out and "T1=1" in out

    def test_service_summary_read_failure_is_silent_on_stderr(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Session close must never noise-fail or crash on an older engine
        # (404) or an unreachable service.
        from nexus._session_end_launcher import _print_service_tier_summary

        monkeypatch.setenv("NX_SESSION_ID", "svc-fail-1111-2222-3333-abcdefabcdef")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            lambda **_kw: ("http://svc.test", "tok"),
        )
        class _BoomStore:
            def query_tier_writes_once(self, **_kw):
                raise RuntimeError("service unreachable")

            def close(self):
                pass

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda **_kw: _BoomStore(),
        )

        _print_service_tier_summary()
        assert capsys.readouterr().err == ""

    def test_service_summary_zero_writes_silent(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus._session_end_launcher import _print_service_tier_summary

        monkeypatch.setenv("NX_SESSION_ID", "svc-zero-1111-2222-3333-abcdefabcdef")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            lambda **_kw: ("http://svc.test", "tok"),
        )
        class _EmptyStore:
            def query_tier_writes_once(self, **_kw):
                return []

            def close(self):
                pass

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda **_kw: _EmptyStore(),
        )

        _print_service_tier_summary()
        assert capsys.readouterr().err == ""

    def test_service_summary_noop_in_local_mode(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus._session_end_launcher import _print_service_tier_summary

        monkeypatch.setenv("NX_SESSION_ID", "loc-1111-2222-3333-abcdefabcdefab")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")

        _print_service_tier_summary()
        assert capsys.readouterr().err == ""

    def test_main_prints_service_summary_after_fork_dispatch(
        self,
        isolated_t2: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Ordering contract (review Critical): the cleanup fork dispatch must
        # happen BEFORE the service summary attempt.
        import nexus._session_end_launcher as launcher

        order: list[str] = []
        monkeypatch.setattr(
            launcher, "_daemonize_and_run", lambda: order.append("fork"),
        )
        monkeypatch.setattr(
            launcher, "_print_service_tier_summary", lambda: order.append("summary"),
        )
        monkeypatch.setattr(
            launcher, "_print_tier_status_summary", lambda: order.append("prefork"),
        )

        launcher.main()

        assert order == ["prefork", "fork", "summary"]

    def test_silent_when_no_session_resolvable(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """No NX_SESSION_ID, no claude session file → silent (the launcher
        would have nothing meaningful to summarise)."""
        from nexus._session_end_launcher import _print_tier_status_summary

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_silent_when_db_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DB file absent → silent. Fresh installs that haven't done any
        tier writes yet shouldn't error at close."""
        from nexus.commands import _helpers
        from nexus._session_end_launcher import _print_tier_status_summary

        # Path that does NOT exist.
        absent = tmp_path / "missing.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: absent)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_silent_when_table_not_initialized(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DB exists but tier_writes table not yet created (no writes ever) → silent."""
        from nexus.commands import _helpers
        from nexus._session_end_launcher import _print_tier_status_summary

        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_swallows_unexpected_exception(
        self,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any unexpected error in the summary path must NOT propagate.
        Telemetry breaking the launcher is the worst regression class."""
        from nexus._session_end_launcher import _print_tier_status_summary

        # Sabotage: make default_db_path raise.
        from nexus.commands import _helpers

        def boom():
            raise RuntimeError("simulated default_db_path failure")

        monkeypatch.setattr("nexus.config.default_db_path", boom)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        # Must not raise.
        _print_tier_status_summary()
        assert capsys.readouterr().err == ""
