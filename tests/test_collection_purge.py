# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 nexus-prgf4: the shared collection-delete cascade.

purge_collection_cascade deletes a T3 collection and best-effort-purges all
derived state. A failed derived-state step must NOT block the physical delete,
but MUST be recorded in ``failures`` so callers can surface it (the silence
regression both P4-follow-up reviewers flagged).
"""
from __future__ import annotations

import chromadb
import pytest

from nexus.db.collection_purge import CascadeCounts, purge_collection_cascade
from nexus.db.storage_mode import StorageBackend
from nexus.db.t3 import T3Database


@pytest.fixture()
def t3() -> T3Database:
    client = chromadb.EphemeralClient()
    return T3Database(_client=client)


def _pin_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the catalog backend to sqlite so the client-side fan-out path runs.

    The hard default is SERVICE (RDR-152); without this the cascade would take
    the RDR-164 P2 service branch and try to reach a service that is not up in
    the unit env. (feedback: pin local mode in mode-sensitive tests.)"""
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SQLITE,
    )


def _seed(t3: T3Database, name: str) -> None:
    try:
        t3._client.delete_collection(name)
    except Exception:
        pass
    col = t3._client.get_or_create_collection(name)
    col.add(ids=["x"], embeddings=[[0.1] * 384], documents=["hi"],
            metadatas=[{"source_path": "a.md"}])


def test_t2_failure_recorded_but_t3_still_deleted(
    t3: T3Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_local(monkeypatch)
    name = "docs__purge__minilm-l6-v2-384__v1"
    _seed(t3, name)

    def _boom(_cascade):
        raise RuntimeError("daemon down")

    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", _boom)

    counts = purge_collection_cascade(t3, name)

    assert not t3.collection_exists(name)  # physical delete still happened
    assert any("taxonomy/chash cascade failed" in f for f in counts.failures)
    assert "daemon down" in " ".join(counts.failures)


def test_clean_run_has_no_failures(
    t3: T3Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_local(monkeypatch)
    name = "docs__purge2__minilm-l6-v2-384__v1"
    _seed(t3, name)
    # Stub the derived-state steps to succeed quietly.
    monkeypatch.setattr(
        "nexus.mcp_infra.t2_index_write",
        lambda _c: ({"topics": 0, "assignments": 0, "links": 0, "meta": 0}, 0),
    )

    counts = purge_collection_cascade(t3, name)

    assert not t3.collection_exists(name)
    # catalog/pipeline may legitimately be absent in the test env; the T2 step
    # we stubbed must not have failed.
    assert all("taxonomy/chash" not in f for f in counts.failures)
    assert isinstance(counts, CascadeCounts)


def test_service_mode_uses_single_endpoint_and_maps_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RDR-164 P2: in service mode the cascade must call the ONE atomic
    # deleteCollection endpoint and map its per-table counts — NOT fan out to
    # the local t2_index_write / chroma path.
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )
    # A db whose delete_collection MUST NOT be called in service mode (the endpoint
    # owns the chunk delete). If the service branch wrongly fell through to the local
    # fan-out it would invoke this and fail the test loudly.
    class _ExplodingDb:
        def delete_collection(self, _name: str) -> None:
            raise AssertionError("service mode must not call db.delete_collection (local fan-out)")

    class _FakeClient:
        def delete_collection(self, name: str) -> dict[str, int]:
            return {
                "chunks_384": 5, "chunks_768": 0, "chunks_1024": 0,
                "chash_index": 5,
                "topic_assignments": 3, "topics": 2, "taxonomy_meta": 1,
                "taxonomy_centroids_384": 2, "taxonomy_centroids_768": 0,
                "taxonomy_centroids_1024": 0,
                "document_aspects": 4, "document_highlights": 1,
                "aspect_extraction_queue": 7,
                "catalog_documents": 6, "catalog_collections": 1,
            }

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _FakeClient()
    )

    counts = purge_collection_cascade(_ExplodingDb(), "knowledge__svc__minilm-l6-v2-384__v1")

    assert counts.chash_deleted == 5
    assert counts.catalog_docs_deleted == 6
    assert counts.catalog_projection_deleted == 1
    # Dict shape must match the local fan-out (topics/assignments/links/meta) so the CLI
    # render does not KeyError; centroids is the service-only addition.
    assert counts.taxonomy == {
        "topics": 2, "assignments": 3, "links": 0, "meta": 1, "centroids": 2,
    }
    assert counts.failures == []


def test_service_mode_endpoint_failure_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )

    class _BoomClient:
        def delete_collection(self, name: str) -> dict[str, int]:
            raise RuntimeError("service unreachable")

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _BoomClient()
    )

    counts = purge_collection_cascade(object(), "knowledge__svc2__minilm-l6-v2-384__v1")

    assert any("service deleteCollection failed" in f for f in counts.failures)
    assert "service unreachable" in " ".join(counts.failures)
