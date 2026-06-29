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
    from nexus.db.migrations import (
        migrate_document_aspects_source_uri,
        migrate_document_aspects_table,
    )
    migrate_document_aspects_table(conn)
    migrate_document_aspects_source_uri(conn)


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


class TestBackfillSourceUriDryRun:
    def test_no_writes_without_apply(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(conn, collection="rdr__nexus", source_path="docs/x.md")
        conn.close()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["backfill-source-uri"])
        assert result.exit_code == 0, result.output
        assert "would backfill 1 row" in result.output
        assert "Re-run with --apply" in result.output

        conn = sqlite3.connect(str(t2_path))
        uri = conn.execute("SELECT source_uri FROM document_aspects").fetchone()[0]
        conn.close()
        assert uri is None


class TestBackfillSourceUriApply:
    def test_filesystem_collections_use_file_scheme(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(conn, collection="rdr__nexus", source_path="docs/x.md")
        _insert_aspect(conn, collection="docs__corpus", source_path="docs/y.md")
        _insert_aspect(conn, collection="code__nexus", source_path="src/cli.py")
        conn.close()

        runner = CliRunner()
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output
        assert "backfilled 3 row" in result.output

        conn = sqlite3.connect(str(t2_path))
        rows = dict(conn.execute(
            "SELECT collection, source_uri FROM document_aspects",
        ).fetchall())
        conn.close()
        for coll, sp in [
            ("rdr__nexus", "docs/x.md"),
            ("docs__corpus", "docs/y.md"),
            ("code__nexus", "src/cli.py"),
        ]:
            assert rows[coll] == "file://" + os.path.abspath(sp)

    def test_knowledge_collections_use_chroma_scheme(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(
            conn, collection="knowledge__delos",
            source_path="papers/aleph.pdf",
        )
        conn.close()

        runner = CliRunner()
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(t2_path))
        uri = conn.execute(
            "SELECT source_uri FROM document_aspects",
        ).fetchone()[0]
        conn.close()
        assert uri == "chroma://knowledge__delos/papers/aleph.pdf"

    def test_empty_string_rows_also_backfilled(self, t2_path: Path) -> None:
        """Original 4.26.2 migration's contract: both NULL and ''
        rows must be backfilled when source_path is populated."""
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(
            conn, collection="rdr__a", source_path="x.md",
            source_uri="",  # explicit empty
        )
        conn.close()

        runner = CliRunner()
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(t2_path))
        uri = conn.execute(
            "SELECT source_uri FROM document_aspects",
        ).fetchone()[0]
        conn.close()
        assert uri and uri.endswith("x.md")

    def test_does_not_overwrite_populated_source_uri(self, t2_path: Path) -> None:
        explicit_uri = "chroma://knowledge__custom/some-source"
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(
            conn, collection="knowledge__custom", source_path="diff-path",
            source_uri=explicit_uri,
        )
        conn.close()

        runner = CliRunner()
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(t2_path))
        uri = conn.execute(
            "SELECT source_uri FROM document_aspects",
        ).fetchone()[0]
        conn.close()
        assert uri == explicit_uri

    def test_idempotent_re_apply(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _insert_aspect(conn, collection="rdr__a", source_path="x.md")
        conn.close()

        runner = CliRunner()
        runner.invoke(aspects_group, ["backfill-source-uri", "--apply"])
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output
        # Second --apply has nothing to do.
        assert "backfilled 0 row" in result.output

    def test_empty_source_path_skipped_for_triage(self, t2_path: Path) -> None:
        """Rows with both empty source_uri and empty source_path
        cannot be backfilled; the verb skips them and reports the
        count separately (research-2 mitigation)."""
        conn = sqlite3.connect(str(t2_path))
        # The schema's PK forbids empty source_path on insert, so seed
        # via direct INSERT bypassing the helper, then flip.
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rdr__edge", "placeholder",
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             "scholarly-paper-v1"),
        )
        conn.execute(
            "UPDATE document_aspects SET source_path = '' WHERE collection = 'rdr__edge'",
        )
        conn.commit()
        conn.close()

        runner = CliRunner()
        result = runner.invoke(
            aspects_group, ["backfill-source-uri", "--apply"]
        )
        assert result.exit_code == 0, result.output
        assert "1 row(s) have empty source_path" in result.output


