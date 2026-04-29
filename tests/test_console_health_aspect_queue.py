# SPDX-License-Identifier: AGPL-3.0-or-later
"""Console health-panel aspect-queue gauge (nexus-qf48).

Tests the ``_collect_aspect_queue_data`` helper plus end-to-end render
of the ``Aspect Queue`` card in ``/health/refresh`` HTMX partial.
Mirrors the ``nx doctor --check-aspect-queue`` doctor surface so a
backlog (or stuck worker) is visible without running the CLI.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``nexus_config_dir`` to a clean tmp dir."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _create_queue_table(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE aspect_extraction_queue (
                collection      TEXT NOT NULL,
                source_path     TEXT NOT NULL,
                content_hash    TEXT NOT NULL DEFAULT '',
                content         TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'pending',
                retry_count     INTEGER NOT NULL DEFAULT 0,
                enqueued_at     TEXT NOT NULL,
                last_attempt_at TEXT,
                last_error      TEXT,
                PRIMARY KEY (collection, source_path)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_row(
    db_path: Path,
    *,
    collection: str,
    source_path: str,
    status: str,
    enqueued_at: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO aspect_extraction_queue "
            "(collection, source_path, status, enqueued_at) "
            "VALUES (?, ?, ?, ?)",
            (collection, source_path, status, enqueued_at),
        )
        conn.commit()
    finally:
        conn.close()


# ── _collect_aspect_queue_data ──────────────────────────────────────────────


class TestCollectAspectQueueData:
    """Direct unit tests for the helper."""

    def test_returns_absent_when_db_missing(self, isolated_config_dir: Path) -> None:
        from nexus.console.routes.health import _collect_aspect_queue_data

        out = _collect_aspect_queue_data()
        assert out == {"present": False}

    def test_returns_absent_when_table_missing(
        self, isolated_config_dir: Path,
    ) -> None:
        from nexus.console.routes.health import _collect_aspect_queue_data

        # T2 file exists but the queue table does not.
        db_path = isolated_config_dir / "memory.db"
        sqlite3.connect(str(db_path)).close()

        out = _collect_aspect_queue_data()
        assert out == {"present": False}

    def test_empty_queue_reports_zero_total(
        self, isolated_config_dir: Path,
    ) -> None:
        from nexus.console.routes.health import _collect_aspect_queue_data

        db_path = isolated_config_dir / "memory.db"
        _create_queue_table(db_path)

        out = _collect_aspect_queue_data()
        assert out["present"] is True
        assert out["total"] == 0
        assert out["by_status"] == {}
        assert out["oldest_pending"] is None
        assert out["failed_count"] == 0

    def test_mixed_statuses_aggregate_correctly(
        self, isolated_config_dir: Path,
    ) -> None:
        from nexus.console.routes.health import _collect_aspect_queue_data

        db_path = isolated_config_dir / "memory.db"
        _create_queue_table(db_path)

        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/1",
            status="pending", enqueued_at="2026-04-29T00:00:00Z",
        )
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/2",
            status="pending", enqueued_at="2026-04-29T01:00:00Z",
        )
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/3",
            status="processing", enqueued_at="2026-04-29T02:00:00Z",
        )
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/4",
            status="failed", enqueued_at="2026-04-29T03:00:00Z",
        )
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/5",
            status="completed", enqueued_at="2026-04-29T04:00:00Z",
        )

        out = _collect_aspect_queue_data()
        assert out["present"] is True
        assert out["total"] == 5
        assert out["by_status"] == {
            "pending": 2, "processing": 1, "failed": 1, "completed": 1,
        }
        # Oldest pending+processing is the earliest of the three matching rows.
        assert out["oldest_pending"] == "2026-04-29T00:00:00Z"
        assert out["failed_count"] == 1


# ── /health render ──────────────────────────────────────────────────────────


class TestHealthRouteRendersAspectQueueCard:
    """End-to-end: the rendered HTML carries the gauge card."""

    def test_aspect_queue_card_present_when_table_populated(
        self, isolated_config_dir: Path,
    ) -> None:
        from nexus.console.app import create_app

        db_path = isolated_config_dir / "memory.db"
        _create_queue_table(db_path)
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/1",
            status="pending", enqueued_at="2026-04-29T00:00:00Z",
        )
        _insert_row(
            db_path, collection="knowledge__a", source_path="/a/2",
            status="failed", enqueued_at="2026-04-29T01:00:00Z",
        )

        app = create_app()
        client = TestClient(app)
        resp = client.get("/health/refresh")
        assert resp.status_code == 200
        body = resp.text
        assert "Aspect Queue" in body
        # 2 rows: 1 pending + 1 failed -> stat-value is the total (2)
        # Substring guards are loose to tolerate template whitespace shifts.
        assert "pending=1" in body
        assert "failed=1" in body
        # Oldest pending field surfaces under populated state.
        assert "oldest pending" in body

    def test_aspect_queue_card_dash_when_table_absent(
        self, isolated_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_collect_aspect_queue_data`` returns ``{present: False}``
        the template renders the muted dash + "no T2 / table absent"
        label.

        We mock the helper rather than relying on an empty-disk fixture
        because ``/health/refresh`` runs a chain of T2-touching health
        checks before the aspect-queue check; on CI those side-effects
        sometimes pre-create the T2 db at ``NEXUS_CONFIG_DIR/memory.db``
        (env-dependent — the failure mode was observed on Ubuntu CI but
        not macOS local). The unit test
        ``TestCollectAspectQueueData::test_returns_absent_when_db_missing``
        already covers the absent-disk path on the helper side; this
        test now isolates the template branch from any T2 side-effects.
        """
        from nexus.console.app import create_app

        monkeypatch.setattr(
            "nexus.console.routes.health._collect_aspect_queue_data",
            lambda: {"present": False},
        )
        app = create_app()
        client = TestClient(app)
        resp = client.get("/health/refresh")
        assert resp.status_code == 200
        body = resp.text
        assert "Aspect Queue" in body
        assert "no T2 / table absent" in body
