# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fv2u4: a semantic-cluster header is the best member's catalog title.

Clustering runs before ``_attach_display_paths``, the only writer of ``_display_title``
and ``_display_path``, so a label taken at clustering time is always the chunk id. These
tests drive ``search_cross_corpus`` end to end with rows shaped like the production
engine's (the row's own stale ``title``/``source_path``, a chunk hash, no ``_display_*``).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from nexus.search_engine import search_cross_corpus


@pytest.fixture(autouse=True)
def _no_contradiction_check(monkeypatch):
    monkeypatch.setattr(
        "nexus.search_engine.load_config",
        lambda: {"search": {"contradiction_check": False}},
    )


class _ClusterT3:
    """Two well-separated groups of three chunks."""

    _voyage_client = "fake"  # enables threshold filtering

    def __init__(self) -> None:
        self.rows = [
            {"id": f"chunk-{i}", "content": f"text {i}", "distance": 0.1 + 0.02 * i,
             "chunk_text_hash": f"h{i}", "title": "stale last writer",
             "source_path": "/stale/last-writer.md"}
            for i in range(6)
        ]

    def search(self, query, collection_names, n_results=10, where=None):
        return list(self.rows)

    def get_embeddings(self, collection_name: str, ids: list[str]) -> np.ndarray:
        out = np.zeros((len(ids), 4), dtype=np.float32)
        for row, cid in enumerate(ids):
            i = int(cid.rsplit("-", 1)[1])
            out[row, 0 if i < 3 else 1] = 1.0
            out[row, 2] = 0.01 * i
        return out


class _OwnerCatalog:
    """Each chash has one owning document; ``resolve_many`` states its catalog title."""

    def chash_positions(self, chashes):
        return [
            {"chash": c, "doc_id": f"1.1.{c[1:]}", "position": 0, "chunk_count": 1}
            for c in chashes
        ]

    def resolve_many(self, doc_ids):
        return {
            d: SimpleNamespace(
                title=f"Catalog Doc {d.rsplit('.', 1)[1]}", file_path="",
                physical_collection="docs__a",
            )
            for d in doc_ids
        }


def _labels(catalog) -> dict[str, str]:
    results = search_cross_corpus(
        "q", ["docs__a"], 10, _ClusterT3(), cluster_by="semantic", catalog=catalog,
    )
    assert len(results) == 6
    assert all("_cluster_index" not in r.metadata for r in results)
    return {r.id: r.metadata["_cluster_label"] for r in results}


def test_header_is_the_best_members_catalog_title() -> None:
    labels = _labels(_OwnerCatalog())
    assert {labels[f"chunk-{i}"] for i in range(3)} == {"Catalog Doc 0"}
    assert {labels[f"chunk-{i}"] for i in range(3, 6)} == {"Catalog Doc 3"}


def test_header_is_the_id_when_the_catalog_is_down() -> None:
    labels = _labels(None)
    assert {labels[f"chunk-{i}"] for i in range(3)} == {"chunk-0"}
    assert {labels[f"chunk-{i}"] for i in range(3, 6)} == {"chunk-3"}
    assert "stale" not in "".join(labels.values())
