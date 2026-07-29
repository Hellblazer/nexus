# SPDX-License-Identifier: AGPL-3.0-or-later
"""``discover_for_collection`` never re-embeds — the 4th copy of the 384d bug.

``d0a3387d`` / ``de07b4f1`` fixed three copies of one algorithm
(``HttpTaxonomyStore.split_topic``, the SQLite ``split_topic``, and the raw CLI
split path), whose commit message says "all three read stored vectors and none
of them embeds". ``discover_for_collection`` was the fourth: when T3 returned
documents without embeddings it re-encoded every chunk with
``LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")``, so a bge-768 or
voyage-1024 collection got topic centroids computed in a DIFFERENT vector
space, persisted into ``taxonomy_centroids_384``. Every later ANN assign then
hit the dimension-mismatch guard, returned ``[]``, and the collection ended up
with topics nothing could be assigned to.

PORTED TO THE SERVICE PATH (nexus-i711w Stage 2 sub-stage C). These tests drove
the RAW path, deleted with the SQLite CatalogTaxonomy whose statics it called.
The old module docstring claimed the service path "already refuses correctly"
and treated that as a reason this suite only ever applied to the raw path — but
``_discover_via_service`` / ``_fetch_service_vectors`` had NO test of their own,
so retiring this file with the raw path would have left a bug that shipped FOUR
TIMES with zero regression coverage on the only surviving path. The subject
moved; it did not die.

NON-VACUITY, the specific trap ``de07b4f1`` recorded hitting: its first
regression test passed against a restored re-embed, because BOTH the correct
and the broken path returned 0 (one by refusing, one via a ``len < k``
short-circuit). Returning 0 is therefore NOT a usable discriminator. The
discriminating assertion is that clustering is never REACHED — the store's
``discover_topics`` / ``rebuild_taxonomy`` record their input, and a refusal
means neither was called — paired with a positive test proving the same
fixtures DO reach clustering when vectors are present.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.commands.taxonomy_cmd import discover_for_collection

# Unpinned from local_catalog_backend in nexus-i711w Stage 2 sub-stage C-store:
# these tests never touched the catalog's local machinery (the pin was blanket
# aqbrk residue), so they run unchanged against whichever catalog is live.
_N_DOCS = 6  # > the n < 5 early return

#: The fake collection's name. Deliberately NOT a voyage-* or bge-* token: the
#: fake supplies 3-d vectors, so naming a real embedder would claim a dimension
#: the fixture does not produce — and these tests assert nothing about
#: embedding mode (every handle is a fake).
_COLLECTION = "docs__probe__stub-embedder-3d__v1"


class _FakeStub:
    """The service collection stub ``_fetch_service_vectors`` pages through."""

    def __init__(self) -> None:
        self._ids = [f"chunk-{i}" for i in range(_N_DOCS)]
        self._docs = [f"document text number {i}" for i in range(_N_DOCS)]

    def get(self, *, include: list[str] | None = None,
            limit: int | None = None, offset: int = 0,
            **_kw: Any) -> dict[str, Any]:
        end = len(self._ids) if limit is None else offset + limit
        out: dict[str, Any] = {"ids": self._ids[offset:end]}
        if include and "documents" in include:
            out["documents"] = self._docs[offset:end]
        return out


class _FakeServiceT3:
    """A service-backed T3 handle.

    ``embeddings`` is what ``get_embeddings`` hands back:

    * ``None``     — the service could not supply stored vectors at all;
    * short list   — the misalignment case (ids dropped server-side);
    * full list    — the healthy case.
    """

    def __init__(self, embeddings: list | None) -> None:
        self._embeddings = embeddings
        self._stub = _FakeStub()

    def count(self, _collection: str) -> int:
        return _N_DOCS

    def get_or_create_collection(self, _collection: str) -> _FakeStub:
        return self._stub

    def get_embeddings(self, _collection: str, _ids: list[str],
                       *, on_progress: Any = None) -> list | None:
        return self._embeddings


class _RecordingTaxonomy:
    """Records whether clustering was reached, and with what vectors."""

    def __init__(self) -> None:
        self.clustered: list[Any] = []

    def get_topics_for_collection(self, *_a: Any, **_kw: Any) -> list:
        return []

    def discover_topics(self, _collection, doc_ids, embeddings, _texts):  # noqa: ANN001, ANN201
        self.clustered.append(embeddings)
        return len(doc_ids)

    def rebuild_taxonomy(self, _collection, doc_ids, embeddings, _texts):  # noqa: ANN001, ANN201
        self.clustered.append(embeddings)
        return len(doc_ids)


def test_discover_refuses_when_service_supplies_no_vectors() -> None:
    """No stored vectors -> refuse, do not re-encode at 384d."""
    tax = _RecordingTaxonomy()
    t3 = _FakeServiceT3(embeddings=None)

    n = discover_for_collection(_COLLECTION, tax, t3)

    assert n == 0
    assert tax.clustered == [], (
        "refusal must happen BEFORE clustering — a run that clusters in the "
        "wrong space and then persists is the bug, not a smaller bug"
    )


def test_discover_refuses_on_embedding_misalignment() -> None:
    """Fewer vectors than ids -> refuse rather than cluster misaligned rows.

    ``get_embeddings`` drops ids the service cannot resolve, which desyncs
    ids/texts/embeddings. Silently wrong per-row clustering is worse than no
    clustering (feedback_no_silent_fallbacks_for_correctness).
    """
    tax = _RecordingTaxonomy()
    t3 = _FakeServiceT3(embeddings=[[0.1, 0.2, 0.3]] * (_N_DOCS - 2))

    n = discover_for_collection(_COLLECTION, tax, t3)

    assert n == 0
    assert tax.clustered == [], "a partial embedding fetch must not reach clustering"


def test_discover_clusters_on_stored_vectors_when_present() -> None:
    """The non-vacuity partner: with vectors present, discovery DOES proceed.

    Without this, both refusals would pass against a function that always
    returned 0 — the exact trap de07b4f1 recorded hitting.
    """
    tax = _RecordingTaxonomy()
    t3 = _FakeServiceT3(embeddings=[[0.1, 0.2, 0.3]] * _N_DOCS)

    n = discover_for_collection(_COLLECTION, tax, t3)

    assert n == _N_DOCS
    assert len(tax.clustered) == 1, "discovery must reach clustering exactly once"
    # The vectors handed to clustering are the STORED ones, at their own
    # dimensionality — never a 384d re-encode.
    assert tax.clustered[0].shape == (_N_DOCS, 3)
