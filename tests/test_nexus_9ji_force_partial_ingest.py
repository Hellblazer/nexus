# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9ji regression — `nx index pdf --force` must break the
partial-ingest deadlock:

  (a) pipeline.db says the content_hash is "completed"  → `create_pipeline`
      returns "skip" → streaming path bails silently.
  (b) T3 has orphan chunks from a prior partial ingest → upsert races
      against orphaned metadata rows.

Pre-fix: `--force` was passed at the CLI, respected in the T3 staleness
check inside `_index_common`, but NEVER plumbed through to the streaming
`pipeline_index_pdf` path — so the streaming path silently no-op'd.

Post-fix contract:
  * `pipeline_index_pdf(..., force=True)` calls
    `db.delete_pipeline_data(content_hash)` before `create_pipeline`.
  * `pipeline_index_pdf(..., force=True)` also deletes orphan T3 chunks
    matching the content_hash in the target collection (so upsert has
    a clean slate, not a half-written prior attempt).
  * `index_pdf(..., force=True)` passes force through to
    `pipeline_index_pdf`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class _FakeServiceCollection:
    """Test double matching the REAL ``_ServiceCollectionStub`` contract
    exactly: ``delete(ids: list[str])`` positional-only, no ``where=``
    kwarg; ``get_all_metadata(where: dict | None = None)`` filters an
    in-memory row store.

    A bare ``MagicMock()`` (what the pre-fix version of this test file
    used) accepts ANY kwargs silently, including ``delete(where=...)`` --
    so it could never catch that the production code was calling a
    ``where=`` parameter the real client doesn't have. This double raises
    ``TypeError`` on that call shape exactly like the real service client
    does, and tracks real row state so tests can assert the POST-STATE of
    a cleanup (rows actually gone) rather than merely "some method was
    called with no exception."
    """

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = dict(rows)  # id -> metadata

    def get_all_metadata(self, where: dict | None = None) -> dict:
        if not where:
            ids = list(self.rows)
        else:
            ids = [
                i for i, m in self.rows.items()
                if all(m.get(k) == v for k, v in where.items())
            ]
        return {"ids": ids, "metadatas": [self.rows[i] for i in ids]}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self.rows.pop(i, None)


# ── pipeline.db "completed" state bypass ────────────────────────────────────


class TestPipelineStateBypass:

    def test_force_wipes_completed_pipeline_state(self, tmp_path: Path):
        """When pipeline.db already records a content_hash as 'completed',
        a force=True caller must see delete_pipeline_data called BEFORE
        create_pipeline so the new run isn't silently skipped."""
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, engine = make_fake_engine_db()
        # Seed: mark a content_hash as completed (aged heartbeat, as a
        # prior ingest would have left it)
        h = "a" * 64
        db.create_pipeline(h, str(tmp_path / "fake.pdf"), "knowledge__test")
        db.mark_completed(h)
        engine.pipelines[h]["updated_at"] = "2026-04-15T00:00:00+00:00"
        # Sanity: create_pipeline returns skip when not forced
        assert db.create_pipeline(h, "fake.pdf", "x") == "skip"

        # Post-fix: delete_pipeline_data wipes the row
        db.delete_pipeline_data(h)
        # create_pipeline now re-inserts as 'created'
        assert db.create_pipeline(h, "fake.pdf", "x") == "created"


# ── pipeline_index_pdf integration ──────────────────────────────────────────


