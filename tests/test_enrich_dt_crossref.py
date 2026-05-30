# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer C — DT-CrossRef bibliographic enrichment (gap-fill-only).

The contract under test (locked in the RDR §Approach Layer C):

* DT-CrossRef is the **lowest-precedence** bib source (S2 > OpenAlex >
  DT-CrossRef). It fills a ``bib_*`` field **only** when the primary backend
  left that field empty/zero, and **never** overwrites a value the primary
  set, even a differently-formatted DOI.
* The guard is per-field (``if not merged.get(k):``), not call-ordering.
* DT unavailable degrades to exact primary-backend-only behaviour (Gap 0).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.commands.enrich import (
    _dt_crossref_bib,
    _merge_bib_gapfill,
    enrich,
)


# --- _dt_crossref_bib: map the live CrossRef flat dict -> nexus bib dict ----

def test_dt_crossref_bib_maps_live_crossref_shape() -> None:
    """The live ``resolve_doi_metadata`` shape is a flat dict; year is a
    string, authors a list. Map to the nexus bib dict (year int, authors
    joined, venue from journal, citation_count absent -> 0)."""
    raw = {
        "doi": "10.1038/nature12373",
        "title": "Nanometre-scale thermometry in a living cell",
        "authors": ["G. Kucsko", "P. C. Maurer", "N. Y. Yao", "M. Kubo",
                     "H. J. Noh", "P. K. Lo"],
        "journal": "Nature",
        "year": "2013",
    }
    with patch("nexus.mcp_client.devonthink.dt_resolve_doi", return_value=raw):
        bib = _dt_crossref_bib("10.1038/nature12373")
    assert bib["year"] == 2013  # string -> int
    assert bib["venue"] == "Nature"
    assert bib["doi"] == "10.1038/nature12373"
    # authors joined, capped at first 5 to match the S2/OpenAlex format
    assert bib["authors"] == "G. Kucsko, P. C. Maurer, N. Y. Yao, M. Kubo, H. J. Noh"
    assert bib["citation_count"] == 0  # CrossRef provides none


def test_dt_crossref_bib_unresolvable_returns_empty() -> None:
    """DT miss / unavailable -> empty dict (no exception)."""
    with patch("nexus.mcp_client.devonthink.dt_resolve_doi", return_value=None):
        assert _dt_crossref_bib("10.0/nope") == {}


def test_dt_crossref_bib_handles_nonnumeric_year() -> None:
    """A malformed year coerces to 0 rather than raising."""
    raw = {"doi": "10.1/x", "journal": "J", "authors": [], "year": "n.d."}
    with patch("nexus.mcp_client.devonthink.dt_resolve_doi", return_value=raw):
        bib = _dt_crossref_bib("10.1/x")
    assert bib["year"] == 0


# --- _merge_bib_gapfill: per-field precedence guard -------------------------

def test_merge_gapfill_fills_only_empty_fields() -> None:
    primary = {"year": 0, "venue": "", "doi": "10.1/A", "citation_count": 7}
    supplement = {"year": 2013, "venue": "Nature", "doi": "10.1/a",
                  "citation_count": 0}
    merged = _merge_bib_gapfill(primary, supplement)
    # gaps filled
    assert merged["year"] == 2013
    assert merged["venue"] == "Nature"
    # set values untouched — DOI keeps the primary's form, citation_count kept
    assert merged["doi"] == "10.1/A"
    assert merged["citation_count"] == 7


def test_merge_gapfill_partial_s2_keeps_doi_fills_year() -> None:
    """The locked partial-precedence case: primary set bib_doi, left year
    empty; DT resolves a DIFFERENT DOI form and a year. DT fills only the
    year; the primary's DOI is unchanged."""
    primary = {"doi": "10.1038/Nature12373", "year": 0, "venue": "",
               "authors": ""}
    dt = {"doi": "10.1038/nature12373", "year": 2013, "venue": "Nature",
          "authors": "G. Kucsko"}
    merged = _merge_bib_gapfill(primary, dt)
    assert merged["doi"] == "10.1038/Nature12373"  # primary form, NOT overwritten
    assert merged["year"] == 2013
    assert merged["venue"] == "Nature"
    assert merged["authors"] == "G. Kucsko"


def test_merge_gapfill_no_primary_hit_fills_all() -> None:
    """No S2 hit (primary empty) -> DT supplies every field (enhanced MVV)."""
    merged = _merge_bib_gapfill({}, {"year": 2013, "venue": "Nature",
                                     "doi": "10.1/a", "authors": "X"})
    assert merged == {"year": 2013, "venue": "Nature", "doi": "10.1/a",
                      "authors": "X"}


