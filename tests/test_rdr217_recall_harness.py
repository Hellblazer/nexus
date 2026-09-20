# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P1.1 (bead nexus-lqo4p.1) — the harness's own tests.

NO RECALL NUMBER IS TAKEN HERE. This file pins the recall computation and the
query set's shape; P1.2 takes the before-number after P1's review, which is
deliberately sequenced BEFORE any number is spent rather than after the work.

The recall computation is pinned at its boundaries because an off-by-one in it
would be invisible: a figure wrong by one rank still looks like a recall
figure, and it feeds the Phase 3 surface decision.
"""
from __future__ import annotations

import json

import pytest

from tests._rdr217_recall_harness import (
    GROUND_TRUTH_KINDS,
    SHAPES,
    CorpusContractError,
    Query,
    QueryResult,
    QuerySet,
    RecallReport,
    UninterpretableMeasurement,
    corpus_fingerprint,
    load_query_set,
    measure,
    precision_at_k,
    recall_at_k,
    recall_ceiling,
    resolve_ground_truth,
    validate_corpus,
)


# ── the recall computation, at its boundaries ────────────────────────────────


def test_recall_is_the_fraction_of_relevant_chunks_retrieved():
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=3) == 1.0
    assert recall_at_k(["a", "x", "y"], {"a", "b"}, k=3) == 0.5
    assert recall_at_k(["x", "y", "z"], {"a", "b"}, k=3) == 0.0


def test_k_slices_the_retrieved_list_at_exactly_k():
    """The off-by-one boundary, both sides. A relevant row at index k-1 is
    INSIDE the window and one at index k is outside — the whole point of
    pinning this is that either error still yields a plausible number.
    """
    retrieved = ["x", "x", "hit"]
    assert recall_at_k(retrieved, {"hit"}, k=3) == 1.0   # at index 2, inside k=3
    assert recall_at_k(retrieved, {"hit"}, k=2) == 0.0   # same row, outside k=2


def test_an_empty_ground_truth_is_undefined_not_zero():
    """Returning 0.0 here would report a CORPUS gap as a RETRIEVAL failure,
    and the two have opposite remedies: index more, versus fix the route.
    """
    assert recall_at_k(["a", "b"], set(), k=2) is None


def test_duplicate_retrieved_ids_count_once():
    """A route returning the same chunk twice must not score 2/2 on a single
    relevant chunk."""
    assert recall_at_k(["a", "a", "a"], {"a", "b"}, k=3) == 0.5


def test_a_non_positive_k_is_refused_rather_than_silently_empty():
    with pytest.raises(ValueError, match="k must be positive"):
        recall_at_k(["a"], {"a"}, k=0)


# ── ground-truth resolution ──────────────────────────────────────────────────


def _q(kind: str, value: str, shape: str = "identifier") -> Query:
    return Query(id="t", shape=shape, text="t", ground_truth_kind=kind,
                 ground_truth_value=value, why="t")


def test_literal_containment_resolves_case_insensitively_over_content():
    corpus = [
        {"id": "c1", "content": "def resolve_active_session_id(): ..."},
        {"id": "c2", "content": "unrelated helper"},
        {"id": "c3", "content": "calls RESOLVE_ACTIVE_SESSION_ID upstream"},
    ]
    assert resolve_ground_truth(_q("literal_token", "resolve_active_session_id"),
                                corpus) == {"c1", "c3"}


def test_source_path_relevance_reads_the_path_fields_not_the_content():
    """The prose shape must NOT be satisfiable by body text, or it would
    collapse into the identifier shape and the contrast the query set relies
    on would be gone.
    """
    corpus = [
        {"id": "c1", "source_uri": "/repo/src/nexus/db/service_endpoint.py",
         "content": "nothing relevant in the body"},
        {"id": "c2", "source_uri": "/repo/src/nexus/other.py",
         "content": "the words service_endpoint appear only here, in the body"},
    ]
    assert resolve_ground_truth(
        _q("source_path_contains", "service_endpoint", shape="prose"), corpus,
    ) == {"c1"}


def test_a_chunk_with_no_id_is_skipped_rather_than_scored_as_empty_string():
    corpus = [{"content": "resolve_active_session_id"}, {"id": "", "content": "x"}]
    assert resolve_ground_truth(_q("literal_token", "resolve_active_session_id"),
                                corpus) == set()


# ── the artifact's shape ─────────────────────────────────────────────────────


def test_the_query_set_loads_and_every_query_declares_its_shape_and_reason():
    qs = load_query_set()
    assert qs.schema_version == 4
    assert qs.corpus_prefixes == ("code__",)
    assert len(qs.queries) >= 10
    for q in qs.queries:
        assert q.shape in SHAPES
        assert q.ground_truth_kind in GROUND_TRUTH_KINDS
        assert q.text.strip()
        # Readable by someone who was not in the room: per query, WHICH shape it
        # tests and WHY it is in the set. The bead's validation criterion.
        assert len(q.why) > 40, f"{q.id}: 'why' is too thin to tell a reader anything"


def test_all_three_shapes_are_represented_with_more_than_one_query_each():
    """A shape with one query cannot distinguish a property of the shape from a
    property of that query."""
    qs = load_query_set()
    by_shape: dict[str, int] = {}
    for q in qs.queries:
        by_shape[q.shape] = by_shape.get(q.shape, 0) + 1
    assert set(by_shape) == set(SHAPES), by_shape
    for shape, n in by_shape.items():
        assert n >= 2, f"shape {shape!r} has only {n} query"


def test_prose_queries_are_not_satisfiable_by_literal_containment():
    """THE INSTRUMENT'S LOAD-BEARING ASSUMPTION, asserted rather than trusted.

    The query set's contrast depends on prose queries being a population no
    lexical gate is privileged on. If a prose query's own words appeared
    verbatim in the path it declares relevant, that query would favour the
    lexical leg exactly as an identifier query does, the contrast would
    collapse, and a Phase 1 report showing "the lexical leg helps on every
    shape" would be an artifact of the query set rather than a finding.

    The check: no prose query may contain its own ground-truth needle as a
    substring of its text.
    """
    qs = load_query_set()
    prose = [q for q in qs.queries if q.shape == "prose"]
    assert prose

    def _flatten(s: str) -> str:
        """Separators removed. The engine's gate is plainto_tsquery OR trigram
        word_similarity >= 0.6 over the WHOLE query string, so a query
        differing from its needle only by an underscore-versus-space clears
        that threshold — an exact-substring check cannot see it. This is the
        leak the Phase 1 review found in prose-01, whose text said "service
        endpoint" against a needle of "service_endpoint"."""
        return s.lower().replace("_", "").replace("-", "").replace(" ", "")

    for q in prose:
        assert q.ground_truth_value.lower() not in q.text.lower(), (
            f"{q.id}: the query text contains its own ground-truth needle "
            f"{q.ground_truth_value!r} verbatim"
        )
        assert _flatten(q.ground_truth_value) not in _flatten(q.text), (
            f"{q.id}: with separators normalised, the query text still contains "
            f"its own ground-truth needle {q.ground_truth_value!r}. The engine's "
            "trigram leg would clear 0.6 on that, so the lexical leg is "
            "privileged on a query whose entire job is to contrast with the "
            "identifier shape"
        )


