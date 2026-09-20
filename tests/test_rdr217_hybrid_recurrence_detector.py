# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P4.1 (bead nexus-lqo4p.9) — the two-call recurrence detector.

TWO CALLS, NOT ONE, and that is forced rather than chosen. A zero-row hybrid
response is a legitimate outcome, and the response carries no field naming
match counts or which leg contributed, so one response cannot distinguish a
true zero from a gate that matched nothing for the wrong reason. The detector
drives ``/search`` and ``/hybrid-search`` with the same query over the same
collections and diffs the two counts.

THE COMPARATOR TAKES THREE INPUTS and fails when, and only when, all three
hold:

    ground_truth_lexical_match is True
      AND hybrid_rows == 0
      AND vector_rows  > 0

All three are JOINTLY NECESSARY; none decides alone. With the ground-truth
flag False the comparator never fails whatever the diff shows, because a fused
zero is then the correct answer. With ``vector_rows == 0`` it does not fire
either, which is what makes the vector count a necessary conjunct rather than
a decoration.

WHAT THE VECTOR COUNT DOES — a different job from the other two, not a lesser
one. It is EXCLUSIONARY: it rules out the degenerate case where nothing
retrieves at all (a wrong collection name, an empty collection, an engine
returning nothing for every call alike). There a fused zero carries no
information about the gate and the detector must stay silent. The vector count
answers "did anything retrieve?", and "did the gate work?" is only a
meaningful question once that answer is yes.

WHY THE GROUND-TRUTH FLAG IS REQUIRED. "Zero from the fused leg where the
vector leg returns rows" is the ORDINARY expected result for any query whose
tokens have no literal or trigram-similar match but which has semantically
near chunks. ``english`` stemming and a 0.6 trigram threshold are much
narrower targets than vector similarity, so that case is common rather than
exceptional. Only a corpus known to contain a lexical match makes a fused zero
have exactly one explanation, so the detector is defined over such a fixture
and only over such a fixture.

ERROR BEHAVIOUR IS THE ABSENCE OF A HANDLER. There is no try/except anywhere
in the detector, on purpose. ``_post`` already raises ``VectorServiceError`` on
a non-200, a timeout or a connection failure, so not catching it IS "propagate
and fail loud". A degraded engine is exactly when a second round trip is
likeliest to fail, so a detector that swallowed that would go quiet precisely
when it is needed. No retry either: a retry that masks a flapping engine
reintroduces the silence this exists to break.

WHAT THIS IS NOT. It is a CI assertion proving the client-side comparison
logic is correct on a fixture where the answer is known. It is NOT production
monitoring, and it would not by itself have caught BUG-0148, whose defining
property was invisibility in production while every health signal stayed
green. The BUG-0148 risk is REDUCED, not closed. Live recurrence detection
needs a canary corpus and a cadence; that is named future work with no bead,
and nothing here should be read as claiming otherwise.