class TestBackfillSourceUriEdgeCases:
    def test_missing_db_clean_noop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        mem_path = tmp_path / "nope.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["backfill-source-uri", "--apply"])
        assert result.exit_code == 0, result.output
        assert "nothing to do" in result.output

    def test_verb_registered(self) -> None:
        assert "backfill-source-uri" in aspects_group.commands


# ── gc-pre-rdr096 verb ────────────────────────────────────────────────────────


def _seed_three_categories(conn: sqlite3.Connection) -> None:
    """Plant 6 rows mirroring the four production categories from
    RDR-096 research-3 (id 1010)."""
    # Category 1: read-failure nulls (3 variants) — should be dropped.
    _insert_aspect(
        conn, collection="rdr__nexus", source_path="docs/missing-1.md",
        extractor="rdr-frontmatter-v1",
        experimental_datasets="[]", experimental_baselines="[]",
        extras="{}", confidence=None,
    )
    _insert_aspect(
        conn, collection="knowledge__hybridrag", source_path="ghost-paper",
        experimental_datasets="[]", experimental_baselines="[]",
        extras="{}", confidence=None,
    )
    # Legacy-ghost variant: extras IS NULL rather than '{}'.
    _insert_aspect(
        conn, collection="rdr__nexus", source_path="docs/legacy-ghost.md",
        extractor="rdr-frontmatter-v1",
        experimental_datasets="[]", experimental_baselines="[]",
        extras=None, confidence=None,
    )
    # Category 2: structured-zero success — must be retained.
    _insert_aspect(
        conn, collection="rdr__nexus", source_path="docs/readme.md",
        extractor="rdr-frontmatter-v1",
        experimental_datasets="[]", experimental_baselines="[]",
        extras="{}", confidence=1.0,
    )
    # Category 3: partial — must be retained.
    _insert_aspect(
        conn, collection="knowledge__delos", source_path="aleph.pdf",
        problem_formulation="atomic broadcast",
        experimental_datasets="[]", experimental_baselines="[]",
        extras="{}", confidence=None,
    )
    # Category 4: full — must be retained.
    _insert_aspect(
        conn, collection="knowledge__delos", source_path="lightweight-smr.pdf",
        problem_formulation="state machine replication",
        proposed_method="median rule",
        experimental_datasets='["TPC-C"]',
        experimental_baselines='["raft"]',
        experimental_results="30% improvement",
        extras='{"venue":"OSDI"}', confidence=0.9,
    )


