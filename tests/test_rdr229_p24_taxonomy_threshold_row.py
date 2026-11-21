# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.4 (nexus-p2m1x.9): the taxonomy projection threshold reads the
catalog row's ``content_type``, not the name's prefix.

The contract is RDR-229 Technical Design item 1's three outcomes:

* Row     -> the threshold of the row's ``content_type`` (knowledge 0.50,
  docs/rdr 0.55, code and anything else 0.70), even when the name's prefix
  says otherwise. This is the behavioural change.
* NoRow   -> the name derivation (today's table); the only signal for an
  unregistered or empty collection.
* Unreadable (the row read raised; ``resolve_collection`` says so and logs it) -> the same
  name derivation; a taxonomy run never fails on it.

An explicit ``--threshold`` short-circuits all three. The tests drive the
real callers (``nx taxonomy project`` single-source, ``--backfill``,
``--prune-below-threshold``, and ``HttpTaxonomyStore.audit_collection``) with
``nexus.mcp_infra.get_collection_row`` faked, so each fails on the name-prefix
reading and passes on the row reading. None needs a tenant.
"""
from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy
from nexus.mcp_infra import get_collection_row as _REAL_GET_COLLECTION_ROW

#: ``strict`` of every fake row read this test made (the fixture below clears it).
_STRICT_SEEN: list[bool] = []

# A code__-named collection whose row says knowledge, and the mirror image.
CODE_NAMED_KNOWLEDGE_ROW = "code__1-1__bge-base-en-v15-768__v1"
KNOWLEDGE_NAMED_CODE_ROW = "knowledge__1-2__bge-base-en-v15-768__v1"
DOCS_NAMED_RDR_ROW = "docs__1-3__bge-base-en-v15-768__v1"
UNREGISTERED_LEGACY = "docs__legacy-no-row"

_ROWS: dict[str, dict[str, str]] = {
    CODE_NAMED_KNOWLEDGE_ROW: {"content_type": "knowledge"},
    KNOWLEDGE_NAMED_CODE_ROW: {"content_type": "code"},
    DOCS_NAMED_RDR_ROW: {"content_type": "rdr"},
}


@pytest.fixture(autouse=True)
def _rows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake the row seam; returns the list of names it was asked about."""
    asked: list[str] = []
    _STRICT_SEEN.clear()

    def _get_collection_row(name: str, *, refresh: bool = True, strict: bool = False):
        asked.append(name)
        _STRICT_SEEN.append(strict)
        return _ROWS.get(name)

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _get_collection_row)
    monkeypatch.setattr("nexus.mcp_infra.get_live_collection_names", lambda: sorted(_ROWS))
    return asked


# ── the function ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        (CODE_NAMED_KNOWLEDGE_ROW, 0.50),
        (KNOWLEDGE_NAMED_CODE_ROW, 0.70),
        (DOCS_NAMED_RDR_ROW, 0.55),
    ],
)
def test_row_content_type_beats_the_name_prefix(name: str, expected: float) -> None:
    from nexus.projection_threshold import projection_threshold

    assert projection_threshold(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        (UNREGISTERED_LEGACY, 0.55),
        ("knowledge__legacy-no-row", 0.50),
        ("code__legacy-no-row", 0.70),
        ("other__weird", 0.70),
        ("noprefix", 0.70),
    ],
)
def test_no_row_falls_back_to_the_name_derivation(name: str, expected: float) -> None:
    from nexus.projection_threshold import projection_threshold

    assert projection_threshold(name) == expected


def test_unreadable_listing_falls_back_to_the_name_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.projection_threshold as pt

    def _boom(name: str, *, refresh: bool = True, strict: bool = False):
        raise RuntimeError("catalog listing unreadable")

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _boom)

    assert pt.projection_threshold(CODE_NAMED_KNOWLEDGE_ROW) == 0.70  # the name says code
    assert pt.projection_threshold(UNREGISTERED_LEGACY) == 0.55
    assert pt.resolve_projection_threshold(UNREGISTERED_LEGACY).source == "unreadable"


# ── the callers ─────────────────────────────────────────────────────────────


class _Taxonomy:
    """Records what the commands hand the store; the store itself is not under test."""

    def __init__(self, distinct: list[str]) -> None:
        self.distinct = distinct
        self.projected: list[tuple[str, float]] = []
        self.pruned: list[tuple[str, float]] = []

    def get_distinct_collections(self) -> list[str]:
        return list(self.distinct)

    def get_projection_counts_by_collection(self) -> dict[str, int]:
        return {}

    def compute_icf_map(self, *, use_cache: bool = True) -> dict:
        return {}

    def project_against(self, source, targets, handle, *, threshold, **_kw) -> dict[str, Any]:
        self.projected.append((source, round(threshold, 2)))
        return {"matched_topics": [], "novel_chunks": [], "total_chunks": 0, "chunk_assignments": []}

    def prune_projection_below(self, prefix: str, thr: float, *, exact: bool = False) -> tuple[int, str | None]:
        self.pruned.append((prefix, round(thr, 2)))
        return 1, ("exact" if exact else "prefix")


