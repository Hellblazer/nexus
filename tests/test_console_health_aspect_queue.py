# SPDX-License-Identifier: AGPL-3.0-or-later
"""Console health-panel aspect-queue gauge (nexus-qf48).

End-to-end render of the ``Aspect Queue`` card in ``/health/refresh``
HTMX partial (the template contract, with the collector mocked).
Mirrors the ``nx doctor --check-aspect-queue`` doctor surface so a
backlog (or stuck worker) is visible without running the CLI.

The ``_collect_aspect_queue_data`` unit tests died with the local-SQLite
reader leg (RDR-158 P3, nexus-7bomn) — the helper now delegates straight
to ``_collect_aspect_queue_data_service`` (engine PG).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``nexus_config_dir`` to a clean tmp dir."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


# ── /health render ──────────────────────────────────────────────────────────


class TestHealthRouteRendersAspectQueueCard:
    """End-to-end: the rendered HTML carries the gauge card."""

    def test_aspect_queue_card_present_when_table_populated(
        self, isolated_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The template renders pending/failed/oldest_pending fields when
        ``_collect_aspect_queue_data`` returns a populated payload.

        We mock the helper rather than relying on a fixture-populated
        ``memory.db`` for the same reason the absent-test mocks it (see
        sibling docstring): ``/health/refresh`` runs a chain of T2-touching
        health checks before the aspect-queue check, and on Ubuntu CI those
        side-effects rewrite the DB the route reads from, so the test's
        ``_create_queue_table`` + ``_insert_row`` setup is not what the
        route sees by the time it queries (observed: only the "failed"
        row survives the round-trip on CI; pending row is dropped). The
        unit tests under ``TestCollectAspectQueueData`` already cover the
        helper's data path against a real ``memory.db``; this test now
        isolates the *template* branch from T2 side-effects.
        """
        from nexus.console.app import create_app

        monkeypatch.setattr(
            "nexus.console.routes.health._collect_aspect_queue_data",
            lambda: {
                "present": True,
                "total": 2,
                "by_status": {"pending": 1, "failed": 1},
                "oldest_pending": "2026-04-29T00:00:00Z",
                "failed_count": 1,
            },
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
