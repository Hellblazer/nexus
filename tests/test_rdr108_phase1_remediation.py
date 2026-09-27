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

import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import ActiveCatalog
from tests.conftest import make_vector_test_client


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unique_coll(prefix: str = "code") -> str:
    # nexus-dbzxb (RDR-191 Phase 5 Python collateral): four-segment
    # conformant (RDR-103) — was f"{prefix}__{uuid...[:12]}" (two segments).
    # ``_seed_chunk`` now ALSO writes a real T3 chunk (via
    # ``seed_manifest_chunks``) to satisfy ``fk_catalog_chunks_chunk``, and
    # the real ``/v1/vectors/upsert-chunks`` endpoint refuses a
    # non-conformant collection name.
    return f"{prefix}__mtest-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"


@pytest.fixture(autouse=True)
def _isolate_backfill_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """nexus-pfuns: every ``t3 backfill-manifest --no-dry-run`` invocation
    in this module reads/writes the resumable state file via
    ``nexus.commands.t3._backfill_state_path()``. Before this fixture, only
    5 of 19 invocations wrapped ``NEXUS_BACKFILL_STATE_FILE`` in a
    ``patch.dict`` context manager (the other 14 predated it and forgot
    the override) -- silently overwriting Sam's real
    ``~/.config/nexus/backfill_state.json`` with mtest- tenant state on
    every full-suite run (T2 nexus/gc-purge-marker-xdist-leak-2026-08-20).
    ``_backfill_state_path()`` was ALSO independently fixed to fall back to
    ``nexus_config_dir()`` (which the suite-wide autouse
    ``_isolate_config_dir`` in tests/conftest.py already redirects for
    every test) rather than a hardcoded real path -- this fixture is a
    second, explicit line of defense specific to this module's own state
    file, not a substitute for that production fix. The 5 existing
    ``patch.dict`` blocks compute the identical ``tmp_path /
    "backfill_state.json"`` path and simply re-set it to the same value
    for their `with` block's duration; harmless, left as-is."""
    state_file = tmp_path / "backfill_state.json"
    monkeypatch.setenv("NEXUS_BACKFILL_STATE_FILE", str(state_file))
    return state_file


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
    content: str,
    doc_id: str,
    chunk_index: int,
    chunk_text_hash: str,
    chunk_id: str | None = None,
    seed_fk: bool = True,
) -> None:
    """Seed one T3 chunk.

    nexus-dmf7r: the T3 chunk's own id IS the chash by construction
    (RDR-108/RDR-180) -- ``chunk_id`` defaults to ``chunk_text_hash`` so
    every ordinary call site matches that production invariant. Pass
    ``chunk_id=`` explicitly ONLY to construct a deliberately DIVERGENT
    fixture (the nexus-dmf7r regression class backfill must now detect
    and skip).

    nexus-r7g3i: ``seed_fk=False`` skips the real-engine FK bookkeeping
    insert below, constructing a chunk whose chash has NO matching
    ``nexus.chunks`` row -- a real ``write_manifest`` call for it
    genuinely 409s against ``fk_catalog_chunks_chunk``. Default True
    (the FK-safe shape every other test in this file wants).
    """
    col = t3_db._client.get_or_create_collection(collection)
    col.add(
        ids=[chunk_id if chunk_id is not None else chunk_text_hash],
        documents=[content],
        metadatas=[{
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "chunk_text_hash": chunk_text_hash,
        }],
    )
    if not seed_fk:
        return
    # nexus-dbzxb (RDR-191 Phase 5 Python collateral): backfill_manifest's
    # WRITE side (write_manifest / atomic_manifest_replace) always goes
    # through the REAL engine catalog (``active_catalog``, see its
    # docstring), while its READ side is this fixture's fake in-memory
    # ``t3_db`` — a deliberate split so these tests exercise the real
    # catalog write behavior without paying for a real T3 round trip on
    # every seed. ``fk_catalog_chunks_chunk`` now requires the manifest's
    # chash to have a matching REAL ``nexus.chunks`` row, which the fake
    # client can never provide — so seed a stub chunk in the real engine
    # too, purely for FK bookkeeping. Nothing under test reads this row;
    # the code under test's T3 reads all go through ``t3_db`` above.
    from tests._catalog_fixture_ops import seed_manifest_chunks

    seed_manifest_chunks(collection, [chunk_text_hash])


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
            # Distinct chunk_text_hash per iteration -- id defaults to it
            # (nexus-dmf7r), so three docs sharing one hash would collide
            # onto the same T3 chunk row instead of seeding three.
            _seed_chunk(
                t3_db, collection=coll,
                content=f"content {i}", doc_id=tumbler, chunk_index=0,
                chunk_text_hash=str(i) * 64,
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
            t3_db, collection=coll,
            content="first", doc_id=first, chunk_index=0,
            chunk_text_hash="a" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll,
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


# ── nexus-gvmbo: zero-chunk-match docs must never write an empty manifest ──


class TestGvmboEmptyManifestGuard:
    """nexus-gvmbo: ``backfill_manifest_for_collection`` must SKIP a
    zero-chunk-match doc, never call ``write_manifest(doc_id, [], ...)``.

    That call is an atomic DELETE+INSERT server-side
    (``CatalogHandler.java`` -> ``repo.writeManifest``): writing ``[]`` for
    a doc whose lookup simply missed its chunks DESTROYS any existing
    manifest. Kill-control: this test was run against the pre-fix code
    (guard removed, resync call removed) and failed — ``after`` came back
    empty and ``docs_skipped_zero_chunks`` stayed 0 — before the fix in
    ``manifest_backfill.py`` landed.
    """

    def test_zero_match_never_destroys_existing_manifest(self, active_catalog, t3_db):
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        chash = "c" * 64
        _seed_chunk(
            t3_db, collection=coll,
            content="alive", doc_id=tumbler, chunk_index=0,
            chunk_text_hash=chash,
        )

        # Pass 1: real chunk present, establishes a real manifest.
        first = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )
        assert first.chunks_written == 1
        assert first.docs_skipped_zero_chunks == 0
        before = active_catalog.get_manifest(tumbler)
        assert len(before) == 1

        # Remove the T3 chunk out from under the manifest -- reproduces the
        # key-mismatch damage class this bead is filed for: the doc HAS an
        # existing manifest, but a subsequent lookup now matches zero chunks.
        col = t3_db._client.get_or_create_collection(coll)
        col.delete(ids=[chash])

        # Pass 2: lookup matches zero chunks.
        second = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )

        # Non-vacuity: the skip is COUNTED, not silent.
        assert second.docs_skipped_zero_chunks == 1
        assert second.docs_processed == 0
        assert second.chunks_written == 0

        # The manifest from pass 1 must survive pass 2 untouched -- this is
        # the assertion that fails red on the pre-fix code (which would have
        # called write_manifest(tumbler, [], ...) and wiped it to 0 rows).
        after = active_catalog.get_manifest(tumbler)
        assert after == before
        assert len(after) == 1

    def test_zero_match_surfaced_in_cli_summary(self, active_catalog, t3_db, runner):
        """The skip must appear in the command's own summary output --
        never silent (bead gvmbo's non-vacuity requirement)."""
        coll = _unique_coll()
        # A registered doc with NO matching T3 chunks under either lookup key.
        tumbler = _register_doc(active_catalog, coll)
        # The T3 collection must actually EXIST (otherwise this doc takes
        # the "no_t3" branch, not the zero-chunks branch under test) --
        # seed an unrelated chunk under a DIFFERENT doc_id so get_collection
        # succeeds but the target doc still matches zero chunks.
        other = _register_doc(active_catalog, coll)
        assert other != tumbler
        _seed_chunk(
            t3_db, collection=coll,
            content="unrelated", doc_id=other, chunk_index=0,
            chunk_text_hash="1" * 64,
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
        assert "zero_chunks" in result.output or "zero chunk" in result.output, (
            result.output
        )


# ── nexus-b91tv: chunk lookup must match BOTH doc_id and catalog_doc_id ────


class TestB91tvKeyUnionLookup:
    """nexus-b91tv: store_put stamps the tumbler under metadata
    ``catalog_doc_id`` (``doc_id`` there carries the chash instead), while
    indexer-origin chunks stamp it under ``doc_id``. Backfill must match
    EITHER key so store_put-origin docs are no longer structurally
    unreachable.
    """

    def _seed_store_put_shaped_chunk(
        self, t3_db: T3Database, *, collection: str,
        content: str, catalog_doc_id: str, chash: str,
        chunk_id: str | None = None,
    ) -> None:
        """Seed a chunk in the store_put shape: metadata carries
        ``catalog_doc_id`` = tumbler and ``doc_id`` = chash (never
        ``content_hash`` -- store_hook.py stamps only ``doc_id``), per
        ``http_vector_client.py:1705-1706`` / ``store_hook.py:328,357,386``.

        nexus-dmf7r: ``chunk_id`` defaults to ``chash`` -- see
        ``_seed_chunk``'s docstring for why.
        """
        col = t3_db._client.get_or_create_collection(collection)
        col.add(
            ids=[chunk_id if chunk_id is not None else chash],
            documents=[content],
            metadatas=[{
                "doc_id": chash,  # chash, NOT the tumbler -- this is the bug
                "catalog_doc_id": catalog_doc_id,
                "chunk_text_hash": chash,
            }],
        )
        # nexus-dbzxb: see _seed_chunk's comment — real-engine FK bookkeeping
        # stub, not read by the test itself.
        from tests._catalog_fixture_ops import seed_manifest_chunks

        seed_manifest_chunks(collection, [chash])

    def test_store_put_shaped_chunk_matches_via_catalog_doc_id(
        self, active_catalog, t3_db,
    ):
        """A chunk whose ONLY tumbler-carrying key is catalog_doc_id (no
        chunk_index, no doc_id==tumbler) must still be found."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        tumbler = _register_doc(active_catalog, coll)
        self._seed_store_put_shaped_chunk(
            t3_db, collection=coll,
            content="store_put note", catalog_doc_id=tumbler, chash="d" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )

        assert result.docs_skipped_zero_chunks == 0
        assert result.docs_processed == 1
        assert result.chunks_written == 1
        manifest = active_catalog.get_manifest(tumbler)
        assert len(manifest) == 1
        assert manifest[0].chash == "d" * 64

    def test_indexer_shaped_chunk_still_matches_via_doc_id(
        self, active_catalog, t3_db,
    ):
        """Regression: the pre-existing indexer-origin (doc_id-keyed) path
        must still work after the key-union change."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="indexed", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="e" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )

        assert result.docs_processed == 1
        assert result.chunks_written == 1
        manifest = active_catalog.get_manifest(tumbler)
        assert len(manifest) == 1
        assert manifest[0].chash == "e" * 64

    def test_collection_with_mixed_origin_docs_both_backfill(
        self, active_catalog, t3_db,
    ):
        """One backfill pass over a collection holding BOTH an
        indexer-origin doc (doc_id-keyed chunk) and a store_put-origin doc
        (catalog_doc_id-keyed chunk) must match and write BOTH -- neither
        lookup key starves the other doc in the same collection.

        (A single DOCUMENT with chunks from both origins is not the
        production shape this bead targets -- store_put and the indexer
        never co-write one doc's chunk_index space -- so the union is
        exercised across two documents here, and same-chash-under-both-
        keys dedup is covered separately below.)
        """
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        indexer_doc = _register_doc(active_catalog, coll)
        store_put_doc = _register_doc(active_catalog, coll)
        assert indexer_doc != store_put_doc
        _seed_chunk(
            t3_db, collection=coll,
            content="indexed part", doc_id=indexer_doc, chunk_index=0,
            chunk_text_hash="f" * 64,
        )
        self._seed_store_put_shaped_chunk(
            t3_db, collection=coll,
            content="store_put part", catalog_doc_id=store_put_doc, chash="a" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )

        assert result.docs_skipped_zero_chunks == 0
        assert result.docs_processed == 2
        assert result.chunks_written == 2
        indexer_manifest = active_catalog.get_manifest(indexer_doc)
        store_put_manifest = active_catalog.get_manifest(store_put_doc)
        assert [r.chash for r in indexer_manifest] == ["f" * 64]
        assert [r.chash for r in store_put_manifest] == ["a" * 64]

    def test_same_chash_under_both_keys_not_double_counted(
        self, active_catalog, t3_db,
    ):
        """A chunk that happens to carry BOTH doc_id==tumbler AND
        catalog_doc_id==tumbler (same chash) is deduped to one manifest row,
        not written twice."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["b" * 64],  # nexus-dmf7r: id == chash (RDR-108/RDR-180 invariant)
            documents=["dual-keyed"],
            metadatas=[{
                "doc_id": tumbler,
                "catalog_doc_id": tumbler,
                "chunk_text_hash": "b" * 64,
                "chunk_index": 0,
            }],
        )
        # nexus-dbzxb: see _seed_chunk's comment — real-engine FK bookkeeping
        # stub, not read by the test itself.
        from tests._catalog_fixture_ops import seed_manifest_chunks

        seed_manifest_chunks(coll, ["b" * 64])

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )

        assert result.chunks_written == 1
        manifest = active_catalog.get_manifest(tumbler)
        assert len(manifest) == 1


# ── nexus-gvmbo item 3: successful writes must resync chunk_count ─────────


class TestChunkCountResync:
    """A successful non-dry-run backfill pass must resync
    ``documents.chunk_count`` (mirrors ``manifest_heal.py:304``'s
    ``atomic_manifest_replace`` + ``resync_chunk_count_cache`` pairing).
    Without it the row stays at whatever stale value it had (registered at
    0 in these tests) and every gap detector re-flags a doc this pass just
    healed."""

    def test_successful_write_resyncs_chunk_count(self, active_catalog, t3_db):
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)  # chunk_count=0 at register
        _seed_chunk(
            t3_db, collection=coll,
            content="resync me", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="9" * 64,
        )

        before = active_catalog.by_doc_id(tumbler)
        assert before.chunk_count == 0

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False
        )
        assert result.chunks_written == 1

        after = active_catalog.by_doc_id(tumbler)
        assert after.chunk_count == 1

    def test_dry_run_does_not_resync(self, active_catalog, t3_db):
        """dry_run must not touch chunk_count -- no writes at all."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="dry run only", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="8" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=True
        )
        assert result.docs_processed == 1
        assert result.chunks_written == 0

        after = active_catalog.by_doc_id(tumbler)
        assert after.chunk_count == 0


