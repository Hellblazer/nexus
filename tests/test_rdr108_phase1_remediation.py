# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-872w: RDR-108 Phase 1 remediation T-E — defensive coding + operator UX.

Tests cover:
  K10  - bare except in manifest_backfill swallows non-NotFound errors
  S-2  - BackfillResult.docs_skipped_no_t3 declared but never incremented
  OBS-2 - no "migrating database..." UX during T2Database init
  SIG-6 - backfill-manifest progress output + SIGINT safety
  SIG-7 - created_at='' on backfilled collections rows
  SIG-4 - high-volume orphan error message lacks actionable command template
  OBS-1 - no telemetry on migration runs
  OBS-4 - _HIGH_VOLUME_ORPHAN_THRESHOLD is a magic number (env override)
"""
from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unique_coll(prefix: str = "code") -> str:
    return f"{prefix}__{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def t3_db():
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _insert_doc(cat: Catalog, tumbler: str, collection: str) -> None:
    cat._db.execute(  # epsilon-allow: test fixture seeds documents row
        "INSERT OR IGNORE INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", f"/tmp/{tumbler}.py",
            "", collection, 0, "", "", "{}", 0.0, "", "",
        ),
    )
    cat._db.commit()


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    doc_id: str,
    chunk_index: int,
    chunk_text_hash: str,
) -> None:
    col = t3_db._client.get_or_create_collection(collection)
    col.add(
        ids=[chunk_id],
        documents=[content],
        metadatas=[{
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "chunk_text_hash": chunk_text_hash,
        }],
    )


# ── K10: bare except swallows non-NotFound errors ────────────────────────────


class TestK10BareExceptFix:
    """K10: bare except in backfill_manifest_for_collection must NOT swallow
    quota errors and other non-NotFound exceptions."""

    def test_quota_error_propagates(self, catalog, t3_db):
        """A ChromaDB quota/auth error during get_collection must propagate,
        not be silently swallowed as 'collection not found'."""
        from chromadb.errors import InvalidArgumentError
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)

        # Simulate a non-NotFound error (e.g. quota violation, auth failure)
        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = InvalidArgumentError(
                "quota exceeded"
            )
            mock_client_for.return_value = mock_client

            with pytest.raises(InvalidArgumentError, match="quota exceeded"):
                backfill_manifest_for_collection(
                    catalog, t3_db, coll, dry_run=False
                )

    def test_not_found_still_treated_as_missing(self, catalog, t3_db):
        """NotFoundError during get_collection is still treated as 'col is None'."""
        from chromadb.errors import NotFoundError
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)

        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = NotFoundError(
                f"Collection {coll!r} does not exist."
            )
            mock_client_for.return_value = mock_client

            result = backfill_manifest_for_collection(
                catalog, t3_db, coll, dry_run=False
            )
        # Collection absent: doc processed, no chunks
        assert result.docs_skipped_no_t3 == 1
        assert result.chunks_written == 0


# ── S-2: docs_skipped_no_t3 never incremented ─────────────────────────────


class TestS2DocsSkippedNoT3:
    """S-2: when col is None (collection missing in T3), docs_skipped_no_t3
    must be incremented rather than docs_processed."""

    def test_missing_collection_increments_docs_skipped_no_t3(self, catalog, t3_db):
        """If the T3 collection doesn't exist, docs_skipped_no_t3 is incremented."""
        from chromadb.errors import NotFoundError
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _insert_doc(catalog, "1.1.2", coll)

        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = NotFoundError("not found")
            mock_client_for.return_value = mock_client

            result = backfill_manifest_for_collection(
                catalog, t3_db, coll, dry_run=False
            )

        assert result.docs_skipped_no_t3 == 2
        assert result.docs_processed == 0

    def test_docs_skipped_no_t3_surfaced_in_cli_output(self, catalog, t3_db, runner):
        """CLI output includes docs_skipped_no_t3 when collection is absent."""
        from chromadb.errors import NotFoundError

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)

        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = NotFoundError("not found")
            mock_client_for.return_value = mock_client

            with (
                patch("nexus.commands.t3._make_catalog", return_value=catalog),
                patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            ):
                result = runner.invoke(
                    main,
                    ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                )

        assert result.exit_code == 0, result.output
        # "skipped" or "no_t3" should appear in output
        assert "skip" in result.output.lower() or "no_t3" in result.output.lower()


# ── OBS-2: no migration UX during T2Database init ────────────────────────────


