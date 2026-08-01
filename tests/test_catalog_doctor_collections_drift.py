# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: ``nx catalog doctor --collections-drift``.

The collections projection (Phase 6 deliverable) is canonical: every
collection name that T3 or the catalog documents knows about must
have a row. Drift is a release blocker: a missing projection row
means downstream Phase 6 work (rename-collection, supersede invariants,
strict naming validation) silently sees the collection as "unknown"
and either skips it or emits incorrect events.

This check verifies:
  - Every T3 collection has a row in the projection (else FAIL,
    operator runs ``nx catalog backfill-collections``).
  - Every distinct ``documents.physical_collection`` value has a row.
  - Projection rows pointing at a T3 collection that no longer exists
    are flagged as orphans UNLESS ``superseded_by`` is set (an
    expected post-rename state).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def t3_db():
    db = T3Database(
        _client=make_vector_test_client(),
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
def catalog():
    # nexus-i711w terminal deletion: seed the ACTIVE (service) catalog —
    # the CLI's own ``_get_catalog()`` resolves the same live catalog, so
    # the ``_get_catalog`` patches died with the local Catalog fixture.
    from tests._catalog_fixture_ops import ActiveCatalog
    return ActiveCatalog()


def _seed_t3(t3_db: T3Database, name: str) -> None:
    col = t3_db._client.get_or_create_collection(name)
    col.add(ids=["c1"], documents=["x"], metadatas=[{"placeholder": "1"}])


def _seed_doc(catalog, *, tumbler: str, collection: str) -> None:
    # Was a raw pinned-tumbler INSERT; the drift check only needs SOME
    # documents row whose physical_collection is *collection*, and
    # register() is the path that exists on the live substrate.
    owner = catalog.register_owner("drift-seed", "curator", repo_hash="")
    catalog.register(
        owner, f"doc-{tumbler}", content_type="text",
        file_path=f"/tmp/{tumbler}.md", physical_collection=collection,
    )


def test_doctor_collections_drift_passes_when_aligned(t3_db, catalog, runner):
    """T3, catalog docs, and projection all aligned → PASS, exit 0."""
    catalog.register_collection("knowledge__delos")
    _seed_t3(t3_db, "knowledge__delos")
    _seed_doc(catalog, tumbler="1.1.1", collection="knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_doctor_collections_drift_fails_on_t3_not_in_projection(
    t3_db, catalog, runner,
):
    """A T3 collection without a projection row is drift → FAIL."""
    _seed_t3(t3_db, "knowledge__delos")  # no register_collection call

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0
    assert "knowledge__delos" in result.output
    assert "FAIL" in result.output


def test_doctor_collections_drift_fails_on_doc_collection_not_in_projection(
    t3_db, catalog, runner,
):
    """A documents.physical_collection without a projection row is drift."""
    _seed_doc(catalog, tumbler="1.1.1", collection="docs__nexus-571b8edd")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0
    assert "docs__nexus-571b8edd" in result.output


def test_doctor_collections_drift_orphan_warning_with_superseded_skip(
    t3_db, catalog, runner,
):
    """Projection row whose underlying T3 collection is gone is an orphan
    UNLESS the row carries ``superseded_by`` (the expected post-rename
    state).
    """
    catalog.register_collection("knowledge__delos")
    catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    catalog.supersede_collection(
        "knowledge__delos", "knowledge__1-1__voyage-context-3__v1",
    )
    _seed_t3(t3_db, "knowledge__1-1__voyage-context-3__v1")
    # Note: knowledge__delos NOT in T3 (gone post-rename) but is
    # superseded_by, so should NOT count as drift.

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code == 0, result.output
    assert "knowledge__delos" not in result.output or "FAIL" not in result.output


def test_doctor_collections_drift_orphan_without_supersede_fails(
    t3_db, catalog, runner,
):
    """Projection row whose T3 collection is gone AND no superseded_by
    is genuine drift (projection ahead of T3).
    """
    catalog.register_collection("knowledge__delos")
    # knowledge__delos in projection but NOT in T3 and NOT superseded.

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0
    assert "knowledge__delos" in result.output


def test_doctor_collections_drift_handles_t3_failure(catalog, runner):
    """When T3 list_collections raises, the check returns ``error``-keyed
    payload and the doctor exits non-zero. Pass-#2 review found this
    path had no test; without it, a silent T3 outage produces a green
    PASS.
    """
    class _BrokenT3:
        def list_collections(self):
            raise RuntimeError("t3 unreachable")

    with patch("nexus.db.make_t3", return_value=_BrokenT3()):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0
    assert "Failed to list T3" in result.output


def test_doctor_collections_drift_json_payload(t3_db, catalog, runner):
    """``--json`` emits machine-readable shape."""
    _seed_t3(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift", "--json"],
        )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert "collections_drift" in payload
    drift = payload["collections_drift"]
    assert drift["pass"] is False
    assert "knowledge__delos" in drift["t3_not_in_projection"]