def test_identifier_and_rare_token_queries_are_single_tokens_not_phrases():
    """These shapes exist to test exact-token retrieval. A multi-word query
    would be measuring something else, and would also change what the engine's
    trigram leg does, since it takes the whole query string as one operand.
    """
    qs = load_query_set()
    for q in qs.queries:
        if q.shape in ("identifier", "rare_token"):
            assert " " not in q.text.strip(), f"{q.id} is a phrase, not a token"
            assert q.ground_truth_value == q.text, (
                f"{q.id}: ground truth {q.ground_truth_value!r} differs from the "
                "query text; for these shapes they must be the same token or the "
                "measurement is not about the query"
            )


def test_the_artifact_records_the_two_choices_the_bead_requires():
    """The bead requires the artifact to STATE its corpus scope and which side
    of the note-geometry change it sits on, plus the ordering choice. A later
    reader who cannot tell which population was measured cannot use the number.
    """
    raw = json.loads((__import__("pathlib").Path(__file__).parent
                      / "fixtures" / "rdr217_lexical_query_set.json").read_text())
    scope = raw["corpus_scope"]
    assert scope["collection_prefixes"] == ["code__"]
    assert "1be4146da" in scope["why"], "the note-geometry commit must be named"
    ordering = raw["ordering"]
    assert "no client-side re-ranking" in ordering["choice"]
    assert "7b1b49075" in ordering["why_this_is_stated_rather_than_left_to_a_lambda"]
    assert raw["relevance_model"]["the_bias_this_carries_and_why_it_is_not_hidden"]
    # The metric correction the Phase 1 review forced, and the fact that it IS
    # a deviation from the RDR's Phase 1 text, both recorded rather than tacit.
    metric = raw["metric"]
    assert metric["primary"] == "precision@k"
    assert "ceiling" in metric["why_not_recall_as_primary"].lower()
    assert "phase 1 text says recall" in metric["this_is_a_recorded_deviation"].lower()
    # Two known truncation traps that would silently shrink the denominator.
    assert "300" in scope["completeness_contract"]
    assert "4000" in scope["completeness_contract"]


