# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, count_documents, only_document



@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim ``catalog_path()`` at the dir ``_make_catalog`` initialises
    (nexus-aqbrk) — ActiveCatalog resolves through the same factories the
    PDF hook uses, and the SQLite arm of those reads ``catalog_path()``."""
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> tuple[Path, ActiveCatalog]:
    # nexus-i711w: the local Catalog.init that used to run here died with
    # the local catalog; the PDF hook registers into the live service catalog.
    catalog_dir = tmp_path / "catalog"
    return catalog_dir, ActiveCatalog()


@pytest.mark.t2_tenant
class TestPdfCatalogHook:
    def test_registers_pdf(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/papers/attention.pdf"),
            collection_name="docs__papers",
            title="Attention Is All You Need",
            author="Vaswani et al.",
            year=2017,
            corpus="papers",
        )
        # Should have created curator owner + document
        rows = (count_documents(),)
        assert rows[0] == 1
        rows = (only_document().title,)
        assert rows[0] == "Attention Is All You Need"

    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        # Should not raise
        _catalog_pdf_hook(
            pdf_path=Path("/data/test.pdf"),
            collection_name="docs__test",
            title="Test",
        )

    def test_uses_filename_when_no_title(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/my-paper.pdf"),
            collection_name="docs__test",
            title="",
        )
        rows = (only_document().title,)
        assert rows[0] == "my-paper"

    def test_update_on_reindex(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"),
            collection_name="docs__v1",
            title="Paper",
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"),
            collection_name="docs__v2",
            title="Paper",
        )
        # Should still be 1 document, updated collection
        rows = (count_documents(),)
        assert rows[0] == 1
        rows = (only_document().physical_collection,)
        assert rows[0] == "docs__v2"


@pytest.mark.t2_tenant
class TestPdfCatalogHookPhysicalCollectionRepointLogging:
    """nexus-t952k half 2: the tail reconcile branch above already stamps
    ``physical_collection`` unconditionally on every re-index — it must
    also LOG a real repoint (distinct from a no-op re-index into the same
    collection) so an operator/doctor pass can see it happened, instead of
    the AgenticScholar incident's silent drift (T2 nexus/index-
    agenticscholar-paper-2026-09-02: the row's physical_collection stayed
    stamped to a run's stale value with no trace)."""

    def test_reindex_into_different_collection_logs_repoint(self, tmp_path, monkeypatch):
        from structlog.testing import capture_logs

        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/t952k-repoint.pdf"),
            collection_name="docs__v1",
            title="Paper",
        )
        with capture_logs() as cap:
            _catalog_pdf_hook(
                pdf_path=Path("/data/t952k-repoint.pdf"),
                collection_name="docs__v2",
                title="Paper",
            )

        events = [
            e for e in cap
            if e.get("event") == "catalog_physical_collection_repointed"
        ]
        assert len(events) == 1, f"expected exactly one repoint log, got {cap}"
        assert events[0]["old_collection"] == "docs__v1"
        assert events[0]["new_collection"] == "docs__v2"
        assert events[0]["tumbler"] == str(only_document().tumbler)

    def test_failed_write_does_not_claim_a_repoint(self, tmp_path, monkeypatch):
        """critique [24114] S1: the log used to fire BEFORE the write, so a
        refused update left a 'repointed' claim in the log for a move that
        never happened. The log now follows the write."""
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/t952k-failed-write.pdf"),
            collection_name="docs__v1",
            title="Paper",
        )
        from nexus.catalog import factory as _factory

        real_writer = _factory.make_catalog_writer()

        class _RefusingWriter:
            def __getattr__(self, name):
                return getattr(real_writer, name)

            def update(self, *_a, **_kw):
                raise RuntimeError("engine refused the write")

        with capture_logs() as cap, patch.object(
            _factory, "make_catalog_writer", return_value=_RefusingWriter(),
        ):
            # The hook records catalog failures rather than propagating
            # them (record_catalog_hook_failure), so no raise is expected —
            # what must not happen is a 'repointed' claim for a row that
            # never moved.
            _catalog_pdf_hook(
                pdf_path=Path("/data/t952k-failed-write.pdf"),
                collection_name="docs__v2",
                title="Paper",
            )

        assert not [
            e for e in cap
            if e.get("event") == "catalog_physical_collection_repointed"
        ], "a failed write must not claim the row was repointed"
        assert only_document().physical_collection == "docs__v1"

    def test_reindex_into_same_collection_logs_nothing(self, tmp_path, monkeypatch):
        from structlog.testing import capture_logs

        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/t952k-nop.pdf"),
            collection_name="docs__v1",
            title="Paper",
        )
        with capture_logs() as cap:
            _catalog_pdf_hook(
                pdf_path=Path("/data/t952k-nop.pdf"),
                collection_name="docs__v1",
                title="Paper",
            )

        events = [
            e for e in cap
            if e.get("event") == "catalog_physical_collection_repointed"
        ]
        assert events == [], f"same-collection re-index must log nothing, got {cap}"