class TestPipelineIndexPdfForce:

    @pytest.fixture
    def fake_pdf(self, tmp_path: Path) -> Path:
        """Minimal valid PDF so pymupdf can read its page count."""
        import pymupdf

        pdf_path = tmp_path / "test.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path

    def _seed_prior_completed(self, db, engine, content_hash: str, pdf_path: Path):
        """Seed the pipeline buffer as if a prior ingest marked this
        content_hash 'completed' with an aged heartbeat. Emulates the
        partial-ingest / force-race scenario."""
        db.create_pipeline(content_hash, str(pdf_path), "knowledge__reproducer")
        db.mark_completed(content_hash)
        engine.pipelines[content_hash]["updated_at"] = "2026-04-15T00:00:00+00:00"

    def test_force_false_still_skips_when_pipeline_completed(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """Default behaviour (no --force) is preserved: pipeline.db says
        completed → skip with no work done."""
        from nexus.pipeline_stages import pipeline_index_pdf
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, engine = make_fake_engine_db()
        h = "b" * 64
        self._seed_prior_completed(db, engine, h, fake_pdf)

        fake_t3 = MagicMock()
        result = pipeline_index_pdf(
            fake_pdf, h, "knowledge__reproducer", fake_t3, db=db,
        )
        assert result == 0
        # Pipeline row is untouched
        row = db.get_pipeline_state(h)
        assert row["status"] == "completed"

    def test_force_true_bypasses_completed_pipeline(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """force=True wipes pipeline.db row + T3 orphans, then runs."""
        from nexus.pipeline_stages import pipeline_index_pdf
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, engine = make_fake_engine_db()
        h = "c" * 64
        self._seed_prior_completed(db, engine, h, fake_pdf)

        fake_t3 = MagicMock()
        fake_col = MagicMock()
        fake_t3.get_or_create_collection.return_value = fake_col
        # Stub the embed_fn so we don't need Voyage credentials
        fake_embed = lambda texts, model: ([[0.1] * 1024] * len(texts), model)

        # Stub extractor + chunker stages so the test doesn't actually
        # exercise the streaming pipeline — we only care about the
        # "skip was bypassed" signal.
        # Return a mock ExtractionResult with a usable metadata dict so
        # post-passes don't AttributeError on a None return.
        fake_extraction = MagicMock()
        fake_extraction.metadata = {"table_regions": []}

        with patch(
            "nexus.pipeline_stages.extractor_loop", return_value=fake_extraction,
        ), patch(
            "nexus.pipeline_stages.chunker_loop", return_value=None,
        ), patch(
            "nexus.pipeline_stages.uploader_loop", return_value=0,
        ), patch(
            "nexus.pipeline_stages._enrich_metadata_from_extraction",
            return_value=True,
        ), patch(
            "nexus.pipeline_stages._update_chunk_metadata",
            return_value=None,
        ):
            pipeline_index_pdf(
                fake_pdf, h, "knowledge__reproducer", fake_t3,
                db=db, embed_fn=fake_embed, force=True,
            )

        # After a successful run the post-passes call delete_pipeline_data,
        # so the row is gone. That is the proof point: the seeded
        # 'completed' row did NOT block the run — force wiped it, the
        # pipeline ran to completion, and the post-pass cleaned up. A
        # zero-row result here means force bypassed the skip; a
        # still-'completed'-from-seed row would mean it didn't.
        row = db.get_pipeline_state(h)
        if row is not None:
            assert row["updated_at"] > "2026-04-15T00:00:00Z", (
                f"force=True did not wipe the seeded 'completed' state. "
                f"Got: {row!r}"
            )

    def test_force_true_deletes_t3_orphan_chunks(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """force=True must ACTUALLY delete the T3 chunks matching this
        content_hash before re-upload -- proven by POST-STATE (the rows
        are gone from the collection), not merely "some method fired
        without raising."

        Uses ``_FakeServiceCollection``, whose ``delete()`` only accepts a
        positional ``ids: list[str]`` (the REAL ``_ServiceCollectionStub``
        contract). The pre-fix production code called
        ``col.delete(where={"content_hash": content_hash})`` -- against
        this double that raises ``TypeError`` exactly as it does against
        the real service client, so this test is red on the pre-fix code
        (the TypeError propagates instead of being silently swallowed,
        since this test's own call is not wrapped in the production
        try/except that used to exist) and green once the cleanup
        resolves ids via ``get_all_metadata`` and deletes those ids.
        """
        from nexus.pipeline_stages import pipeline_index_pdf
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, _engine = make_fake_engine_db()
        h = "d" * 64

        fake_col = _FakeServiceCollection({
            "orphan_0": {"content_hash": h},
            "orphan_1": {"content_hash": h},
            "keep_0": {"content_hash": "unrelated" + h},
        })
        fake_t3 = MagicMock()
        fake_t3.get_or_create_collection.return_value = fake_col

        # Return a mock ExtractionResult with a usable metadata dict so
        # post-passes don't AttributeError on a None return.
        fake_extraction = MagicMock()
        fake_extraction.metadata = {"table_regions": []}

        with patch(
            "nexus.pipeline_stages.extractor_loop", return_value=fake_extraction,
        ), patch(
            "nexus.pipeline_stages.chunker_loop", return_value=None,
        ), patch(
            "nexus.pipeline_stages.uploader_loop", return_value=0,
        ), patch(
            "nexus.pipeline_stages._enrich_metadata_from_extraction",
            return_value=True,
        ), patch(
            "nexus.pipeline_stages._update_chunk_metadata",
            return_value=None,
        ):
            pipeline_index_pdf(
                fake_pdf, h, "knowledge__reproducer", fake_t3,
                db=db, embed_fn=lambda t, m: ([[0.0] * 1024] * len(t), m),
                force=True,
            )

        # Post-state: the two orphan chunks sharing this content_hash are
        # GONE; the unrelated chunk (different content_hash) survives.
        assert "orphan_0" not in fake_col.rows, "matching orphan chunk was not deleted"
        assert "orphan_1" not in fake_col.rows, "matching orphan chunk was not deleted"
        assert "keep_0" in fake_col.rows, "unrelated chunk must not be swept up"

    def test_force_true_no_orphans_is_quiet_success(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """force=True with NO matching T3 orphans must succeed quietly --
        no exception, no chunks touched. A cleanup that raises on a
        legitimately empty result would make every --force ingest of a
        brand-new content_hash fail."""
        from nexus.pipeline_stages import pipeline_index_pdf
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, _engine = make_fake_engine_db()
        h = "f" * 64

        fake_col = _FakeServiceCollection({
            "keep_0": {"content_hash": "unrelated" + h},
        })
        fake_t3 = MagicMock()
        fake_t3.get_or_create_collection.return_value = fake_col

        fake_extraction = MagicMock()
        fake_extraction.metadata = {"table_regions": []}

        with patch(
            "nexus.pipeline_stages.extractor_loop", return_value=fake_extraction,
        ), patch(
            "nexus.pipeline_stages.chunker_loop", return_value=None,
        ), patch(
            "nexus.pipeline_stages.uploader_loop", return_value=0,
        ), patch(
            "nexus.pipeline_stages._enrich_metadata_from_extraction",
            return_value=True,
        ), patch(
            "nexus.pipeline_stages._update_chunk_metadata",
            return_value=None,
        ):
            # Must not raise.
            pipeline_index_pdf(
                fake_pdf, h, "knowledge__reproducer", fake_t3,
                db=db, embed_fn=lambda t, m: ([[0.0] * 1024] * len(t), m),
                force=True,
            )

        assert "keep_0" in fake_col.rows, "unrelated chunk must survive an empty cleanup"

    def test_force_true_orphan_cleanup_failure_propagates(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """A genuine T3 orphan-cleanup failure must be LOUD, not swallowed
        into a warning. Pre-fix, a bare ``except Exception`` turned every
        failure here (including a plain ``TypeError`` programming error)
        into a ``force_t3_orphan_cleanup_failed`` warning and let the
        pipeline continue -- so ``--force`` silently did NOT break the
        deadlock it exists to break, with no signal to the caller."""
        from nexus.pipeline_stages import pipeline_index_pdf
        from tests.pipeline_fake_engine import make_fake_engine_db

        db, _engine = make_fake_engine_db()
        h = "g" * 64

        fake_col = MagicMock()
        fake_col.get_all_metadata.side_effect = RuntimeError("store service unreachable")
        fake_t3 = MagicMock()
        fake_t3.get_or_create_collection.return_value = fake_col

        with pytest.raises(RuntimeError, match="store service unreachable"):
            pipeline_index_pdf(
                fake_pdf, h, "knowledge__reproducer", fake_t3,
                db=db, embed_fn=lambda t, m: ([[0.0] * 1024] * len(t), m),
                force=True,
            )


# ── CLI plumbing ────────────────────────────────────────────────────────────


class TestIndexPdfPassesForce:
    """force from the CLI must reach pipeline_index_pdf."""

    def test_index_pdf_forwards_force_to_streaming_path(
        self, tmp_path: Path,
    ):
        """index_pdf(force=True) must forward force to pipeline_index_pdf."""
        import pymupdf
        from nexus.doc_indexer import index_pdf

        # Minimal valid PDF
        pdf_path = tmp_path / "t.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        fake_t3 = MagicMock()
        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "metadatas": []}
        fake_t3.get_or_create_collection.return_value = fake_col

        captured: dict = {}
        def fake_pipeline(*args, **kwargs):
            captured.update(kwargs)
            return 0

        with patch(
            "nexus.pipeline_stages.pipeline_index_pdf",
            side_effect=fake_pipeline,
        ):
            index_pdf(
                pdf_path, "reproducer", t3=fake_t3, force=True,
                collection_name="knowledge__reproducer",
                embed_fn=lambda t, m: ([[0.0]] * len(t), m),
            )

        assert captured.get("force") is True, (
            f"index_pdf(force=True) must pass force=True through to "
            f"pipeline_index_pdf. Got kwargs: {captured!r}"
        )
