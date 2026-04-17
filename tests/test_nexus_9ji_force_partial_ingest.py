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


# ── pipeline.db "completed" state bypass ────────────────────────────────────


class TestPipelineStateBypass:

    def test_force_wipes_completed_pipeline_state(self, tmp_path: Path):
        """When pipeline.db already records a content_hash as 'completed',
        a force=True caller must see delete_pipeline_data called BEFORE
        create_pipeline so the new run isn't silently skipped."""
        from nexus.pipeline_buffer import PipelineDB

        db_path = tmp_path / "pipeline.db"
        db = PipelineDB(db_path)
        # Seed: mark a content_hash as completed in pipeline.db
        h = "a" * 64
        conn = db._conn()
        conn.execute(
            "INSERT INTO pdf_pipeline "
            "(content_hash, pdf_path, collection, status, started_at, "
            " updated_at) VALUES (?, ?, ?, 'completed', ?, ?)",
            (h, str(tmp_path / "fake.pdf"), "knowledge__test",
             "2026-04-15T00:00:00Z", "2026-04-15T00:00:00Z"),
        )
        conn.commit()
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

    def _seed_prior_completed(self, db, content_hash: str, pdf_path: Path):
        """Seed pipeline.db as if a prior ingest marked this content_hash
        'completed'. Emulates the partial-ingest / force-race scenario."""
        conn = db._conn()
        conn.execute(
            "INSERT INTO pdf_pipeline "
            "(content_hash, pdf_path, collection, status, started_at, "
            " updated_at) VALUES (?, ?, ?, 'completed', ?, ?)",
            (content_hash, str(pdf_path), "knowledge__reproducer",
             "2026-04-15T00:00:00Z", "2026-04-15T00:00:00Z"),
        )
        conn.commit()

    def test_force_false_still_skips_when_pipeline_completed(
        self, tmp_path: Path, fake_pdf: Path,
    ):
        """Default behaviour (no --force) is preserved: pipeline.db says
        completed → skip with no work done."""
        from nexus.pipeline_buffer import PipelineDB
        from nexus.pipeline_stages import pipeline_index_pdf

        db = PipelineDB(tmp_path / "pipeline.db")
        h = "b" * 64
        self._seed_prior_completed(db, h, fake_pdf)

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
        from nexus.pipeline_buffer import PipelineDB
        from nexus.pipeline_stages import pipeline_index_pdf

        db = PipelineDB(tmp_path / "pipeline.db")
        h = "c" * 64
        self._seed_prior_completed(db, h, fake_pdf)

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
        """force=True must delete T3 chunks matching this content_hash
        before re-upload so orphans from a partial prior ingest don't
        race with the new chunks."""
        from nexus.pipeline_buffer import PipelineDB
        from nexus.pipeline_stages import pipeline_index_pdf

        db = PipelineDB(tmp_path / "pipeline.db")
        h = "d" * 64

        fake_t3 = MagicMock()
        fake_col = MagicMock()
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

        # The collection should have received a pre-flight delete scoped
        # to this content_hash.
        assert fake_col.delete.called, (
            "force=True must call col.delete to purge T3 orphan chunks"
        )
        # The delete call should scope to this content_hash (via where
        # clause) rather than wiping the whole collection.
        delete_kwargs = fake_col.delete.call_args.kwargs
        where = delete_kwargs.get("where") or {}
        assert where.get("content_hash") == h, (
            f"force delete should scope to content_hash={h!r}; "
            f"got where={where!r}"
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