# ── nexus-3n7pr G1: --only-gapped touches only zero-manifest documents ────


class TestG1OnlyGappedFilter:
    """G1: a repair pass over a mostly-healthy collection must touch ONLY
    documents with zero manifest rows. ``only_gapped=True`` pre-passes with
    ONE ``catalog.get_manifests(...)`` call and skips any doc already
    present in the result -- BEFORE any T3 read or ``write_manifest`` call,
    in both dry-run and real-run mode.
    """

    def _seed_healthy_and_gapped(
        self, active_catalog: Any, t3_db: T3Database, coll: str,
    ) -> tuple[str, str]:
        """Register two docs; give ONE ("healthy") a real manifest row via a
        direct ``write_manifest`` call (not a whole-collection backfill, so
        the "gapped" doc is never touched during setup); leave the other
        ("gapped") with a real T3 chunk but zero manifest rows.
        """
        healthy = _register_doc(active_catalog, coll)
        gapped = _register_doc(active_catalog, coll)

        _seed_chunk(
            t3_db, collection=coll,
            content="healthy doc", doc_id=healthy, chunk_index=0,
            chunk_text_hash="1" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll,
            content="gapped doc", doc_id=gapped, chunk_index=0,
            chunk_text_hash="2" * 64,
        )

        # Direct manifest write for `healthy` only -- `gapped` is untouched.
        active_catalog.write_manifest(
            healthy,
            [{
                "chash": "1" * 64,
                "position": 0,
                "line_start": None,
                "line_end": None,
                "char_start": None,
                "char_end": None,
            }],
            collection=coll,
        )
        before = active_catalog.get_manifest(healthy)
        assert len(before) == 1

        return healthy, gapped

    def test_only_gapped_skips_healthy_processes_gapped(self, active_catalog, t3_db):
        """--only-gapped skips the doc WITH a manifest, processes the one
        WITHOUT, and never calls write_manifest for the healthy doc."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        healthy, gapped = self._seed_healthy_and_gapped(active_catalog, t3_db, coll)
        healthy_manifest_before = active_catalog.get_manifest(healthy)

        write_manifest_orig = active_catalog.write_manifest
        with patch.object(
            active_catalog, "write_manifest", wraps=write_manifest_orig,
        ) as spy:
            result = backfill_manifest_for_collection(
                active_catalog, t3_db, coll, dry_run=False, only_gapped=True,
            )

        # The counter: exactly one doc skipped as already-has-manifest.
        assert result.docs_skipped_has_manifest == 1
        assert result.docs_processed == 1
        assert result.chunks_written == 1

        # write_manifest must never be called with the healthy doc_id --
        # only the gapped one.
        called_doc_ids = [call.args[0] for call in spy.call_args_list]
        assert healthy not in called_doc_ids
        assert gapped in called_doc_ids

        # The healthy doc's manifest is byte-identical to before -- never
        # rewritten (an --only-gapped bug would replace it with the same
        # T3-derived content and this assertion would still pass, which is
        # why the spy assertion above is the one that carries the proof; this is the
        # end-state cross-check).
        assert active_catalog.get_manifest(healthy) == healthy_manifest_before

        # The gapped doc now has its manifest.
        gapped_manifest = active_catalog.get_manifest(gapped)
        assert len(gapped_manifest) == 1
        assert gapped_manifest[0].chash == "2" * 64

    def test_only_gapped_dry_run_reports_same_partition_without_writing(
        self, active_catalog, t3_db,
    ):
        """A dry run under --only-gapped reports the identical
        skip/process partition as a real run, and writes nothing."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        healthy, gapped = self._seed_healthy_and_gapped(active_catalog, t3_db, coll)

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=True, only_gapped=True,
        )

        assert result.docs_skipped_has_manifest == 1
        assert result.docs_processed == 1
        assert result.chunks_written == 0  # dry run: nothing written
        # ...but it reports what it WOULD write (conexus-4b's live dry run
        # printed "would write 0" for a doc it was about to heal).
        assert result.chunks_would_write > 0

        # No manifest materialized for the gapped doc -- dry run never wrote.
        assert active_catalog.get_manifest(gapped) == []

        assert result.dry_run is True

        # With no FK-409 in this fixture, the real run writes exactly what
        # the dry run planned (in general the dry-run count is an upper
        # bound: FK-409 is decided server-side at write time).
        real = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False, only_gapped=True,
        )
        assert real.chunks_written == result.chunks_would_write
        assert real.chunks_would_write == 0
        assert real.dry_run is False

    def test_only_gapped_limit_bounds_the_gapped_set_not_the_raw_list(
        self, active_catalog, t3_db,
    ):
        """``limit`` under --only-gapped bounds the GAPPED set (critique
        T2 [22623]): the seed registers the healthy doc FIRST, so a limit
        applied to the raw tumbler-ordered list would select only the
        healthy doc and process zero gapped ones -- the plan's canary
        (``-n 25 --only-gapped``) would exit clean without exercising the
        write path. With the fix, limit=1 still processes the one gapped
        doc and the healthy doc is counted as skipped."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        healthy, gapped = self._seed_healthy_and_gapped(active_catalog, t3_db, coll)

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False, only_gapped=True, limit=1,
        )

        assert result.docs_processed == 1
        assert result.docs_skipped_has_manifest == 1
        assert len(active_catalog.get_manifest(gapped)) == 1

    def test_only_gapped_default_off_processes_every_doc(self, active_catalog, t3_db):
        """Default (only_gapped unset/False) behavior is unchanged: a
        healthy doc is reprocessed too, not skipped."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        healthy, gapped = self._seed_healthy_and_gapped(active_catalog, t3_db, coll)

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_skipped_has_manifest == 0
        assert result.docs_processed == 2
        assert result.chunks_written == 2


