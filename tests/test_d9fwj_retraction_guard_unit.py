# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d9fwj: pure-unit coverage of
``_retract_manifest_rows_for_chash``'s blank-collection guard, with fake
reader/writer doubles instead of the real engine substrate.

This is a companion to ``tests/test_d9fwj_ghost_doc_retraction.py``, which
exercises the same fix through the real catalog (``ActiveCatalog`` /
``make_catalog_reader``/``make_catalog_writer`` over the live engine
substrate) — the substrate-integration test is the one that actually
proves the fix against ``HttpCatalogClient.write_manifest``'s real
``ValueError``. This file exists so the guard's own branch logic (blank
``physical_collection`` with/without an ``expected_collection`` fallback,
and the non-blank pass-through case) has a fast, substrate-free
falsifier that runs without a built engine jar. No ``NX_TEST_T2_
SUBSTRATE=none`` opt-out is declared in-file (the autouse fixture reads
the env var directly, not via a fixture override) — run this file with
that env var set when the engine substrate is unavailable, e.g.:

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_d9fwj_retraction_guard_unit.py
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from nexus.catalog.store_hook import _retract_manifest_rows_for_chash


@dataclass
class _FakeRow:
    chash: str
    position: int = 0
    chunk_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class _FakeEntry:
    tumbler: str
    physical_collection: str


class _FakeReader:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def get_manifest(self, tumbler_str: str) -> list[_FakeRow]:
        assert tumbler_str  # sanity: always called with the entry's tumbler
        return self._rows


@dataclass
class _FakeWriter:
    calls: list[tuple[str, list[dict], str]] = field(default_factory=list)

    def write_manifest(self, tumbler_str: str, remaining: list[dict], *, collection: str) -> None:
        self.calls.append((tumbler_str, remaining, collection))


def test_blank_physical_collection_no_fallback_skips_without_raising() -> None:
    """The exact pre-fix crash shape: physical_collection == "" and no
    expected_collection. Must return cleanly, log-and-skip, never call
    write_manifest (there is nothing safe to stamp the row with)."""
    entry = _FakeEntry(tumbler="1.2.3", physical_collection="")
    reader = _FakeReader([_FakeRow(chash="deadbeef")])
    writer = _FakeWriter()

    _retract_manifest_rows_for_chash(reader, writer, entry, "deadbeef")

    assert writer.calls == [], (
        "with no collection available (blank physical_collection, no "
        "expected_collection fallback), write_manifest must never be "
        "called — reverting the fix makes this raise ValueError instead"
    )


def test_blank_physical_collection_falls_back_to_expected_collection() -> None:
    """physical_collection == "" but a caller-supplied expected_collection
    is available: retraction must proceed, stamping the retained rows
    with the fallback collection."""
    entry = _FakeEntry(tumbler="1.2.4", physical_collection="")
    reader = _FakeReader([_FakeRow(chash="target"), _FakeRow(chash="keep-me")])
    writer = _FakeWriter()

    _retract_manifest_rows_for_chash(
        reader, writer, entry, "target", expected_collection="knowledge__fallback__voyage-context-3__v1",
    )

    assert len(writer.calls) == 1, "the fallback collection must let retraction proceed"
    tumbler_str, remaining, collection = writer.calls[0]
    assert tumbler_str == "1.2.4"
    assert [r["chash"] for r in remaining] == ["keep-me"]
    assert collection == "knowledge__fallback__voyage-context-3__v1", (
        "the retained row must be stamped with the expected_collection "
        "fallback, not the entry's own blank physical_collection"
    )


def test_non_blank_physical_collection_ignores_expected_collection_fallback() -> None:
    """Pre-existing, non-ghost behavior must be unaffected: a real
    physical_collection is used verbatim, even when an (irrelevant)
    expected_collection is also supplied."""
    entry = _FakeEntry(tumbler="1.2.5", physical_collection="knowledge__real__voyage-context-3__v1")
    reader = _FakeReader([_FakeRow(chash="target")])
    writer = _FakeWriter()

    _retract_manifest_rows_for_chash(
        reader, writer, entry, "target", expected_collection="knowledge__should-be-ignored__voyage-context-3__v1",
    )

    assert len(writer.calls) == 1
    _, remaining, collection = writer.calls[0]
    assert remaining == []
    assert collection == "knowledge__real__voyage-context-3__v1"


def test_no_matching_chash_is_a_pure_noop() -> None:
    """Unrelated pre-existing behavior, pinned so the guard's early
    return doesn't accidentally change it: when *chash* is not present in
    the manifest at all, write_manifest must never be called, regardless
    of physical_collection."""
    entry = _FakeEntry(tumbler="1.2.6", physical_collection="")
    reader = _FakeReader([_FakeRow(chash="unrelated")])
    writer = _FakeWriter()

    _retract_manifest_rows_for_chash(reader, writer, entry, "not-in-manifest")

    assert writer.calls == []