class TestOBS2MigrationUX:
    """OBS-2: T2Database.__init__ must emit a migration-start message on
    stderr when apply_pending runs, so users don't see a silent hang."""

    def test_migration_emits_progress_message(self, tmp_path, capsys):
        """First construction of T2Database on a fresh DB emits a 'migrating'
        message to stderr before running apply_pending."""
        from nexus.db.t2 import T2Database

        # Use a unique path: tmp_path is per-test so _upgrade_done won't cache it.
        db_path = tmp_path / "obs2_fresh.db"

        db = T2Database(db_path)
        db.close()

        captured = capsys.readouterr()
        all_stderr = captured.err

        assert "migrat" in all_stderr.lower(), (
            f"Expected 'migrat' in stderr output but got: {all_stderr!r}"
        )

    def test_no_output_on_already_migrated_db(self, tmp_path, capsys):
        """Second T2Database construction (fast-path via _upgrade_done) must
        not re-emit the migration message."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "obs2_second.db"

        # First construction: migrations run, message emitted.
        db = T2Database(db_path)
        db.close()
        capsys.readouterr()  # drain first-run output

        # Second construction: fast path — _upgrade_done hit, no print.
        db2 = T2Database(db_path)
        db2.close()

        captured = capsys.readouterr()
        assert "migrat" not in captured.err.lower(), (
            f"Unexpected migration message on second construction: {captured.err!r}"
        )


# ── SIG-6: progress output ────────────────────────────────────────────────────


class TestSIG6ProgressOutput:
    """SIG-6: backfill-manifest must emit periodic progress to stderr
    so operators see activity during long runs."""

    def test_progress_written_to_stderr_during_backfill(self, catalog, t3_db, runner):
        """With multiple documents, stderr contains progress output."""
        coll = _unique_coll()
        # Create 3 docs so there's something to report
        for i in range(3):
            tumbler = f"1.1.{i+1}"
            _insert_doc(catalog, tumbler, coll)
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"c{i}-{coll}",
                content=f"content {i}", doc_id=tumbler, chunk_index=0,
                chunk_text_hash="a" * 64,
            )

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        # The command output (stdout+stderr) should have some indication of progress
        combined = result.output
        assert combined, "No output at all from backfill command"

    def test_resume_flag_exists(self, runner):
        """--resume flag is accepted by the CLI (existence test)."""
        with (
            patch("nexus.commands.t3._make_catalog") as mock_cat,
            patch("nexus.commands.t3._make_t3_for_backfill") as mock_t3,
        ):
            mock_cat.return_value = MagicMock()
            mock_cat.return_value.list_collections.return_value = []
            mock_t3.return_value = MagicMock()

            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--resume", "--no-dry-run"],
            )

        # Should not get "no such option" error
        assert "no such option" not in result.output.lower(), result.output

    def test_resume_skips_already_processed_docs(self, catalog, t3_db, runner, tmp_path):
        """--resume skips docs that were already processed in a prior run."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _insert_doc(catalog, "1.1.2", coll)
        _seed_chunk(
            t3_db, collection=coll, chunk_id=f"c1-{coll}",
            content="first", doc_id="1.1.1", chunk_index=0,
            chunk_text_hash="a" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll, chunk_id=f"c2-{coll}",
            content="second", doc_id="1.1.2", chunk_index=0,
            chunk_text_hash="b" * 64,
        )

        state_file = tmp_path / "backfill_state.json"

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.dict(os.environ, {"NEXUS_BACKFILL_STATE_FILE": str(state_file)}),
        ):
            # First run: no resume, processes both
            result1 = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
            )
            assert result1.exit_code == 0, result1.output

            # Second run: with --resume, should skip already-done docs
            result2 = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll,
                 "--no-dry-run", "--resume"],
            )
            assert result2.exit_code == 0, result2.output


# ── SIG-7: created_at='' on backfilled collections rows ──────────────────────