@pytest.mark.t2_tenant
class TestPdfCatalogHookTitleBackfill:
    """Mirror of nexus-ivzw8 for PDFs (papers/2512.11001.pdf, 2026-08-19).

    The RDR-102 D1 pre-flight registers every PDF with ``title=stem`` before
    extraction runs, so the post-index hook always takes the ``existing``
    branch — which wrote only chunk_count/indexed_at/mtime. The extracted
    title/author/year therefore NEVER reached the catalog for any PDF; it
    only looked right when the filename happened to be the title. The
    update branch must stem-guard backfill exactly like the markdown hook.
    """

    def test_reindex_backfills_stem_placeholder(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # pre-flight shape: no title yet -> stem placeholder
        _catalog_pdf_hook(
            pdf_path=Path("/data/2512.11001.pdf"),
            collection_name="knowledge__x",
            title="",
        )
        assert only_document().title == "2512.11001"

        _catalog_pdf_hook(
            pdf_path=Path("/data/2512.11001.pdf"),
            collection_name="knowledge__x",
            title="Rethinking Query Optimization for Multi-Agent Systems [Vision]",
            author="Zoi Kaoudi, Ioana Giurgiu",
            year=2025,
            chunk_count=78,
        )
        doc = only_document()
        assert doc.title == "Rethinking Query Optimization for Multi-Agent Systems [Vision]"
        assert doc.author == "Zoi Kaoudi, Ioana Giurgiu"
        assert doc.year == 2025
        assert doc.chunk_count == 78

    def test_reindex_never_clobbers_curated_title(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Curated Title", author="A. Author", year=2020,
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Re-extracted Different Title", author="B. Other", year=2021,
        )
        doc = only_document()
        assert doc.title == "Curated Title"
        assert doc.author == "A. Author"
        assert doc.year == 2020

    def test_reindex_with_empty_title_keeps_existing(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Real Title",
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="",
        )
        assert only_document().title == "Real Title"

    def test_normalised_stem_is_also_a_placeholder(self, tmp_path, monkeypatch):
        """resolve_pdf_title falls to derive_title's NORMALISED stem when no
        extractor/H1 title exists; that must not be backfilled-then-locked
        (nexus-ov5tc critique S1): a later real title still lands."""
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        p = Path("/data/attention-is-all.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="")   # pre-flight shape
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="Attention Is All")  # normalised stem
        assert only_document().title in ("attention-is-all", "Attention Is All")
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="Attention Is All You Need")
        assert only_document().title == "Attention Is All You Need"


# ── RDR-237 (nexus-tr1rj.3): the extraction record on the catalog document ───────────────────

_DEGRADED = {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [3, 7], "pymupdf-text": [12]}}
_CLEAN = {"extraction_method": "mineru", "extraction_pages": {}}


def _meta() -> dict:
    return dict(only_document().meta)


