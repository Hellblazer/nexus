# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: ``nx catalog backfill-collections`` verb.

The collections projection ships empty in existing catalogs because the
table is added by the Phase 6 schema and CollectionCreated events were
no-ops in Phases 1-5. This verb walks both T3 (live ChromaDB) and the
catalog ``documents.physical_collection`` column, unions their
collection-name sets, and emits a CollectionCreated event for each
name not already in the projection.

Conformance is decided by the projector via
``is_conformant_collection_name``, not by the caller. The
backfill writer supplies empty canonical fields, which the projector
preserves verbatim; conformant names that callers want decomposed
into segments should re-register with the parsed segments after
backfill.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_document(catalog: Catalog, *, tumbler: str, collection: str) -> None:
    catalog._db.execute(
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "text", f"/tmp/{tumbler}.md",
            "", collection, 1, "", "", "{}", 0.0, "", "",
        ),
    )
    catalog._db.commit()


class _FakeT3:
    """Minimal stand-in for T3Database.list_collections.

    Returns a list of {"name": str} dicts, mirroring the real method's
    shape (matches usage in commands/t3.py prune-stale).
    """

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_collections(self) -> list[dict]:
        return [{"name": n} for n in self._names]


def test_backfill_registers_t3_and_catalog_collections(catalog, runner):
    """Every name from T3 and from documents.physical_collection becomes
    a row in the collections projection.
    """
    fake_t3 = _FakeT3(
        names=[
            "code__1-1__voyage-code-3__v1",  # conformant
            "knowledge__delos",              # legacy
            "rdr__nexus-571b8edd",           # legacy
        ]
    )
    _seed_document(catalog, tumbler="1.1.1", collection="docs__nexus-571b8edd")

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])

    assert result.exit_code == 0, result.output
    rows = catalog.list_collections()
    names = sorted(r["name"] for r in rows)
    assert names == [
        "code__1-1__voyage-code-3__v1",
        "docs__nexus-571b8edd",
        "knowledge__delos",
        "rdr__nexus-571b8edd",
    ]


def test_backfill_marks_legacy_via_projector(catalog, runner):
    """Non-conformant names land with legacy_grandfathered=True;
    conformant names land False.
    """
    fake_t3 = _FakeT3(
        names=[
            "code__1-1__voyage-code-3__v1",
            "knowledge__delos",
        ]
    )

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])

    assert result.exit_code == 0, result.output
    assert catalog.is_legacy_collection("knowledge__delos") is True
    assert catalog.is_legacy_collection("code__1-1__voyage-code-3__v1") is False


def test_backfill_idempotent(catalog, runner):
    """Re-running backfill on an already-backfilled catalog produces
    one row per name (no duplicates) and reports zero new rows.
    """
    fake_t3 = _FakeT3(names=["knowledge__delos"])

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])
        result = runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])

    assert result.exit_code == 0
    rows = catalog._db.execute(
        "SELECT COUNT(*) FROM collections WHERE name = ?",
        ("knowledge__delos",),
    ).fetchone()
    assert rows[0] == 1


def test_backfill_dry_run_no_writes(catalog, runner):
    """``--dry-run`` reports the candidates and writes nothing."""
    fake_t3 = _FakeT3(names=["knowledge__delos", "docs__nexus-571b8edd"])

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main, ["catalog", "backfill-collections", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert "knowledge__delos" in result.output
    assert "docs__nexus-571b8edd" in result.output
    assert "would register" in result.output
    rows = catalog._db.execute("SELECT COUNT(*) FROM collections").fetchone()
    assert rows[0] == 0


def test_backfill_empty_t3_and_catalog(catalog, runner):
    """Nothing to backfill produces a clean zero summary."""
    fake_t3 = _FakeT3(names=[])

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])

    assert result.exit_code == 0
    assert "Nothing to backfill" in result.output
    assert "0 new" in result.output


def test_backfill_skips_already_registered(catalog, runner):
    """Names already in the collections projection are not re-emitted."""
    catalog.register_collection("knowledge__delos")
    fake_t3 = _FakeT3(names=["knowledge__delos", "docs__nexus-571b8edd"])

    with patch("nexus.db.make_t3", return_value=fake_t3), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(main, ["catalog", "backfill-collections", "--no-dry-run"])

    assert result.exit_code == 0, result.output
    rows = catalog.list_collections()
    names = sorted(r["name"] for r in rows)
    assert names == ["docs__nexus-571b8edd", "knowledge__delos"]
    assert "Done: 1 new" in result.output


def test_backfill_aborts_on_t3_failure(catalog, runner):
    """T3 list_collections raising must abort the verb with a non-zero
    exit, NOT silently fall back to a catalog-only partial backfill.

    A partial backfill is operationally hostile: the operator gets a
    green exit and half the projection missing. Re-running the verb
    after T3 recovers would silently fix it, but the operator never
    learned T3 was down to begin with.
    """
    class _BrokenT3:
        def list_collections(self):
            raise RuntimeError("t3 unreachable")

    _seed_document(catalog, tumbler="1.1.1", collection="docs__nexus-571b8edd")

    with patch("nexus.db.make_t3", return_value=_BrokenT3()), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main, ["catalog", "backfill-collections", "--no-dry-run"],
        )
    assert result.exit_code != 0
    assert "Failed to list T3 collections" in result.output
    # No partial backfill happened
    rows = catalog._db.execute("SELECT COUNT(*) FROM collections").fetchone()
    assert rows[0] == 0
