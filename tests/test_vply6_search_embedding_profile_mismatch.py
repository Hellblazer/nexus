# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-vply6 (Sam's decision 2026-09-24): a query whose embedder cannot
serve a targeted collection's registered model must raise a named error,
never come back as an empty or quietly-partial result.

``docs/desktop-deployment.md:262`` is the canonical repro: a GUI-launched
subprocess with no shell environment resolves the LOCAL bge-768 embedder
against ``voyage-*``-registered collections. The engine already refuses
this loudly PER COLLECTION (422 ``EmbeddingModelUnavailableException`` —
"this install's profile names a model this mode cannot serve") but that
refusal used to be swallowed at ``search_cross_corpus``'s per-collection
isolation seam (the nexus-9tsdf / nexus-d9xt2 "isolate one bad collection,
keep searching the rest" design) whenever at least one OTHER targeted
collection was servable.

WHY A MOCK T3, NOT THE LIVE ENGINE, FOR THE MISMATCH ITSELF. Verified live
against ``tests/_engine_substrate.py``'s test engine (which always boots
``onnx-local`` — no ``NX_VOYAGE_API_KEY``): RDR-204's registration guard
(``CatalogRepository.upsertCollection`` / ``EmbedderRouter.
seedEmbeddingProfileForContentType``) makes a genuine cross-profile
mismatch IMPOSSIBLE to create through the client registration API against
a single live engine boot, by design --

  1. ``seedEmbeddingProfileForContentType`` auto-seeds an ``embedding_
     profile`` row for ANY content_type (falling back to the "unknown"
     bucket token when the type is unrecognised) the FIRST time anything
     registers against it, using the CURRENT boot's mode -- there is no
     "profile-free" content_type to exploit, even a freshly invented one.
  2. A NEW collection whose requested ``embedding_model`` disagrees with
     that profile is refused at registration (422
     ``EmbeddingProfileConflictException``), and an EXISTING collection's
     ``embedding_model`` cannot be changed by re-registering it either
     (same exception, the other branch).

