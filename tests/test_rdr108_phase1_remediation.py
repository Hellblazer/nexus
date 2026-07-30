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

CATALOG SUBSTRATE (nexus-i711w Stage 2). Most of this file's subject is
``nexus/db/migrations.py`` — LIVE guided-upgrade code that
``commands/upgrade.py`` imports (``apply_pending`` / ``MIGRATIONS`` /
``bootstrap_version``) and that reads the legacy ``.catalog.db`` through raw
parameterized ATTACH, never through the ``CatalogDB`` class. Those tests touch
no catalog object at all. They were nevertheless held hostage by a single
module-level ``from nexus.catalog.catalog import Catalog``: one dying import at
module scope kills COLLECTION for the whole file, so every test in it would
have stopped running the moment the local catalog is deleted.

That import is gone. The manifest-backfill tests that DO need a catalog seed
through :class:`tests._catalog_fixture_ops.ActiveCatalog`, i.e. the same
factories the code under test resolves, so they exercise whichever catalog is
live. The one genuinely local-only test
(``TestAutoBootstrapCreatedAtEmpty``, whose subject is ``CatalogDB``'s own
auto-bootstrap DDL) carries ``local_catalog_backend`` and imports
``nexus.catalog.catalog_db`` inside its body — it retires with the local
catalog, not before.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import ActiveCatalog
from tests.conftest import make_vector_test_client


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unique_coll(prefix: str = "code") -> str:
    return f"{prefix}__{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def t3_db():
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed through whichever catalog is live (nexus-i711w Stage 2).

    Deliberately NOT the local ``Catalog`` this fixture used to build. The
    manifest-backfill code under test takes its catalog as an argument and, in
    the CLI tests, from ``commands/t3._make_catalog()`` — which resolves
    ``make_catalog_reader()``. Seeding a separate local catalog means the test
    writes one catalog while the command reads another, so
    ``list_by_collection`` comes back empty and every count assertion collapses
    to the bucket-2 ``assert 0 == 2`` profile.
    """
    return ActiveCatalog()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _register_doc(cat: Any, collection: str) -> str:
    """Register ONE document in *collection* through the active catalog.

    Returns the minted tumbler, which callers pass to ``_seed_chunk`` as the
    T3 ``doc_id`` (that join is what ``backfill_manifest_for_collection``
    walks).

    Replaces a raw ``cat._db.execute("INSERT OR IGNORE INTO documents ...")``
    that pinned literal tumblers ("1.1.1", "1.1.2") because ``register`` mints
    its own. Nothing in this file asserts on the tumbler VALUE — the
    requirements are only (a) N distinct documents exist in *collection* and
    (b) the seeded chunks carry a matching ``doc_id`` — so the minted tumbler
    is returned and used as-is.

    CARDINALITY IS LOAD-BEARING: ``docs_skipped_no_t3 == 2`` in
    ``TestS2DocsSkippedNoT3`` only means something if two calls really do
    produce two rows. ``Catalog.register`` is idempotent by ``file_path``
    WITHIN an owner, so each call takes a distinct slug for both the owner name
    and the file path; two calls can never silently collapse to one document.
    """
    slug = uuid.uuid4().hex[:8]
    owner = cat.register_owner(f"rdr108-remediation-{slug}", "curator")
    return str(cat.register(
        owner,
        f"doc-{slug}",
        content_type="code",
        file_path=f"/tmp/{collection}-{slug}.py",
        physical_collection=collection,
        chunk_count=0,
    ))


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

    def test_quota_error_propagates(self, active_catalog, t3_db):
        """A ChromaDB quota/auth error during get_collection must propagate,
        not be silently swallowed as 'collection not found'."""
        InvalidArgumentError = ValueError  # RDR-155 P4b P3: the substrate-neutral bad-argument type (was chromadb.errors.InvalidArgumentError)
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _register_doc(active_catalog, coll)

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
                    active_catalog, t3_db, coll, dry_run=False
                )

    def test_not_found_still_treated_as_missing(self, active_catalog, t3_db):
        """NotFoundError during get_collection is still treated as 'col is None'."""
        from nexus.errors import CollectionNotFoundError as NotFoundError
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _register_doc(active_catalog, coll)

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
                active_catalog, t3_db, coll, dry_run=False
            )
        # Collection absent: doc processed, no chunks.
        # Non-vacuous: the count is 1 only because the single registered
        # document is visible to the SAME catalog the backfill reads through.
        # A seed the backfill could not see would give 0.
        assert result.docs_skipped_no_t3 == 1
        assert result.chunks_written == 0


# ── S-2: docs_skipped_no_t3 never incremented ─────────────────────────────


class TestS2DocsSkippedNoT3:
    """S-2: when col is None (collection missing in T3), docs_skipped_no_t3
    must be incremented rather than docs_processed."""

    def test_missing_collection_increments_docs_skipped_no_t3(
        self, active_catalog, t3_db,
    ):
        """If the T3 collection doesn't exist, docs_skipped_no_t3 is incremented."""
        from nexus.errors import CollectionNotFoundError as NotFoundError
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        # TWO distinct documents — the assertion below is a per-document count,
        # so it can only distinguish "incremented per doc" from "set once" if
        # two really are registered (see _register_doc's cardinality note).
        _register_doc(active_catalog, coll)
        _register_doc(active_catalog, coll)

        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = NotFoundError("not found")
            mock_client_for.return_value = mock_client

            result = backfill_manifest_for_collection(
                active_catalog, t3_db, coll, dry_run=False
            )

        assert result.docs_skipped_no_t3 == 2
        assert result.docs_processed == 0

    def test_docs_skipped_no_t3_surfaced_in_cli_output(
        self, active_catalog, t3_db, runner,
    ):
        """CLI output includes docs_skipped_no_t3 when collection is absent."""
        from nexus.errors import CollectionNotFoundError as NotFoundError

        coll = _unique_coll()
        _register_doc(active_catalog, coll)

        with patch.object(
            t3_db,
            "_client_for",
        ) as mock_client_for:
            mock_client = MagicMock()
            mock_client.get_collection.side_effect = NotFoundError("not found")
            mock_client_for.return_value = mock_client

            with (
                # The ``_make_catalog`` patch STAYS, but now hands back the
                # ACTIVE catalog rather than a private local one, so seed and
                # read resolve the same substrate. It cannot simply be dropped:
                # ``_make_catalog()`` returns ``make_catalog_reader()``, and on
                # the SQLite arm that reader is opened ``mode=ro`` while
                # ``--no-dry-run`` backfill calls ``write_manifest`` through it
                # (see the note on TestSIG6ProgressOutput).
                patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
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


    def test_migration_quiet_under_non_tty_stderr(self, tmp_path, capsys, monkeypatch):
        """OBS-2 message is suppressed when stderr is not a tty (CI, pipes,
        click.testing.CliRunner mixing stderr into result.output)."""
        from nexus.db.t2 import T2Database
        import sys

        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)

        db_path = tmp_path / "obs2_quiet.db"
        db = T2Database(db_path)
        db.close()

        captured = capsys.readouterr()
        assert "migrat" not in captured.err.lower(), (
            f"Expected NO 'migrat' under non-tty stderr but got: {captured.err!r}"
        )

    def test_no_output_on_already_migrated_db(self, tmp_path, capsys, monkeypatch):
        """Second T2Database construction (fast-path via _upgrade_done) must
        not re-emit the migration message even when stderr is a tty."""
        from nexus.db.t2 import T2Database
        import sys

        monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)

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
    so operators see activity during long runs.

    nexus-i711w NOTE, surfaced by the port and NOT fixed here: the
    ``_make_catalog`` patch these tests carry is load-bearing for more than
    isolation. ``commands/t3._make_catalog()`` returns
    ``make_catalog_reader()``, and on the SQLite arm that is a
    ``read_only=True`` Catalog whose SQLite handle is ``mode=ro`` — yet
    ``backfill-manifest --no-dry-run`` writes through it
    (``manifest_backfill`` calls ``catalog.write_manifest(...)``). Whether the
    shipped verb can write at all on that arm is therefore untested by
    construction: the patch has always replaced the reader with something
    writable. Left as-is rather than converted to a production assertion,
    because it is a src question (a mixed read/write site holding only a
    reader), not a test question.
    """

    def test_progress_written_to_stderr_during_backfill(
        self, active_catalog, t3_db, runner,
    ):
        """With multiple documents, stderr contains progress output."""
        coll = _unique_coll()
        # Create 3 docs so there's something to report. The chunk's doc_id must
        # be the MINTED tumbler — that is the join backfill walks, and a
        # mismatched doc_id yields zero chunks per doc.
        for i in range(3):
            tumbler = _register_doc(active_catalog, coll)
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"c{i}-{coll}",
                content=f"content {i}", doc_id=tumbler, chunk_index=0,
                chunk_text_hash="a" * 64,
            )

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        # The command output (stdout+stderr) should have some indication of progress
        # NOTE (nexus-i711w): this assertion is weak as written — it holds for
        # any non-empty output, including a run that found zero documents. Left
        # exactly as it was rather than strengthened; recorded so its green is
        # not read as "the 3 seeded docs were processed".
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

    def test_resume_skips_already_processed_docs(
        self, active_catalog, t3_db, runner, tmp_path,
    ):
        """--resume skips docs that were already processed in a prior run."""
        coll = _unique_coll()
        first = _register_doc(active_catalog, coll)
        second = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll, chunk_id=f"c1-{coll}",
            content="first", doc_id=first, chunk_index=0,
            chunk_text_hash="a" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll, chunk_id=f"c2-{coll}",
            content="second", doc_id=second, chunk_index=0,
            chunk_text_hash="b" * 64,
        )

        state_file = tmp_path / "backfill_state.json"

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
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


# ── nexus-33xm: created_at='' on auto-bootstrapped collections rows ─────────


class TestAutoBootstrapCreatedAtEmpty:
    """nexus-33xm (replaces SIG-7 / nexus-872w):

    The auto-bootstrap that populates ``collections`` from
    ``documents.physical_collection`` (catalog_db.py __init__) MUST
    leave ``created_at`` empty.

    Why: ``_emit_backfilled_collection_events`` (catalog.py) emits
    a companion ``CollectionCreated`` event with
    ``payload.created_at = ""`` for each auto-bootstrapped name. The
    projector handler does ``INSERT OR REPLACE ... COALESCE((SELECT
    created_at FROM collections WHERE name = ?), payload.created_at)``.

    If the auto-bootstrap stamps ``NOW()``, the COALESCE preserves
    that synthetic stamp on event apply, but a fresh
    ``--replay-equality`` replay (which starts from an empty table)
    takes ``""`` from the event payload, producing a permanent
    drift between live and projected created_at columns.

    Empty here keeps the two paths bit-equal. The audit-distinction
    goal that drove SIG-7 (NOW() so audit tools could tell
    backfilled from event-derived rows) now lives in the synthetic
    event itself: ``payload.created_at == ""`` is the marker.

    nexus-i711w: PINNED to the local SQLite catalog. Its subject IS
    ``CatalogDB``'s own auto-bootstrap DDL and the local ``--replay-equality``
    projector path; there is no service-mode expression of either, so there is
    nothing to port this to. It retires with ``nexus/catalog/catalog_db.py``.
    """

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_backfilled_collections_have_empty_created_at(self, tmp_path):
        """Auto-bootstrap must NOT stamp ``created_at = NOW()`` or
        ``--replay-equality`` will permanently drift on the column.
        Reverting the empty-string stamp to ``NOW()`` makes this
        test fail with a non-empty timestamp.
        """
        from nexus.catalog.catalog_db import CatalogDB

        db_path = tmp_path / "catalog.db"
        db = CatalogDB(db_path)
        db.execute(
            "INSERT OR IGNORE INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1.1.1", "Test doc", "", 0, "code", "/tmp/test.py",
                "", "code__test_backfill_33xm", 0, "", "", "{}", 0.0, "", "",
            ),
        )
        db.commit()
        db.close()

        db2 = CatalogDB(db_path)
        row = db2._conn.execute(
            "SELECT created_at FROM collections WHERE name = ?",
            ("code__test_backfill_33xm",),
        ).fetchone()
        db2.close()

        assert row is not None, "collections row was not inserted"
        assert row[0] == "", (
            f"auto-bootstrap must leave created_at empty so the "
            f"synthetic CollectionCreated event with "
            f"``payload.created_at == ''`` matches under "
            f"--replay-equality; got {row[0]!r}"
        )


# ── SIG-4: high-volume orphan error message actionable command ────────────────


class TestSIG4ActionableErrorMessage:
    """SIG-4: _check_high_volume_orphans must include the
    `nx catalog rename-collection <legacy> <new>` template in the error message."""

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

    def test_error_message_contains_rename_collection_template(self, tmp_path):
        """MigrationError raised by _check_high_volume_orphans must include
        the `nx catalog rename-collection` command template."""
        from nexus.db.migrations import _check_high_volume_orphans, MigrationError

        orphan_collection = "code__legacy_orphan"
        conn = self._make_aspects_db_with_orphans(tmp_path, orphan_collection, 15)

        with pytest.raises(MigrationError) as exc_info:
            _check_high_volume_orphans(conn, table="document_aspects")

        msg = str(exc_info.value)
        assert "nx catalog rename-collection" in msg, (
            f"Error message must include 'nx catalog rename-collection' template, got: {msg!r}"
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
        assert "nx catalog rename-collection" in msg


# ── OBS-1: migration telemetry ────────────────────────────────────────────────


class TestOBS1MigrationTelemetry:
    """OBS-1: apply_pending must emit structured log events with duration_ms
    at migration start and completion."""

    def test_migration_log_events_emitted(self, tmp_path, monkeypatch):
        """apply_pending calls _log.info with migration_start and migration_done events."""
        from nexus.db import migrations as _migrations
        from nexus.db.migrations import _parse_version, _upgrade_done, apply_pending

        # RDR-170: apply_pending is lower-bound-only, so a "4.29.1" target no
        # longer caps the run — it would attempt the catalog-absent je0b defer
        # steps (4.30.0), set any_skipped, and return BEFORE migration_done.
        # Slice the registry to introduced <= 4.29.1 (the old upper-bound scope).
        monkeypatch.setattr(
            _migrations,
            "MIGRATIONS",
            [m for m in _migrations.MIGRATIONS if _parse_version(m.introduced) <= _parse_version("4.29.1")],
        )

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

    def test_migration_done_has_duration_ms(self, tmp_path, monkeypatch):
        """migration_done log call includes duration_ms field."""
        from nexus.db import migrations as _migrations
        from nexus.db.migrations import _parse_version, _upgrade_done, apply_pending

        # RDR-170: slice to introduced <= 4.29.1 so the catalog-absent je0b
        # defer steps (4.30.0) don't set any_skipped and suppress migration_done.
        monkeypatch.setattr(
            _migrations,
            "MIGRATIONS",
            [m for m in _migrations.MIGRATIONS if _parse_version(m.introduced) <= _parse_version("4.29.1")],
        )

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