ONE ASYMMETRY BETWEEN THE TWO FIXTURES, STATED RATHER THAN HIDDEN. In Fixture
A the zero is produced by the REAL gate; in Fixture B it is INJECTED at the
``_post`` seam. The two are identical in their OBSERVABLES and differ in one
DECLARED thing, the ground-truth flag, which is what the comparator's
discrimination requires — but the mechanism producing the zero is not the
same, and Fixture B's cannot be genuine. Planting "the corpus holds the
query's literal tokens AND the fused leg returns zero" is impossible against a
healthy engine; BUG-0148 needed stale planner statistics to produce it. So B
injects the zero while the corpus and the vector call stay real. Saying this
out loud is deliberate: hiding it is how earlier drafts of this Test Plan
acquired the defects its Contradiction Check enumerates.
"""
from __future__ import annotations

import hashlib

import pytest

import nexus.db.http_vector_client as hvc

_COLLECTION = "code__rdr217-detector__bge-base-en-v15-768__v1"

# Carried literally by the seeded corpus, so a fused zero on this query has
# exactly one explanation.
_LEXICAL_QUERY = "resolve_active_session_id"

# NO literal and no trigram match against that corpus, and deliberately
# multi-word. The trigram leg takes the WHOLE query string as its left operand
# at word_similarity_threshold = 0.6, so a multi-word query carrying an
# invented token cannot trigram-match ordinary prose. Also kept clear of
# near-stems, since `english` collides retrieving with retrieval.
_NO_MATCH_QUERY = "quarterly budget variance across regional subsidiaries"


class HybridGateRegression(AssertionError):
    """The fused leg returned nothing where the corpus is known to hold a
    lexical match and the vector leg returned rows.

    An ``AssertionError`` subclass rather than an unrelated base, because the
    detector IS an assertion — and a subclass rather than a bare
    ``AssertionError`` so ``pytest.raises`` stays discriminating.
    """


def detect_hybrid_gate_regression(
    client: hvc.HttpVectorClient,
    query: str,
    collections: list[str],
    *,
    ground_truth_lexical_match: bool,
    n_results: int = 10,
) -> tuple[int, int]:
    """Drive both routes and compare. Returns ``(hybrid_rows, vector_rows)``.

    The counts come back rather than a bare pass/fail so P4.2's fixture
    comparison has recorded observables to assert on instead of re-deriving
    them — the two fixtures whose observables are IDENTICAL and whose
    ground-truth flags differ are the point of that bead, and they can only be
    written against returned counts.

    Raises :class:`HybridGateRegression` on the three-input condition. Raises
    whatever the client raises otherwise, by not catching it.
    """
    hybrid_rows = client.hybrid_search(query, collections, n_results=n_results)
    vector_rows = client.search(query, collections, n_results=n_results)

    if ground_truth_lexical_match and len(hybrid_rows) == 0 and len(vector_rows) > 0:
        raise HybridGateRegression(
            f"the fused leg returned 0 rows for {query!r} over {collections} "
            f"while the vector leg returned {len(vector_rows)}, and the corpus "
            "and the fixture's declared ground truth says the corpus carries "
            "a chunk holding this query's literal tokens. "
            "That is BUG-0148's signature: the text gate stopped matching "
            "while every other signal stayed healthy."
        )
    return len(hybrid_rows), len(vector_rows)


def _seed(db: hvc.HttpVectorClient, n: int = 6) -> tuple[list[str], list[str]]:
    """Chunks only, no catalog. Manifest-less chunks pass the gate: the
    tombstone clause excludes a chunk only when ALL its manifest rows point at
    deleted documents, so a chunk with no manifest row at all is not excluded.
    """
    ids, docs, metas = [], [], []
    for i in range(n):
        chash = hashlib.sha256(f"rdr217-detector:{i}".encode()).hexdigest()
        ids.append(chash)
        docs.append(
            f"def {_LEXICAL_QUERY}_{i}(session):\n"
            f"    # {_LEXICAL_QUERY} reads the leased session id\n"
            f"    return session.id  # probe {i}\n"
        )
        metas.append({"chunk_text_hash": chash, "title": f"detector_{i}.py:1-3"})
    db.upsert_chunks_with_embeddings(
        _COLLECTION, ids=ids, documents=docs, embeddings=[], metadatas=metas,
    )
    return ids, docs


def _corpus_carries_literal_tokens(query: str, documents: list[str]) -> bool:
    """Ground truth, from the FIXTURE's own text rather than from the engine.

    The fixture owns the corpus, so the honest check is token membership in
    Python. Asking the engine whether the corpus holds a lexical match would
    be circular with the ``hybrid_rows == 0`` observable this exists to
    explain.
    """
    blob = " ".join(documents).lower()
    return all(tok in blob for tok in query.lower().split())


def test_a_healthy_gate_reports_rows_on_both_legs(t2_service_env) -> None:
    """The baseline: both legs retrieve, so the comparator stays silent and
    the counts it reports are non-zero on both sides.

    This is the reading that makes every other case in this file
    interpretable — without it a silent comparator could mean a healthy gate
    or a detector that never fires.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _seed(db)

    hybrid_n, vector_n = detect_hybrid_gate_regression(
        db, _LEXICAL_QUERY, [_COLLECTION], ground_truth_lexical_match=True,
    )

    assert hybrid_n > 0, "the lexical gate matched nothing on a corpus built for it"
    assert vector_n > 0


