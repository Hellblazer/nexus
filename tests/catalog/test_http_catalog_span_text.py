# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-p8nd5 — HttpCatalogClient.resolve_span_text service-mode parity.

The stub returned ``None`` unconditionally, so the majority topology
(service mode) never saw the ib6uy distinguishability contract the local
``Catalog.resolve_span_text`` honours: genuinely-unresolvable → ``None``,
DEGRADED vector service → :class:`VectorServiceError` raised for the
boundary to render. These tests pin the parity, including the underlying
``t3._client`` seam fix in ``catalog_spans`` (HttpVectorClient deliberately
has no ``_client`` attribute — the old code AttributeError'd into the
broad except and masked every service-mode chash span to ``None``).
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from nexus.catalog.catalog import CatalogEntry
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.http_vector_client import VectorServiceError

_CHASH = hashlib.sha256(b"span target text").hexdigest()


def _entry(**over) -> CatalogEntry:
    kw = dict(
        tumbler=Tumbler.parse("1.2.3"),
        title="Doc",
        author="",
        year=0,
        content_type="knowledge",
        file_path="",
        corpus="knowledge",
        physical_collection="knowledge__t__voyage-context-3__v1",
        chunk_count=1,
        head_hash="",
        indexed_at="",
    )
    kw.update(over)
    return CatalogEntry(**kw)


class _ServiceT3:
    """HttpVectorClient-shaped double: get_collection but NO _client attr."""

    def __init__(self, docs=None, error: Exception | None = None):
        self._docs = docs or []
        self._error = error
        self.where_seen: list[dict] = []

    def get_collection(self, name):
        outer = self

        class _Col:
            def get(self, *, ids=None, where=None, include=None, **kw):
                if outer._error is not None:
                    raise outer._error
                outer.where_seen.append(where or {"ids": ids})
                if outer._docs:
                    return {"ids": ["x"], "documents": list(outer._docs),
                            "metadatas": [{}] * len(outer._docs)}
                return {"ids": [], "documents": [], "metadatas": []}

        return _Col()

    # deliberate: no _client attribute (pinned HttpVectorClient shape)


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(base_url="http://127.0.0.1:1", tenant="t", _token="test-token")


def test_chash_span_resolves_through_the_service_shape():
    c = _client()
    t3 = _ServiceT3(docs=["span target text"])
    with patch.object(HttpCatalogClient, "resolve", return_value=_entry()), \
         patch("nexus.db.make_t3", return_value=t3):
        out = c.resolve_span_text("1.2.3", f"chash:{_CHASH}")
    assert out == "span target text"
    assert t3.where_seen and t3.where_seen[0] == {"chunk_text_hash": _CHASH}


def test_degraded_service_raises_never_masks_to_none():
    """ib6uy: unreachable is never collapsed into not-found."""
    c = _client()
    t3 = _ServiceT3(error=VectorServiceError("service unreachable", code=503))
    with patch.object(HttpCatalogClient, "resolve", return_value=_entry()), \
         patch("nexus.db.make_t3", return_value=t3):
        with pytest.raises(VectorServiceError):
            c.resolve_span_text("1.2.3", f"chash:{_CHASH}")


def test_unknown_tumbler_is_none():
    c = _client()
    with patch.object(HttpCatalogClient, "resolve", return_value=None):
        assert c.resolve_span_text("9.9.9", f"chash:{_CHASH}") is None


def test_empty_span_is_none_without_resolving():
    c = _client()
    with patch.object(HttpCatalogClient, "resolve") as res:
        assert c.resolve_span_text("1.2.3", "") is None
    res.assert_not_called()


def test_missing_chunk_is_none_not_error():
    c = _client()
    t3 = _ServiceT3(docs=[])
    with patch.object(HttpCatalogClient, "resolve", return_value=_entry()), \
         patch("nexus.db.make_t3", return_value=t3):
        assert c.resolve_span_text("1.2.3", f"chash:{_CHASH}") is None


def test_shared_resolver_uses_handle_when_no_client_attr():
    """The seam fix itself: resolve_span_text_for_entry must reach the T3
    read through the HANDLE when ``_client`` is absent (service shape) —
    the old ``t3._client`` read AttributeError'd into the broad except and
    masked every service-mode chash span to None."""
    from nexus.catalog.catalog_spans import resolve_span_text_for_entry

    t3 = _ServiceT3(docs=["via handle"])
    with patch("nexus.db.make_t3", return_value=t3):
        out = resolve_span_text_for_entry(_entry(), f"chash:{_CHASH}")
    assert out == "via handle"
