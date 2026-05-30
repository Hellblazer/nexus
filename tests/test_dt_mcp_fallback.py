# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gap-0 fallback suite: every DT layer degrades EXACTLY when DT is absent (RDR-139).

The contract (RDR §Optionality and Fallback Contract) is per-layer and EXACT,
not "no crash":

- **Layer B (linking)**: zero new edges — the catalog edge set after a run with
  ``available()=False`` equals the edge set before; no ``created_by=dt_*`` rows.
- **Layer F (write-back)**: no DT-side mutation; index/enrich result unchanged,
  exit 0.  *(Added when P1.7 Layer F lands — nexus-x70wg.)*
- **Layer C (bib enrich)**: DT-CrossRef gap-fill is a no-op — the resolved bib
  dict equals the primary-backend-only result; ``dt_resolve_doi`` is never
  called.  *(Added when P2.1 Layer C lands — nexus-8h9t5.)*
- **Layer D (content)**: non-file-backed records are skipped (no chunks
  written, ``index_markdown`` never called); file-backed chunks carry NO
  ``extraction_source`` key (absent == file).  *(P2.2 Layer D — nexus-t62jy.)*

Integration over mocks: a real ``Catalog`` on tmp SQLite; only the DT client's
``available()`` is forced False (the genuine fallback trigger).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.dt_link_generator import generate_dt_links
from nexus.dt_writeback import writeback_record


