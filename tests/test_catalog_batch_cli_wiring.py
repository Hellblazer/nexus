# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vxgnh: the update_many/delete_many BATCHED branch at the 3 CLI sites.

nexus-xedhp wired ``getattr(writer, "update_many"/"delete_many", None)``
capability checks into ``nx catalog update`` (batch mode), ``nx catalog gc``,
and ``nx catalog prune-stale`` — but every pre-existing CLI test ran against
the local SQLite writer, which lacks those methods by design
(_SERVICE_ONLY_WRITE_OPS), so ONLY the unbatched fallback branch was ever
exercised. These tests drive each command through the documented
``nexus.commands.catalog._get_catalog`` / ``_get_catalog_writer`` seams with
a capability-bearing stub, asserting: the batched call fires exactly once
with the right payload, the per-entry fallback stays silent, and (for the
capability-absent stub) the fallback still works.

The 4th call site (indexer Pass 1b) is already covered by
tests/test_catalog_indexer_hook.py::TestCatalogHookBatchedServiceMode.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.tumbler import Tumbler
from nexus.cli import main


class _Entry:
    def __init__(self, tumbler: str, file_path: str = "", meta: dict | None = None):
        self.tumbler = Tumbler.parse(tumbler)
        self.title = f"doc {tumbler}"
        self.file_path = file_path
        self.physical_collection = "knowledge__t__v1"
        self.meta = meta or {}


class _StubCat:
    """Reader stub: just enough surface for the three commands."""

    def __init__(self, entries: list[_Entry]):
        self._entries = entries

    def by_owner(self, _owner):
        return list(self._entries)

    def all_documents(self, limit: int = 200, offset: int = 0):
        page = self._entries[offset : offset + limit]
        return list(page)

    def owners_with_roots(self):
        return {"1.1": "/nonexistent-root-vxgnh"}

    def find(self, _q):
        return list(self._entries)


class _BatchWriter:
    """Service-shaped writer: HAS update_many/delete_many, records calls."""

    def __init__(self):
        self.update_many_calls: list[list[dict]] = []
        self.delete_many_calls: list[list] = []
        self.update_calls: list = []
        self.delete_document_calls: list = []

    def update_many(self, docs: list[dict]) -> list[int]:
        self.update_many_calls.append(docs)
        return [1] * len(docs)

    def delete_many(self, tumblers: list) -> list:
        self.delete_many_calls.append(list(tumblers))
        return list(tumblers)

    def update(self, tumbler, **fields):
        self.update_calls.append((tumbler, fields))

    def delete_document(self, tumbler) -> bool:
        self.delete_document_calls.append(tumbler)
        return True

    def close(self):
        pass


class _FallbackWriter(_BatchWriter):
    """SQLite-shaped writer: NO batch capabilities (attrs absent, not None)."""
    update_many = None  # type: ignore[assignment]
    delete_many = None  # type: ignore[assignment]


def _wire(monkeypatch, cat, writer):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)
    # gc backs up before deleting — neutralize the snapshot machinery.
    monkeypatch.setattr(
        "nexus.catalog.catalog_backup.snapshot_documents",
        lambda *a, **kw: None,
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── nx catalog update --owner (batch mode) ───────────────────────────────────


def test_update_owner_batch_routes_through_update_many(runner, monkeypatch):
    cat = _StubCat([_Entry("1.1.1"), _Entry("1.1.2")])
    writer = _BatchWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(main, ["catalog", "update", "--owner", "1.1", "--author", "New A"])

    assert result.exit_code == 0, result.output
    assert "Updated 2 entries" in result.output
    assert len(writer.update_many_calls) == 1, "batched path must fire once"
    payload = writer.update_many_calls[0]
    assert [d["tumbler"] for d in payload] == ["1.1.1", "1.1.2"]
    assert all(d["author"] == "New A" for d in payload)
    assert writer.update_calls == [], "per-entry update() must stay silent"


def test_update_owner_batch_falls_back_without_capability(runner, monkeypatch):
    cat = _StubCat([_Entry("1.1.1"), _Entry("1.1.2")])
    writer = _FallbackWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(main, ["catalog", "update", "--owner", "1.1", "--author", "New A"])

    assert result.exit_code == 0, result.output
    assert len(writer.update_calls) == 2, "capability-absent writer uses per-entry update()"


# ── nx catalog gc ────────────────────────────────────────────────────────────


def _orphan(tumbler: str) -> _Entry:
    return _Entry(tumbler, file_path=f"src/{tumbler}.py", meta={"miss_count": 3})


def test_gc_routes_through_delete_many(runner, monkeypatch):
    cat = _StubCat([_orphan("1.1.1"), _orphan("1.1.2")])
    writer = _BatchWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(main, ["catalog", "gc", "--no-dry-run", "--confirm"])

    assert result.exit_code == 0, result.output
    assert "Deleted 2 orphan entries" in result.output
    assert len(writer.delete_many_calls) == 1
    assert [str(t) for t in writer.delete_many_calls[0]] == ["1.1.1", "1.1.2"]
    assert writer.delete_document_calls == [], "per-entry delete must stay silent"


def test_gc_falls_back_without_capability(runner, monkeypatch):
    cat = _StubCat([_orphan("1.1.1")])
    writer = _FallbackWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(main, ["catalog", "gc", "--no-dry-run", "--confirm"])

    assert result.exit_code == 0, result.output
    assert len(writer.delete_document_calls) == 1


# ── nx catalog prune-stale ───────────────────────────────────────────────────


def test_prune_stale_routes_through_delete_many(runner, monkeypatch, tmp_path):
    # Relative file_path anchored at a nonexistent owner root → stale.
    cat = _StubCat([_Entry("1.1.1", file_path="src/gone_a.py"),
                    _Entry("1.1.2", file_path="src/gone_b.py")])
    writer = _BatchWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(
        main, ["catalog", "prune-stale", "--no-dry-run", "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert len(writer.delete_many_calls) == 1, result.output
    assert [str(t) for t in writer.delete_many_calls[0]] == ["1.1.1", "1.1.2"]
    assert writer.delete_document_calls == []
    assert "deleted 2 catalog entries" in result.output


def test_prune_stale_falls_back_without_capability(runner, monkeypatch):
    cat = _StubCat([_Entry("1.1.1", file_path="src/gone.py")])
    writer = _FallbackWriter()
    _wire(monkeypatch, cat, writer)

    result = runner.invoke(
        main, ["catalog", "prune-stale", "--no-dry-run", "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert len(writer.delete_document_calls) == 1
