# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog reconcile-stale --execute drop-orphan-collections``. nexus-8tnz2.

T3 collections with chunks but ZERO catalog documents referencing them --
benchmark/gate debris (T2
nexus/catalog-cleanup-2026-08-03-executed-and-prevention [21385] item 3).
Consumes the SAME classification (``classify_t3_orphan_collections``,
src/nexus/commands/catalog_cmds/t3_orphans.py) that ``nx catalog doctor
--t3-vs-catalog`` reports as ``t3_orphans`` and ``nx catalog verify``
reports as ``orphan_collections``.

Fakes mirror ``tests/test_catalog_reconcile_stale.py`` /
``tests/test_reconcile_stale_ghost_notes.py``.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main


class _Cat:
    """Empty-catalog fake: this arm only cares about
    ``collection_doc_counts()`` (used by ``classify_t3_orphan_collections``
    to decide which T3 collections have zero LIVE referencing docs, and
    which of those are tombstoned-only vs genuinely orphaned) and
    ``all_documents``/``get_manifests``/``owners_with_roots`` (used by the
    base ``_classify`` census every ``reconcile-stale`` invocation runs
    first, regardless of --execute arm).

    nexus-8tnz2 fix-round CRITICAL 2: ``doc_counts`` models the default,
    live-only read; ``doc_counts_all`` models the ``include_deleted=True``
    read (live + tombstoned). Defaults to the SAME dict as ``doc_counts``
    (no tombstone population) unless a test explicitly diverges them.
    """

    def __init__(
        self, doc_counts: dict[str, int] | None = None,
        doc_counts_all: dict[str, int] | None = None,
        *, doc_counts_all_exc: Exception | None = None,
    ):
        self._doc_counts = doc_counts or {}
        self._doc_counts_all = (
            dict(self._doc_counts) if doc_counts_all is None else doc_counts_all
        )
        self._doc_counts_all_exc = doc_counts_all_exc

    def all_documents(self, limit=0):
        return []

    def stats(self):
        return {"doc_count": 0}

    def collection_doc_counts(self, *, include_deleted=False):
        if include_deleted:
            if self._doc_counts_all_exc is not None:
                raise self._doc_counts_all_exc
            return dict(self._doc_counts_all)
        return dict(self._doc_counts)

    def owners_with_roots(self):
        return {}

    def get_manifests(self, doc_ids):
        return {}


class _Collection:
    def __init__(self, count: int | None = None, raises: Exception | None = None):
        self._count = count
        self._raises = raises

    def count(self):
        if self._raises is not None:
            raise self._raises
        return self._count


class _T3:
    def __init__(
        self, names: list[str], *, counts: dict[str, int] | None = None,
        count_errors: dict[str, Exception] | None = None,
        list_collections_exc: Exception | None = None,
    ):
        self._names = names
        self._counts = counts or {}
        self._count_errors = count_errors or {}
        self._list_collections_exc = list_collections_exc

    def list_collections(self):
        if self._list_collections_exc is not None:
            raise self._list_collections_exc
        return [{"name": n} for n in self._names]

    def get_collection(self, name):
        if name in self._count_errors:
            return _Collection(raises=self._count_errors[name])
        return _Collection(count=self._counts.get(name, 0))


class _Writer:
    def __init__(self, *, raise_for: set[str] | None = None):
        self.deleted: list[str] = []
        self.closed = False
        self._raise_for = raise_for or set()

    def delete_collection(self, name):
        if name in self._raise_for:
            raise RuntimeError(f"simulated delete failure for {name}")
        self.deleted.append(name)
        return {"chunks": 3, "catalog_documents": 0}

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
    """Two genuine orphans (chunks present, zero catalog docs -- live or
    tombstoned), one tombstoned-only collection (chunks present, zero LIVE
    docs but 2 tombstoned/restorable docs -- nexus-8tnz2 fix-round
    CRITICAL 2, never a delete target), one zombie (zero chunks, zero
    docs -- not a target for THIS arm), one normal collection (docs
    reference it), and one whose chunk count read fails (unresolvable --
    never a delete target)."""
    t3_names = [
        "code__test-repo-abc123__voyage-code-3__v1",  # orphan: 5 chunks, 0 docs (live+all)
        "docs__hotfix_smoke",                          # orphan: 2 chunks, 0 docs (live+all)
        "docs__1-2188",                                 # tombstoned-only: 7 chunks, 0 live, 2828 tombstoned
        "docs__empty_zombie",                          # 0 chunks, 0 docs -- not this arm's target
        "knowledge__normal",                           # 3 chunks, 3 docs -- clean
        "docs__unreadable",                             # count() raises -- unresolvable
    ]
    cat = _Cat(
        doc_counts={"knowledge__normal": 3},  # live-only: docs__1-2188 absent -> 0
        doc_counts_all={"knowledge__normal": 3, "docs__1-2188": 2828},
    )
    t3 = _T3(
        t3_names,
        counts={
            "code__test-repo-abc123__voyage-code-3__v1": 5,
            "docs__hotfix_smoke": 2,
            "docs__1-2188": 7,
            "docs__empty_zombie": 0,
            "knowledge__normal": 3,
        },
        count_errors={"docs__unreadable": RuntimeError("T3 read timeout")},
    )
    return cat, t3


