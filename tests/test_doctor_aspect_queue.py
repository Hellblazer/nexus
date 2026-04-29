# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1pfq: ``nx doctor --check-aspect-queue`` reports queue health.

RDR-089 nexus-qeo8 introduced an async aspect-extraction worker
(``src/nexus/aspect_worker.py``) with a daemon thread draining the T2
``aspect_extraction_queue`` table. The queue is fully populated and
readable via SQL but has zero observability surface — a backlog can
grow silently because the async path is invisible.

This adds a ``nx doctor --check-aspect-queue`` flag that reports:

  * total queued rows
  * rows currently in ``processing`` status
  * oldest ``enqueued_at`` timestamp (lag indicator)
  * rows in ``failed`` status with their last error (so a stuck
    worker is visible)

Bead's two-part scope (doctor + console) is split: this commit
ships the doctor surface; the console-gauge piece is deferred or
filed as a follow-up bead so the running PR doesn't grow the
console-UI surface area in the same arc.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.migrations import apply_pending
from nexus.commands.upgrade import _current_version


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed_db(tmp_path: Path) -> Path:
    """Return a path to a T2 DB with the migrations applied."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    apply_pending(conn, _current_version())
    conn.close()
    return db_path


def _enqueue(
    db_path: Path, *, collection: str, source_path: str,
    status: str = "pending", error: str = "",
    enqueued_at: str = "2026-04-29T00:00:00+00:00",
) -> None:
    """Insert a queue row directly via SQL."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO aspect_extraction_queue "
        "(collection, source_path, content_hash, content, status, "
        " retry_count, enqueued_at, last_error) "
        "VALUES (?, ?, '', '', ?, 0, ?, ?)",
        (collection, source_path, status, enqueued_at, error),
    )
    conn.commit()
    conn.close()


# ── Empty queue ────────────────────────────────────────────────────────────


class TestEmptyQueue:
    def test_empty_queue_reports_zero(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        db_path = _seed_db(tmp_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "queue" in out
        # Empty queue: explicit 0 (or "no items") rather than skipping.
        assert "0" in result.output

    def test_missing_db_handled_gracefully(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        """No T2 db on disk yet (fresh install) must not crash."""
        nonexistent = tmp_path / "missing.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: nonexistent,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        # Some sensible message, not a stack trace.
        assert "not found" in out or "no" in out


# ── Populated queue ────────────────────────────────────────────────────────


class TestPopulatedQueue:
    def test_reports_total_count(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        db_path = _seed_db(tmp_path)
        for i in range(3):
            _enqueue(db_path, collection="docs__a", source_path=f"/x/d{i}.md")
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        assert "3" in result.output

    def test_reports_status_breakdown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        db_path = _seed_db(tmp_path)
        _enqueue(db_path, collection="c", source_path="/p1", status="pending")
        _enqueue(db_path, collection="c", source_path="/p2", status="processing")
        _enqueue(
            db_path, collection="c", source_path="/p3",
            status="failed", error="boom: details",
        )
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "pending" in out
        assert "processing" in out
        assert "failed" in out

    def test_failed_rows_show_last_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        db_path = _seed_db(tmp_path)
        _enqueue(
            db_path, collection="docs__test",
            source_path="/x/broken.md",
            status="failed",
            error="Voyage 413: chunk too large",
        )
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        # Operator needs to see WHY the row failed, not just that it did.
        assert "Voyage 413" in result.output

    def test_reports_oldest_enqueued_at(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ):
        """The oldest pending row's enqueued_at indicates worker lag."""
        db_path = _seed_db(tmp_path)
        _enqueue(
            db_path, collection="c", source_path="/older",
            enqueued_at="2026-04-01T00:00:00+00:00",
        )
        _enqueue(
            db_path, collection="c", source_path="/newer",
            enqueued_at="2026-04-29T00:00:00+00:00",
        )
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        # Output should reference the older timestamp; the newer one
        # may also appear if the report shows both, but the older
        # must be present as the lag indicator.
        assert "2026-04-01" in result.output