# ── measure(), wired to a fake retriever so no number is taken ───────────────


class _FakeClient:
    """Records the calls and returns canned rows. No engine, no measurement:
    these tests are about the harness's plumbing, not about retrieval."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def search(self, query, collection_names, n_results=10):
        self.calls.append(("vector", query))
        return self.rows

    def hybrid_search(self, query, collection_names, n_results=10):
        self.calls.append(("hybrid", query))
        return self.rows


def _one_query_set() -> QuerySet:
    return QuerySet(schema_version=1, corpus_prefixes=("code__",), queries=(
        Query(id="a", shape="identifier", text="tok", ground_truth_kind="literal_token",
              ground_truth_value="tok", why="x" * 50),
    ))


def test_measure_routes_to_the_named_leg():
    corpus = [{"id": "c1", "content": "tok here"}]
    client = _FakeClient([{"id": "c1"}])

    measure(client, _one_query_set(), ["code__x"], corpus, route="vector", k=5)
    measure(client, _one_query_set(), ["code__x"], corpus, route="hybrid", k=5)

    assert [c[0] for c in client.calls] == ["vector", "hybrid"]


def test_measure_refuses_an_unknown_route_rather_than_defaulting():
    with pytest.raises(ValueError, match="unknown route"):
        measure(_FakeClient([]), _one_query_set(), ["code__x"], [], route="both", k=5)


def test_an_unanswerable_query_is_reported_not_scored_as_zero():
    """A query whose ground truth resolves to nothing measures the CORPUS. It
    must not enter the average as a zero, and the report must name it — a run
    where several appear is not measuring what it claims.
    """
    corpus = [{"id": "c1", "content": "nothing matching"}]
    report = measure(_FakeClient([{"id": "c1"}]), _one_query_set(), ["code__x"],
                     corpus, route="vector", k=5)

    assert report.unanswerable() == ("a",)
    assert report.macro_recall() is None


def test_macro_recall_averages_per_query_not_per_hit():
    """Macro, so one identifier with many relevant chunks cannot dominate its
    shape. Built by hand rather than through measure() so the arithmetic is the
    only thing under test.
    """
    report = RecallReport(
        route="vector", k=10, collections=("code__x",),
        corpus_fingerprint="13:abc", corpus_size=13, per_query=(
            QueryResult("a", "identifier", relevant=10, retrieved=10, hits=10,
                        precision=1.0, recall=1.0, recall_ceiling=1.0),
            QueryResult("b", "identifier", relevant=1, retrieved=10, hits=0,
                        precision=0.0, recall=0.0, recall_ceiling=1.0),
            QueryResult("c", "prose", relevant=2, retrieved=10, hits=1,
                        precision=0.1, recall=0.5, recall_ceiling=1.0),
        ))

    assert report.macro_recall("identifier") == 0.5   # (1.0 + 0.0) / 2
    assert report.macro_recall("prose") == 0.5
    assert report.macro_recall() == 0.5               # (1.0 + 0.0 + 0.5) / 3


def test_load_query_set_refuses_a_duplicate_id(tmp_path):
    """Two entries with one id would be counted twice and silently reweight
    their shape's average."""
    bad = tmp_path / "qs.json"
    bad.write_text(json.dumps({
        "schema_version": 1,
        "corpus_scope": {"collection_prefixes": ["code__"]},
        "queries": [
            {"id": "dup", "shape": "identifier", "text": "a",
             "ground_truth": {"kind": "literal_token", "token": "a"}, "why": "x"},
            {"id": "dup", "shape": "identifier", "text": "b",
             "ground_truth": {"kind": "literal_token", "token": "b"}, "why": "x"},
        ],
    }))
    with pytest.raises(ValueError, match="duplicate query id"):
        load_query_set(bad)