So the only way a real install ends up with a ``voyage-*``-registered
collection that a LATER boot can't serve is a genuine cross-boot config
change (the collection was written while the engine ran in voyage mode;
a later boot -- this same engine, no restart in between, hence no
profile re-seed -- runs onnx-local because the credential dropped out of
a GUI subprocess's environment). Reproducing that live would mean
restarting the shared, session-scoped test engine mid-suite with a
different embedder posture, which is not float-safe under a parallel
run and not worth the blast radius for one test file. The established
precedent for exactly this shape of test in this codebase --
``TestPerCollectionErrorIsolation`` / ``TestDimensionMismatchLoggingQuieted``
in ``tests/test_search_engine.py`` -- already tests ``search_cross_corpus``'s
per-collection failure handling via a T3 stand-in that raises
``VectorServiceError`` with the engine's verbatim wording; these tests
follow the same pattern, verbatim-matching ``EmbedderRouter.
resolveEmbedderStrict``'s exact message
(service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java) so
a wording drift there would break this test rather than silently stop
being caught.

The "matched pair still searches normally" case has NO such obstacle --
it is a plain, healthy write+search round trip -- and runs against the
REAL engine substrate (``t2_service_env``, autouse) accordingly.
"""
from __future__ import annotations

import uuid

import pytest

from nexus.corpus import _write_intent_embedding_model
from nexus.db.http_vector_client import VectorServiceError
from nexus.errors import SearchEmbeddingProfileMismatchError
from nexus.search_engine import search_cross_corpus

# EmbedderRouter.resolveEmbedderStrict's exact wording (see module
# docstring) for a voyage-context-3-registered collection queried by an
# onnx-local-only engine.
_ENGINE_MODEL_UNAVAILABLE_MESSAGE = (
    "this install's profile names a model this mode cannot serve — "
    "collection 'knowledge__seam-b-test__voyage-context-3__v1' resolves "
    "to model 'voyage-context-3', which embedding mode onnx-local has no "
    "embedder for. Available models: [bge-base-en-v15-768]. Voyage "
    "collections need NX_VOYAGE_API_KEY in the service environment "
    "(supervisor plumbs it from the nexus credential chain when set)."
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class _MismatchedModeT3:
    """T3 stand-in: collections named in *mismatched* raise the engine's
    verbatim ``EmbeddingModelUnavailableException`` text; every other
    requested collection returns its canned result list. Mirrors
    ``tests/test_search_engine.py``'s ``_FailingT3`` (nexus-pebfx.8), with
    the specific 422 model-unavailable wording nexus-vply6 classifies on,
    rather than that class's dimension-mismatch (400) wording.
    """

    def __init__(self, results_by_col: dict[str, list[dict]], mismatched: set[str]) -> None:
        self._results = results_by_col
        self._mismatched = set(mismatched)

    def search(self, query, collection_names, n_results=10, where=None):
        col = collection_names[0]
        if col in self._mismatched:
            raise VectorServiceError(
                f"POST /v1/vectors/search → HTTP 422: {_ENGINE_MODEL_UNAVAILABLE_MESSAGE}",
                code=422,
            )
        return self._results.get(col, [])

    def embedding_mode(self) -> str:
        return "onnx-local"


# ── the mismatch itself raises the named error ──────────────────────────


def test_single_mismatched_collection_raises_named_error() -> None:
    bad = "knowledge__seam-b-test__voyage-context-3__v1"
    t3 = _MismatchedModeT3({}, mismatched={bad})

    with pytest.raises(SearchEmbeddingProfileMismatchError) as excinfo:
        search_cross_corpus("what does this collection contain", [bad], n_results=5, t3=t3)

    message = str(excinfo.value)
    # Names the collection's registered model...
    assert "voyage-context-3" in message
    assert bad in message
    # ...and the query-side embedder's actual serving mode.
    assert "onnx-local" in message
    # ...and says how to fix it.
    assert "NX_VOYAGE_API_KEY" in message
    assert excinfo.value.serving_mode == "onnx-local"
    assert bad in excinfo.value.mismatches


def test_partial_mismatch_across_a_multi_collection_search_still_raises() -> None:
    """The exact silent-empty-result shape the bead closes: one OTHER
    targeted collection is perfectly healthy, but the mismatch must still
    surface loud rather than being isolated away as a quiet partial
    success (nexus-9tsdf's per-collection isolation, which this bead
    deliberately does NOT apply to this failure class -- see
    nexus.errors.SearchEmbeddingProfileMismatchError's docstring)."""
    healthy = "code__nexus__voyage-code-3__v1"
    bad = "knowledge__seam-b-test__voyage-context-3__v1"
    t3 = _MismatchedModeT3(
        {healthy: [{"id": "a", "content": "a real hit", "distance": 0.10}]},
        mismatched={bad},
    )

    with pytest.raises(SearchEmbeddingProfileMismatchError) as excinfo:
        search_cross_corpus("q", [healthy, bad], n_results=5, t3=t3)

    assert bad in excinfo.value.mismatches
    # The healthy collection is NOT part of the failure -- only the
    # genuinely-unservable one is named.
    assert healthy not in excinfo.value.mismatches


def test_non_mismatch_failure_keeps_the_existing_graceful_degrade() -> None:
    """A DIFFERENT per-collection failure class (nexus-9tsdf's stale
    dimension-mismatch-on-an-otherwise-resolvable-embedder orphan, or any
    other VectorServiceError) is UNCHANGED by this bead -- it still
    isolates and keeps searching the healthy majority, exactly as before.
    Only the systemic model-unavailable class raises unconditionally."""
    healthy = "code__nexus__voyage-code-3__v1"
    orphan = "knowledge__stale-orphan__minilm-l6-v2-384__v1"
    t3 = _MismatchedModeT3(
        {healthy: [{"id": "a", "content": "a real hit", "distance": 0.10}]},
        mismatched=set(),
    )
    # Reuse _MismatchedModeT3's search() shape but raise the DIMENSION
    # class's wording for `orphan` specifically (not the model-unavailable
    # marker this bead's fix targets).
    orig_search = t3.search

    def _search(query, collection_names, n_results=10, where=None):
        if collection_names[0] == orphan:
            raise VectorServiceError(
                "POST /v1/vectors/search → HTTP 400: query embedder produced "
                "a 1024-dim vector but the collections dispatch to the "
                "embedding_384 column",
            )
        return orig_search(query, collection_names, n_results=n_results, where=where)

    t3.search = _search  # type: ignore[method-assign]

    results = search_cross_corpus("q", [healthy, orphan], n_results=5, t3=t3)

    assert {r.id for r in results} == {"a"}


# ── a matched pair still searches normally (real engine, no regression) ──


@pytest.fixture()
def t3(t2_service_env):
    from nexus.db.http_vector_client import get_http_vector_client

    return get_http_vector_client()


def test_matched_pair_searches_normally(t3) -> None:
    model = _write_intent_embedding_model("knowledge")
    name = _unique("knowledge__vply6") + f"__{model}__v1"
    t3.put(collection=name, content="the quick brown fox jumps over the lazy dog",
           title="quick brown fox")

    results = search_cross_corpus("quick brown fox", [name], n_results=5, t3=t3)

    assert results
    assert any("quick brown fox" in r.content for r in results)