# ── nexus-3n7pr G2/G1: CLI surfaces both new skip counters ────────────────


class TestG1G2CliCounters:
    """G2: ``docs_skipped_phase3_no_index`` was counted but never printed.
    G1: ``docs_skipped_has_manifest`` (the --only-gapped skip) must also be
    surfaced. Neither may be silent in the CLI's per-collection or summary
    output.
    """

    def test_phase3_no_index_surfaced_in_cli_output(self, active_catalog, t3_db, runner):
        """A multi-chunk doc with no chunk_index metadata anywhere trips
        Phase3ChunkIndexMissingError; the CLI must print the count."""
        from tests._catalog_fixture_ops import seed_manifest_chunks

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            # nexus-dmf7r: id == chash (RDR-108/RDR-180 invariant).
            ids=["3" * 64, "4" * 64],
            documents=["chunk one", "chunk two"],
            metadatas=[
                {"doc_id": tumbler, "chunk_text_hash": "3" * 64},
                {"doc_id": tumbler, "chunk_text_hash": "4" * 64},
            ],
        )
        seed_manifest_chunks(coll, ["3" * 64, "4" * 64])

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
        assert "phase3 no chunk_index" in result.output, result.output

    def test_only_gapped_skip_surfaced_in_cli_output(self, active_catalog, t3_db, runner):
        """--only-gapped's has_manifest skip count must be printed too."""
        coll = _unique_coll()
        healthy = _register_doc(active_catalog, coll)
        gapped = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="healthy", doc_id=healthy, chunk_index=0,
            chunk_text_hash="5" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll,
            content="gapped", doc_id=gapped, chunk_index=0,
            chunk_text_hash="6" * 64,
        )
        active_catalog.write_manifest(
            healthy,
            [{
                "chash": "5" * 64,
                "position": 0,
                "line_start": None,
                "line_end": None,
                "char_start": None,
                "char_end": None,
            }],
            collection=coll,
        )

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                [
                    "t3", "backfill-manifest", "--collection", coll,
                    "--no-dry-run", "--only-gapped",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "already has manifest" in result.output, result.output
        # The gapped doc healed; the healthy one untouched.
        assert len(active_catalog.get_manifest(gapped)) == 1
        assert len(active_catalog.get_manifest(healthy)) == 1


# ── nexus-dmf7r: manifest chash keyed on the T3 chunk's own id ────────────


class TestDmf7rChashKeyedOnId:
    """nexus-dmf7r: the manifest's chash must come from the T3 chunk's
    own id (what ``fk_catalog_chunks_chunk`` actually validates against),
    never from ``metadata['chunk_text_hash']`` -- a redundant copy that
    can silently diverge from the row it was copied from. A divergence
    skips the WHOLE document (never guesses which value is correct),
    counted in ``docs_skipped_chash_divergent``.
    """

    def test_aligned_chunk_writes_manifest_keyed_on_id(self, active_catalog, t3_db):
        """The ordinary (aligned) case: id == metadata chash. The
        manifest row carries that value; no divergence counted."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="aligned", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="7" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_skipped_chash_divergent == 0
        assert result.docs_processed == 1
        assert result.chunks_written == 1
        manifest = active_catalog.get_manifest(tumbler)
        assert len(manifest) == 1
        assert manifest[0].chash == "7" * 64

    def test_divergent_id_and_metadata_skips_doc_not_write(
        self, active_catalog, t3_db,
    ):
        """A chunk whose T3 id disagrees with its ``chunk_text_hash``
        metadata copy must skip the whole document -- never write a
        manifest row keyed on the divergent copy (which would 409
        against ``fk_catalog_chunks_chunk`` on cloud)."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        # Deliberately divergent: the T3 row's real id differs from the
        # chunk_text_hash metadata copy the pre-fix code trusted.
        _seed_chunk(
            t3_db, collection=coll,
            content="divergent", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="8" * 64,
            chunk_id="stale-chunk-id-not-a-chash",
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_skipped_chash_divergent == 1
        assert result.docs_processed == 0
        assert result.chunks_written == 0
        # Never written -- no manifest row for this doc at all.
        assert active_catalog.get_manifest(tumbler) == []

    def test_divergent_doc_does_not_destroy_existing_manifest(
        self, active_catalog, t3_db,
    ):
        """A doc that already has a healthy manifest must NOT have it
        wiped out by a later pass whose T3 chunk has since diverged
        (e.g. a rekey / hand-patched metadata row) -- the same
        never-destroy guarantee nexus-gvmbo established for
        zero-chunk-match, extended to this skip class."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        chash = "9" * 63 + "1"
        _seed_chunk(
            t3_db, collection=coll,
            content="was healthy", doc_id=tumbler, chunk_index=0,
            chunk_text_hash=chash,
        )
        first = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )
        assert first.chunks_written == 1
        before = active_catalog.get_manifest(tumbler)
        assert len(before) == 1

        # Simulate the metadata copy drifting from the row's own id (a
        # rekey / hand patch) without re-seeding a fresh, aligned chunk.
        col = t3_db._client.get_or_create_collection(coll)
        col.update(
            ids=[chash],
            metadatas=[{
                "doc_id": tumbler,
                "chunk_index": 0,
                "chunk_text_hash": "a" * 64,  # diverged from the id above
            }],
        )

        second = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )
        assert second.docs_skipped_chash_divergent == 1
        assert second.docs_processed == 0

        after = active_catalog.get_manifest(tumbler)
        assert after == before
        assert len(after) == 1

    def test_chash_divergent_surfaced_in_cli_output(
        self, active_catalog, t3_db, runner,
    ):
        """The skip must appear in the command's own summary output --
        never silent (mirrors nexus-gvmbo's non-vacuity requirement)."""
        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="divergent", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="b" * 64,
            chunk_id="not-the-chash",
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
        assert "chash id/metadata divergent" in result.output, result.output


# ── nexus-r7g3i: FK-409 on write_manifest is a per-doc skip, not a ────────
# ── per-collection abort ───────────────────────────────────────────────


class TestR7g3iFk409Skip:
    """nexus-r7g3i: a manifest write that 409s against
    ``fk_catalog_chunks_chunk`` (no matching ``nexus.chunks`` row for the
    written chash) must be caught PER DOCUMENT, counted in
    ``docs_skipped_fk_409``, and NOT abort the rest of the collection --
    pre-fix, any per-doc write error propagated out of this function and
    was caught per-COLLECTION by the CLI, abandoning every unprocessed
    document in that collection without marking any of them.
    """

    def test_fk_409_skips_doc_and_continues_collection(
        self, active_catalog, t3_db,
    ):
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        # doomed: its chash has NO matching nexus.chunks row -- a real
        # write_manifest call for it genuinely 409s against
        # fk_catalog_chunks_chunk.
        doomed = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="no FK backing", doc_id=doomed, chunk_index=0,
            chunk_text_hash="c" * 64,
            seed_fk=False,
        )
        # healthy: seeded normally (real nexus.chunks row present) --
        # proves the loop kept going past the 409 rather than stopping.
        healthy = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="FK-safe", doc_id=healthy, chunk_index=0,
            chunk_text_hash="d" * 64,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_skipped_fk_409 == 1
        assert result.docs_processed == 1
        assert result.chunks_written == 1
        assert active_catalog.get_manifest(doomed) == []
        healthy_manifest = active_catalog.get_manifest(healthy)
        assert len(healthy_manifest) == 1
        assert healthy_manifest[0].chash == "d" * 64

    def test_fk_409_surfaced_in_cli_output(self, active_catalog, t3_db, runner):
        coll = _unique_coll()
        doomed = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="no FK backing", doc_id=doomed, chunk_index=0,
            chunk_text_hash="e" * 64,
            seed_fk=False,
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
        assert "FK conflict" in result.output, result.output

    def test_other_write_errors_still_propagate(self, active_catalog, t3_db):
        """A non-409 write failure must still propagate (preserve prior
        per-collection abort behavior) -- only a 409 is downgraded to a
        per-doc skip."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        tumbler = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="whatever", doc_id=tumbler, chunk_index=0,
            chunk_text_hash="f" * 64,
        )

        boom = httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=MagicMock(status_code=500),
        )
        with (
            patch.object(active_catalog, "write_manifest", side_effect=boom),
            pytest.raises(httpx.HTTPStatusError),
        ):
            backfill_manifest_for_collection(
                active_catalog, t3_db, coll, dry_run=False,
            )


# ── nexus-r7g3i: --resume + --only-gapped combine correctly ───────────────


class _ScopedCollectionsCatalog:
    """Thin proxy limiting ``list_collections()`` to an explicit set,
    delegating everything else to *inner*.

    A real ``--resume``/``--only-gapped`` run with no ``-c`` enumerates
    EVERY collection in the tenant's catalog via ``list_collections()``,
    which would make the combined-flags test below slow and
    non-deterministic against a shared engine substrate holding other
    tests' collections too. Everything else -- ``list_by_collection``,
    ``get_manifests``, ``write_manifest``, ``resync_chunk_count_cache`` --
    still goes through the real catalog via ``__getattr__``.
    """

    def __init__(self, inner: Any, collections: list[str]) -> None:
        self._inner = inner
        self._collections = collections

    def list_collections(self) -> list[dict]:
        return [{"name": c} for c in self._collections]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestResumeOnlyGappedCombined:
    """nexus-r7g3i (critique T2 [22623]): --resume and --only-gapped are
    structurally orthogonal -- --resume skips whole COLLECTIONS already
    marked done in the state file, --only-gapped skips individual
    DOCUMENTS within a collection that still has a manifest. A combined
    invocation must apply both correctly at once: a done collection is
    skipped entirely (never even queried for gapped docs), while a
    pending collection still gets per-doc gapped filtering.
    """

    def test_resume_skips_done_collection_only_gapped_filters_the_rest(
        self, active_catalog, t3_db, runner, tmp_path,
    ):
        coll_done = _unique_coll("done")
        coll_pending = _unique_coll("pending")

        # coll_done: a gapped doc that would be healed if this collection
        # were touched at all -- proves --resume skipped it outright, not
        # "processed it and it happened to be filtered away by chance".
        done_doc = _register_doc(active_catalog, coll_done)
        _seed_chunk(
            t3_db, collection=coll_done,
            content="done collection doc", doc_id=done_doc, chunk_index=0,
            chunk_text_hash="1" * 64,
        )

        # coll_pending: one healthy (has manifest) doc + one gapped doc.
        healthy = _register_doc(active_catalog, coll_pending)
        gapped = _register_doc(active_catalog, coll_pending)
        _seed_chunk(
            t3_db, collection=coll_pending,
            content="healthy", doc_id=healthy, chunk_index=0,
            chunk_text_hash="2" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll_pending,
            content="gapped", doc_id=gapped, chunk_index=0,
            chunk_text_hash="3" * 64,
        )
        active_catalog.write_manifest(
            healthy,
            [{
                "chash": "2" * 64, "position": 0, "line_start": None,
                "line_end": None, "char_start": None, "char_end": None,
            }],
            collection=coll_pending,
        )

        state_file = tmp_path / "backfill_state.json"
        state_file.write_text(json.dumps({coll_done: ["__done__"]}))

        scoped_cat = _ScopedCollectionsCatalog(
            active_catalog, [coll_done, coll_pending],
        )
        write_manifest_orig = active_catalog.write_manifest
        with (
            patch("nexus.commands.t3._make_catalog", return_value=scoped_cat),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.object(active_catalog, "write_manifest", wraps=write_manifest_orig) as spy,
            patch.dict(os.environ, {"NEXUS_BACKFILL_STATE_FILE": str(state_file)}),
        ):
            result = runner.invoke(
                main,
                [
                    "t3", "backfill-manifest",
                    "--no-dry-run", "--resume", "--only-gapped",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        # coll_done was skipped ENTIRELY -- its gapped doc was never
        # touched, so it must still have zero manifest rows.
        assert active_catalog.get_manifest(done_doc) == []
        called_doc_ids = [call.args[0] for call in spy.call_args_list]
        assert done_doc not in called_doc_ids

        # coll_pending: --only-gapped still filtered within it.
        assert healthy not in called_doc_ids
        assert gapped in called_doc_ids
        assert len(active_catalog.get_manifest(gapped)) == 1
        assert len(active_catalog.get_manifest(healthy)) == 1  # untouched, pre-existing


# ── nexus-69c94 critique S1/S2 (substantive-critic, T2 scratch 79007753) ──


class TestNexus69c94CritiqueFixes:
    """S1: an fk_409/chash_divergent residual must NOT mark the
    collection __done__ in the resume state file (both classes are
    caught per-doc and never raise, so the collection "completes"
    without error) -- it is marked __partial__ instead, and a future
    --resume reprocesses the whole collection rather than permanently
    stranding the un-healed docs.

    C2: docs_skipped_zero_chunks joins the same residual -- it is the
    DOMINANT live gap class (894/895 in the nexus-3n7pr population), and
    a future remediation pass (re-index / re-put) can make exactly those
    docs recoverable. A collection whose ONLY gaps are zero_chunks must
    also stay revisitable by --resume, not be marked permanently done.

    S2: the fk_409 structlog line must name the attempted chash(es),
    matching the actionability of chash_divergent's chunk_id/meta_chash
    logging.
    """

    def test_fk_409_residual_marks_partial_not_done_and_resume_reprocesses(
        self, active_catalog, t3_db, runner, tmp_path,
    ):
        coll = _unique_coll()
        doomed = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="no FK backing", doc_id=doomed, chunk_index=0,
            chunk_text_hash="1" * 64,
            seed_fk=False,
        )
        healthy = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="FK-safe", doc_id=healthy, chunk_index=0,
            chunk_text_hash="2" * 64,
        )

        state_file = tmp_path / "backfill_state.json"

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.dict(os.environ, {"NEXUS_BACKFILL_STATE_FILE": str(state_file)}),
        ):
            result1 = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                catch_exceptions=False,
            )
            assert result1.exit_code == 0, result1.output
            assert "NOT marked done" in result1.output, result1.output

            state = json.loads(state_file.read_text())
            assert state[coll][0] == "__partial__", state
            assert state[coll] != ["__done__"]

            # Second run, --resume: the collection must be REPROCESSED,
            # not skipped -- the doomed doc fails again with fk_409,
            # proving the resume-skip gate did not treat this collection
            # as done.
            result2 = runner.invoke(
                main,
                [
                    "t3", "backfill-manifest", "--collection", coll,
                    "--no-dry-run", "--resume",
                ],
                catch_exceptions=False,
            )
        assert result2.exit_code == 0, result2.output
        assert "FK conflict" in result2.output, result2.output

    def test_zero_chunks_only_residual_marks_partial_not_done(
        self, active_catalog, t3_db, runner, tmp_path,
    ):
        """C2: a collection whose ONLY gaps are zero_chunks (no fk_409,
        no chash_divergent) must ALSO be marked __partial__, not
        __done__ -- a future Phase-5 remediation pass (re-index /
        re-put) can make exactly those docs recoverable, so --resume
        must keep revisiting the collection."""
        coll = _unique_coll()
        # No matching T3 chunk under either lookup key at all -- the
        # doc takes the zero_chunks branch, not no_t3 (an unrelated
        # chunk exists so `col` resolves) and not fk_409/chash_divergent.
        stranded = _register_doc(active_catalog, coll)
        other = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="unrelated", doc_id=other, chunk_index=0,
            chunk_text_hash="5" * 64,
        )

        state_file = tmp_path / "backfill_state.json"

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.dict(os.environ, {"NEXUS_BACKFILL_STATE_FILE": str(state_file)}),
        ):
            result1 = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                catch_exceptions=False,
            )
            assert result1.exit_code == 0, result1.output
            assert "NOT marked done" in result1.output, result1.output

            state = json.loads(state_file.read_text())
            assert state[coll][0] == "__partial__", state
            assert state[coll] != ["__done__"]
            assert any(entry.startswith("zero_chunks=1") for entry in state[coll]), state

            # A second --resume run must REPROCESS the collection, not
            # skip it -- the stranded doc still shows zero_chunks again.
            result2 = runner.invoke(
                main,
                [
                    "t3", "backfill-manifest", "--collection", coll,
                    "--no-dry-run", "--resume",
                ],
                catch_exceptions=False,
            )
        assert result2.exit_code == 0, result2.output
        assert "zero chunk matches" in result2.output, result2.output
        assert active_catalog.get_manifest(stranded) == []

    def test_clean_collection_still_marks_done(
        self, active_catalog, t3_db, runner, tmp_path,
    ):
        """Regression guard: a collection with ZERO fk_409/chash_divergent/
        zero_chunks residual must still be marked __done__ as before --
        the fix only changes behavior for the residual case."""
        coll = _unique_coll()
        healthy = _register_doc(active_catalog, coll)
        _seed_chunk(
            t3_db, collection=coll,
            content="clean", doc_id=healthy, chunk_index=0,
            chunk_text_hash="3" * 64,
        )

        state_file = tmp_path / "backfill_state.json"

        with (
            patch("nexus.commands.t3._make_catalog", return_value=active_catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.dict(os.environ, {"NEXUS_BACKFILL_STATE_FILE": str(state_file)}),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "NOT marked done" not in result.output, result.output
        state = json.loads(state_file.read_text())
        assert state[coll] == ["__done__"]

    def test_fk_409_log_line_names_the_attempted_chashes(
        self, active_catalog, t3_db,
    ):
        from structlog.testing import capture_logs

        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        doomed = _register_doc(active_catalog, coll)
        chash = "4" * 64
        _seed_chunk(
            t3_db, collection=coll,
            content="no FK backing", doc_id=doomed, chunk_index=0,
            chunk_text_hash=chash,
            seed_fk=False,
        )

        with capture_logs() as logs:
            result = backfill_manifest_for_collection(
                active_catalog, t3_db, coll, dry_run=False,
            )

        assert result.docs_skipped_fk_409 == 1
        events = [
            e for e in logs
            if e["event"] == "manifest_backfill_doc_skipped_fk_409"
        ]
        assert len(events) == 1, logs
        assert events[0]["chashes"] == [chash]
        assert events[0]["doc_id"] == doomed
        assert events[0]["collection"] == coll


# ── nexus-wbfpw.7: REVERSE notes discovery ─────────────────────────────────


def _register_note_doc(cat: Any, coll: str, chash: str, *, title: str | None = None) -> str:
    """Register ONE live, note-shaped document (no ``file_path``) whose OWN
    ``meta['doc_id']`` names *chash* -- the reverse notes-guard shape
    (``catalog/store_hook.py::single_chunk_manifest_metadata``,
    :func:`nexus.indexer_utils.is_note_shaped`)."""
    slug = uuid.uuid4().hex[:8]
    owner = cat.register_owner(f"wbfpw7-note-{slug}", "curator")
    return str(cat.register(
        owner, title or f"wbfpw7-note-{slug}",
        content_type="knowledge",
        physical_collection=coll,
        meta={"doc_id": chash},
    ))


def _seed_reverse_chunk(
    t3_db: T3Database, *, collection: str, content: str, chunk_text_hash: str,
) -> None:
    """Seed one T3 chunk with NO forward pointer at all (neither ``doc_id``
    nor ``catalog_doc_id``) -- the legacy shape the reverse notes-guard
    rescue exists for: a chunk's forward metadata key is absent (or, in
    production, could instead name a tombstoned document), and only a live
    note's own reverse ``meta['doc_id']`` names it."""
    col = t3_db._client.get_or_create_collection(collection)
    col.add(
        ids=[chunk_text_hash],
        documents=[content],
        metadatas=[{"chunk_text_hash": chunk_text_hash}],
    )
    from tests._catalog_fixture_ops import seed_manifest_chunks

    seed_manifest_chunks(collection, [chunk_text_hash])


class TestWbfpw7ReverseNotesDiscovery:
    """nexus-wbfpw.7: ``manifest_less_census.sql`` (nexus-wbfpw.4) classifies
    a chunk as ``legacy-unmanifested`` via TWO paths -- the pre-existing
    forward metadata-key path, and a REVERSE path: a chunk with no live
    forward owner whose chash is instead named by a live, note-shaped
    document's own ``meta['doc_id']``. Before this fix, backfill's
    discovery only ever queried the forward path, so a reverse-owned note
    always matched zero chunks and the census-zero gate could never read
    zero for this shape. These tests exercise the fix against the real
    engine substrate (``active_catalog``), with the fake in-memory ``t3_db``
    standing in for T3 reads (same split every other test in this module
    uses -- see ``_seed_chunk``'s docstring)."""

    def test_reverse_owned_note_gets_manifested_and_census_reads_zero(
        self, active_catalog, t3_db, t2_service_env,
    ):
        """(a) A note whose chunk has no forward key but whose meta.doc_id
        names it must be manifested into that note, and the census (the
        real engine route nexus-wbfpw.4 landed) must then report
        legacy-unmanifested 0 for this collection."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection
        from nexus.db.http_vector_client import HttpVectorClient

        coll = _unique_coll("knowledge")
        chash = "d" * 64
        note = _register_note_doc(active_catalog, coll, chash)
        _seed_reverse_chunk(
            t3_db, collection=coll, content="note body", chunk_text_hash=chash,
        )

        # RED (pre-fix): the note's forward lookup matches zero chunks, so
        # this chunk was entirely invisible to backfill.
        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_discovered == 1
        assert result.docs_skipped_zero_chunks == 0
        assert result.docs_processed == 1
        assert result.chunks_written == 1

        manifest = active_catalog.get_manifest(note)
        assert len(manifest) == 1
        assert manifest[0].chash == chash

        db = HttpVectorClient(tenant=t2_service_env)
        totals = db.manifest_less_census(coll)["totals"]
        assert totals["legacy-unmanifested"] == 0, totals

    def test_dry_run_reports_reverse_count_separately_and_writes_nothing(
        self, active_catalog, t3_db,
    ):
        """(d) A dry run must count the reverse discovery (separately from
        forward discovery) but write nothing."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        chash = "e" * 64
        note = _register_note_doc(active_catalog, coll, chash)
        _seed_reverse_chunk(
            t3_db, collection=coll, content="note body", chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=True,
        )

        assert result.docs_reverse_discovered == 1
        assert result.docs_processed == 1
        assert result.chunks_written == 0
        assert active_catalog.get_manifest(note) == []

    def test_reverse_tie_break_picks_fewest_manifest_rows_anywhere(
        self, active_catalog, t3_db,
    ):
        """(b) Two live notes both name the SAME chash -- the census's own
        tie-break (fewest manifest rows anywhere, THEN lowest tumbler)
        must decide, not registration order alone. ``note_with_other_row``
        is registered FIRST (so it would win a tumbler-only tie-break) but
        carries an unrelated manifest row elsewhere, so its total count is
        1 -- it must LOSE to the second-registered, zero-manifest note."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        chash = "f" * 64
        note_with_other_row = _register_note_doc(
            active_catalog, coll, chash, title="wbfpw7-first",
        )
        note_clean = _register_note_doc(
            active_catalog, coll, chash, title="wbfpw7-second",
        )

        other_chash = "1" * 64
        from tests._catalog_fixture_ops import seed_manifest_chunks

        seed_manifest_chunks(coll, [other_chash])
        active_catalog.write_manifest(
            note_with_other_row,
            [{
                "chash": other_chash, "position": 0, "line_start": None,
                "line_end": None, "char_start": None, "char_end": None,
            }],
            collection=coll,
        )

        _seed_reverse_chunk(
            t3_db, collection=coll, content="shared note body",
            chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_discovered == 1

        clean_manifest = active_catalog.get_manifest(note_clean)
        assert len(clean_manifest) == 1
        assert clean_manifest[0].chash == chash

        # The first-registered note's manifest is untouched -- only its
        # pre-existing unrelated row, never the shared chash.
        other_manifest = active_catalog.get_manifest(note_with_other_row)
        assert len(other_manifest) == 1
        assert other_manifest[0].chash == other_chash

    def test_forward_owned_live_document_still_wins_over_reverse_note(
        self, active_catalog, t3_db,
    ):
        """(c) A chunk with a LIVE forward pointer must be manifested into
        its forward owner, never into an unrelated note that also
        reverse-matches the same chash -- forward wins whenever it is
        live, exactly like the census's own precedence."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        chash = "9" * 64
        forward_doc = _register_doc(active_catalog, coll)
        note = _register_note_doc(active_catalog, coll, chash)

        _seed_chunk(
            t3_db, collection=coll,
            content="forward-owned", doc_id=forward_doc, chunk_index=0,
            chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_discovered == 0

        forward_manifest = active_catalog.get_manifest(forward_doc)
        assert len(forward_manifest) == 1
        assert forward_manifest[0].chash == chash

        # The note gets nothing -- forward already owns this chash, and a
        # note with zero manifestable chunks is a zero-chunks skip, not a
        # reverse discovery.
        assert active_catalog.get_manifest(note) == []

    def test_cross_collection_forward_owner_excludes_reverse_and_is_reported(
        self, active_catalog, t3_db,
    ):
        """(e) fix-round-1 CRITICAL: a chunk physically living in collection
        A whose forward pointer names a document that is LIVE but
        registered under a DIFFERENT collection B must NEVER be manifested
        into a coincidental same-collection note that also reverse-matches
        the same chash -- the tenant-wide forward-liveness check must
        exclude it regardless of which collection the live owner is
        registered under, and the exclusion must be REPORTED (never a
        silent drop), since backfill cannot reach the true owner from a
        collection-A-scoped, document-driven run.

        Pre-fix, the exclusion set was built from THIS collection's own
        live-doc list only (``{str(d.tumbler) for d in docs}``), which does
        NOT contain forward_doc (registered under collB) -- so forward_hint
        would NOT be found in that set and the note would wrongly win as
        the sole reverse candidate with zero manifest rows, corrupting
        chunk ownership."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll_a = _unique_coll("knowledge")
        coll_b = _unique_coll("knowledge")
        chash = "2" * 64

        # forward_doc is LIVE and registered under coll_b -- a different
        # collection than the chunk physically lives in.
        forward_doc = _register_doc(active_catalog, coll_b)
        # note is registered under coll_a and coincidentally reverse-claims
        # the SAME chash as its own identity.
        note = _register_note_doc(active_catalog, coll_a, chash)

        # The chunk physically lives in coll_a's T3 store and forward-points
        # to forward_doc (registered under coll_b).
        _seed_chunk(
            t3_db, collection=coll_a,
            content="cross-collection owned", doc_id=forward_doc,
            chunk_index=0, chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll_a, dry_run=False,
        )

        assert result.docs_cross_collection_forward_owner_skipped == 1
        assert result.docs_reverse_discovered == 0
        # The note must get NOTHING -- the coincidental collision must never
        # be granted regardless of the true owner's collection.
        assert active_catalog.get_manifest(note) == []

    def test_tombstoned_forward_owner_does_not_block_reverse_rescue(
        self, active_catalog, t3_db,
    ):
        """Round-2 review suggestion: a forward pointer naming a TOMBSTONED
        document must not exclude reverse candidacy. The census rescues
        such a chunk through a live note (a live owner beats a dead one),
        so backfill must manifest it into that note, exactly as when the
        forward pointer is absent."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        chash = "4" * 64
        dead_doc = _register_doc(active_catalog, coll)
        note = _register_note_doc(active_catalog, coll, chash)
        _seed_chunk(
            t3_db, collection=coll,
            content="rescued by a live note", doc_id=dead_doc,
            chunk_index=0, chunk_text_hash=chash,
        )
        active_catalog.delete_document(dead_doc)

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_discovered == 1
        assert result.docs_cross_collection_forward_owner_skipped == 0
        assert [r.chash for r in active_catalog.get_manifest(note)] == [chash]

    def test_reverse_multi_piece_note_is_skipped_not_partially_healed(
        self, active_catalog, t3_db,
    ):
        """(f) fix-round-1: a legacy multi-piece note (registered
        chunk_count > 1) with no forward pointer on any piece must be
        SKIPPED by the reverse path, never manifested with only its
        identity chash at position 0 -- that would resync chunk_count to 1
        and permanently orphan the remaining pieces with no diagnostic."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll("knowledge")
        chash = "3" * 64
        note = _register_note_doc(active_catalog, coll, chash)
        # Simulate the pre-note-splitting legacy shape: the catalog record
        # says this note has 3 chunks, but only piece 0's identity chash is
        # ever discoverable via the reverse path (its own meta.doc_id).
        active_catalog.update(note, chunk_count=3)

        _seed_reverse_chunk(
            t3_db, collection=coll, content="piece 0 of 3", chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_multi_piece_skipped == 1
        assert result.docs_reverse_discovered == 0
        # Never partially healed -- the note's manifest stays untouched.
        assert active_catalog.get_manifest(note) == []

    def test_reverse_sole_candidate_with_rows_elsewhere_is_not_granted(
        self, active_catalog, t3_db,
    ):
        """(g) coverage gap named in T2 nexus/review-wbfpw7-code: a SOLE
        reverse candidate (no tie-break needed) that already carries a
        manifest row in another collection must NOT be granted the reverse
        chash -- matching the census's own ``total_count = 0`` eligibility
        condition exactly. A candidate with rows elsewhere is dead-owner
        territory, not legacy-unmanifested."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection
        from tests._catalog_fixture_ops import seed_manifest_chunks

        coll = _unique_coll("knowledge")
        chash = "4" * 64
        note = _register_note_doc(active_catalog, coll, chash)

        # note already has ONE manifest row elsewhere -- total_count > 0,
        # so it is NOT eligible for the reverse rescue even as the sole
        # candidate.
        other_chash = "5" * 64
        seed_manifest_chunks(coll, [other_chash])
        active_catalog.write_manifest(
            note,
            [{
                "chash": other_chash, "position": 0, "line_start": None,
                "line_end": None, "char_start": None, "char_end": None,
            }],
            collection=coll,
        )

        _seed_reverse_chunk(
            t3_db, collection=coll, content="not eligible", chunk_text_hash=chash,
        )

        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )

        assert result.docs_reverse_discovered == 0
        # The note's pre-existing manifest row is untouched; the reverse
        # chash was never granted.
        manifest = active_catalog.get_manifest(note)
        assert len(manifest) == 1
        assert manifest[0].chash == other_chash

    def test_reverse_tie_break_matches_census_route_on_both_levels(
        self, active_catalog, t3_db, t2_service_env,
    ):
        """nexus-wbfpw.8 (T2 nexus/review-rdr-192-phase1-code, finding 3):
        ``_reverse_note_owner_by_doc``'s tie-break docstring claims to
        mirror ``manifest_less_census.sql``'s ``rev_candidates`` CTE
        tie-break "exactly" (fewest manifest rows anywhere, THEN lowest
        tumbler), but nothing tested the two independently-maintained
        implementations against each other directly. ONE shared fixture,
        seeded once against the real engine substrate, exercises BOTH
        tie-break levels in the SAME collection and asserts the live
        census route's reported owner for each manifest-less chunk equals
        the note backfill actually manifests it into.

        Registration order is deliberately adversarial to the count-level
        case: the eventual LOSER (extra manifest row elsewhere) is
        registered FIRST (so it would win a tumbler-only comparison), and
        the eventual WINNER (zero rows) is registered SECOND (higher
        tumbler) -- so a correct tie-break can only reach the right answer
        by consulting manifest-row count first, exactly like
        ``test_reverse_tie_break_picks_fewest_manifest_rows_anywhere``
        above, but this test additionally cross-checks against the real
        census SQL rather than only against backfill's own bookkeeping.
        """
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection
        from nexus.catalog.tumbler import Tumbler
        from nexus.db.http_vector_client import HttpVectorClient
        from tests._catalog_fixture_ops import seed_manifest_chunks

        coll = _unique_coll("knowledge")

        # ── Level 1: manifest-row COUNT decides ─────────────────────────
        # note_count_loser is registered FIRST (lower tumbler) but carries
        # an unrelated manifest row elsewhere (total_count=1); note_count_
        # winner is registered SECOND (higher tumbler) with zero rows.
        # Only a count-first tie-break reaches the right answer here.
        chash_count = "6" * 64
        note_count_loser = _register_note_doc(
            active_catalog, coll, chash_count, title="wbfpw8-count-loser",
        )
        note_count_winner = _register_note_doc(
            active_catalog, coll, chash_count, title="wbfpw8-count-winner",
        )
        other_chash = "7" * 64
        seed_manifest_chunks(coll, [other_chash])
        active_catalog.write_manifest(
            note_count_loser,
            [{
                "chash": other_chash, "position": 0, "line_start": None,
                "line_end": None, "char_start": None, "char_end": None,
            }],
            collection=coll,
        )
        _seed_reverse_chunk(
            t3_db, collection=coll, content="count-level tie-break",
            chunk_text_hash=chash_count,
        )

        # ── Level 2: counts TIE at zero -- lowest tumbler decides ───────
        chash_tumbler = "8" * 64
        note_tumbler_a = _register_note_doc(
            active_catalog, coll, chash_tumbler, title="wbfpw8-tumbler-a",
        )
        note_tumbler_b = _register_note_doc(
            active_catalog, coll, chash_tumbler, title="wbfpw8-tumbler-b",
        )
        expected_tumbler_winner = str(min(
            Tumbler.parse(note_tumbler_a), Tumbler.parse(note_tumbler_b),
        ))
        expected_tumbler_loser = (
            note_tumbler_b if expected_tumbler_winner == note_tumbler_a
            else note_tumbler_a
        )
        _seed_reverse_chunk(
            t3_db, collection=coll, content="tumbler-level tie-break",
            chunk_text_hash=chash_tumbler,
        )

        # The real engine's own SQL (independent implementation) resolves
        # each chunk's owner BEFORE backfill writes anything -- read while
        # both chunks are still genuinely manifest-less.
        db = HttpVectorClient(tenant=t2_service_env)
        census = db.manifest_less_census(coll)
        assert census["owners"][chash_count] == {
            "owner_tumbler": note_count_winner, "owner_path": "reverse",
        }, census["owners"][chash_count]
        assert census["owners"][chash_tumbler] == {
            "owner_tumbler": expected_tumbler_winner, "owner_path": "reverse",
        }, census["owners"][chash_tumbler]

        # Backfill's Python-side tie-break, read via which document it
        # actually manifests each chash into.
        result = backfill_manifest_for_collection(
            active_catalog, t3_db, coll, dry_run=False,
        )
        assert result.docs_reverse_discovered == 2

        loser_manifest = active_catalog.get_manifest(note_count_loser)
        assert [r.chash for r in loser_manifest] == [other_chash]
        winner_manifest = active_catalog.get_manifest(note_count_winner)
        assert [r.chash for r in winner_manifest] == [chash_count]

        tumbler_winner_doc = (
            note_tumbler_a if expected_tumbler_winner == note_tumbler_a
            else note_tumbler_b
        )
        tumbler_loser_doc = (
            note_tumbler_b if tumbler_winner_doc == note_tumbler_a
            else note_tumbler_a
        )
        assert [r.chash for r in active_catalog.get_manifest(tumbler_winner_doc)] == [
            chash_tumbler,
        ]
        assert active_catalog.get_manifest(tumbler_loser_doc) == []

        # Cross-implementation agreement: the census's independently
        # computed owner for each chash is the SAME document backfill
        # picked.
        assert census["owners"][chash_count]["owner_tumbler"] == note_count_winner
        assert census["owners"][chash_tumbler]["owner_tumbler"] == tumbler_winner_doc
        assert expected_tumbler_loser == tumbler_loser_doc


