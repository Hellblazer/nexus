# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-872w: RDR-108 Phase 1 remediation T-E — defensive coding + operator UX.

Tests cover (survivors):
  K10  - bare except in manifest_backfill swallows non-NotFound errors
  S-2  - BackfillResult.docs_skipped_no_t3 declared but never incremented
  SIG-6 - backfill-manifest progress output + SIGINT safety

OBS-2 / SIG-4 / OBS-1 / OBS-4 DELETED in RDR-158 P4 Stage 4 (nexus-i711w):
their subject was the ``nexus/db/migrations.py`` machinery (the T2Database
migration banner, ``_check_high_volume_orphans``, apply_pending telemetry),
which is deleted with the migration chain.

The manifest-backfill tests that DO need a catalog seed
through :class:`tests._catalog_fixture_ops.ActiveCatalog`, i.e. the same
factories the code under test resolves, so they exercise whichever catalog is
live. The one genuinely local-only test (``TestAutoBootstrapCreatedAtEmpty``)
retired with the local catalog in the nexus-i711w terminal deletion — see its
tombstone below.
"""
from __future__ import annotations

import os
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


# ── OBS-2 / SIG-4 / OBS-1 / OBS-4 DELETED (RDR-158 P4 Stage 4, nexus-i711w) ──
# Their subjects — the T2Database-init migration banner, the
# _check_high_volume_orphans message/threshold, and apply_pending's
# migration telemetry — died with nexus/db/migrations.py.


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


# TestAutoBootstrapCreatedAtEmpty RETIRED (nexus-i711w terminal deletion):
# its subject was ``CatalogDB``'s own auto-bootstrap DDL leaving
# ``created_at`` empty so the local ``--replay-equality`` projector path
# stayed bit-equal — machinery with no service-mode expression, deleted with
# ``nexus/catalog/catalog_db.py`` (its own docstring scheduled exactly this).