def test_dry_run_default_lists_candidates_and_never_builds_the_writer(world, monkeypatch) -> None:
    cat, t3 = world
    _patch(monkeypatch, cat, t3, writer=None)

    result = CliRunner().invoke(
        main, ["catalog", "reconcile-stale", "--execute", "drop-orphan-collections"],
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "drop-orphan-collections: 2 candidate(s)" in out, out
    assert "code__test-repo-abc123__voyage-code-3__v1" in out
    assert "docs__hotfix_smoke" in out
    assert "docs__empty_zombie" not in out  # zombie, not an orphan
    assert "1 collection(s) are tombstoned-only" in out, out
    assert "docs__1-2188" in out
    assert "tombstoned_docs=2828" in out, out
    assert "skipped 1 whose chunk count could not be read from T3" in out, out
    assert "docs__unreadable" in out
    assert "dry-run" in out.lower()


def test_write_time_guard_line_names_the_bead(world, monkeypatch) -> None:
    cat, t3 = world
    _patch(monkeypatch, cat, t3, writer=None)

    result = CliRunner().invoke(
        main, ["catalog", "reconcile-stale", "--execute", "drop-orphan-collections"],
    )

    assert result.exit_code == 0, result.output
    assert "Write-time guard" in result.output
    assert "nexus-8tnz2" in result.output


def test_confirmed_drops_exactly_the_classified_orphans(world, monkeypatch) -> None:
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert set(writer.deleted) == {
        "code__test-repo-abc123__voyage-code-3__v1", "docs__hotfix_smoke",
    }
    assert writer.closed
    assert "Done: dropped 2 orphan collection(s)." in result.output
    # Never a raw vector-store delete or the empty zombie / unresolvable /
    # tombstoned-only rows -- a tombstoned-only collection is restorable
    # (nexus-8tnz2 fix-round CRITICAL 2) and must never be hard-deleted.
    assert "docs__empty_zombie" not in writer.deleted
    assert "docs__unreadable" not in writer.deleted
    assert "docs__1-2188" not in writer.deleted


def test_tombstoned_only_collection_is_listed_but_never_dropped(world, monkeypatch) -> None:
    """nexus-8tnz2 fix-round CRITICAL 2: a collection whose catalog docs
    are ALL soft-tombstoned (still restorable until purge-trash) is
    reported distinctly and is NEVER a delete target, even under
    --no-dry-run --confirm."""
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert "docs__1-2188" not in writer.deleted
    assert "tombstoned-only" in result.output
    assert "docs__1-2188" in result.output
    assert "tombstoned_docs=2828" in result.output


def test_unavailable_tombstone_count_refuses_execute(world, monkeypatch) -> None:
    """nexus-8tnz2 fix-round CRITICAL 2: when the include_deleted=True read
    itself fails, this arm must refuse --execute outright (INCOMPLETE) --
    it must never guess whether a zero-live-doc collection is a genuine
    orphan or merely tombstoned-only, and must never drop around the
    ambiguity."""
    cat, t3 = world
    cat._doc_counts_all_exc = RuntimeError("engine unreachable for include_deleted read")
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code != 0, result.output
    assert "INCOMPLETE" in result.output
    assert writer.deleted == []


def test_orphan_still_drops_when_tombstone_count_is_available(world, monkeypatch) -> None:
    """The disambiguation read must not become a blanket refusal -- when
    the tombstone count IS available and reads zero, a genuine orphan
    still drops normally."""
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert "code__test-repo-abc123__voyage-code-3__v1" in writer.deleted
    assert "docs__hotfix_smoke" in writer.deleted


def test_without_confirm_nothing_is_written(world, monkeypatch) -> None:
    cat, t3 = world
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale", "--execute", "drop-orphan-collections", "--no-dry-run",
    ])

    assert result.exit_code == 0, result.output
    assert writer.deleted == []


def test_no_orphans_means_nothing_to_drop(monkeypatch) -> None:
    cat = _Cat(doc_counts={"knowledge__normal": 3})
    t3 = _T3(["knowledge__normal"], counts={"knowledge__normal": 3})
    writer = _Writer()
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 0, result.output
    assert writer.deleted == []
    assert "Nothing to drop." in result.output


def test_classification_failure_refuses_incomplete_and_never_deletes(world, monkeypatch) -> None:
    """t3.list_collections() raising is the same INCOMPLETE contract every
    other arm honors -- refuse outright, never act on an untrustworthy
    classification."""
    cat, _t3 = world
    broken_t3 = _T3([], list_collections_exc=RuntimeError("engine unreachable"))
    writer = _Writer()
    _patch(monkeypatch, cat, broken_t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code != 0, result.output
    assert "INCOMPLETE" in result.output
    assert writer.deleted == []


def test_delete_failure_for_one_collection_reports_and_exits_nonzero(world, monkeypatch) -> None:
    cat, t3 = world
    writer = _Writer(raise_for={"docs__hotfix_smoke"})
    _patch(monkeypatch, cat, t3, writer=writer)

    result = CliRunner().invoke(main, [
        "catalog", "reconcile-stale",
        "--execute", "drop-orphan-collections", "--no-dry-run", "--confirm",
    ])

    assert result.exit_code == 1, result.output
    assert writer.deleted == ["code__test-repo-abc123__voyage-code-3__v1"]
    assert writer.closed
    assert "1 failure(s)" in result.output
    assert "docs__hotfix_smoke" in result.output