class TestGcPreRdr096:
    def test_dry_run_reports_count(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _seed_three_categories(conn)
        conn.close()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-pre-rdr096"])
        assert result.exit_code == 0, result.output
        assert "would delete 3" in result.output
        # No rows actually deleted.
        conn = sqlite3.connect(str(t2_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM document_aspects",
        ).fetchone()[0]
        conn.close()
        assert n == 6

    def test_apply_drops_only_read_failure_nulls(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _seed_three_categories(conn)
        conn.close()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-pre-rdr096", "--apply"])
        assert result.exit_code == 0, result.output
        assert "deleted 3" in result.output

        conn = sqlite3.connect(str(t2_path))
        kept = conn.execute(
            "SELECT collection, source_path FROM document_aspects "
            "ORDER BY collection, source_path",
        ).fetchall()
        conn.close()
        # Structured-zero + partial + full retained; all 3 read-failure
        # variants (including the extras-IS-NULL legacy ghost) dropped.
        assert kept == [
            ("knowledge__delos", "aleph.pdf"),
            ("knowledge__delos", "lightweight-smr.pdf"),
            ("rdr__nexus", "docs/readme.md"),
        ]

    def test_idempotent_on_re_apply(self, t2_path: Path) -> None:
        conn = sqlite3.connect(str(t2_path))
        _seed_three_categories(conn)
        conn.close()

        runner = CliRunner()
        runner.invoke(aspects_group, ["gc-pre-rdr096", "--apply"])
        result = runner.invoke(aspects_group, ["gc-pre-rdr096", "--apply"])
        assert result.exit_code == 0, result.output
        # Second run: 0 matching rows.
        assert "0 pre-RDR-096" in result.output

    def test_empty_db_clean_noop(self, t2_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-pre-rdr096", "--apply"])
        assert result.exit_code == 0, result.output
        assert "0 pre-RDR-096" in result.output

    def test_missing_db_clean_noop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        mem_path = tmp_path / "nope.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-pre-rdr096", "--apply"])
        assert result.exit_code == 0, result.output
        assert "nothing to do" in result.output

    def test_verb_registered(self) -> None:
        assert "gc-pre-rdr096" in aspects_group.commands


# ── nx aspects requeue-failed (nexus-2c51v) ──────────────────────────────────


class TestRequeueFailed:
    """`nx aspects requeue-failed` bulk-recovers terminal-failed queue rows."""

    @pytest.fixture
    def _sqlite_t2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Pin the aspect queue to SQLite on a tmp DB and route the CLI's read
        (default_db_path) + daemon write (t2_index_write) at it."""
        from nexus.db.t2 import T2Database

        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
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
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        store = AspectExtractionQueue(db_path)
        for coll, sp in [("knowledge__a", "x.pdf"), ("knowledge__a", "y.pdf"),
                         ("knowledge__b", "z.pdf")]:
            store.enqueue(coll, sp, content="c")
            store.mark_failed(coll, sp, "boom")
        store.enqueue("knowledge__a", "ok.pdf")  # stays pending
        store.close()

    def _status(self, db_path: Path, source_path: str) -> str:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        store = AspectExtractionQueue(db_path)
        try:
            row = store.conn.execute(
                "SELECT status FROM aspect_extraction_queue WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            return row[0] if row else "<absent>"
        finally:
            store.close()

    def test_dry_run_reports_without_writing(self, _sqlite_t2: Path) -> None:
        self._seed_failed(_sqlite_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Would re-enqueue 3 failed row(s)" in res.output
        # Dry-run writes nothing: the rows stay failed.
        assert self._status(_sqlite_t2, "x.pdf") == "failed"

    def test_requeue_resets_failed_to_pending(self, _sqlite_t2: Path) -> None:
        self._seed_failed(_sqlite_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 3 failed row(s)" in res.output
        for sp in ("x.pdf", "y.pdf", "z.pdf"):
            assert self._status(_sqlite_t2, sp) == "pending"

    def test_collection_scope(self, _sqlite_t2: Path) -> None:
        self._seed_failed(_sqlite_t2)
        res = CliRunner().invoke(
            aspects_group, ["requeue-failed", "--collection", "knowledge__a"],
        )
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 2 failed row(s) in knowledge__a" in res.output
        assert self._status(_sqlite_t2, "x.pdf") == "pending"   # knowledge__a
        assert self._status(_sqlite_t2, "z.pdf") == "failed"    # knowledge__b untouched

    def test_limit_caps_requeue_count(self, _sqlite_t2: Path) -> None:
        self._seed_failed(_sqlite_t2)  # 3 failed rows
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "2"])
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 2 failed row(s)" in res.output
        # Exactly 2 of the 3 flip to pending (oldest-enqueued first); 1 stays failed.
        statuses = [self._status(_sqlite_t2, sp) for sp in ("x.pdf", "y.pdf", "z.pdf")]
        assert statuses.count("pending") == 2
        assert statuses.count("failed") == 1

    def test_limit_rejects_nonpositive(self, _sqlite_t2: Path) -> None:
        self._seed_failed(_sqlite_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "0"])
        assert res.exit_code == 1
        assert "--limit must be a positive integer" in res.output
        assert self._status(_sqlite_t2, "x.pdf") == "failed"  # nothing written

    def test_no_failed_rows_message(self, _sqlite_t2: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        store = AspectExtractionQueue(_sqlite_t2)
        store.enqueue("knowledge__a", "only-pending.pdf")
        store.close()
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "no failed rows" in res.output