def _wire(monkeypatch: pytest.MonkeyPatch, distinct: list[str]) -> _Taxonomy:
    tax = _Taxonomy(distinct)

    class _Db:
        taxonomy = tax

        def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", lambda *_a, **_k: _Db())
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._without_superseded", lambda cols: cols)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    # The prune path asks the engine's /version for the exact-name mode; no tenant here, so no probe.
    monkeypatch.setattr("nexus.commands.taxonomy_cmd.prune_supports_exact", lambda: False)
    return tax


def test_single_source_default_reads_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    tax = _wire(monkeypatch, [])
    result = CliRunner().invoke(
        taxonomy, ["project", CODE_NAMED_KNOWLEDGE_ROW, "--against", "knowledge__other"],
    )
    assert result.exit_code == 0, result.output
    assert tax.projected == [(CODE_NAMED_KNOWLEDGE_ROW, 0.50)]


def test_single_source_explicit_threshold_short_circuits_the_row(
    monkeypatch: pytest.MonkeyPatch, _rows: list[str],
) -> None:
    tax = _wire(monkeypatch, [])
    result = CliRunner().invoke(
        taxonomy,
        ["project", CODE_NAMED_KNOWLEDGE_ROW, "--against", "knowledge__other", "--threshold", "0.81"],
    )
    assert result.exit_code == 0, result.output
    assert tax.projected == [(CODE_NAMED_KNOWLEDGE_ROW, 0.81)]
    assert _rows == [], "an explicit --threshold must not read a row"


def test_backfill_default_reads_each_sources_row(monkeypatch: pytest.MonkeyPatch) -> None:
    tax = _wire(monkeypatch, [CODE_NAMED_KNOWLEDGE_ROW, KNOWLEDGE_NAMED_CODE_ROW, UNREGISTERED_LEGACY])
    result = CliRunner().invoke(taxonomy, ["project", "--backfill"])
    assert result.exit_code == 0, result.output
    assert dict(tax.projected) == {
        CODE_NAMED_KNOWLEDGE_ROW: 0.50,
        KNOWLEDGE_NAMED_CODE_ROW: 0.70,
        UNREGISTERED_LEGACY: 0.55,
    }


def test_prune_with_backfill_prunes_each_collection_at_its_rows_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tax = _wire(monkeypatch, [UNREGISTERED_LEGACY])
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    # One prune per full collection name (live listing plus the collections with topics),
    # never one per name prefix.
    assert dict(tax.pruned) == {
        CODE_NAMED_KNOWLEDGE_ROW: 0.50,
        KNOWLEDGE_NAMED_CODE_ROW: 0.70,
        DOCS_NAMED_RDR_ROW: 0.55,
        UNREGISTERED_LEGACY: 0.55,
    }
    assert len(tax.pruned) == 4
    assert "Pruned 4 projection assignment(s)." in result.output


def test_audit_collection_default_threshold_reads_the_row() -> None:
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
    store._get = lambda path, params=None, **_kw: (  # type: ignore[method-assign]
        {"similarities": [0.10, 0.45, 0.52, 0.80], "hub_rows": []} if path == "/audit" else {}
    )
    store.compute_icf_map = lambda *a, **k: {}  # type: ignore[method-assign]
    report = store.audit_collection(CODE_NAMED_KNOWLEDGE_ROW)
    assert report.threshold == 0.50
    assert report.below_threshold_count == 2


# ── the fix round (review r1 I1, S5, S6, S9; critique r1 S2) ────────────────


def test_the_row_read_is_strict() -> None:
    """``strict=True`` is what makes an unreadable listing distinguishable from NoRow: dropping it
    turns Unreadable into a silent NoRow with the same value and no log (review S6)."""
    from nexus.projection_threshold import projection_threshold

    projection_threshold(CODE_NAMED_KNOWLEDGE_ROW)
    assert _STRICT_SEEN == [True]