class TestPdfCatalogHookNamesTheRunsThatRecordNothing:
    """Every run on which the hook cannot write the extraction record logs one structured warning
    that names the document and says why."""

    def _warnings(self, cap, reason):
        return [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded" and e.get("reason") == reason]

    def test_no_catalog_reader(self, tmp_path, monkeypatch):
        from structlog.testing import capture_logs

        from nexus.catalog import factory as _factory
        from nexus.pipeline_stages import _catalog_pdf_hook

        monkeypatch.setattr(_factory, "make_catalog_reader", lambda: None)
        with capture_logs() as cap:
            _catalog_pdf_hook(
                pdf_path=Path("/data/papers/rdr237-noreader.pdf"), collection_name="docs__v1",
                title="Paper", source_uri="x-devonthink-item://U1", extraction=_DEGRADED,
            )
        (w,) = self._warnings(cap, "no_catalog_reader")
        assert w["log_level"] == "warning"
        assert w["file_path"].endswith("rdr237-noreader.pdf") and w["source_uri"] == "x-devonthink-item://U1"
        assert w["extraction_pages"] == _DEGRADED["extraction_pages"], "the lost record is in the log"

    def test_no_warning_when_there_was_no_extraction_to_lose(self, tmp_path, monkeypatch):
        from structlog.testing import capture_logs

        from nexus.catalog import factory as _factory
        from nexus.pipeline_stages import _catalog_pdf_hook

        monkeypatch.setattr(_factory, "make_catalog_reader", lambda: None)
        with capture_logs() as cap:
            _catalog_pdf_hook(pdf_path=Path("/data/papers/x.pdf"), collection_name="docs__v1", title="Paper")
        assert not [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded"]


class TestExtractionRecord:
    """``extraction_record`` turns an extraction result's metadata into what the hook writes."""

    def test_keeps_the_method_and_a_sorted_map(self):
        from nexus.pipeline_stages import extraction_record

        assert extraction_record({
            "extraction_method": "mineru+degraded",
            "extraction_pages": {"pymupdf-text": [12, 3], "mineru-no-formula": [7]},
            "page_count": 12,
        }) == {"extraction_method": "mineru+degraded",
               "extraction_pages": {"pymupdf-text": [3, 12], "mineru-no-formula": [7]}}

    def test_drops_empty_rungs_and_reads_a_missing_map_as_clean(self):
        from nexus.pipeline_stages import extraction_record

        assert extraction_record({"extraction_method": "mineru", "extraction_pages": {"unextracted": []}}) == {
            "extraction_method": "mineru", "extraction_pages": {}}
        # A non-ladder extractor reports no map: the record says "none degraded", never "unknown".
        assert extraction_record({"extraction_method": "pymupdf_normalized"}) == {
            "extraction_method": "pymupdf_normalized", "extraction_pages": {}}
        assert extraction_record(None) == {"extraction_method": "", "extraction_pages": {}}
        assert extraction_record({"extraction_pages": "garbage"}) == {"extraction_method": "", "extraction_pages": {}}

    @pytest.mark.parametrize("bad_pages", [["x", 3], [None, 3], [4.5, 3], [True, 3], "3,4"])
    def test_a_malformed_map_is_skipped_and_named_never_raised(self, bad_pages):
        """The streaming caller runs this outside any try, after all the work is done."""
        from structlog.testing import capture_logs

        from nexus.pipeline_stages import extraction_record

        with capture_logs() as cap:
            rec = extraction_record({
                "extraction_method": "mineru+degraded",
                "extraction_pages": {"mineru-no-formula": bad_pages, "pymupdf-text": [12]},
            })
        assert rec["extraction_method"] == "mineru+degraded"
        assert rec["extraction_pages"].get("pymupdf-text") == [12], "the well-formed rung survives"
        if isinstance(bad_pages, str):
            assert "mineru-no-formula" not in rec["extraction_pages"]
            events = {e["event"] for e in cap}
            assert events == {"extraction_record_skipped_a_malformed_rung"}
        else:
            assert rec["extraction_pages"]["mineru-no-formula"] == [3]
            assert {e["event"] for e in cap} == {"extraction_record_skipped_a_malformed_page"}
        assert all(e["log_level"] == "warning" for e in cap)


class TestCatalogBibFields:
    """nexus-6pfgb.28: what an ``--enrich`` lookup sends to the catalog document's columns. The engine
    sets every column it is sent, an empty string and a zero included, so only real values go; the
    document is written against the real service by ``tests/integration/test_rdr223_pdf_journey.py``."""

    def test_a_full_lookup_names_every_column_and_stamps_enriched_at(self):
        from nexus.pipeline_stages import _catalog_bib_fields

        fields = _catalog_bib_fields({
            "year": 2019, "venue": "OSDI", "authors": "A, B", "citation_count": 42,
            "semantic_scholar_id": "ss1", "references": ["r1"],
        })
        assert {k: v for k, v in fields.items() if k != "bib_enriched_at"} == {
            "bib_year": 2019, "bib_venue": "OSDI", "bib_authors": "A, B", "bib_citation_count": 42,
            "bib_semantic_scholar_id": "ss1"}
        assert fields["bib_enriched_at"]

    def test_a_miss_sends_nothing_and_so_stamps_nothing(self):
        from nexus.pipeline_stages import _catalog_bib_fields

        assert _catalog_bib_fields({}) == {}

    def test_empty_values_are_left_out_but_a_zero_citation_count_is_a_count(self):
        from nexus.pipeline_stages import _catalog_bib_fields

        fields = _catalog_bib_fields({"year": 0, "venue": "", "authors": "", "citation_count": 0,
                                      "semantic_scholar_id": ""})
        assert set(fields) == {"bib_citation_count", "bib_enriched_at"}
        assert fields["bib_citation_count"] == 0