def test_a_fused_zero_with_the_flag_false_is_the_correct_answer(t2_service_env) -> None:
    """The common case, and why the ground-truth flag is not ceremony.

    A query with no literal or trigram-similar match against a corpus that
    holds semantically near chunks returns zero from the fused leg and rows
    from the vector leg. That is the route working as designed — `english`
    stemming and a 0.6 trigram threshold are far narrower than vector
    similarity — so with the flag False the comparator must stay silent.

    Note this is the SAME observable pair the regression case produces
    (hybrid 0, vector > 0). The flag is the only thing that separates them,
    which is exactly why a two-input rule cannot work.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _ids, docs = _seed(db)

    # Ground truth asserted from the fixture's own text, not assumed.
    assert not _corpus_carries_literal_tokens(_NO_MATCH_QUERY, docs), (
        "this fixture's premise is that the corpus holds none of this query's "
        "literal tokens; if it does, the flag below is a lie"
    )

    hybrid_n, vector_n = detect_hybrid_gate_regression(
        db, _NO_MATCH_QUERY, [_COLLECTION], ground_truth_lexical_match=False,
    )

    assert hybrid_n == 0, (
        "precondition: this query must NOT match the gate, or it tests nothing "
        f"about the flag — got {hybrid_n} fused rows"
    )
    assert vector_n > 0, (
        "precondition: the vector leg must retrieve, or this is the degenerate "
        "nothing-retrieves case rather than the flag case"
    )


def test_the_vector_count_is_a_necessary_conjunct_not_a_decoration(t2_service_env) -> None:
    """With nothing seeded, BOTH legs return zero and the comparator stays
    silent even with the ground-truth flag True.

    This is the exclusionary job the vector count does: a wrong collection
    name, an empty collection or an engine answering nothing for every call
    means a fused zero carries no information about the gate. A two-input rule
    reading only the flag and the fused count would fire here and be wrong —
    it would report a gate regression on a corpus that holds nothing at all.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    ids, _docs = _seed(db)
    # Emptied, not absent. An UNREGISTERED collection 422s rather than
    # returning nothing (measured: "collection ... is not registered for
    # tenant"), so querying a name that was never created would exercise the
    # error path above instead of this one. Registering and then emptying is
    # what reproduces the real degenerate shape: the collection exists, the
    # engine answers, and there is simply nothing in it.
    assert db.delete_by_chunk_ids(_COLLECTION, ids) == len(ids)

    hybrid_n, vector_n = detect_hybrid_gate_regression(
        db, _LEXICAL_QUERY, [_COLLECTION], ground_truth_lexical_match=True,
    )

    assert (hybrid_n, vector_n) == (0, 0), (
        "precondition: both legs must retrieve nothing, or this is not the "
        "degenerate case the vector conjunct exists to exclude"
    )


# ── P4.2 (bead nexus-lqo4p.10): the two fixtures, and the proof the detector
# is not vacuous ─────────────────────────────────────────────────────────────
#
# Fixture A is the test above: the real gate returns zero, the flag is False,
# the comparator stays silent. Fixture B below carries the IDENTICAL observable
# pair and the opposite flag, and the comparator fires. The pair is the whole
# discrimination, which is why a detector that has never been observed failing
# would be a sweep that found nothing to check.


