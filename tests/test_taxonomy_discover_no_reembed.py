# SPDX-License-Identifier: AGPL-3.0-or-later
"""``discover_for_collection`` never re-embeds — the 4th copy of the 384d bug.

``d0a3387d`` / ``de07b4f1`` fixed three copies of one algorithm
(``HttpTaxonomyStore.split_topic``, ``CatalogTaxonomy.split_topic``, and the
raw CLI split path), whose commit message says "all three read stored vectors
and none of them embeds". ``discover_for_collection``'s raw path was the
fourth: when T3 returned documents without embeddings it re-encoded every
chunk with ``LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")``, so a
bge-768 or voyage-1024 collection got topic centroids computed in a DIFFERENT
vector space, persisted into ``taxonomy_centroids_384``. Every later ANN
assign then hits the dimension-mismatch guard, returns ``[]``, and the
collection ends up with topics nothing can be assigned to.

The surviving service path (``_discover_via_service``) already refuses
correctly — it reads stored vectors and returns 0 when they are unavailable —
so this only ever applied to the raw path.

NON-VACUITY, the specific trap ``de07b4f1`` recorded hitting: its first
regression test passed against a restored re-embed, because BOTH the correct
and the broken path returned 0 (one by refusing, one via a ``len < k``
short-circuit). Returning 0 is therefore NOT a usable discriminator here
either. The discriminating assertion is that the embedding function is never
CONSTRUCTED: patched to raise, the pre-fix code dies and the fixed code never
touches it.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from nexus.commands.taxonomy_cmd import discover_for_collection

pytestmark = pytest.mark.usefixtures("local_catalog_backend")

_N_DOCS = 6  # > the n < 5 early return

#: The fake collection's name. Deliberately NOT a voyage-* or bge-* token: the
#: fake supplies 3-d vectors, so naming a real embedder would claim a
#: dimension the fixture does not produce — and these tests assert nothing
#: about embedding mode (every client is a fake). The 384d-vs-collection-space
#: mismatch this suite exists to pin is explained in the module docstring,
#: which is where that belongs.
_COLLECTION = "docs__probe__stub-embedder-3d__v1"


class _FakeCollection:
    """A T3 collection that has documents but hands back no vectors.

    The exact shape the bug needs: ``get`` returns ``embeddings=None``, which
    is what a collection stored without vectors (or a client that omits them)
    produces.
    """

    def __init__(self, *, embeddings: list | None) -> None:
        self._ids = [f"chunk-{i}" for i in range(_N_DOCS)]
        self._docs = [f"document text number {i}" for i in range(_N_DOCS)]
        self._embs = embeddings

    def count(self) -> int:
        return len(self._ids)

    def get(self, *, include: list[str] | None = None,
            limit: int | None = None, offset: int = 0,
            **_kw: Any) -> dict[str, Any]:
        end = len(self._ids) if limit is None else offset + limit
        out: dict[str, Any] = {"ids": self._ids[offset:end]}
        if include and "documents" in include:
            out["documents"] = self._docs[offset:end]
        if include and "embeddings" in include:
            out["embeddings"] = (
                None if self._embs is None else self._embs[offset:end]
            )
        return out


class _FakeClient:
    def __init__(self, coll: _FakeCollection) -> None:
        self._coll = coll

    def get_collection(self, _name: str, *, embedding_function: Any = None):
        return self._coll

    def get_or_create_collection(self, _name: str, **_kw: Any):
        # The centroid collection, built unconditionally before the
        # discover/rebuild branch. Never written in these tests: clustering is
        # stubbed to produce no specs, so the centroid upsert is skipped.
        return self._coll


class _FakeTaxonomy:
    """Raw-access shape: ``_has_raw_access`` keys on ``.conn`` + ``._lock``."""

    def __init__(self) -> None:
        self.conn = object()
        self._lock = object()

    def get_topics_for_collection(self, _collection: str) -> list:
        return []


@pytest.fixture
def _no_embedding_allowed():
    """Any construction of the 384d EF is a failure of the contract."""
    def _boom(*_a: Any, **_k: Any):
        raise AssertionError(
            "discover_for_collection must not re-embed: topic centroids must "
            "share the collection's own vector space",
        )

    with patch("nexus.db.local_ef.LocalEmbeddingFunction", _boom):
        yield


@pytest.fixture
def clustered():
    """Capture the embeddings handed to clustering; produce no topics.

    Returning ``[]`` short-circuits the persist, centroid-upsert and
    cross-link work, so these stay unit tests. Reaching this function at all
    is the "discovery proceeded" signal; not reaching it is "refused".
    """
    calls: list[Any] = []

    def _capture(_collection, _ids, embeddings, _texts):
        calls.append(embeddings)
        return []

    with patch(
        "nexus.db.t2.catalog_taxonomy.CatalogTaxonomy.compute_discovered_topics",
        staticmethod(_capture),
    ):
        yield calls


def test_discover_refuses_when_t3_supplies_no_vectors(
    _no_embedding_allowed: None, clustered: list,
) -> None:
    """No stored vectors -> refuse, do not re-encode at 384d."""
    tax = _FakeTaxonomy()
    t3 = _FakeClient(_FakeCollection(embeddings=None))

    n = discover_for_collection(_COLLECTION, tax, t3)

    assert n == 0
    assert clustered == [], (
        "refusal must happen BEFORE clustering — a run that clusters in the "
        "wrong space and then persists is the bug, not a smaller bug"
    )


def test_discover_clusters_on_stored_vectors_when_present(
    _no_embedding_allowed: None, clustered: list,
) -> None:
    """The happy path still runs, on the collection's OWN vectors.

    The non-vacuity half: without it, a discover that refused unconditionally
    would also pass the test above. Here vectors ARE available, so clustering
    must be reached — and the array it receives must be the collection's 3-d
    vectors, which a 384d re-encode could not produce.
    """
    tax = _FakeTaxonomy()
    vectors = [[float(i), float(i + 1), 0.5] for i in range(_N_DOCS)]
    t3 = _FakeClient(_FakeCollection(embeddings=vectors))

    discover_for_collection(_COLLECTION, tax, t3)

    assert len(clustered) == 1, "clustering must be reached when vectors exist"
    assert clustered[0].shape == (_N_DOCS, 3), (
        f"must cluster on the collection's own 3-d vectors; got "
        f"{clustered[0].shape} — 384 would mean it re-encoded"
    )
