# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: ``nx catalog rename-collection`` verb.

Combined verb that does both the data-plane rename (T3 native modify
+ T2 cascade + catalog docs re-point) and the Phase 6 control-plane
work (conformance check on new name, collections projection update,
CollectionSuperseded event emission).

Validation gates fire BEFORE any side effect:
  - new name must be conformant (or --allow-legacy)
  - old name must be in the collections projection
  - old name must not already be superseded
  - new name must not already exist in T3

Tests use a real T3Database (chromadb.EphemeralClient) and a real
Catalog rooted at tmp_path so the data plane and event log are both
exercised.
"""
from __future__ import annotations

from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.catalog.events import TYPE_COLLECTION_SUPERSEDED
from nexus.cli import main
from nexus.db.t3 import T3Database


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def t3_db():
    """Fresh ephemeral T3 with all pre-existing collections deleted.

    chromadb.EphemeralClient is process-singleton-ish so other tests'
    collections persist across fixture rebuilds; explicitly clearing
    keeps assertions about collection_exists deterministic.
    """
    db = T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_t3_collection(t3_db: T3Database, name: str) -> None:
    """Create a minimal collection in T3."""
    col = t3_db._client.get_or_create_collection(name)
    col.add(ids=["c1"], documents=["seeded chunk"], metadatas=[{"doc_id": "1.1.1"}])


def _seed_catalog_doc(catalog: Catalog, *, tumbler: str, collection: str) -> None:
    catalog._db.execute(  # epsilon-allow: fixture seeds a documents row with caller-pinned tumbler; Catalog.register mints its own owner-prefixed tumbler
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


# ── Validation gates fire before side effects ────────────────────────────


def test_rename_rejects_non_conformant_new(t3_db, catalog, runner):
    """The new name must match is_conformant_collection_name unless --allow-legacy."""
    catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos", "knowledge__papers"],
        )
    assert result.exit_code != 0
    assert "not conformant" in result.output.lower()
    # T3 untouched (old still exists, new wasn't created)
    assert t3_db.collection_exists("knowledge__delos")
    assert not t3_db.collection_exists("knowledge__papers")


def test_rename_rejects_old_not_in_projection(t3_db, catalog, runner):
    _seed_t3_collection(t3_db, "knowledge__delos")
    # Note: old name not registered in the collections projection.

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code != 0
    assert "not registered" in result.output.lower()


def test_rename_rejects_already_superseded(t3_db, catalog, runner):
    catalog.register_collection("knowledge__delos")
    catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    catalog.supersede_collection(
        "knowledge__delos", "knowledge__1-1__voyage-context-3__v1",
    )
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v2"],
        )
    assert result.exit_code != 0
    assert "superseded" in result.output.lower()


def test_rename_rejects_new_already_exists_in_t3(t3_db, catalog, runner):
    catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__1-1__voyage-context-3__v1")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code != 0
    assert "already exists" in result.output.lower()


# ── Happy path ────────────────────────────────────────────────────────────


def test_rename_conformant_new_succeeds(t3_db, catalog, runner):
    """End-to-end rename: T3 collection moves, projection rows updated,
    CollectionSuperseded event emitted, catalog docs re-pointed.
    """
    catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")
    _seed_catalog_doc(catalog, tumbler="1.1.1", collection="knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1",
             "--yes"],
        )
    assert result.exit_code == 0, result.output

    # T3: new exists, old doesn't
    assert not t3_db.collection_exists("knowledge__delos")
    assert t3_db.collection_exists("knowledge__1-1__voyage-context-3__v1")

    # Collections projection: new is registered + not legacy; old is superseded.
    new_row = catalog.get_collection("knowledge__1-1__voyage-context-3__v1")
    assert new_row is not None
    assert new_row["legacy_grandfathered"] is False
    assert new_row["content_type"] == "knowledge"
    assert new_row["embedding_model"] == "voyage-context-3"

    old_row = catalog.get_collection("knowledge__delos")
    assert old_row is not None
    assert old_row["superseded_by"] == "knowledge__1-1__voyage-context-3__v1"
    assert old_row["superseded_at"]

    # CollectionSuperseded event in the log
    events = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_SUPERSEDED
    ]
    assert len(events) == 1
    assert events[0].payload.old_coll_id == "knowledge__delos"
    assert events[0].payload.new_coll_id == "knowledge__1-1__voyage-context-3__v1"

    # Catalog documents re-pointed
    rows = catalog._db.execute(
        "SELECT physical_collection FROM documents WHERE tumbler = ?",
        ("1.1.1",),
    ).fetchone()
    assert rows[0] == "knowledge__1-1__voyage-context-3__v1"


def test_rename_dry_run_no_writes(t3_db, catalog, runner):
    """``--dry-run`` reports the plan and writes nothing."""
    catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1",
             "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "would rename" in result.output.lower()
    assert t3_db.collection_exists("knowledge__delos")
    assert not t3_db.collection_exists("knowledge__1-1__voyage-context-3__v1")
    # Old projection row not yet superseded
    old_row = catalog.get_collection("knowledge__delos")
    assert old_row["superseded_by"] == ""


def test_rename_no_yes_falls_back_to_report_only(t3_db, catalog, runner):
    """Neither ``--yes`` nor ``--dry-run`` falls back to report-only."""
    catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code == 0
    assert "add --yes" in result.output.lower()
    assert t3_db.collection_exists("knowledge__delos")


def test_rename_to_self_rejected(t3_db, catalog, runner):
    """Renaming OLD to itself is a no-op; reject with a clear message
    rather than running the full data plane that would then trip the
    ``new already exists`` gate (misleading).
    """
    catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    _seed_t3_collection(t3_db, "knowledge__1-1__voyage-context-3__v1")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__1-1__voyage-context-3__v1",
             "knowledge__1-1__voyage-context-3__v1",
             "--yes"],
        )
    assert result.exit_code != 0
    assert "identical" in result.output.lower()


def test_rename_allow_legacy_lets_non_conformant_through(t3_db, catalog, runner):
    """``--allow-legacy`` skips the conformance gate; the new collection
    is registered as legacy_grandfathered=True via the projector regex.
    """
    catalog.register_collection("docs__nexus-571b8edd")
    _seed_t3_collection(t3_db, "docs__nexus-571b8edd")

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "docs__nexus-571b8edd",
             "docs__renamed-legacy",
             "--yes", "--allow-legacy"],
        )
    assert result.exit_code == 0, result.output
    assert t3_db.collection_exists("docs__renamed-legacy")
    new_row = catalog.get_collection("docs__renamed-legacy")
    assert new_row is not None
    assert new_row["legacy_grandfathered"] is True
