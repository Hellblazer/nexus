# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 nexus-prgf4: the shared collection-delete cascade.

purge_collection_cascade deletes a T3 collection and best-effort-purges all
derived state via the engine's ONE atomic deleteCollection endpoint
(RDR-164 P2). A failed step must be recorded in ``failures`` so callers can
surface it (the silence regression both P4-follow-up reviewers flagged).
The local (sqlite/Chroma) fan-out arm and its tests died with the =sqlite
opt-out (RDR-158 P3, nexus-7bomn).
"""
from __future__ import annotations

import pytest

from nexus.db.collection_purge import purge_collection_cascade


def test_service_mode_uses_single_endpoint_and_maps_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RDR-164 P2: in service mode the cascade must call the ONE atomic
    # deleteCollection endpoint and map its per-table counts — NOT fan out to
    # the local t2_index_write / chroma path.
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
    # RDR-186 .16: the pipeline step now routes to the engine's
    # delete_collection endpoint; wire it to the fake engine so the cascade's
    # pipeline leg succeeds (failures == [] assertion below).
    from tests.pipeline_fake_engine import make_fake_engine_db

    pipeline_db, _engine = make_fake_engine_db()
    monkeypatch.setattr(
        "nexus.db.http_pipeline_client.HttpPipelineDB", lambda: pipeline_db
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

    class _BoomClient:
        def delete_collection(self, name: str) -> dict[str, int]:
            raise RuntimeError("service unreachable")

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _BoomClient()
    )

    counts = purge_collection_cascade(object(), "knowledge__svc2__minilm-l6-v2-384__v1")

    assert any("service deleteCollection failed" in f for f in counts.failures)
    assert "service unreachable" in " ".join(counts.failures)