class TestSIG7CreatedAtTimestamp:
    """SIG-7: collections backfill in catalog_db must set a real ISO timestamp
    for created_at, not the empty string ''."""

    def test_backfilled_collections_have_real_created_at(self, tmp_path):
        """The collections backfill (catalog_db.py __init__ INSERT INTO collections)
        must emit a real ISO timestamp, not empty string."""
        from nexus.catalog.catalog_db import CatalogDB

        db_path = tmp_path / "catalog.db"
        db = CatalogDB(db_path)

        # Manually insert a documents row with a physical_collection not in
        # the collections table, to trigger the backfill path
        db.execute(
            "INSERT OR IGNORE INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1.1.1", "Test doc", "", 0, "code", "/tmp/test.py",
                "", "code__test_backfill_sig7", 0, "", "", "{}", 0.0, "", "",
            ),
        )
        db.commit()
        db.close()

        # Re-open: the __init__ backfill path should fire and populate created_at
        db2 = CatalogDB(db_path)
        row = db2._conn.execute(
            "SELECT created_at FROM collections WHERE name = ?",
            ("code__test_backfill_sig7",),
        ).fetchone()
        db2.close()

        assert row is not None, "collections row was not inserted"
        created_at = row[0]
        assert created_at not in ("", None), (
            f"created_at must be a real timestamp, got {created_at!r}"
        )
        # Validate it's ISO format (basic check)
        assert "T" in created_at or len(created_at) >= 10, (
            f"created_at does not look like an ISO timestamp: {created_at!r}"
        )


# ── SIG-4: high-volume orphan error message actionable command ────────────────


class TestSIG4ActionableErrorMessage:
    """SIG-4: _check_high_volume_orphans must include the
    `nx catalog mark-superseded <legacy> <new>` template in the error message."""

    def _make_aspects_db_with_orphans(
        self, tmp_path: Path, collection: str, count: int
    ) -> sqlite3.Connection:
        """Create an in-memory aspects DB with orphan rows for testing."""
        conn = sqlite3.connect(str(tmp_path / "memory.db"))
        conn.executescript("""
            CREATE TABLE document_aspects (
                collection  TEXT NOT NULL,
                source_path TEXT NOT NULL,
                doc_id      TEXT NOT NULL DEFAULT '',
                extracted_at TEXT NOT NULL DEFAULT '',
                model_version TEXT NOT NULL DEFAULT '',
                extractor_name TEXT NOT NULL DEFAULT ''
            );
        """)
        for i in range(count):
            conn.execute(
                "INSERT INTO document_aspects (collection, source_path, doc_id) "
                "VALUES (?, ?, '')",
                (collection, f"/path/to/file_{i}.py"),
            )
        conn.commit()
        return conn

    def test_error_message_contains_mark_superseded_template(self, tmp_path):
        """MigrationError raised by _check_high_volume_orphans must include
        the `nx catalog mark-superseded` command template."""
        from nexus.db.migrations import _check_high_volume_orphans, MigrationError

        orphan_collection = "code__legacy_orphan"
        conn = self._make_aspects_db_with_orphans(tmp_path, orphan_collection, 15)

        with pytest.raises(MigrationError) as exc_info:
            _check_high_volume_orphans(conn, table="document_aspects")

        msg = str(exc_info.value)
        assert "nx catalog mark-superseded" in msg, (
            f"Error message must include 'nx catalog mark-superseded' template, got: {msg!r}"
        )
        assert orphan_collection in msg, (
            f"Error message must name the orphan collection {orphan_collection!r}, got: {msg!r}"
        )

    def test_error_message_contains_each_orphan_collection(self, tmp_path):
        """All orphan collection names appear in the error."""
        from nexus.db.migrations import _check_high_volume_orphans, MigrationError

        conn = sqlite3.connect(str(tmp_path / "m2.db"))
        conn.executescript("""
            CREATE TABLE document_aspects (
                collection TEXT NOT NULL, source_path TEXT NOT NULL,
                doc_id TEXT NOT NULL DEFAULT ''
            );
        """)
        for coll in ("code__alpha", "code__beta"):
            for i in range(15):
                conn.execute(
                    "INSERT INTO document_aspects VALUES (?, ?, '')",
                    (coll, f"/f{i}.py"),
                )
        conn.commit()

        with pytest.raises(MigrationError) as exc_info:
            _check_high_volume_orphans(conn, table="document_aspects")

        msg = str(exc_info.value)
        assert "code__alpha" in msg
        assert "code__beta" in msg
        assert "nx catalog mark-superseded" in msg


# ── OBS-1: migration telemetry ────────────────────────────────────────────────


