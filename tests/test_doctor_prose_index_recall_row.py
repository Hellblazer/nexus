# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Prose collections at the index threshold" row (nexus-qqoak, RDR-227 residual).

A collection at or above ``NX_SEARCH_EXACT_MAX_ROWS`` rows (default 20,000) is routed to its own HNSW index. For
prose that index's recall was measured under the 0.99 target at 10,308 and 14,300 rows and not at all above 20,000
(T2 ``nexus_rdr/227-research-12``), and nothing checked that no prose collection had crossed. The ``GET /v1/status``
object carries counts only, so the row reads the collection listing.

The rows below have the shape ``HttpVectorClient.list_collections`` returns: ``name``, live ``count`` and physical
``stored_count``. Not-applicable cases come first: a new row on a virgin box must stay green.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.health import DEFAULT_SEARCH_EXACT_MAX_ROWS, _check_prose_index_recall

_ENV = "NX_SEARCH_EXACT_MAX_ROWS"
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_threshold_env(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


def _col(name: str, rows: int, *, live: int | None = None, kind: str | None = None) -> dict:
    """A listing row. ``content_type`` is the catalog attribute the stats route joins onto every registered
    collection; it defaults to the name's first segment, as the engine's catalog row has it."""
    return {"name": name, "count": rows if live is None else live, "stored_count": rows,
            "content_type": name.split("__", 1)[0] if kind is None else kind}


def _row(collections):
    rows = _check_prose_index_recall(collections)
    assert len(rows) == 1
    assert rows[0].label == "Prose collections at the index threshold"
    return rows[0]


# ── not applicable, and green ────────────────────────────────────────────────


def test_an_empty_or_unreadable_listing_is_not_applicable_and_green() -> None:
    for listing in ([], None, "x", {"name": "docs__a"}):
        r = _row(listing)
        assert r.ok is True and not r.warn and "not applicable" in r.detail, listing


def test_a_listing_that_raises_is_not_applicable_and_green() -> None:
    with patch("nexus.db.make_t3", side_effect=RuntimeError("no engine")):
        (r,) = _check_prose_index_recall()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_virgin_box_listing_nothing_is_not_applicable_and_green() -> None:
    class _Virgin:
        def list_collections(self):
            return []  # HttpVectorClient.list_collections degrades to [] when the engine is down or empty

    with patch("nexus.db.make_t3", return_value=_Virgin()):
        (r,) = _check_prose_index_recall()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


# ── the three acceptance cases ───────────────────────────────────────────────


def test_a_prose_collection_at_the_threshold_warns_naming_it_and_its_count_and_what_to_run() -> None:
    r = _row([_col("rdr__1-79__voyage-context-3__v1", 20_000)])
    assert r.ok is False and r.warn is True
    assert "rdr__1-79__voyage-context-3__v1" in r.detail and "20,000 rows" in r.detail
    fix = " ".join(r.fix_suggestions)
    assert "RDR-227 Gap 1 recall check" in fix and "nexus_rdr/227-research-12" in fix
    assert "rdr-227-per-collection-hnsw-above-the-router-threshold.md" in fix


def test_a_code_collection_at_or_above_the_threshold_does_not_warn() -> None:
    for rows in (20_000, 29_355, 59_257):
        r = _row([_col("code__1-20__voyage-code-3__v1", rows)])
        assert r.ok is True and not r.warn, rows


def test_a_prose_collection_below_the_threshold_does_not_warn() -> None:
    r = _row([_col("docs__1-2__voyage-context-3__v1", 19_999), _col("rdr__1-79__voyage-context-3__v1", 14_300)])
    assert r.ok is True and not r.warn


# ── which collections are prose, and which count ─────────────────────────────


def test_docs_rdr_and_knowledge_are_prose_and_are_all_named_largest_first() -> None:
    r = _row([_col("docs__a__voyage-context-3__v1", 21_000), _col("knowledge__b__voyage-context-3__v1", 40_000),
              _col("rdr__c__voyage-context-3__v1", 30_000), _col("code__d__voyage-code-3__v1", 90_000)])
    assert r.warn is True
    assert "code__d" not in r.detail
    assert r.detail.index("knowledge__b") < r.detail.index("rdr__c") < r.detail.index("docs__a")


def test_the_catalog_content_type_decides_not_the_name() -> None:
    # RDR-204: the catalog row is the authority; the name is never parsed (the collection-name parse census counts it).
    assert _row([_col("misc__a__m__v1", 30_000, kind="docs")]).warn is True
    assert not _row([_col("docs__a__m__v1", 30_000, kind="code")]).warn


def test_a_collection_with_no_catalog_content_type_is_counted_and_said_not_silently_passed() -> None:
    bare = {"name": "docs__a__m__v1", "count": 90_000, "stored_count": 90_000}  # no catalog row: no content_type
    # Everything unclassified: the row cannot judge anything, so it is not applicable, never a green pass.
    r = _row([bare])
    assert r.ok is True and not r.warn and "not applicable" in r.detail and "content types" in r.detail
    # Some classified: the unchecked ones are named by count in a pass and in a warning.
    r = _row([bare, _col("docs__b__m__v1", 5)])
    assert r.ok is True and not r.warn and "1 of 2 collections have no catalog content type" in r.detail
    r = _row([bare, _col("docs__b__m__v1", 25_000)])
    assert r.warn is True and "1 of 2 collections have no catalog content type" in r.detail


def test_the_physical_count_is_compared_not_the_live_one() -> None:
    # Tombstoned chunks still sit in the leaf the router counts; the listing's live count is lower.
    assert _row([_col("docs__a__m__v1", 20_500, live=19_000)]).warn is True
    # A row from an engine that omits stored_count falls back to count.
    assert _row([{"name": "docs__a__m__v1", "count": 25_000, "content_type": "docs"}]).warn is True
    assert not _row([{"name": "docs__a__m__v1", "count": 5, "content_type": "docs"}]).warn


def test_a_malformed_row_is_skipped_not_fatal() -> None:
    r = _row(["x", {"count": 99_999}, {"name": 7, "count": 99_999},
              {"name": "docs__a__m__v1", "count": "many", "content_type": "docs"}, _col("docs__b__m__v1", 20_001)])
    assert r.warn is True and "docs__b__m__v1" in r.detail and "docs__a" not in r.detail


def test_more_than_five_collections_are_summarised() -> None:
    r = _row([_col(f"docs__{i}__m__v1", 30_000 + i) for i in range(8)])
    assert r.warn is True and "and 3 more" in r.detail


# ── the threshold is the engine's setting ────────────────────────────────────


def test_the_environment_setting_moves_the_threshold(monkeypatch) -> None:
    listing = [_col("docs__a__m__v1", 12_000)]
    assert not _row(listing).warn
    monkeypatch.setenv(_ENV, "10000")
    r = _row(listing)
    assert r.warn is True and "10,000" in r.detail
    monkeypatch.setenv(_ENV, "30000")
    assert not _row([_col("docs__a__m__v1", 25_000)]).warn


def test_an_unusable_setting_reads_as_the_default(monkeypatch) -> None:
    for raw in ("", "  ", "abc", "0", "-5", "1000001", "2e4"):
        monkeypatch.setenv(_ENV, raw)
        assert not _row([_col("docs__a__m__v1", DEFAULT_SEARCH_EXACT_MAX_ROWS - 1)]).warn, raw
        assert _row([_col("docs__a__m__v1", DEFAULT_SEARCH_EXACT_MAX_ROWS)]).warn is True, raw


def test_the_default_is_the_engines_default() -> None:
    """The engine's default lives in Java and the status body does not carry it; this is the one client mirror."""
    java = (_ROOT / "service/src/main/java/dev/nexus/service/db/PgSession.java").read_text()
    m = re.search(r"DEFAULT_SEARCH_EXACT_MAX_ROWS\s*=\s*([0-9_]+)\s*;", java)
    assert m, "PgSession.DEFAULT_SEARCH_EXACT_MAX_ROWS not found: the drift pin has nothing to compare"
    assert int(m.group(1).replace("_", "")) == DEFAULT_SEARCH_EXACT_MAX_ROWS


# ── wiring ───────────────────────────────────────────────────────────────────


def test_a_listing_passed_in_is_used_and_nothing_is_fetched() -> None:
    with patch("nexus.db.make_t3", side_effect=AssertionError("must not list")):
        assert _check_prose_index_recall([_col("docs__a__m__v1", 50_000)])[0].warn is True


def test_the_default_listing_comes_from_the_t3_client() -> None:
    class _T3:
        def list_collections(self):
            return [_col("docs__a__m__v1", 50_000)]

    with patch("nexus.db.make_t3", return_value=_T3()):
        (r,) = _check_prose_index_recall()
    assert r.warn is True and "docs__a__m__v1" in r.detail


def test_nx_doctor_prints_the_row(monkeypatch) -> None:
    class _T3:
        def list_collections(self):
            return [_col("rdr__1-79__voyage-context-3__v1", 23_456)]

    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _T3())
    result = CliRunner().invoke(main, ["doctor"])
    assert "Prose collections at the index threshold" in result.output, result.output
    assert "rdr__1-79__voyage-context-3__v1" in result.output and "23,456" in result.output