def test_load_query_set_refuses_an_empty_ground_truth(tmp_path):
    """An empty needle matches every chunk, making recall trivially 1.0."""
    bad = tmp_path / "qs.json"
    bad.write_text(json.dumps({
        "schema_version": 1,
        "corpus_scope": {"collection_prefixes": ["code__"]},
        "queries": [{"id": "e", "shape": "identifier", "text": "a",
                     "ground_truth": {"kind": "literal_token", "token": ""},
                     "why": "x"}],
    }))
    with pytest.raises(ValueError, match="empty ground truth"):
        load_query_set(bad)


# ── the metric correction, and the guards the Phase 1 review added ───────────


def test_precision_is_bounded_at_one_however_large_the_ground_truth():
    """THE WHOLE POINT OF THE CORRECTION. Ten hits out of ten returned is a
    perfect window whether the corpus holds 10 relevant chunks or 700, so a
    route cannot be punished for the size of the ground truth.
    """
    retrieved = [f"c{i}" for i in range(10)]
    relevant_small = set(retrieved)
    relevant_huge = relevant_small | {f"x{i}" for i in range(690)}

    assert precision_at_k(retrieved, relevant_small, k=10) == 1.0
    assert precision_at_k(retrieved, relevant_huge, k=10) == 1.0
    # ...while recall@10 over the same two, which is why it cannot be primary.
    assert recall_at_k(retrieved, relevant_small, k=10) == 1.0
    assert round(recall_at_k(retrieved, relevant_huge, k=10), 4) == 0.0143


def test_precision_over_an_empty_window_is_undefined_not_zero():
    """0.0 would report "the route returned junk" for "the route returned
    nothing" — opposite diagnoses."""
    assert precision_at_k([], {"a"}, k=10) is None


def test_the_recall_ceiling_announces_a_capped_query():
    """A capped recall figure is not wrong, it is incomparable with an uncapped
    one, so it has to announce itself rather than read as a poor result."""
    assert recall_ceiling({"a", "b"}, k=10) == 1.0
    assert recall_ceiling({f"c{i}" for i in range(50)}, k=10) == 0.2
    assert recall_ceiling(set(), k=10) is None

    uncapped = QueryResult("a", "identifier", 2, 10, 2, 1.0, 1.0, 1.0)
    capped = QueryResult("b", "identifier", 50, 10, 10, 1.0, 0.2, 0.2)
    assert uncapped.recall_is_capped is False
    assert capped.recall_is_capped is True


def test_a_mostly_unanswerable_report_refuses_to_be_read_as_a_measurement():
    """Bead .2 code-review finding 1: nothing stopped a run where most queries
    were unanswerable from reporting a healthy average over the remainder. The
    recording step calls this, so the refusal is mechanical rather than a
    comment asking someone to look.
    """
    report = RecallReport(
        route="vector", k=10, collections=("code__x",),
        corpus_fingerprint="3:abc", corpus_size=3, per_query=(
            QueryResult("a", "identifier", 0, 10, 0, 0.0, None, None),
            QueryResult("b", "identifier", 0, 10, 0, 0.0, None, None),
            QueryResult("c", "prose", 2, 10, 2, 0.2, 1.0, 1.0),
        ))
    assert report.macro_recall() == 1.0  # the flattering remnant average
    with pytest.raises(UninterpretableMeasurement, match="unanswerable"):
        report.must_be_interpretable()


