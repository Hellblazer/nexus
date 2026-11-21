# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28): ``nx doctor`` names a live collection tuple held by more than one registry row.

The engine's partial UNIQUE index on ``catalog_collections`` (tenant_id, content_type, owner_id, embedding_model,
model_version) WHERE superseded_by = '' AND lifecycle_state <> 'quarantine' is created by a changeset that WAITS for the
re-own move and for no duplicate to remain (``collections-001-2``). A duplicate holds it back, quietly, at every boot;
this row says which tuple and which collections, so an operator sees why. The rows come from the catalog's collection
listing (the caller's own tenant, authenticated): ``GET /v1/status`` is unauthenticated and carries no tenant text.

A warning while a duplicate exists, and no row at all when there is none (the index exists, or only the move is
pending, which the data-move rows already show).
"""
from __future__ import annotations

from unittest.mock import patch

import nexus.health as health
from nexus.collection_tuple_health import LABEL, check_collection_tuples, duplicate_tuples

_MODEL = "bge-base-en-v15-768"


def _row(name: str, *, owner: str = "owner-1", content_type: str = "docs", model: str = _MODEL,
         version: str = "", lifecycle: str = "live", superseded_by: str = "") -> dict:
    return {
        "name": name, "content_type": content_type, "owner_id": owner, "embedding_model": model,
        "model_version": version, "lifecycle_state": lifecycle, "superseded_by": superseded_by,
    }


def test_two_live_rows_on_one_tuple_are_one_warning_naming_the_tuple_and_both_collections() -> None:
    rows = check_collection_tuples([_row("docs__a"), _row("docs__b"), _row("docs__other", owner="owner-2")])

    assert len(rows) == 1
    r = rows[0]
    assert r.label == LABEL
    assert r.ok is False and r.warn is True and r.fatal is False
    assert "docs__a" in r.detail and "docs__b" in r.detail
    assert "docs__other" not in r.detail
    assert "owner-1" in r.detail and _MODEL in r.detail
    assert any("collections-001-2" in s or "unique index" in s.lower() for s in r.fix_suggestions)


def test_no_duplicate_is_no_row() -> None:
    assert check_collection_tuples([_row("docs__a"), _row("docs__b", owner="owner-2")]) == []
    assert check_collection_tuples([]) == []


def test_what_the_index_predicate_leaves_out_is_not_a_duplicate() -> None:
    rows = [
        _row("docs__a"),
        _row("quarantine-docs__a", lifecycle="quarantine"),  # a quarantine sibling
        _row("docs__old", superseded_by="docs__a"),  # a superseded row
        _row("docs__v2", version="v2"),  # a different model_version (a same-model re-embed target)
    ]
    assert check_collection_tuples(rows) == []
    assert duplicate_tuples(rows) == []


def test_a_dormant_or_disputed_row_still_holds_the_tuple() -> None:
    (r,) = check_collection_tuples([_row("docs__a"), _row("docs__b", lifecycle="dormant"), _row("docs__c", lifecycle="disputed")])

    assert all(n in r.detail for n in ("docs__a", "docs__b", "docs__c"))


def test_a_row_without_lifecycle_or_supersession_keys_counts_as_live() -> None:
    bare = {"name": "docs__x", "content_type": "docs", "owner_id": "o", "embedding_model": _MODEL, "model_version": ""}
    twin = dict(bare, name="docs__y")

    assert [d.names for d in duplicate_tuples([bare, twin])] == [("docs__x", "docs__y")]


def test_each_duplicated_tuple_is_named_and_a_long_list_is_capped_with_a_count() -> None:
    rows = []
    for i in range(8):
        rows += [_row(f"docs__a{i}", owner=f"owner-{i}"), _row(f"docs__b{i}", owner=f"owner-{i}")]

    (r,) = check_collection_tuples(rows)

    assert "owner-0" in r.detail
    assert "3 more" in r.detail
    assert "8" in r.detail  # the total


def test_an_unreadable_registry_is_no_row_and_never_raises() -> None:
    with patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("no engine")):
        assert check_collection_tuples() == []

    class _Broken:
        def list_collections(self, *a, **k):
            raise RuntimeError("boom")

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_Broken()):
        assert check_collection_tuples() == []
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        assert check_collection_tuples() == []


def test_the_rows_come_from_the_reader_when_none_are_given() -> None:
    class _Reader:
        def list_collections(self, *a, **k):
            return [_row("docs__a"), _row("docs__b")]

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_Reader()):
        (r,) = check_collection_tuples()

    assert "docs__a" in r.detail and "docs__b" in r.detail


def test_the_sweep_has_the_row_under_a_check_name_so_a_stubbed_sweep_steps_over_it() -> None:
    assert callable(health._check_collection_tuples)
    with patch("nexus.collection_tuple_health.check_collection_tuples", return_value=[]) as inner:
        assert health._check_collection_tuples() == []
    inner.assert_called_once_with()
