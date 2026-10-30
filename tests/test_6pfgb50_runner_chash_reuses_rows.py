# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.50 (RDR-236 Phase 2, RF-10): the plan runner's chash path reuses the owner rows its
``store_get_many`` call already fetched.

An operator step handed chash ids hydrated them with ``store_get_many(structured=True)``, which asks the
catalog ``chash_positions`` (every live manifest row holding each chash) and ``resolve_many`` (the owners,
for the source notes), and then asked the catalog again, ``docs_for_chashes`` and a second ``resolve_many``,
to learn which of the same chashes belong to a non-evidentiary document. Four catalog calls for one step,
two of them re-reading what the first two returned. The non-evidentiary check now reads the rows and entries
the hydration holds; it asks only for owners the hydration did not resolve.

Storage-free: T3 and the catalog are stubbed with a call-counting fake, so no test here mints a tenant.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _entry, _position, _t3

Y = "b" * 64


class _CountingCatalog:
    """The catalog reads a chash-path hydration can make, with a per-method call log."""

    def __init__(self, entries, positions, *, docs_for_chashes=True, positions_fail=False) -> None:
        self.entries = {str(e.tumbler): e for e in entries}
        self.positions = positions
        self.calls: list[tuple[str, tuple]] = []
        self._positions_fail = positions_fail
        if not docs_for_chashes:
            self.docs_for_chashes = None  # type: ignore[assignment]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def chash_positions(self, chashes):
        self.calls.append(("chash_positions", tuple(chashes)))
        if self._positions_fail:
            raise RuntimeError("engine without the route")
        return [p for p in self.positions if p["chash"] in chashes]

    def resolve_many(self, doc_ids):
        self.calls.append(("resolve_many", tuple(doc_ids)))
        return {d: self.entries[d] for d in doc_ids if d in self.entries}

    def docs_for_chashes(self, chashes):
        self.calls.append(("docs_for_chashes", tuple(chashes)))
        out: dict[str, list[str]] = {}
        for p in self.positions:
            if p["chash"] in chashes:
                out.setdefault(p["chash"], []).append(p["doc_id"])
        return out


def _stamped(tumbler: str, **kw):
    return _entry(tumbler, "Stamped", "2026-09-01T00:00:00+00:00", meta={"non_evidentiary": True}, **kw)


def _plain(tumbler: str, **kw):
    return _entry(tumbler, "Plain", "2026-09-01T00:00:00+00:00", **kw)


def _hydrate_step(catalog, ids, rows):
    """One operator step over chash *ids*: the real runner and the real ``store_get_many``."""
    from nexus.plans.runner import _hydrate_operator_args

    with patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._get_t3", return_value=_t3(rows)), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        return _hydrate_operator_args("summarize", {"ids": ids, "collections": COLL})


def test_a_chash_step_makes_one_owner_row_read_and_one_resolve_not_four() -> None:
    """The count pin. Before: chash_positions 1, resolve_many 2, docs_for_chashes 1. The stamped
    document's chunk is still dropped, the plain one kept."""
    catalog = _CountingCatalog(
        [_stamped("1.1.1"), _plain("1.1.2")],
        [_position(X, "1.1.1"), _position(Y, "1.1.2")],
    )
    tool, args = _hydrate_step(catalog, [X, Y], {X: {"content": "fixture text"}, Y: {"content": "real text"}})

    assert tool == "operator_summarize"
    assert "real text" in args["content"] and "fixture text" not in args["content"]
    assert catalog.count("chash_positions") == 1
    assert catalog.count("docs_for_chashes") == 0, "the owners were already fetched by store_get_many"
    assert catalog.count("resolve_many") == 1, "the owner entries were already resolved for the source notes"
    assert len(catalog.calls) == 2, catalog.calls


def test_a_chash_shared_with_a_plain_document_stays_in_through_the_reused_rows() -> None:
    """The ``all()`` rule survives the move: a chunk is dropped only when every owner is stamped."""
    catalog = _CountingCatalog(
        [_stamped("1.1.1"), _plain("1.1.2"), _stamped("1.1.3")],
        [_position(X, "1.1.1"), _position(X, "1.1.2"), _position(Y, "1.1.1"), _position(Y, "1.1.3")],
    )
    _, args = _hydrate_step(catalog, [X, Y], {X: {"content": "shared"}, Y: {"content": "both stamped"}})

    assert "shared" in args["content"] and "both stamped" not in args["content"]
    assert catalog.count("docs_for_chashes") == 0


def test_an_owner_the_hydration_did_not_resolve_is_fetched_in_one_batch_not_per_chash() -> None:
    """The source notes resolve only owners in the chunk's own collection. The non-evidentiary check
    is tenant-wide (as ``docs_for_chashes`` was), so an owner elsewhere is fetched, once for the step.
    Collection choice, recorded: a stamped owner here plus a plain owner in ANOTHER collection keeps
    the chunk in, exactly as before."""
    catalog = _CountingCatalog(
        [_stamped("1.1.1"), _plain("1.2.1", collection="knowledge__elsewhere"),
         _stamped("1.1.3"), _stamped("1.2.3", collection="knowledge__elsewhere")],
        [_position(X, "1.1.1"), _position(X, "1.2.1", "knowledge__elsewhere"),
         _position(Y, "1.1.3"), _position(Y, "1.2.3", "knowledge__elsewhere")],
    )
    _, args = _hydrate_step(catalog, [X, Y], {X: {"content": "kept"}, Y: {"content": "dropped"}})

    assert "kept" in args["content"] and "dropped" not in args["content"]
    resolves = [c for n, c in catalog.calls if n == "resolve_many"]
    assert len(resolves) == 2, resolves
    assert sorted(resolves[1]) == ["1.2.1", "1.2.3"], "only the owners the first resolve did not cover"
    assert catalog.count("docs_for_chashes") == 0


def test_without_the_owner_route_the_check_falls_back_to_the_reverse_lookup() -> None:
    """chash_positions failing (an engine that predates it) leaves the old, still correct path."""
    catalog = _CountingCatalog([_stamped("1.1.1")], [_position(X, "1.1.1")], positions_fail=True)
    _, args = _hydrate_step(catalog, [X], {X: {"content": "fixture text"}})

    assert "fixture text" not in args["content"]
    assert catalog.count("docs_for_chashes") == 1


def test_a_failing_second_resolve_leaves_every_candidate_in() -> None:
    class _Flaky(_CountingCatalog):
        def resolve_many(self, doc_ids):
            self.calls.append(("resolve_many", tuple(doc_ids)))
            if len([1 for n, _ in self.calls if n == "resolve_many"]) > 1:
                raise RuntimeError("engine down")
            return {d: self.entries[d] for d in doc_ids if d in self.entries}

    catalog = _Flaky(
        [_stamped("1.1.1"), _stamped("1.2.1", collection="knowledge__elsewhere")],
        [_position(X, "1.1.1"), _position(X, "1.2.1", "knowledge__elsewhere")],
    )
    _, args = _hydrate_step(catalog, [X], {X: {"content": "unclassifiable"}})
    assert "unclassifiable" in args["content"]


@pytest.mark.parametrize("ids", [[X], [X, Y]])
def test_the_step_still_hydrates_when_no_owner_exists(ids) -> None:
    catalog = _CountingCatalog([], [])
    _, args = _hydrate_step(catalog, ids, {X: {"content": "orphan x"}, Y: {"content": "orphan y"}})
    assert "orphan x" in args["content"]
    assert catalog.count("chash_positions") == 1