def test_the_real_row_read_reports_an_unreadable_listing_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real ``get_collection_row`` with a listing that raises ``ServiceNotReadyError``:
    the answer carries ``source == "unreadable"`` (not ``"no_row"``)."""
    import nexus.mcp_infra as mi
    import nexus.projection_threshold as pt

    def _down(*_a, **_kw):
        raise mi.ServiceNotReadyError("the service is not answering")

    monkeypatch.setattr(mi, "get_collection_row", _REAL_GET_COLLECTION_ROW)
    monkeypatch.setattr(mi, "_refresh_collections_cache_if_stale", _down)

    answer = pt.resolve_projection_threshold(CODE_NAMED_KNOWLEDGE_ROW)
    assert answer.source == "unreadable"
    assert answer.value == 0.70, "the name derivation still answers"


def test_resolve_names_where_the_answer_came_from() -> None:
    from nexus.projection_threshold import resolve_projection_threshold

    row = resolve_projection_threshold(CODE_NAMED_KNOWLEDGE_ROW)
    no_row = resolve_projection_threshold(UNREGISTERED_LEGACY)
    assert (row.value, row.source) == (0.50, "row")
    assert (no_row.value, no_row.source) == (0.55, "no_row")


def test_a_programming_error_in_the_row_read_is_not_an_unreadable_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_collection`` raises a ``TypeError``, ``AttributeError`` or ``KeyError`` out of the
    row read as a bug instead of answering it as Unreadable (and so as a guess)."""
    import nexus.projection_threshold as pt

    def _bug(name: str, *, refresh: bool = True, strict: bool = False):
        raise TypeError("a bug in the reader")

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _bug)
    with pytest.raises(TypeError):
        pt.projection_threshold(CODE_NAMED_KNOWLEDGE_ROW)


def test_a_row_and_a_no_row_name_of_the_same_type_give_the_same_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """One table: for each content type the table knows (and one it does not), a row of that
    ``content_type`` and a NoRow name of that prefix answer the same value, so the Row and the
    NoRow outcomes of item 1 cannot drift apart."""
    import nexus.projection_threshold as pt

    types = [*pt._THRESHOLD_BY_CONTENT_TYPE, "code", "other"]
    assert {"knowledge", "docs", "rdr"} <= set(types), "the table lost a type"
    for ct in types:
        monkeypatch.setitem(_ROWS, f"{ct}__1-9__bge-base-en-v15-768__v1", {"content_type": ct})
    for ct in types:
        with_row = pt.resolve_projection_threshold(f"{ct}__1-9__bge-base-en-v15-768__v1")
        no_row = pt.resolve_projection_threshold(f"{ct}__legacy-no-row-{ct}")
        assert (with_row.source, no_row.source) == ("row", "no_row"), ct
        assert with_row.value == no_row.value == pt.threshold_for_content_type(ct), ct


def _catalog_client(handler):
    import httpx

    from nexus.catalog.http_catalog_client import HttpCatalogClient

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


def test_supports_taxonomy_prune_exact_counts_only_json_true_from_one_cached_version_read() -> None:
    import httpx

    asked: list[str] = []

    def make(body):
        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            return httpx.Response(200, json=body)

        return _catalog_client(handler)

    assert make({"taxonomy_prune_exact_supported": True}).supports_taxonomy_prune_exact() is True
    for not_true in ("true", 1, None, [True], {}):
        assert make({"taxonomy_prune_exact_supported": not_true}).supports_taxonomy_prune_exact() is False, repr(not_true)
    assert make({}).supports_taxonomy_prune_exact() is False, "an engine without the key"
    assert make({"catalog_move_supported": True}).supports_taxonomy_prune_exact() is False, "its own flag"
    asked.clear()
    cat = make({"taxonomy_prune_exact_supported": True})
    assert [cat.supports_taxonomy_prune_exact() for _ in range(5)] == [True] * 5
    assert asked == ["/version"], "cached: one /version read however many times it is asked"


def test_supports_taxonomy_prune_exact_is_false_when_version_cannot_be_read() -> None:
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert _catalog_client(boom).supports_taxonomy_prune_exact() is False


def test_prune_projection_below_posts_match_exact_only_when_asked() -> None:
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    bodies: list[dict] = []
    store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
    store._post = lambda path, body, **_kw: bodies.append(body) or {"removed": 3}  # type: ignore[method-assign]
    assert store.prune_projection_below("code__x", 0.7) == (3, None), "an engine without the echo: mode None"
    assert store.prune_projection_below("code__x", 0.7, exact=True) == (3, None)
    assert bodies == [
        {"source_collection_prefix": "code__x", "min_similarity": 0.7},
        {"source_collection_prefix": "code__x", "min_similarity": 0.7, "match": "exact"},
    ]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ({"removed": 3, "match": "exact"}, (3, "exact")),
        ({"removed": 3, "match": "prefix"}, (3, "prefix")),
        ({"removed": 3}, (3, None)),
        ({"removed": 3, "match": 7}, (3, None)),
        ({}, (0, None)),
    ],
    ids=["exact", "prefix", "absent", "non-string", "empty"],
)
def test_prune_projection_below_returns_the_echoed_mode_beside_the_count(reply, expected) -> None:
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
    store._post = lambda path, body, **_kw: reply  # type: ignore[method-assign]
    assert store.prune_projection_below("code__x", 0.7, exact=True) == expected