def test_merge_gapfill_never_inserts_falsy_supplement() -> None:
    """A falsy supplement value does not create or clobber a key."""
    merged = _merge_bib_gapfill({"year": 0}, {"year": 0, "venue": ""})
    assert merged["year"] == 0
    assert merged.get("venue", "MISSING") == "MISSING"


# --- CLI wiring: nx enrich bib --source dt ----------------------------------

_CHUNK_META = {"title": "Paper A", "source_path": "/papers/a.pdf"}
_DOI = "10.1038/nature12373"


def _two_chunk_collection(mock_retry: MagicMock) -> MagicMock:
    """Wire _chroma_with_retry for a single-title two-chunk collection."""
    mock_retry.side_effect = [
        {"ids": ["c1", "c2"], "metadatas": [dict(_CHUNK_META), dict(_CHUNK_META)]},
        # identifier scan (documents + metadatas) — DOI lives in the body text
        {"ids": ["c1", "c2"],
         "documents": [f"Title.\nDOI: {_DOI}\nAbstract...", "more"],
         "metadatas": [dict(_CHUNK_META), dict(_CHUNK_META)]},
        # re-fetch before update
        {"ids": ["c1", "c2"], "metadatas": [dict(_CHUNK_META), dict(_CHUNK_META)]},
        None,  # col.update
    ]
    return mock_retry


@patch("nexus.mcp_client.devonthink.available", return_value=True)
@patch("nexus.mcp_client.devonthink.dt_resolve_doi")
@patch("nexus.bib_enricher_openalex.enrich")
@patch("nexus.bib_enricher_openalex.enrich_by_doi")
@patch("nexus.retry._chroma_with_retry")
@patch("nexus.db.make_t3")
def test_source_dt_gapfills_when_primary_misses(
    mock_t3: MagicMock,
    mock_retry: MagicMock,
    mock_by_doi: MagicMock,
    mock_title: MagicMock,
    mock_dt_resolve: MagicMock,
    mock_avail: MagicMock,
    monkeypatch,
) -> None:
    """``--source dt`` with no OpenAlex hit but a resolvable DOI writes
    bib_* from DT-CrossRef."""
    monkeypatch.delenv("S2_API_KEY", raising=False)  # pin primary -> openalex
    mock_by_doi.return_value = {}     # primary DOI lookup misses
    mock_title.return_value = {}      # primary title search misses
    mock_dt_resolve.return_value = {
        "doi": _DOI, "year": "2013", "journal": "Nature",
        "authors": ["X", "Y"],
    }
    _two_chunk_collection(mock_retry)
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = MagicMock()
    mock_t3.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich, ["bib", "knowledge__t", "--delay", "0", "--source", "dt"]
    )
    assert result.exit_code == 0, result.output
    assert "enriched 2 chunks across 1 titles" in result.output
    mock_dt_resolve.assert_called_once()
    # the update payload carried DT's year
    update_call = [c for c in mock_retry.call_args_list
                   if "metadatas" in c.kwargs]
    assert update_call, "expected a col.update with metadatas"
    assert update_call[-1].kwargs["metadatas"][0]["bib_year"] == 2013


@patch("nexus.mcp_client.devonthink.available", return_value=False)
@patch("nexus.bib_enricher_openalex.enrich")
@patch("nexus.bib_enricher_openalex.enrich_by_doi")
@patch("nexus.retry._chroma_with_retry")
@patch("nexus.db.make_t3")
def test_source_dt_unavailable_degrades_to_primary_only(
    mock_t3: MagicMock,
    mock_retry: MagicMock,
    mock_by_doi: MagicMock,
    mock_title: MagicMock,
    mock_avail: MagicMock,
    monkeypatch,
) -> None:
    """DT absent -> exact primary-backend-only behaviour: a primary miss is
    skipped, no DT call, exit 0 (Gap 0)."""
    monkeypatch.delenv("S2_API_KEY", raising=False)  # pin primary -> openalex
    mock_by_doi.return_value = {}
    mock_title.return_value = {}
    mock_retry.side_effect = [
        {"ids": ["c1"], "metadatas": [dict(_CHUNK_META)]},
        {"ids": ["c1"], "documents": ["body"], "metadatas": [dict(_CHUNK_META)]},
    ]
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = MagicMock()
    mock_t3.return_value = mock_db

    with patch("nexus.mcp_client.devonthink.dt_resolve_doi") as mock_dt:
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["bib", "knowledge__t", "--delay", "0", "--source", "dt"]
        )
        assert result.exit_code == 0, result.output
        assert "1 titles had no" in result.output
        mock_dt.assert_not_called()
