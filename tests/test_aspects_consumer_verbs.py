# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 §A8 / nexus-6y2a9: ``nx aspects backfill-source-uri`` and
``nx aspects gc-pre-rdr096`` verbs.

These two verbs carry forward the substantive behaviour of three
migrations whose bodies were demoted to no-ops under RDR-120's
substrate-vs-consumer boundary:

  - ``migrate_document_aspects_source_uri`` (4.16.0)  →  DDL only
  - ``migrate_document_aspects_source_uri_backfill_empty`` (4.26.2) →  no-op
  - ``migrate_drop_null_aspect_rows`` (4.16.0)        →  no-op

The verb-tests below port the same scenarios the migration tests in
``tests/test_migrations_rdr096.py`` used to exercise, plus add CLI-
level coverage (dry-run, --apply, missing DB, missing column).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.aspects import aspects_group


# ── Helpers ───────────────────────────────────────────────────────────────────


def _seed_aspects_schema(conn: sqlite3.Connection) -> None:
    """Set up document_aspects at the post-RDR-096 schema (with source_uri).

    Mirrors what the migrations on real installs produce after they have
    run: the DDL is in place, but rows can carry NULL or empty
    ``source_uri`` for the backfill verb to act on.
    """
    # RDR-158 P4 Stage 4 (nexus-i711w): frozen DDL snapshot of what
    # migrate_document_aspects_table + migrate_document_aspects_source_uri
    # produced — the migration chain died with nexus/db/migrations.py.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS document_aspects (
            collection             TEXT NOT NULL,
            source_path            TEXT NOT NULL,
            problem_formulation    TEXT,
            proposed_method        TEXT,
            experimental_datasets  TEXT,
            experimental_baselines TEXT,
            experimental_results   TEXT,
            extras                 TEXT,
            confidence             REAL,
            extracted_at           TEXT NOT NULL,
            model_version          TEXT NOT NULL,
            extractor_name         TEXT NOT NULL,
            source_uri             TEXT,
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor
            ON document_aspects(extractor_name, model_version);
    """)


def _insert_aspect(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_path: str,
    source_uri: str | None = None,
    extractor: str = "scholarly-paper-v1",
    problem_formulation: str | None = None,
    proposed_method: str | None = None,
    experimental_datasets: str = "[]",
    experimental_baselines: str = "[]",
    experimental_results: str | None = None,
    extras: str | None = "{}",
    confidence: float | None = None,
) -> None:
    if source_uri is None:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, source_path, problem_formulation, proposed_method,
             experimental_datasets, experimental_baselines, experimental_results,
             extras, confidence,
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             extractor),
        )
    else:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, source_uri, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, source_path, source_uri,
             problem_formulation, proposed_method,
             experimental_datasets, experimental_baselines, experimental_results,
             extras, confidence,
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             extractor),
        )
    conn.commit()


@pytest.fixture
def t2_path(tmp_path: Path, monkeypatch) -> Path:
    """Patch default_db_path to a tmp memory.db. The verbs open
    T2Database under the hood; ensure the file exists with the
    post-RDR-096 aspect schema."""
    mem_path = tmp_path / "memory.db"
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path",
        lambda: mem_path,
    )
    # Hand-seed the schema (T2Database.__init__ would also do this
    # via its store init paths, but spelling it out keeps each test
    # honest about what it expects).
    conn = sqlite3.connect(str(mem_path))
    _seed_aspects_schema(conn)
    conn.close()
    return mem_path


# ── backfill-source-uri verb ──────────────────────────────────────────────────


class TestRetiredRepairVerbsRefuse:
    """RDR-158 P4 Stage 4 (nexus-i711w, critique Critical): the two
    pre-migration repair verbs — ``backfill-source-uri`` and
    ``gc-pre-rdr096`` — carried the last unguarded raw-SQLite WRITE paths
    into the frozen migration source (RDR-176 Gap 2). Both are now
    unconditional guided refusals; the behavioural suites that drove
    their raw UPDATE/DELETE arms died with the arms (repair, if ever
    needed, happens on the last migration-capable 6.x release)."""

    @pytest.mark.parametrize("argv", [
        ["backfill-source-uri"],
        ["backfill-source-uri", "--apply"],
        ["gc-pre-rdr096"],
        ["gc-pre-rdr096", "--apply"],
    ])
    def test_refuses_loud_without_touching_the_frozen_source(
        self, argv: list[str], t2_path: Path,
    ) -> None:
        before = t2_path.read_bytes() if t2_path.exists() else None
        result = CliRunner().invoke(aspects_group, argv)
        assert result.exit_code == 2, result.output
        assert "retired" in result.output
        assert "frozen migration source" in result.output
        assert "6.x" in result.output
        assert "Traceback" not in result.output
        after = t2_path.read_bytes() if t2_path.exists() else None
        assert before == after, "refusal must not write the frozen source"

    @pytest.mark.parametrize("verb", ["backfill-source-uri", "gc-pre-rdr096"])
    def test_verb_registered(self, verb: str) -> None:
        # The refusal IS the contract; the verb must stay registered so old
        # scripts fail with the explanation rather than a bare usage error.
        assert verb in aspects_group.commands


class TestRequeueFailed:
    """`nx aspects requeue-failed` bulk-recovers terminal-failed queue rows.

    Substrate (nexus-i711w): the queue is ``HttpAspectQueue`` against the
    hermetic engine substrate (fresh tenant per test) — the SQLite
    ``AspectExtractionQueue`` these tests used to seed died with the SQLite
    T2 stores. Status is observed through the queue's public read surfaces
    (``list_failed`` / ``list_pending``) instead of raw SQL.
    """

    @pytest.fixture
    def _queue_t2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Route the CLI's read (default_db_path) + write (t2_index_write) at
        a tmp-pathed T2Database. The queue is engine-side; deliberately NO
        ``touch()`` of the local file — the verb's old local-file existence
        gate silently no-opped requeue-failed on any box without the legacy
        memory.db (porter-b defect, fixed in this commit), and running
        against an absent file pins that fix."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        import nexus.mcp_infra as infra

        def _direct_index_write(write_fn):  # noqa: ANN001
            with T2Database(db_path) as db:
                return write_fn(db)

        monkeypatch.setattr(infra, "t2_index_write", _direct_index_write)
        return db_path

    def _seed_failed(self, db_path: Path) -> None:
        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            q = db.aspect_queue
            for coll, sp in [("knowledge__a", "x.pdf"), ("knowledge__a", "y.pdf"),
                             ("knowledge__b", "z.pdf")]:
                q.enqueue(coll, sp, content="c")
                # Claim first (pending -> in_progress) so mark_failed mirrors
                # the worker's real state transitions on the engine.
                claimed = q.claim_next()
                assert claimed is not None and claimed.source_path == sp
                q.mark_failed(coll, sp, "boom")
            q.enqueue("knowledge__a", "ok.pdf")  # stays pending

    def _status(self, db_path: Path, source_path: str) -> str:
        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            q = db.aspect_queue
            if any(r.source_path == source_path for r in q.list_failed()):
                return "failed"
            if any(r.source_path == source_path for r in q.list_pending()):
                return "pending"
            return "<absent>"

    def test_dry_run_reports_without_writing(self, _queue_t2: Path) -> None:
        self._seed_failed(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Would re-enqueue 3 failed row(s)" in res.output
        # Dry-run writes nothing: the rows stay failed.
        assert self._status(_queue_t2, "x.pdf") == "failed"

    def test_requeue_resets_failed_to_pending(self, _queue_t2: Path) -> None:
        self._seed_failed(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 3 failed row(s)" in res.output
        for sp in ("x.pdf", "y.pdf", "z.pdf"):
            assert self._status(_queue_t2, sp) == "pending"

    def test_collection_scope(self, _queue_t2: Path) -> None:
        self._seed_failed(_queue_t2)
        res = CliRunner().invoke(
            aspects_group, ["requeue-failed", "--collection", "knowledge__a"],
        )
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 2 failed row(s) in knowledge__a" in res.output
        assert self._status(_queue_t2, "x.pdf") == "pending"   # knowledge__a
        assert self._status(_queue_t2, "z.pdf") == "failed"    # knowledge__b untouched

    def test_limit_caps_requeue_count(self, _queue_t2: Path) -> None:
        self._seed_failed(_queue_t2)  # 3 failed rows
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "2"])
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 2 failed row(s)" in res.output
        # Exactly 2 of the 3 flip to pending (oldest-enqueued first); 1 stays failed.
        statuses = [self._status(_queue_t2, sp) for sp in ("x.pdf", "y.pdf", "z.pdf")]
        assert statuses.count("pending") == 2
        assert statuses.count("failed") == 1

    def test_limit_rejects_nonpositive(self, _queue_t2: Path) -> None:
        self._seed_failed(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "0"])
        assert res.exit_code == 1
        assert "--limit must be a positive integer" in res.output
        assert self._status(_queue_t2, "x.pdf") == "failed"  # nothing written

    def test_no_failed_rows_message(self, _queue_t2: Path) -> None:
        from nexus.db.t2 import T2Database
        with T2Database(_queue_t2) as db:
            db.aspect_queue.enqueue("knowledge__a", "only-pending.pdf")
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "no failed rows" in res.output