@pytest.fixture
def cat(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


class _UnavailableDT:
    """DT client whose server is down. Any read/write helper would be a bug to call."""

    def available(self, *, refresh=False):
        return False

    def dt_find_similar(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("dt_find_similar called despite available()=False")

    def dt_record_links(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_record_links called despite available()=False")

    def dt_call(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_call called despite available()=False")

    def dt_set_tags(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_set_tags called despite available()=False")

    def dt_set_annotation(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_set_annotation called despite available()=False")

    def dt_set_custom_metadata(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_set_custom_metadata called despite available()=False")

    def dt_annotation_text(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_annotation_text called despite available()=False")

    def dt_extract_content(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_extract_content called despite available()=False")


@pytest.fixture
def indexed(cat):
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    this = cat.register(
        owner, "this", content_type="paper", file_path="",
        source_uri="x-devonthink-item://THIS",
    )
    other = cat.register(
        owner, "other", content_type="paper", file_path="",
        source_uri="x-devonthink-item://A",
    )
    # A pre-existing non-DT edge: the fallback must leave it untouched.
    cat.link_if_absent(this, other, "relates", created_by="auto_linker")
    return cat, this, other


def _edge_snapshot(cat: Catalog, tumbler):
    return sorted(
        (str(e.from_tumbler), str(e.to_tumbler), e.link_type, e.created_by)
        for e in cat.links_from(tumbler)
    )


class TestLayerBFallback:
    """Layer B: DT absent → zero new edges; pre-existing edge set unchanged."""

    def test_zero_new_edges_when_unavailable(self, indexed):
        cat, this, _other = indexed
        before = _edge_snapshot(cat, this)
        counts = generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())
        after = _edge_snapshot(cat, this)
        assert counts == {"similar": 0, "link": 0}
        assert after == before  # EXACT: edge set unchanged, not merely "no crash"

    def test_no_dt_attributed_rows_added(self, indexed):
        cat, this, _other = indexed
        generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())
        assert cat.link_query(created_by="dt_similar") == []
        assert cat.link_query(created_by="dt_link") == []

    def test_no_read_helper_invoked_when_unavailable(self, indexed):
        # _UnavailableDT raises if any read helper is called; reaching here proves
        # the available() gate short-circuits before any DT read.
        cat, this, _other = indexed
        generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())


class TestLayerFFallback:
    """Layer F: DT absent → no DT-side mutation; write-back is skipped, exit clean."""

    def test_writeback_skipped_no_mutation_when_unavailable(self):
        # _UnavailableDT raises if any write helper is reached; the available()
        # gate must short-circuit before any DT write is attempted.
        out = writeback_record("U", "1.2.3", dt_client=_UnavailableDT())
        assert out == {"tags": False, "annotation": False, "metadata": False, "skipped": True}

    def test_writeback_with_keywords_still_skips_when_unavailable(self):
        out = writeback_record(
            "U", "1.2.3", aspect_keywords=["TPC-C", "RAG"], dt_client=_UnavailableDT()
        )
        assert out["skipped"] is True
        assert not any(out[k] for k in ("tags", "annotation", "metadata"))


class TestLayerCFallback:
    """Layer C: DT absent → bib gap-fill is a no-op; primary bib unchanged."""

    def test_dt_crossref_bib_empty_when_dt_down(self):
        # dt_resolve_doi returns None when DT is unreachable → empty supplement.
        from nexus.commands.enrich import _dt_crossref_bib
        with patch("nexus.mcp_client.devonthink.dt_resolve_doi", return_value=None):
            assert _dt_crossref_bib("10.1/x") == {}

    def test_merge_identity_when_supplement_empty(self):
        # An empty DT supplement leaves the primary bib EXACTLY as-is.
        from nexus.commands.enrich import _merge_bib_gapfill
        primary = {"year": 2024, "venue": "VLDB", "authors": "A", "doi": "10.1/A"}
        assert _merge_bib_gapfill(primary, {}) == primary

    def test_enrich_source_dt_makes_no_dt_call_when_unavailable(self):
        # CLI path: available()=False → dt_resolve_doi is never invoked, the
        # primary miss is skipped, exit 0 (exact pre-RDR-139 behaviour).
        from unittest.mock import MagicMock

        from click.testing import CliRunner
        from nexus.commands.enrich import enrich

        meta = {"title": "P", "source_path": "/p.pdf"}
        with patch("nexus.mcp_client.devonthink.available", return_value=False), \
             patch("nexus.mcp_client.devonthink.dt_resolve_doi") as mock_resolve, \
             patch("nexus.bib_enricher_openalex.enrich", return_value={}), \
             patch("nexus.bib_enricher_openalex.enrich_by_doi", return_value={}), \
             patch("nexus.db.make_t3") as mock_t3, \
             patch("nexus.retry._chroma_with_retry") as mock_retry:
            mock_retry.side_effect = [
                {"ids": ["c1"], "metadatas": [dict(meta)]},
                {"ids": ["c1"], "documents": ["body"], "metadatas": [dict(meta)]},
            ]
            mock_t3.return_value.get_or_create_collection.return_value = MagicMock()
            res = CliRunner().invoke(
                enrich, ["bib", "knowledge__t", "--delay", "0", "--source", "dt"],
            )
            assert res.exit_code == 0, res.output
            mock_resolve.assert_not_called()
            assert "1 titles had no" in res.output


class TestLayerDFallback:
    """Layer D: DT absent → non-file-backed records skipped; file chunks
    carry no extraction_source key (absent == file)."""

    def test_dt_content_record_skipped_when_dt_down(self):
        from nexus.commands.dt import _index_dt_content_record
        with patch("nexus.doc_indexer.index_markdown") as idx, \
             patch("nexus.mcp_client.devonthink.dt_extract_content", return_value=None):
            assert _index_dt_content_record("U", collection="c", corpus="dt") is False
            idx.assert_not_called()  # never reached the chunking pipeline

    def test_file_backed_chunk_has_no_extraction_source(self):
        from nexus.metadata_schema import make_chunk_metadata
        meta = make_chunk_metadata(
            content_type="markdown",
            chunk_text_hash="a" * 64,
            content_hash="b" * 64,
            indexed_at="2026-05-30T00:00:00Z",
            embedding_model="voyage-context-3",
        )
        assert "extraction_source" not in meta  # absent == file
