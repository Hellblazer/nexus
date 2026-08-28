# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog reconcile-stale --execute tombstone-ghost-notes``. nexus-1uekf.

The arm's target set is ``zero_count_rdr145_exempt`` narrowed PER ROW at
execution time by two facts it re-proves itself: the manifest is still
empty, and T3 has no chunk under the note's chash OR its title
(``integrity.note_chunks_present``, the lookup both integrity instruments
report by). A note with chunks under either key is a manifest-only gap and
is never tombstoned; a note whose probe raises is never tombstoned.

Fakes mirror ``tests/test_catalog_reconcile_stale.py``; the T3 fake here
additionally answers the two lookups the arm makes.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main

COL = "knowledge__knowledge__voyage-context-3__v1"


class _Entry:
    def __init__(self, tumbler, title, *, chash="", chunk_count=0, file_path="", source_uri=""):
        self.tumbler = tumbler
        self.title = title
        self.physical_collection = COL
        self.chunk_count = chunk_count
        self.alias_of = ""
        self.file_path = file_path
        self.source_uri = source_uri
        self.meta = {"doc_id": chash} if chash else {}


class _Cat:
    def __init__(self, entries, manifests=None):
        self._entries = entries
        self._manifests = manifests or {}

    def all_documents(self, limit=0):
        return list(self._entries)

    def collection_doc_counts(self):
        return {COL: len(self._entries)}

    def owners_with_roots(self):
        return {}

    def get_manifests(self, doc_ids):
        return {d: self._manifests[d] for d in doc_ids if d in self._manifests}


class _T3:
    def __init__(self, *, chashes=frozenset(), titles=frozenset(), raise_for=frozenset()):
        self._chashes, self._titles, self._raise = set(chashes), set(titles), set(raise_for)
        self.probed: list[tuple[str, str]] = []

    def list_collections(self):
        return [{"name": COL}]

    def get_by_id(self, collection, doc_id):
        self.probed.append(("chash", doc_id))
        return {"id": doc_id} if doc_id in self._chashes else None

    def find_ids_by_title(self, collection, title):
        self.probed.append(("title", title))
        if title in self._raise:
            raise TimeoutError("simulated")
        return ["c"] if title in self._titles else []


class _Writer:
    def __init__(self):
        self.deleted: list = []
        self.closed = False

    def delete_many(self, tumblers):
        self.deleted.extend(tumblers)
        return list(tumblers)

    def close(self):
        self.closed = True


def _writer_that_must_not_be_built():
    raise AssertionError("a dry-run must never construct the catalog writer")


def _patch(monkeypatch, cat, t3, writer):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
    monkeypatch.setattr("nexus.commands.catalog._make_t3", lambda: t3)
    monkeypatch.setattr(
        "nexus.commands.catalog._get_catalog_writer",
        (lambda: writer) if writer is not None else _writer_that_must_not_be_built,
    )


@pytest.fixture()
def world():
    """Five exempt-shaped notes, one outcome each, plus a non-exempt control."""
    entries = [
        _Entry("1.11.1", "gone-by-both-keys", chash="a" * 32),          # ghost
        _Entry("1.11.2", "gone-no-chash"),                               # ghost (title only)
        _Entry("1.11.3", "kept-title-chunks", chash="b" * 32),          # chunks by title
        _Entry("1.11.4", "kept-chash-chunk", chash="c" * 64),           # chunk by chash
        _Entry("1.11.5", "probe-fails", chash="d" * 32),                # unverifiable
        _Entry("1.11.6", "manifest-resurfaced", chash="e" * 32),        # invariant skip
        _Entry("1.11.7", "file-backed", file_path="/p.md"),             # not exempt at all
    ]
    manifests = {"1.11.6": [{"chash": "e" * 32, "position": 0}]}
    t3 = _T3(chashes={"c" * 64}, titles={"kept-title-chunks"}, raise_for={"probe-fails"})
    return _Cat(entries, manifests), t3


def test_dry_run_default_plans_and_never_builds_the_writer(world, monkeypatch) -> None:
    cat, t3 = world
    _patch(monkeypatch, cat, t3, writer=None)

    result = CliRunner().invoke(
        main, ["catalog", "reconcile-stale", "--execute", "tombstone-ghost-notes"],
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "tombstone-ghost-notes: 2 candidate(s)" in out, out
    assert "skipped 2 whose chunks ARE in T3" in out, out
    assert "skipped 1 whose T3 probe FAILED" in out, out
    assert "dry-run" in out.lower()


def test_confirmed_tombstones_exactly_the_proven_ghosts(world, monkeypatch) -> None:
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "tombstone-ghost-notes", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert {str(t) for t in writer.deleted} == {"1.11.1", "1.11.2"}, writer.deleted
    assert writer.closed
    assert "tombstoned 2 ghost note(s)" in result.output


def test_every_candidate_is_probed_in_t3_before_the_write(world, monkeypatch) -> None:
    """The precondition is asserted PER ROW at execution time -- never off the
    classification alone."""
    cat, t3 = world
    _patch(monkeypatch, cat, t3, writer=_Writer())

    CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "tombstone-ghost-notes", "--no-dry-run", "--confirm",
    ])

    probed_titles = {t for kind, t in t3.probed if kind == "title"}
    assert {"gone-by-both-keys", "gone-no-chash", "kept-title-chunks", "probe-fails"} <= probed_titles
    assert ("chash", "c" * 64) in t3.probed, "a chash is tried before the title"
    # The manifest-resurfaced note never reaches T3: the invariant skip is first.
    assert "manifest-resurfaced" not in probed_titles


def test_no_exempt_notes_means_nothing_to_do(monkeypatch) -> None:
    cat = _Cat([_Entry("1.11.7", "file-backed", file_path="/p.md")])
    writer = _Writer()
    _patch(monkeypatch, cat, _T3(), writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "tombstone-ghost-notes", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert writer.deleted == []
    assert "Nothing to tombstone" in result.output


def test_without_confirm_nothing_is_written(world, monkeypatch) -> None:
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale", "--execute", "tombstone-ghost-notes", "--no-dry-run",
    ])

    assert result.exit_code == 0, result.output
    assert writer.deleted == []
