# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Prose collections at the index threshold" row (nexus-qqoak, RDR-227 residual).

A collection above ``NX_SEARCH_EXACT_MAX_ROWS`` rows (default 20,000; at or below it the router runs exact) is routed
to its own HNSW index once one is built. For prose on ``voyage-context-3`` that index's recall was measured under the
0.99 target at 10,308 and 14,300 rows and not at all above 20,000 (T2 ``nexus_rdr/227-research-12``); no other model
was measured. Nothing checked that no prose collection had crossed. The ``GET /v1/status`` object carries counts only,
so the row reads the collection listing.

The rows below have the shape ``HttpVectorClient.list_collections`` returns: ``name``, live ``count``, physical
``stored_count``, and the catalog attributes ``content_type``, ``embedding_model`` and ``lifecycle_state``.
Not-applicable cases come first: a new row on a virgin box must stay green.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.health as health
from nexus.cli import main
from nexus.config import is_local_mode
from nexus.health import DEFAULT_SEARCH_EXACT_MAX_ROWS, _check_prose_index_recall

_ENV = "NX_SEARCH_EXACT_MAX_ROWS"
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_threshold_env(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


def _col(name: str, rows: int, *, live: int | None = None, kind: str | None = None, model: str | None = None,
         state: str | None = None) -> dict:
    """A listing row. ``content_type`` is the catalog attribute the stats route joins onto every registered
    collection; it defaults to the name's first segment, as the engine's catalog row has it. ``embedding_model``
    and ``lifecycle_state`` are added only when given (they are ABSENT, never null, with no catalog row)."""
    row = {"name": name, "count": rows if live is None else live, "stored_count": rows,
           "content_type": name.split("__", 1)[0] if kind is None else kind}
    if model is not None:
        row["embedding_model"] = model
    if state is not None:
        row["lifecycle_state"] = state
    return row


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


_VCTX = "voyage-context-3"


def test_a_prose_collection_above_the_threshold_warns_naming_it_its_count_and_how_to_measure() -> None:
    r = _row([_col("rdr__1-79__voyage-context-3__v1", 20_001, model=_VCTX)])
    assert r.ok is False and r.warn is True
    assert "rdr__1-79__voyage-context-3__v1" in r.detail and "20,001 rows" in r.detail
    fix = " ".join(r.fix_suggestions)
    # The remedy is the procedure, inline: no T2 or repo-path pointer an installed operator cannot open, and no
    # "raise NX_SEARCH_EXACT_MAX_ROWS" (the doctor reads its own shell, so that hint cannot clear the warning).
    for k in ("40", "60", "80", "100", "120"):
        assert k in fix
    assert "0.99" in fix and "own topics" in fix and "RDR-227 Gap 1" in fix
    assert "nexus_rdr" not in fix and "docs/rdr" not in fix and "nexus-a6mys" not in fix
    assert "raise" not in fix.lower() and "NX_SEARCH_EXACT_MAX_ROWS" not in fix


def test_the_threshold_itself_does_not_warn_the_router_runs_it_exact() -> None:
    assert not _row([_col("rdr__a__voyage-context-3__v1", 20_000, model=_VCTX)]).warn
    assert _row([_col("rdr__a__voyage-context-3__v1", 20_001, model=_VCTX)]).warn is True


def test_the_route_is_worded_as_conditional_and_the_thresholds_source_is_named() -> None:
    r = _row([_col("docs__a__m__v1", 25_000, model=_VCTX)])
    assert "is routed to its own HNSW index once one is built" in r.detail
    assert "NX_SEARCH_PCI_BUILD_MIN_ROWS" in r.detail and "NX_SEARCH_PCI" in r.detail
    assert "searches use" not in r.detail and " uses " not in r.detail
    assert "this shell's NX_SEARCH_EXACT_MAX_ROWS" in r.detail and "not visible to nx doctor" in r.detail


# ── the evidence is quoted per embedding model ───────────────────────────────


def test_voyage_context_3_gets_the_measurement_and_nothing_else_is_claimed() -> None:
    r = _row([_col("docs__a__m__v1", 25_000, model=_VCTX)])
    assert "0.9833 minimum tail recall (mean 0.9992, no top-1 miss) at 10,308 and 14,300 rows" in r.detail
    assert "under the 0.99 target" in r.detail and "unmeasured" not in r.detail
    assert "voyage-context-3" in r.detail


def test_any_other_model_is_unmeasured_and_the_measurement_is_not_quoted_for_it() -> None:
    for model in ("bge-768", "voyage-3", ""):
        r = _row([_col("docs__a__m__v1", 25_000, model=model)] if model else [_col("docs__a__m__v1", 25_000)])
        assert r.warn is True
        assert "0.9833" not in r.detail and "0.9992" not in r.detail, model
        assert (f"recall on {model} is unmeasured" if model else "embedding model is not in the listing") in r.detail


def test_mixed_models_each_get_their_own_sentence() -> None:
    r = _row([_col("docs__a__m__v1", 25_000, model=_VCTX), _col("docs__b__m__v1", 24_000, model="bge-768")])
    assert "0.9833 minimum tail recall" in r.detail and "recall on bge-768 is unmeasured" in r.detail
    assert "docs__a__m__v1 (25,000 rows, voyage-context-3)" in r.detail
    assert "docs__b__m__v1 (24,000 rows, bge-768)" in r.detail


def test_a_knowledge_collection_is_flagged_as_included_by_analogy() -> None:
    r = _row([_col("knowledge__a__m__v1", 25_000, model=_VCTX)])
    assert r.warn is True and "knowledge collections were not themselves measured" in r.detail
    assert "knowledge collections were not" not in _row([_col("docs__a__m__v1", 25_000, model=_VCTX)]).detail


# ── only live collections ────────────────────────────────────────────────────


def test_a_collection_that_is_not_live_is_ignored() -> None:
    for state in ("quarantine", "dormant", "disputed"):
        r = _row([_col("docs__a__m__v1", 90_000, model=_VCTX, state=state), _col("docs__b__m__v1", 5, model=_VCTX)])
        assert r.ok is True and not r.warn, state
    # A live row, and a row with no lifecycle_state key (no catalog row), still count.
    assert _row([_col("docs__a__m__v1", 90_000, model=_VCTX, state="live")]).warn is True
    assert _row([_col("docs__a__m__v1", 90_000, model=_VCTX)]).warn is True
    # Only the live one is named.
    r = _row([_col("docs__dead__m__v1", 90_000, state="quarantine"), _col("docs__live__m__v1", 30_000, state="live")])
    assert "docs__live__m__v1" in r.detail and "docs__dead" not in r.detail
    # A rename tombstone (lifecycle live, superseded_by set) is not a search target either.
    tomb = {**_col("docs__old__m__v1", 90_000), "lifecycle_state": "live", "superseded_by": "docs__new__m__v1"}
    assert not _row([tomb, _col("docs__b__m__v1", 5)]).warn


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
    assert r.warn is True and "above 10,000 rows" in r.detail
    monkeypatch.setenv(_ENV, "30000")
    assert not _row([_col("docs__a__m__v1", 25_000)]).warn


def test_an_unusable_setting_reads_as_the_default(monkeypatch) -> None:
    for raw in ("", "  ", "abc", "0", "-5", "1000001", "2e4"):
        monkeypatch.setenv(_ENV, raw)
        assert not _row([_col("docs__a__m__v1", DEFAULT_SEARCH_EXACT_MAX_ROWS - 1)]).warn, raw
        assert not _row([_col("docs__a__m__v1", DEFAULT_SEARCH_EXACT_MAX_ROWS)]).warn, raw
        assert _row([_col("docs__a__m__v1", DEFAULT_SEARCH_EXACT_MAX_ROWS + 1)]).warn is True, raw


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


def test_one_doctor_run_lists_the_collections_once_for_the_t3_count_and_this_row(monkeypatch) -> None:
    """The row and "T3 collections" share the one listing ``run_health_checks`` fetches: a run with both rows real
    makes no more ``list_collections`` calls than the same run with both rows stubbed out (the shared fetch is in
    both), and each row is handed that listing."""
    def run(*, stub_rows: bool) -> tuple[int, list]:
        calls = []

        class _T3:
            def list_collections(self, *a, **k):
                calls.append(1)
                return [_col("rdr__1-79__voyage-context-3__v1", 23_456, model=_VCTX)]

        seen: list = []
        real_prose = health._check_prose_index_recall
        real_local = health._check_t3_local

        def spy(collections=health._ENGINE_STATUS_UNSET):
            seen.append(collections)
            return real_prose(collections)

        def spy_local(collections=health._ENGINE_STATUS_UNSET):
            seen_local.append(collections)
            return real_local(collections)

        monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _T3())
        if stub_rows:
            monkeypatch.setattr(health, "_check_t3_local", lambda *a, **k: [])
            monkeypatch.setattr(health, "_check_prose_index_recall", lambda *a, **k: [])
        else:
            monkeypatch.setattr(health, "_check_prose_index_recall", spy)
            monkeypatch.setattr(health, "_check_t3_local", spy_local)
        CliRunner().invoke(main, ["doctor"])
        return len(calls), seen

    seen_local: list = []
    with_rows, seen = run(stub_rows=False)
    without_rows, _ = run(stub_rows=True)
    assert with_rows == without_rows, (with_rows, without_rows)
    assert without_rows >= 1  # non-vacuity: the shared fetch happened
    assert len(seen) == 1 and isinstance(seen[0], list) and seen[0][0]["name"].startswith("rdr__1-79")
    # "T3 collections" runs only in local mode; when it runs it gets the same list object.
    assert len(seen_local) == (1 if is_local_mode() else 0), (len(seen_local), is_local_mode())
    assert all(x is seen[0] for x in seen_local)


def test_a_failed_shared_listing_leaves_both_rows_degraded_not_crashed(monkeypatch) -> None:
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no engine")))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert "Prose collections at the index threshold" in result.output
    (r,) = health._check_prose_index_recall(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail
    rows = {x.label: x for x in health._check_t3_local(None)}
    assert rows["T3 collections"].ok is True and rows["T3 collections"].detail == "could not query"