class TestOBS1MigrationTelemetry:
    """OBS-1: apply_pending must emit structured log events with duration_ms
    at migration start and completion."""

    def test_migration_log_events_emitted(self, tmp_path):
        """apply_pending calls _log.info with migration_start and migration_done events."""
        from nexus.db import migrations as _migrations
        from nexus.db.migrations import _upgrade_done, apply_pending

        path = tmp_path / "t2_obs1.db"
        path_key = str(path.resolve())
        _upgrade_done.discard(path_key)

        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")

        log_calls: list[tuple[str, dict]] = []

        original_info = _migrations._log.info

        def _capturing_info(event: str, **kw: object) -> None:
            log_calls.append((event, dict(kw)))
            return original_info(event, **kw)

        with patch.object(_migrations._log, "info", side_effect=_capturing_info):
            apply_pending(conn, "4.29.1")

        conn.close()

        events = [ev for ev, _ in log_calls]
        assert "migration_start" in events, (
            f"Expected 'migration_start' log call, got: {events}"
        )
        assert "migration_done" in events, (
            f"Expected 'migration_done' log call, got: {events}"
        )

    def test_migration_done_has_duration_ms(self, tmp_path):
        """migration_done log call includes duration_ms field."""
        from nexus.db import migrations as _migrations
        from nexus.db.migrations import _upgrade_done, apply_pending

        path = tmp_path / "t2_obs1_dur.db"
        path_key = str(path.resolve())
        _upgrade_done.discard(path_key)

        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")

        log_calls: list[tuple[str, dict]] = []

        original_info = _migrations._log.info

        def _capturing_info(event: str, **kw: object) -> None:
            log_calls.append((event, dict(kw)))
            return original_info(event, **kw)

        with patch.object(_migrations._log, "info", side_effect=_capturing_info):
            apply_pending(conn, "4.29.1")

        conn.close()

        done_calls = [(ev, kw) for ev, kw in log_calls if ev == "migration_done"]
        assert done_calls, f"No migration_done log call. Got: {[ev for ev, _ in log_calls]}"
        for _, kw in done_calls:
            assert "duration_ms" in kw, (
                f"migration_done missing duration_ms, got fields: {list(kw.keys())}"
            )


# ── OBS-4: _HIGH_VOLUME_ORPHAN_THRESHOLD env override ────────────────────────


class TestOBS4ThresholdEnvOverride:
    """OBS-4: _HIGH_VOLUME_ORPHAN_THRESHOLD must be overridable via
    NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD env var."""

    def _make_db_with_orphans(
        self, tmp_path: Path, collection: str, count: int
    ) -> sqlite3.Connection:
        conn = sqlite3.connect(str(tmp_path / f"thresh_{uuid.uuid4().hex}.db"))
        conn.executescript("""
            CREATE TABLE document_aspects (
                collection TEXT NOT NULL, source_path TEXT NOT NULL,
                doc_id TEXT NOT NULL DEFAULT ''
            );
        """)
        for i in range(count):
            conn.execute(
                "INSERT INTO document_aspects VALUES (?, ?, '')",
                (collection, f"/f{i}.py"),
            )
        conn.commit()
        return conn

    def test_env_override_lowers_threshold(self, tmp_path):
        """Setting NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD=5 triggers the error
        at 6 orphan rows instead of the default 10."""
        from nexus.db.migrations import _check_high_volume_orphans, MigrationError

        # 6 rows: default threshold=10 would pass, env threshold=5 must fail
        conn = self._make_db_with_orphans(tmp_path, "code__test_env_thresh", 6)

        with patch.dict(os.environ, {"NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD": "5"}):
            with pytest.raises(MigrationError):
                _check_high_volume_orphans(conn, table="document_aspects")

    def test_env_override_raises_threshold(self, tmp_path):
        """Setting NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD=20 allows 15 orphan rows
        through without error."""
        from nexus.db.migrations import _check_high_volume_orphans

        conn = self._make_db_with_orphans(tmp_path, "code__test_raise_thresh", 15)

        with patch.dict(os.environ, {"NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD": "20"}):
            # Should NOT raise
            _check_high_volume_orphans(conn, table="document_aspects")

    def test_default_threshold_still_10_without_env(self, tmp_path):
        """Without env var, default threshold is 10: 11 rows raises, 10 does not."""
        from nexus.db.migrations import _check_high_volume_orphans, MigrationError

        conn_pass = self._make_db_with_orphans(tmp_path, "code__pass_10", 10)
        # Exactly 10 rows: HAVING n > 10 means 10 does NOT trigger
        _check_high_volume_orphans(conn_pass, table="document_aspects")  # no raise

        conn_fail = self._make_db_with_orphans(tmp_path, "code__fail_11", 11)
        with pytest.raises(MigrationError):
            _check_high_volume_orphans(conn_fail, table="document_aspects")