def test_an_interpretable_report_passes_the_same_check():
    """Non-vacuity for the guard above: it must not refuse everything."""
    report = RecallReport(
        route="vector", k=10, collections=("code__x",),
        corpus_fingerprint="2:abc", corpus_size=2, per_query=(
            QueryResult("a", "identifier", 2, 10, 2, 0.2, 1.0, 1.0),
            QueryResult("b", "prose", 2, 10, 1, 0.1, 0.5, 1.0),
        ))
    report.must_be_interpretable()


def test_the_corpus_contract_is_enforced_not_documented():
    """Bead .2 code-review finding 2: P1.2's corpus builder does not exist yet,
    so a shape mismatch would have resolved ground truth to nothing and
    reported every query as unanswerable."""
    with pytest.raises(CorpusContractError, match="empty"):
        validate_corpus([])
    with pytest.raises(CorpusContractError, match="carry no id"):
        validate_corpus([{"content": "x"}, {"content": "y"}, {"content": "z"}])
    validate_corpus([{"id": "c1", "content": "x"}])
    validate_corpus([{"chunk_text_hash": "c1", "content": "x"}])


def test_the_corpus_fingerprint_distinguishes_two_different_corpora():
    """Bead .2 code-review finding 3: comparability between the before- and
    after-measurement rested on caller discipline with nothing to check
    afterwards. Two reports with different fingerprints were not measured
    against the same corpus, whatever their prose claims."""
    a = [{"id": "c1"}, {"id": "c2"}]
    assert corpus_fingerprint(a) == corpus_fingerprint(list(reversed(a)))
    assert corpus_fingerprint(a) != corpus_fingerprint([{"id": "c1"}])
    assert corpus_fingerprint(a).startswith("2:")


def test_measure_stamps_the_fingerprint_and_keeps_hits_consistent():
    """Finding 4: the hit count was computed twice. One computation now feeds
    hits, precision and recall, so they cannot desync."""
    corpus = [{"id": "c1", "content": "tok here"}, {"id": "c2", "content": "no"}]
    report = measure(_FakeClient([{"id": "c1"}, {"id": "c2"}]), _one_query_set(),
                     ["code__x"], corpus, route="vector", k=10)

    r = report.per_query[0]
    assert report.corpus_fingerprint == corpus_fingerprint(corpus)
    assert report.corpus_size == 2
    assert (r.hits, r.relevant, r.retrieved) == (1, 1, 2)
    assert r.precision == 0.5 and r.recall == 1.0


def test_measure_refuses_a_corpus_that_breaks_the_contract():
    with pytest.raises(CorpusContractError):
        measure(_FakeClient([]), _one_query_set(), ["code__x"], [],
                route="vector", k=10)


def test_a_rare_token_label_is_checked_against_the_corpus_not_trusted():
    """The hole my own non-vacuity plant found in my own fix.

    Restoring "pgvector" (681 literal occurrences) into the rare-token slot
    tripped NOTHING, because a shape label is prose in the artifact and no
    load-time check can see rarity — rarity is a property of the corpus. So the
    check lives where the corpus is known. A token with more than k relevant
    chunks is not rare by any reading, and leaving it in means the rare-token
    shape is not measured at all while appearing to be.
    """
    report = RecallReport(
        route="vector", k=10, collections=("code__x",),
        corpus_fingerprint="700:abc", corpus_size=700, per_query=(
            QueryResult("rare-03", "rare_token", relevant=213, retrieved=10, hits=10,
                        precision=1.0, recall=0.047, recall_ceiling=0.047),
            QueryResult("id-01", "identifier", relevant=2, retrieved=10, hits=2,
                        precision=0.2, recall=1.0, recall_ceiling=1.0),
        ))

    assert report.mislabelled_rare_tokens() == ("rare-03",)
    with pytest.raises(UninterpretableMeasurement, match="rare_token"):
        report.must_be_interpretable()


def test_a_genuinely_rare_token_passes_that_check():
    """Non-vacuity for the guard above."""
    report = RecallReport(
        route="vector", k=10, collections=("code__x",),
        corpus_fingerprint="700:abc", corpus_size=700, per_query=(
            QueryResult("rare-03", "rare_token", relevant=1, retrieved=10, hits=1,
                        precision=0.1, recall=1.0, recall_ceiling=1.0),
        ))
    assert report.mislabelled_rare_tokens() == ()
    report.must_be_interpretable()
