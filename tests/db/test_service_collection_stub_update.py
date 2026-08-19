# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_ServiceCollectionStub.update`` — the collection-handle metadata update
``nx enrich bib`` calls (``commands/enrich.py::run_bib_enrichment`` →
``_vector_with_retry(col.update, ids=..., metadatas=...)``).

2026-08-19: the stub shipped ``get``/``count``/``delete`` only, so every
service-mode ``nx enrich bib`` run died with ``AttributeError:
'_ServiceCollectionStub' object has no attribute 'update'`` at the first
title it resolved — bib enrichment was unusable in production since the
Chroma retirement, and nothing asserted the handle satisfied the enrich
path's protocol. The method forwards to ``/v1/vectors/update-metadata``,
the same endpoint :meth:`HttpVectorClient.update_chunks` uses.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.db.http_vector_client import _ServiceCollectionStub
from nexus.retry import _vector_with_retry


def test_update_posts_ids_and_metadatas_to_update_metadata() -> None:
    stub = _ServiceCollectionStub(name="knowledge__x__test-model__v1", tenant="t1")
    with patch("nexus.db.http_vector_client._post", return_value={"updated": 2, "missing": []}) as post:
        out = stub.update(ids=["a", "b"], metadatas=[{"bib_year": 2025}, {"bib_year": 2025}])
    post.assert_called_once_with(
        "/v1/vectors/update-metadata",
        {
            "collection": "knowledge__x__test-model__v1",
            "ids": ["a", "b"],
            "metadatas": [{"bib_year": 2025}, {"bib_year": 2025}],
        },
        tenant="t1",
    )
    assert out == {"updated": 2, "missing": []}


def test_update_empty_ids_is_a_noop() -> None:
    stub = _ServiceCollectionStub(name="c")
    with patch("nexus.db.http_vector_client._post") as post:
        assert stub.update(ids=[], metadatas=[]) == {"updated": 0, "missing": []}
    post.assert_not_called()


def test_enrich_bib_call_shape_is_satisfied_by_the_stub() -> None:
    """The exact kwargs ``run_bib_enrichment`` passes through ``_vector_with_retry``."""
    stub = _ServiceCollectionStub(name="c")
    with patch("nexus.db.http_vector_client._post", return_value={"updated": 1, "missing": []}):
        _vector_with_retry(stub.update, ids=["x"], metadatas=[{"bib_venue": "CIDR"}])
