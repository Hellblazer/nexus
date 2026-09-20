# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 Phase 3 — the user-visible surface (beads .14, .15, .16).

Three behaviours, each settled by Sam on 2026-09-19 and each with a reason
Phase 1 then measured:

ADDITIVE, not a mode swap. The hybrid route gates on text before the vector
ranks anything, so it returned ZERO rows for every prose query in the Phase 1
measurement. Selecting it instead of the vector leg would hand a
natural-language question an empty result; union keeps every row the vector
leg returns today and adds the rare-token wins on top (0.167 -> 0.698
precision@10 on rare tokens).

EXEMPT FROM THE DISTANCE THRESHOLD. The threshold is calibrated on vector
distance. A lexical hit whose vector distance exceeds it IS the target row —
Phase 1's word_similarity_threshold query scored 0.000 precision AND 0.000
recall on the vector leg — so applying a vector threshold to a
lexically-matched row drops precisely what the leg exists to find.

REFUSES on a backend without the route, rather than falling back. The
fall-back is the dangerous option because it is silent: vector-only rows for
a lexical request look entirely plausible, and Phase 1 put a number on what
they would be (0.167 precision on the population the user was reaching for).
"""
from __future__ import annotations

import inspect

import pytest

from nexus.db.http_vector_client import VectorServiceError
from nexus.mcp import core

from nexus.search_engine import LexicalLegUnavailableError, search_cross_corpus

_COLL = "code__x__voyage-code-3__v1"


class _VectorOnlyBackend:
    """A backend with no hybrid route, like T3Database. Deliberately does NOT
    define supports_hybrid_search — absence is the signal, exactly as
    InMemoryVectorClient omits supports_server_rerank.

    THRESHOLDS MUST BE ACTIVE or every threshold assertion below passes for
    the wrong reason. ``_voyage_thresholds_active`` (search_engine.py:332-342)
    gates on ``isinstance(t3, HttpVectorClient)`` before consulting
    ``embedding_mode()``, so a fake class can never reach that branch; the
    documented escape for "test fakes, injected stubs" is a non-None
    ``_voyage_client``. The first version of this file set neither, so
    thresholds were OFF entirely and the exemption test went green against a
    code path that never ran — measured, not suspected: the paired
    does-not-leak test failed and exposed it.
    """

    #: Turns the distance threshold ON for a fake (see the class docstring).
    _voyage_client = object()

    def embedding_mode(self):
        return "voyage"

    def search(self, query, collection_names, n_results=10, where=None, **kw):
        return [{"id": "v1", "content": "x", "distance": 0.2, "collection": _COLL}]


class _LexicalBackend(_VectorOnlyBackend):
    supports_hybrid_search = True

    def __init__(self, lexical_rows=None, vector_rows=None):
        self._lex = lexical_rows if lexical_rows is not None else []
        self._vec = vector_rows
        self.hybrid_calls: list[dict] = []

    def search(self, query, collection_names, n_results=10, where=None, **kw):
        if self._vec is not None:
            return list(self._vec)
        return super().search(query, collection_names, n_results, where, **kw)

    def hybrid_search(self, query, collection_names, n_results=10, where=None, **kw):
        self.hybrid_calls.append({"query": query, "cols": list(collection_names)})
        return list(self._lex)


def test_lexical_refuses_a_backend_without_the_route(monkeypatch):
    """The refusal, and it must name the remedy rather than just failing.

    Sam's decision. The alternative is silent: vector-only rows come back
    looking like an answer while the leg the caller asked for never ran.
    """
    with pytest.raises(LexicalLegUnavailableError) as exc:
        search_cross_corpus("q", [_COLL], n_results=5,
                            t3=_VectorOnlyBackend(), lexical=True)
    msg = str(exc.value)
    assert "hybrid-search" in msg
    assert "falling back" in msg
    assert "nx daemon service start" in msg


def test_without_lexical_a_routeless_backend_is_never_asked(monkeypatch):
    """Non-vacuity for the refusal: the gate must fire ONLY on lexical=True.
    A backend with no hybrid route is still a perfectly good vector backend.
    """
    rows = search_cross_corpus("q", [_COLL], n_results=5,
                               t3=_VectorOnlyBackend(), lexical=False)
    assert [r.id for r in rows] == ["v1"]


def test_lexical_is_additive_and_never_replaces_the_vector_rows():
    """ADDITIVE. Both legs' rows come back, deduplicated by id.

    The Phase 1 finding this encodes: hybrid alone returned nothing for every
    prose query, so a mode swap would delete prose retrieval.
    """
    backend = _LexicalBackend(
        vector_rows=[{"id": "v1", "content": "a", "distance": 0.2, "collection": _COLL}],
        lexical_rows=[{"id": "L1", "content": "b", "distance": 0.30, "collection": _COLL}],
    )
    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)

    assert backend.hybrid_calls, "the lexical leg was never called"
    assert {r.id for r in rows} == {"v1", "L1"}


def test_a_row_both_legs_return_is_not_duplicated():
    backend = _LexicalBackend(
        vector_rows=[{"id": "same", "content": "a", "distance": 0.2, "collection": _COLL}],
        lexical_rows=[{"id": "same", "content": "a", "distance": 0.2, "collection": _COLL}],
    )
    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)
    assert [r.id for r in rows] == ["same"]


def test_a_lexical_row_survives_a_threshold_that_would_drop_it():
    """THE EXEMPTION, and the case that motivated it.

    code__ collections threshold at 0.45. The lexical row here sits at 0.92 —
    far outside it — which is exactly the shape of Phase 1's
    word_similarity_threshold query: a token the corpus genuinely holds, whose
    chunk is nowhere near the query in vector space. Without the exemption the
    leg returns the row and the threshold throws it away.
    """
    backend = _LexicalBackend(
        vector_rows=[{"id": "v1", "content": "a", "distance": 0.20, "collection": _COLL}],
        lexical_rows=[{"id": "far", "content": "b", "distance": 0.92, "collection": _COLL}],
    )
    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)

    assert "far" in {r.id for r in rows}, (
        "the lexical hit was dropped by the vector distance threshold — the "
        "exemption is not in force, and this is precisely the row the leg exists "
        "to find"
    )


def test_the_exemption_does_not_leak_to_vector_rows():
    """Non-vacuity for the exemption: a VECTOR row beyond the threshold is
    still dropped. If the exemption were implemented as "skip the threshold
    whenever lexical is on", this test would fail and the flag would quietly
    widen every result set.
    """
    backend = _LexicalBackend(
        vector_rows=[
            {"id": "near", "content": "a", "distance": 0.20, "collection": _COLL},
            {"id": "far_vec", "content": "b", "distance": 0.92, "collection": _COLL},
        ],
        lexical_rows=[],
    )
    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)
    ids = {r.id for r in rows}
    assert "near" in ids
    assert "far_vec" not in ids, (
        "a vector row past the threshold survived; the exemption is keyed on "
        "the flag rather than on the row's provenance"
    )


# ── the MCP surface (bead .15) ───────────────────────────────────────────────


def test_the_mcp_page_cache_key_includes_lexical():
    """A silent-wrong-answer path, found by the RDR-217 enrichment pass before
    the parameter existed.

    The page cache keys on the retrieval identity and lives 120s. `lexical`
    changes WHICH rows are retrieved, so a lexical call served a cached
    vector-only page would get quietly wrong results with no error — and the
    wrapper calls _search_render twice, so it is reachable inside one request.
    Asserted against the source because the cache is internal to the render
    path and a behavioural test here would need a live engine.
    """
    src = inspect.getsource(core._search_render)
    key_src = src[src.index("cache_key = ("):]
    # Close on the key tuple's OWN closing paren, not on tuple(target)'s.
    key_src = key_src[:key_src.index("\n        )")]
    assert "lexical" in key_src, (
        "the page-cache key omits `lexical`: a lexical=True call can be served "
        f"a cached vector-only page inside the TTL. Key is: {key_src!r}"
    )


def test_the_mcp_search_tool_declares_the_parameter_with_a_description():
    """The WIRE-FACING tool's parameter carries a Field description.

    CORRECTED after the P3 critique caught this as the sixth instance of this
    epic's own cited failure class. It previously inspected
    ``_search_render``'s signature and claimed to guard the
    ``Field(description=...)`` lint — but ``_search_render`` is the internal
    business-logic function and carries no ``Annotated``/``Field`` metadata at
    all, so the assertion could not fail for the reason its name gave. It
    checked a signature and claimed to check a schema.

    The Field annotations live on the registered MCP tool. Read the SOURCE of
    the wire-facing declaration, which is where the lint reads them from too.
    """
    src = inspect.getsource(core)
    decl = src[src.index("    lexical: Annotated[bool, Field("):]
    decl = decl[:decl.index(")] = ")]
    assert "description=" in decl, (
        "the wire-facing `lexical` parameter has no Field description; the "
        "lint-marked tests/test_mcp_tool_description_lint.py would red the lint "
        "bucket, which `pytest -n auto` does not run"
    )
    assert "ADD" in decl, "the description must say the leg is additive"
    assert "Refuses" in decl, "the description must say it refuses rather than degrades"


def test_the_render_parameter_is_off_by_default():
    """Separate from the schema check above, because they are different claims
    about different objects — conflating them is what made the original test
    unable to fail for its stated reason."""
    sig = inspect.signature(core._search_render)
    assert "lexical" in sig.parameters
    assert sig.parameters["lexical"].default is False, "must be off by default"


# ── the three P3-review ship-blockers, each with a test that fails without
# its fix ────────────────────────────────────────────────────────────────────


def test_a_lexical_leg_failure_does_not_discard_the_vector_rows_silently():
    """P3 review CRITICAL 1. The two legs shared one try/except, so a transient
    hybrid-only failure reported "all collections failed" — throwing away a
    vector search that had already succeeded, and on a multi-collection batch
    feeding the per-collection fallback that can memoize a collection as
    permanently poisoned over a blip in a route the caller merely asked to ALSO
    consult.

    It still fails LOUD (returning vector-only rows for a --lexical request is
    the silent outcome the design forbids), but it must fail as a LEXICAL
    failure, naming that leg and saying the vector search itself was fine.
    """
    class _LexBlows(_LexicalBackend):
        def hybrid_search(self, *a, **kw):
            raise VectorServiceError("hybrid route had a blip", code=503)

    backend = _LexBlows(
        vector_rows=[{"id": "v1", "content": "a", "distance": 0.2, "collection": _COLL}],
    )
    with pytest.raises(LexicalLegUnavailableError) as exc:
        search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)

    msg = str(exc.value)
    assert "lexical leg failed" in msg, "the failure must name the lexical leg"
    assert "vector search itself succeeded" in msg, (
        "the message must say the vector leg was fine, or a reader will chase "
        "the wrong cause"
    )


def test_the_exemption_survives_a_row_both_legs_returned():
    """P3 review IMPORTANT 3, and the routine case rather than the edge.

    ``per_k`` deliberately over-fetches past the threshold, so a lexical hit is
    OFTEN already in the vector leg's raw pool. The first implementation
    stamped a marker on lexical-only rows and kept the vector copy on a tie, so
    exactly those rows lost the marker and got threshold-dropped — the
    exemption worked only for rows the vector leg missed entirely.

    Provenance is tracked in a set now, so a row BOTH legs returned is still a
    lexical hit and still exempt.
    """
    row = {"id": "both", "content": "a", "distance": 0.92, "collection": _COLL}
    backend = _LexicalBackend(vector_rows=[dict(row)], lexical_rows=[dict(row)])

    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)

    assert "both" in {r.id for r in rows}, (
        "a row returned by BOTH legs was dropped by the vector threshold; the "
        "exemption is keyed on which copy survived dedup rather than on whether "
        "the lexical leg matched it"
    )


def test_no_internal_provenance_marker_reaches_a_result_row():
    """P3 review IMPORTANT 4. The marker rode ``r.metadata`` into
    ``nx search --json`` output via format_json's ``**r.metadata`` spread — an
    undocumented public field. Tracking provenance in a set instead means there
    is nothing to leak, and this asserts the absence rather than trusting it.
    """
    backend = _LexicalBackend(
        vector_rows=[{"id": "v1", "content": "a", "distance": 0.20, "collection": _COLL}],
        lexical_rows=[{"id": "L1", "content": "b", "distance": 0.30, "collection": _COLL}],
    )
    rows = search_cross_corpus("q", [_COLL], n_results=5, t3=backend, lexical=True)

    for r in rows:
        assert "_lexical_only" not in r.metadata, (
            f"{r.id} carries the internal provenance marker in metadata, which "
            "format_json spreads into --json output"
        )


def test_the_lexical_leg_is_reranked_whenever_the_vector_leg_is():
    """P3 critique CRITICAL 1, the defect that made the feature not work.

    ``search_cmd.py`` puts rows carrying a ``rerank_score`` FIRST and truncates
    at n. A lexical row that was never scored lands in the unscored tail and is
    dropped — in the DEFAULT invocation, which is exactly where Phase 1
    measured the rare-token win. The union was real and invisible.

    Both routes share one rerank tail server-side, so the two legs' scores are
    on the same scale and the rows can be ordered against each other honestly.
    This asserts the lexical call actually requests it.
    """
    class _RecordingRerank(_LexicalBackend):
        supports_server_rerank = True

        def __init__(self, **kw):
            super().__init__(**kw)
            self.hybrid_kwargs: list[dict] = []

        def search(self, query, collection_names, n_results=10, where=None, **kw):
            rows = super().search(query, collection_names, n_results, where, **kw)
            if kw.get("rerank") and kw.get("rerank_meta_out") is not None:
                kw["rerank_meta_out"].update({"degraded": False})
            return rows

        def hybrid_search(self, query, collection_names, n_results=10, where=None, **kw):
            self.hybrid_kwargs.append(dict(kw))
            return list(self._lex)

    backend = _RecordingRerank(
        vector_rows=[{"id": "v1", "content": "a", "distance": 0.2, "collection": _COLL}],
        lexical_rows=[{"id": "L1", "content": "b", "distance": 0.3, "collection": _COLL}],
    )
    search_cross_corpus("q", [_COLL], n_results=5, t3=backend,
                        lexical=True, rerank=True)

    assert backend.hybrid_kwargs, "the lexical leg was never called"
    assert backend.hybrid_kwargs[0].get("rerank") is True, (
        "the lexical leg was not reranked while the vector leg was, so its rows "
        "carry no rerank_score and search_cmd.py truncates them away"
    )
