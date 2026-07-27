# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1B nx tier-status CLI (nexus-a52i).

Covers:
- Default mode reads NX_SESSION_ID env, queries tier_writes, prints summary.
- --session, --last, --since, --json modes.
- Empty / missing-table cases produce clean output (no traceback).
- Mutual exclusion of --session/--last/--since.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests._t2_fixture_ops import seed_tier_write


def _seed_t2(db_path: Path, rows: list[tuple]) -> None:
    """Insert tier_writes rows into whichever T2 store the CLI will read.

    Each row is (session_id, tool, tier, agent, project, target_title).
    Timestamps auto-stamp at row time.

    nexus-aqbrk: was a raw ``sqlite3.connect`` + INSERT. ``nx tier-status`` is
    SERVICE-AWARE — ``HttpTelemetryStore.query_tier_writes`` is the documented
    twin of ``tier_status._query``, and this file's own
    ``TestServiceModeReadParity`` covers that path — so under the engine
    substrate the CLI read the service while the seed landed in a local file
    it never opened, and every assertion got "(no writes)". Routed through
    ``seed_tier_write``, which branches once (raw INSERT on SQLite,
    ``import_tier_write`` on the service arm) so the same seed reaches
    whichever store is real.

    ROWS ARE STAMPED ONE SECOND APART, deliberately. The service's import
    path is ``onConflictDoNothing`` on an ETL dedup key that does NOT include
    ``target_title``, so two writes sharing a timestamp and differing only in
    which entry they targeted COLLAPSE into one row — while SQLite, which has
    no such constraint, inserts both. A single shared ``ts`` (what this
    seeder used to do, harmlessly, on SQLite) therefore made the two seeded
    ``memory_put`` rows read back as ``total: 2`` instead of ``total: 3`` on
    the engine. Distinct stamps are also what production produces: ``ts`` is
    stamped per write, not per batch.
    """
    from datetime import timedelta

    from nexus.db.t2 import T2Database

    base = datetime.now(timezone.utc)
    with T2Database(db_path) as db:
        for offset, (sid, tool, tier, agent, project, title) in enumerate(rows):
            seed_tier_write(
                db,
                session_id=sid,
                tool=tool,
                tier=tier,
                agent=agent,
                project=project,
                target_title=title,
                ts=(base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )


@pytest.fixture
def isolated_t2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_db_path to a tmp file."""
    from nexus.commands import _helpers, tier_status as ts_mod
    db = tmp_path / "t.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db)
    monkeypatch.setattr(ts_mod, "default_db_path", lambda: db)
    return db


class TestDefaultSession:
    def test_default_uses_env_session_id(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        _seed_t2(isolated_t2, [
            ("sess-A", "memory_put", "T2", None, "nexus", "finding-1"),
            ("sess-A", "memory_put", "T2", None, "nexus", "finding-2"),
            ("sess-A", "scratch_put", "T1", None, None, "hypothesis"),
            ("sess-B", "memory_put", "T2", None, "other", "noise"),
        ])

        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "session sess-A" in result.output
        assert "total: 3" in result.output
        assert "T2" in result.output
        assert "T1" in result.output
        # sess-B's row must not leak into sess-A's count
        assert "noise" not in result.output

    def test_default_no_session_resolvable_exits_clean(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        # Pre-create the DB so the no-session-resolvable check fires
        # before the missing-DB check.
        sqlite3.connect(str(isolated_t2)).close()

        # Ensure no session resolvable.
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        import nexus.commands.tier_status as ts_mod
        monkeypatch.setattr(ts_mod, "read_claude_session_id", lambda: None)

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 1
        assert "No current session resolvable" in result.output


class TestExplicitFlags:
    def test_session_flag_overrides_env(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-X", "store_put", "T3", None, None, "permanent"),
            ("sess-X", "plan_save", "plan", None, "nexus", "research-default"),
            ("sess-Y", "memory_put", "T2", None, "nexus", "noise"),
        ])
        monkeypatch.setenv("NX_SESSION_ID", "sess-Y")  # ignored

        result = CliRunner().invoke(tier_status_cmd, ["--session", "sess-X"])
        assert result.exit_code == 0, result.output
        assert "session sess-X" in result.output
        assert "total: 2" in result.output
        assert "T3" in result.output
        assert "plan" in result.output
        assert "noise" not in result.output

    def test_last_n_aggregates_recent_sessions(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-1", "memory_put", "T2", None, "nexus", "a"),
            ("sess-2", "memory_put", "T2", None, "nexus", "b"),
            ("sess-3", "memory_put", "T2", None, "nexus", "c"),
        ])

        result = CliRunner().invoke(tier_status_cmd, ["--last", "3"])
        assert result.exit_code == 0, result.output
        assert "last 3 session(s)" in result.output
        assert "total: 3" in result.output

    def test_json_output_is_valid_and_complete(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-J", "memory_put", "T2", "developer", "nexus", "f1"),
            ("sess-J", "scratch_put", "T1", None, None, "h1"),
        ])

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "sess-J", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["session_id"] == "sess-J"
        assert payload["total_writes"] == 2
        assert payload["by_tier"]["T2"] == 1
        assert payload["by_tier"]["T1"] == 1
        assert payload["by_tier"]["T3"] == 0
        assert any(r["tool"] == "memory_put" and r["agent"] == "developer"
                   for r in payload["rows"])

    def test_mutually_exclusive_flags(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "x", "--last", "5"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestEmptyOrMissing:
    def test_empty_session_prints_no_writes(
        self, isolated_t2: Path,
    ) -> None:
        """Session with zero tier_writes prints '(no writes)' rather than
        empty output or traceback."""
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("populated-sess", "memory_put", "T2", None, "nexus", "x"),
        ])

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "empty-sess"],
        )
        assert result.exit_code == 0, result.output
        assert "no writes" in result.output

    def test_missing_table_treated_as_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If tier_writes table doesn't exist (no recorder writes ever),
        CLI prints zero rather than erroring on missing table."""
        from nexus.commands import _helpers, tier_status as ts_mod
        from nexus.commands.tier_status import tier_status_cmd

        # Empty DB — no tier_writes table.
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db)
        monkeypatch.setattr(ts_mod, "default_db_path", lambda: db)

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "any-sess"],
        )
        assert result.exit_code == 0, result.output
        assert "no writes" in result.output