def test_positive_control_the_route_is_alive_on_this_very_collection(
    t2_service_env,
) -> None:
    """Fixture A's zero is evidence about the GATE only if the route works at
    all here. This asserts a SECOND query, whose literal tokens the same
    collection does carry, returns rows from that same collection.

    Without this, Fixture A passes unchanged on a substrate where
    /hybrid-search is dead, or against a mistyped collection name, or an
    unreachable engine — the reference-point failure this project has been
    bitten by more than once. The RDR does not name this control; it is here
    because A's assertion is not interpretable without it.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _ids, docs = _seed(db)

    assert _corpus_carries_literal_tokens(_LEXICAL_QUERY, docs)
    alive = db.hybrid_search(_LEXICAL_QUERY, [_COLLECTION], n_results=10)

    assert alive, (
        "the route returned nothing for a query whose literal tokens the "
        "corpus carries, so Fixture A's zero cannot be attributed to the gate"
    )


def test_fixture_b_the_planted_bug_0148_condition_makes_the_detector_fire(
    t2_service_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture B: the planted condition, and the only test here that observes
    the comparator FAILING.

    The zero is injected at the module-global ``_post`` seam while the corpus
    and the vector call stay real — see the module docstring for why B cannot
    be genuine. ``match=`` is narrow on purpose: a different AssertionError
    escaping the detector must not satisfy this.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _ids, docs = _seed(db)
    assert _corpus_carries_literal_tokens(_LEXICAL_QUERY, docs), (
        "Fixture B's premise is a corpus that DOES carry the query's literal "
        "tokens; without that the flag below would be a lie and the fire "
        "spurious"
    )

    real_post = hvc._post

    def planted_post(path, body, tenant=None, **kw):
        if path == "/v1/vectors/hybrid-search":
            return []  # BUG-0148: the gate stops matching, nothing else changes
        return real_post(path, body, tenant=tenant, **kw)

    monkeypatch.setattr(hvc, "_post", planted_post)

    # The observables, recorded before the detector is asked to judge them, so
    # the comparison test below has something to compare.
    assert db.hybrid_search(_LEXICAL_QUERY, [_COLLECTION], n_results=10) == []
    assert db.search(_LEXICAL_QUERY, [_COLLECTION], n_results=10), (
        "the vector leg must still retrieve, or this is the degenerate case "
        "rather than the planted one"
    )

    with pytest.raises(HybridGateRegression, match="ground truth"):
        detect_hybrid_gate_regression(
            db, _LEXICAL_QUERY, [_COLLECTION], ground_truth_lexical_match=True,
        )


def test_the_two_fixtures_are_identical_in_observables_and_differ_in_one_thing(
    t2_service_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 8: compare the fixtures AS FIXTURES.

    Two scenarios asserting opposite detector outcomes from an identical
    observable signature prove nothing unless the one declared difference is
    itself asserted. So: A and B produce the same (hybrid, vector) shape —
    zero fused, non-zero vector — and differ only in whether the corpus
    carries the query's literal tokens, checked in Python against the seeded
    text rather than inferred from the outcome.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _ids, docs = _seed(db)

    # A: real gate, flag False.
    a_hybrid, a_vector = detect_hybrid_gate_regression(
        db, _NO_MATCH_QUERY, [_COLLECTION], ground_truth_lexical_match=False,
    )

    # B: injected zero, flag True. Same observables by construction.
    real_post = hvc._post

    def planted_post(path, body, tenant=None, **kw):
        if path == "/v1/vectors/hybrid-search":
            return []
        return real_post(path, body, tenant=tenant, **kw)

    monkeypatch.setattr(hvc, "_post", planted_post)
    b_hybrid = len(db.hybrid_search(_LEXICAL_QUERY, [_COLLECTION], n_results=10))
    b_vector = len(db.search(_LEXICAL_QUERY, [_COLLECTION], n_results=10))

    # Identical observable signature.
    assert a_hybrid == b_hybrid == 0
    assert a_vector > 0 and b_vector > 0

    # And exactly one declared difference.
    assert _corpus_carries_literal_tokens(_NO_MATCH_QUERY, docs) is False
    assert _corpus_carries_literal_tokens(_LEXICAL_QUERY, docs) is True


@pytest.mark.parametrize("failing_route", [
    "/v1/vectors/hybrid-search",
    "/v1/vectors/search",
])
@pytest.mark.parametrize("failure", ["non_200", "timeout"])
def test_fixture_c_a_failing_call_propagates_whichever_leg_fails(
    t2_service_env, monkeypatch: pytest.MonkeyPatch,
    failing_route: str, failure: str,
) -> None:
    """Fixture C: the error path, parametrized over WHICH leg fails.

    A detector that propagates a hybrid-call failure but swallows a
    vector-call one passes a single-leg test, so both legs are planted
    separately. Two failure shapes as well: a non-200 and a transport
    timeout.

    What this pins is the ABSENCE of a handler. A failed call is not a
    zero-row reading — substituting one would make a degraded engine
    indistinguishable from the empty-corpus case, and would go quiet exactly
    when the detector is needed.
    """
    db = hvc.HttpVectorClient(tenant=t2_service_env)
    _seed(db)

    real_post = hvc._post
    exc: Exception = (
        hvc.VectorServiceError("engine is having a moment", code=502)
        if failure == "non_200" else TimeoutError("read timed out")
    )

    def failing_post(path, body, tenant=None, **kw):
        if path == failing_route:
            raise exc
        return real_post(path, body, tenant=tenant, **kw)

    monkeypatch.setattr(hvc, "_post", failing_post)

    with pytest.raises(type(exc)):
        detect_hybrid_gate_regression(
            db, _LEXICAL_QUERY, [_COLLECTION], ground_truth_lexical_match=True,
        )
