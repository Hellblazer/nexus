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
from unittest.mock import MagicMock, patch

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


# ── nexus-e1k14: tombstoned-vs-absent split ─────────────────────────────
#
# A projection row whose T3 collection is missing from list_collections()
# is NOT automatically "gone" — on HttpVectorClient, list_collections()
# reads the tombstone-filtered collection_vector_stats view (RDR-156 P3),
# so a collection whose every document is trashed (deleted_at set, but the
# physical chunk rows still present) vanishes from it exactly like one
# that never existed. Printing the supersede recipe against a tombstoned
# name is destructive: superseding sets superseded_by, which permanently
# excludes the name from resolution, turning a reversible trash into an
# unreachable orphan. These tests exercise the fix with a REAL
# HttpVectorClient (only the network boundary -- module-level _get -- is
# patched), matching the pattern in
# tests/test_collection_rename_tombstone_probe.py, so the split is proven
# against the actual three-state dispatch, not a mock standing in for it.

from typing import Any as _Any  # noqa: E402 -- grouped with this section's imports

from nexus.db.collection_state import CollectionState  # noqa: E402
from nexus.db.http_vector_client import HttpVectorClient  # noqa: E402

_STATS_PATH = "/v1/vectors/stats"
_COLLECTIONS_PATH = "/v1/vectors/collections"


def _patch_stats_and_raw(monkeypatch, *, stats_names: set[str], raw_names: set[str]) -> None:
    def fake_get(path: str, *, tenant: str = "default") -> _Any:
        if path == _STATS_PATH:
            return [
                {"name": n, "dim": 384, "count": 1, "last_write": "x"}
                for n in stats_names
            ]
        if path == _COLLECTIONS_PATH:
            return [{"name": n} for n in raw_names]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)


def test_doctor_collections_drift_tombstoned_gets_restore_guidance_not_supersede(
    catalog, runner, monkeypatch,
):
    """A projection row whose T3 collection is TOMBSTONED (every chunk
    belongs to a trashed document, but the raw chunk rows are still
    present) must be reported with restore-first guidance -- never the
    supersede recipe, whose literal execution would destroy restorability.
    """
    catalog.register_collection("knowledge__delos")
    # Stats view (tombstone-filtered) is empty: no LIVE chunks.
    # Raw /v1/vectors/collections listing still names it: chunk rows
    # exist physically, every one just belongs to a trashed document.
    _patch_stats_and_raw(
        monkeypatch, stats_names=set(), raw_names={"knowledge__delos"},
    )
    client = HttpVectorClient()

    with patch("nexus.db.make_t3", return_value=client):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0, result.output
    assert "knowledge__delos" in result.output
    assert "TRASHED" in result.output or "RESTORABLE" in result.output
    assert "restore" in result.output.lower()
    # The destructive recipe must not be printed for this name: it must
    # not appear under the "gone and not superseded" supersede bucket.
    assert "gone and not" not in result.output
    assert "supersede_collection" not in result.output


def test_doctor_collections_drift_truly_absent_still_gets_supersede_recipe(
    catalog, runner, monkeypatch,
):
    """A projection row whose T3 collection genuinely never existed (no
    live chunks AND no raw chunk rows) is real drift -- the supersede
    recipe remains the correct remedy for this case.
    """
    catalog.register_collection("knowledge__delos")
    _patch_stats_and_raw(monkeypatch, stats_names=set(), raw_names=set())
    client = HttpVectorClient()

    with patch("nexus.db.make_t3", return_value=client):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift"],
        )
    assert result.exit_code != 0, result.output
    assert "knowledge__delos" in result.output
    assert "gone and not" in result.output
    assert "supersede_collection" in result.output
    assert "TRASHED" not in result.output


def test_doctor_collections_drift_json_payload_splits_tombstoned(
    catalog, runner, monkeypatch,
):
    """``--json`` exposes the split as two distinct keys."""
    catalog.register_collection("knowledge__delos")
    _patch_stats_and_raw(
        monkeypatch, stats_names=set(), raw_names={"knowledge__delos"},
    )
    client = HttpVectorClient()

    with patch("nexus.db.make_t3", return_value=client):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift", "--json"],
        )
    assert result.exit_code != 0
    drift = json.loads(result.stdout)["collections_drift"]
    assert drift["projection_tombstoned"] == ["knowledge__delos"]
    assert drift["projection_not_in_t3"] == []


def test_doctor_collections_drift_probe_failure_is_reported_not_fatal(
    catalog, runner, monkeypatch,
):
    """A transient ``probe_collection_state`` failure for ONE candidate
    name must not kill the whole check: the other candidate is still
    classified normally, and the failing name is reported in a dedicated
    "needs rerun" bucket -- never silently dropped, never misclassified
    into either remedy bucket (nexus-e1k14 critique fixup).
    """
    catalog.register_collection("knowledge__delos")
    catalog.register_collection("knowledge__thera")

    def fake_probe(t3_db, name):
        if name == "knowledge__delos":
            raise RuntimeError("transient network error")
        return CollectionState.ABSENT

    monkeypatch.setattr(
        "nexus.db.collection_state.probe_collection_state", fake_probe,
    )
    t3_db = MagicMock()
    t3_db.list_collections.return_value = []

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift", "--json"],
        )
    assert result.exit_code != 0, result.output
    drift = json.loads(result.stdout)["collections_drift"]
    assert drift["projection_unprobed"] == [
        {"name": "knowledge__delos", "error": "transient network error"},
    ]
    assert drift["projection_not_in_t3"] == ["knowledge__thera"]
    assert drift["projection_tombstoned"] == []
    assert drift["pass"] is False


def test_doctor_collections_drift_present_mid_loop_is_skipped_entirely(
    catalog, runner, monkeypatch,
):
    """TOCTOU: the collection was restored between the T3/projection
    snapshot and the per-name probe. A live (PRESENT) collection must
    never receive either recipe -- it is skipped entirely, not flagged
    as drift under any bucket.
    """
    catalog.register_collection("knowledge__delos")

    def fake_probe(t3_db, name):
        return CollectionState.PRESENT

    monkeypatch.setattr(
        "nexus.db.collection_state.probe_collection_state", fake_probe,
    )
    t3_db = MagicMock()
    t3_db.list_collections.return_value = []

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["catalog", "doctor", "--collections-drift", "--json"],
        )
    assert result.exit_code == 0, result.output
    drift = json.loads(result.stdout)["collections_drift"]
    assert drift["projection_not_in_t3"] == []
    assert drift["projection_tombstoned"] == []
    assert drift["projection_unprobed"] == []
    assert drift["pass"] is True