def _seed_local_t2(db_path: Path, rows: list[tuple]) -> None:
    """Seed the LOCAL sqlite tier_writes table, whatever the substrate.

    The deliberate counterpart to :func:`_seed_t2`. Used only by
    :class:`TestServiceModeReadParity`, whose subject is that service mode
    must NOT read the local table — so the row has to exist locally and
    ONLY locally, or the assertion proves nothing.
    """
    from nexus.db.migrations import migrate_tier_writes

    conn = sqlite3.connect(str(db_path))
    try:
        migrate_tier_writes(conn)
        ts = datetime.now(timezone.utc).isoformat()
        for sid, tool, tier, agent, project, title in rows:
            conn.execute(
                "INSERT INTO tier_writes "
                "(session_id, ts, tool, tier, agent, project, target_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, ts, tool, tier, agent, project, title),
            )
        conn.commit()
    finally:
        conn.close()


class TestServiceModeReadParity:
    """nexus-wyu1g: in service mode tier_writes live in Postgres, not local
    SQLite. The diagnostics must report that honestly instead of silently
    showing 0 (false wrong-result)."""

    @pytest.fixture(autouse=True)
    def _no_reachable_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make service mode UNREACHABLE, which is this class's precondition.

        nexus-aqbrk: these tests assert the honest BAIL-OUT — "service-backed,
        counts unavailable" — which by definition is what the CLI prints when
        it cannot reach an engine. They got that precondition for free on the
        SQLite arm, where nothing sets an endpoint. Under the engine substrate
        ``t2_service_env`` points at a LIVE test engine, so the CLI correctly
        read real counts and the bail-out never fired: the tests were asserting
        an unreachable-engine message in a world with a reachable engine.

        Stripping the endpoint restores the state under test. The reachable
        half is not lost — ``test_tier_status_service_mode_reads_real_counts``
        below owns it, with a fake store standing in for a post-59wjj engine.

        Same shape as tests/db/test_om64x_stale_port_recovery.py: a module
        whose subject is a no-endpoint code path, handed an endpoint by the
        substrate pin.
        """
        for var in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN",
                    "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
            monkeypatch.delenv(var, raising=False)

    def test_tier_status_service_mode_reports_service_backed(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        # Seed a local row — service mode must NOT read it (it would falsely
        # report counts; in real service mode the local table is empty/stale).
        _seed_local_t2(isolated_t2, [("sess-A", "memory_put", "T2", None, "nexus", "x")])
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "service-backed" in result.output
        assert "total: 1" not in result.output  # did not read the local row

    def test_tier_status_service_mode_json(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        result = CliRunner().invoke(tier_status_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload.get("service_backed") is True

    def test_tier_status_service_mode_reads_real_counts(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # nexus-59wjj: with a reachable post-59wjj engine, tier-status shows
        # REAL service-side counts instead of the honest bail-out message.
        from nexus.commands.tier_status import tier_status_cmd

        class _FakeStore:
            def query_tier_writes(self, *, session_id=None, since=None, last_n=None):
                assert session_id == "sess-A"
                return [
                    ("memory_put", "T2", "developer", "nexus", 3),
                    ("store_put", "T3", None, None, 1),
                ]

        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "total: 4" in result.output
        assert "memory_put" in result.output
        assert "service-backed telemetry store" not in result.output

    def test_tier_status_service_mode_json_real_counts(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        class _FakeStore:
            def query_tier_writes(self, *, session_id=None, since=None, last_n=None):
                return [("scratch", "T1", None, None, 2)]

        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )

        result = CliRunner().invoke(tier_status_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["total_writes"] == 2
        assert payload["by_tier"]["T1"] == 2
        assert payload["rows"][0]["tool"] == "scratch"

    def test_doctor_service_mode_real_counts_without_local_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Critique Critical-1: service is the RDR-152 default and a fresh
        # service-mode install may never create memory.db — the service read
        # must be reachable WITHOUT a local db (the old ordering bailed with
        # "T2 database not found (skip)" before the SERVICE check).
        import click as _click
        from click.testing import CliRunner as _CR

        import nexus.commands.doctor as doc

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: tmp_path / "does-not-exist" / "memory.db",
        )
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        class _FakeStore:
            def query_tier_writes(self, *, session_id=None, since=None, last_n=None):
                assert session_id == "sess-A"
                return [
                    ("memory_put", "T2", "developer", "nexus", 3),
                    ("scratch", "T1", None, None, 1),
                ]

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )

        @_click.command()
        def _wrap() -> None:
            doc._run_check_tier_discipline()

        result = _CR().invoke(_wrap, [])
        assert result.exit_code == 0, result.output
        assert "total writes: 4" in result.output
        assert "T2 database not found" not in result.output

    def test_doctor_service_mode_zero_writes_prints_guidance_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Review Medium-2: the service branch must print the same operator
        # guidance the local branch does on zero writes.
        import click as _click
        from click.testing import CliRunner as _CR

        import nexus.commands.doctor as doc

        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: tmp_path / "none" / "memory.db",
        )
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        class _EmptyStore:
            def query_tier_writes(self, **_kw):
                return []

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _EmptyStore(),
        )

        @_click.command()
        def _wrap() -> None:
            doc._run_check_tier_discipline()

        result = _CR().invoke(_wrap, [])
        assert result.exit_code == 0, result.output
        assert "WARNING: zero tier writes" in result.output
        assert "nx tier-status --session sess-A" in result.output

    def test_tier_status_service_read_404_names_engine_skew(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Critique Significant-3: a 404 (engine predates the route) must be
        # diagnosed as version skew, NOT lumped with real engine errors.
        import httpx

        from nexus.commands.tier_status import tier_status_cmd

        class _404Store:
            def query_tier_writes(self, **_kw):
                resp = httpx.Response(404, request=httpx.Request("GET", "http://x/q"))
                raise httpx.HTTPStatusError("404", request=resp.request, response=resp)

        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _404Store(),
        )

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "predates the tier_writes/query route" in result.output

    def test_tier_status_service_read_500_names_engine_error(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ...while a 500 from a live engine must point at the engine, not at
        # version skew.
        import httpx

        from nexus.commands.tier_status import tier_status_cmd

        class _500Store:
            def query_tier_writes(self, **_kw):
                resp = httpx.Response(500, request=httpx.Request("GET", "http://x/q"))
                raise httpx.HTTPStatusError("500", request=resp.request, response=resp)

        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _500Store(),
        )

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "HTTP 500" in result.output
        assert "investigate the engine" in result.output

    def test_doctor_tier_discipline_service_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner as _CR
        import nexus.commands.doctor as doc

        db = tmp_path / "t.db"
        db.touch()
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db)
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

        # _run_check_tier_discipline prints via click.echo; capture with a runner.
        import click

        @click.command()
        def _wrap() -> None:
            doc._run_check_tier_discipline()

        result = _CR().invoke(_wrap, [])
        assert result.exit_code == 0, result.output
        assert "service-backed" in result.output and "N/A" in result.output
        assert "no writes seen" not in result.output
