# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-v4pj4 (substantive-critic follow-on to nexus-f3yxx/nexus-iygza):
sampled audit of cross-collection ("projection") topic assignments against
an exact recompute.

Since engine-service-v0.1.132 the cross ("projection") pass of
``assign_from_chashes_<dim>`` picks each chunk's nearest FOREIGN-collection
centroid via an HNSW LATERAL (approximate,
``taxonomy-018-assign-cross-lateral-hnsw.xml``) instead of an exact join.
Measured equal to exact under production insertion order (0 wrong over
~49k decisions, 2026-09-25), but a wrong pick would be silent -- nothing
else in the audit surface re-derives the pick and compares.

ROUND-1 REVIEW FIX (both reviewers, not-justified): the first cut compared
a STORED pick against TODAY's live foreign centroids, so healthy taxonomy
growth (a topic discovered after the assignment) read as ANN drift. This
suite now covers the eligibility-cutoff mitigation (``nexus.
doctor_assignments``'s module docstring has the full design note and the
open engine-route question that mitigation does not fully close):
``test_a_topic_added_after_assignment_does_not_alarm`` is the exact
regression case the reviewers asked for.

Same shape as ``nx doctor --check-embeddings``
(``tests/test_doctor_embeddings.py``): pure-part unit tests, real-engine
substrate tests, and CLI exit-code tests.
"""
from __future__ import annotations

import hashlib
import itertools
import random

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import make_t3
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.doctor_assignments import (
    SIMILARITY_TIE_TOLERANCE,
    CollectionAssignmentDrift,
    Disagreement,
    _cosine,
    format_report,
    probe_collection,
)

_SRC = "knowledge__v4pj4-src__bge-base-en-v15-768__v1"
_DST = "knowledge__v4pj4-dst__bge-base-en-v15-768__v1"
_BARE = "knowledge__v4pj4-bare__bge-base-en-v15-768__v1"

#: topics.id is a global sequence in the substrate database, shared by
#: every tenant a test mints -- each seeded topic needs its own id
#: (mirrors tests/test_iygza_unassigned_drain.py's ``_TOPIC_IDS`` pattern).
_TOPIC_IDS = itertools.count(94001)

#: Well before any test's own wall-clock "now" -- used as a topic's
#: created_at when it must be ELIGIBLE for every assignment this suite
#: makes (real server-stamped assigned_at values are always "now" at
#: test-run time, which is always after this).
_LONG_AGO = "2020-01-01T00:00:00Z"
#: Well after any test's own wall-clock "now" -- used either as a topic's
#: created_at when it must be INELIGIBLE for an assignment already made,
#: or as a planted row's assigned_at when it must win the "most recently
#: decided" stored-pick rule against a real, server-stamped "now" row.
_FAR_FUTURE = "2099-01-01T00:00:00Z"

_TEXTS = [
    "Hilbert curves preserve locality when mapping a cube onto a line.",
    "Postgres advisory locks serialise a critical section across sessions.",
    "The rain in the valley fed three rivers before the spring thaw.",
    "A binary search tree rebalances itself after every insertion.",
    "Coastal fog rolls inland every morning before the sun burns it off.",
    "The ferry crosses the strait twice a day regardless of the tide.",
]


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _topic_created_at(tax) -> dict[int, str]:
    """Same fetch ``run_check_assignments`` makes: every topic id
    tenant-wide, mapped to its ``created_at``."""
    return {int(t["id"]): str(t.get("created_at") or "") for t in tax.get_all_topics()}


def _seed_src_with_two_dst_topics(t3, tax) -> tuple[list[str], int, int]:
    """SRC collection with real chunks; DST collection with two topics,
    both created LONG AGO (eligible for anything this suite assigns),
    whose centroids are two of SRC's own real vectors (a deterministic,
    exact-recomputable nearest-neighbor setup). Runs the real engine's
    ``assign_from_chashes`` cross pass to produce genuine projection rows.

    Returns ``(chashes, topic_a, topic_b)``; ``ids[0]``'s nearest DST
    topic is unambiguously ``topic_a`` (cosine 1.0 against its own vector).
    """
    ids = [_chash(t) for t in _TEXTS]
    t3.upsert_chunks_with_embeddings(
        collection_name=_SRC, ids=ids, documents=_TEXTS,
        embeddings=[[] for _ in ids], metadatas=[{}] * len(ids),
    )
    real = t3.get_embeddings(_SRC, ids)

    topic_a = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-a", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_LONG_AGO,
        review_status="pending", terms=None,
    )
    topic_b = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-b", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_LONG_AGO,
        review_status="pending", terms=None,
    )
    tax._centroid.upsert([
        {"collection": _DST, "topic_id": topic_a, "embedding": real[0].tolist(),
         "label": "v4pj4-a", "doc_count": 0},
        {"collection": _DST, "topic_id": topic_b, "embedding": real[-1].tolist(),
         "label": "v4pj4-b", "doc_count": 0},
    ])

    result = tax.assign_from_chashes(_SRC, ids, cross_collection=True)
    assert result["cross_assigned"] == len(ids), result

    return ids, topic_a, topic_b


def _plant_wrong_projection(tax, ids: list[str], topic_a: int, topic_b: int) -> tuple[str, int, int]:
    """Plant a competing, WRONG 'projection' row on a MIDDLE chunk (not
    ``ids[0]``/``ids[-1]``, which ARE the two centroids and so score a
    real cosine of exactly 1.0 -- nothing can out-similarity that).
    ``topic_assignments``' conflict key is ``(tenant, doc_id, topic_id)``,
    so this INSERTS a second row rather than overwriting the real one.
    The stored pick is now chosen by RECENCY (max ``assigned_at``), so
    the plant's ``assigned_at`` is set to :data:`_FAR_FUTURE`, reliably
    later than the real row's server-stamped "now" -- not by an inflated
    similarity (round-1's now-corrected rule). Returns
    ``(victim, real_topic, wrong_topic)``.
    """
    victim = ids[2]
    real_topic = tax.get_assignments_for_docs([victim])[victim]
    wrong_topic = topic_b if real_topic == topic_a else topic_a
    tax.assign_topic(
        victim, wrong_topic, "projection", similarity=0.5,
        source_collection=_SRC, assigned_at=_FAR_FUTURE,
    )
    return victim, real_topic, wrong_topic


# ── pure parts ──────────────────────────────────────────────────────────────


def test_cosine_matches_identical_and_orthogonal_vectors() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_report_is_not_clean_when_nothing_was_compared() -> None:
    lines, ok = format_report(
        [CollectionAssignmentDrift(collection="c", size=3, projection_count=1)],
        sample=20, seed=1,
    )
    assert not ok
    assert any("nothing was compared" in line for line in lines)


def test_report_flags_a_disagreement_with_its_gap() -> None:
    r = CollectionAssignmentDrift(collection="c", size=5, projection_count=5, compared=1)
    r.disagreements.append(Disagreement(
        doc_id="a" * 64, stored_topic_id=1, stored_topic_similarity=0.5,
        exact_topic_id=2, exact_similarity=0.9, gap=0.4,
        stored_assigned_at="2026-01-01T00:00:00Z",
    ))
    lines, ok = format_report([r], sample=20, seed=1)
    assert not ok
    assert any("1/1 disagree" in line for line in lines)
    assert any("stored=1 exact=2" in line for line in lines)


def test_report_names_unprobed_collections() -> None:
    probed = CollectionAssignmentDrift(collection="good", size=2, projection_count=2, compared=2)
    failed = CollectionAssignmentDrift(collection="bad", size=2, projection_count=2, error="VectorServiceError: 503")
    lines, ok = format_report([probed, failed], sample=20, seed=1)
    assert not ok
    assert any("bad: NOT PROBED" in line for line in lines)


def test_an_inconclusive_probe_is_named_not_fatal_alone() -> None:
    """A known-population collection this run's sample failed to reach is
    reported as INCONCLUSIVE, not silently dropped, and does not by
    itself flip an otherwise-clean run to failing."""
    inconclusive = CollectionAssignmentDrift(
        collection="quiet", size=2, projection_count=2, compared=0, inconclusive=True,
    )
    clean = CollectionAssignmentDrift(collection="busy", size=2, projection_count=2, compared=2)
    lines, ok = format_report([inconclusive, clean], sample=20, seed=1)
    assert ok
    assert any("INCONCLUSIVE" in line and "quiet" in line for line in lines)


def test_probe_collection_flags_a_planted_disagreement_with_fake_stores() -> None:
    """Pure-logic proof, no engine: two foreign centroids where the stored
    projection topic is provably NOT the nearest one. Both topics'
    creation times are unknown to the caller (empty map), which this
    probe treats as eligible-by-default."""

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    class _Centroid:
        def get_foreign(self, name):
            return {
                "embeddings": [[1.0, 0.0], [0.0, 1.0]],
                "metadatas": [{"topic_id": 10}, {"topic_id": 11}],
            }

    class _Taxo:
        _centroid = _Centroid()

        def get_assignment_details(self, doc_ids):
            return [{"doc_id": "a" * 64, "topic_id": 11, "assigned_by": "projection",
                      "source_collection": "c", "similarity": 0.0,
                      "assigned_at": "2026-01-01T00:00:00Z"}]

    r = probe_collection(_Taxo(), _T3(), "c", size=1, projection_count=1,
                          sample=20, rng=random.Random(1), topic_created_at={})

    assert r.error is None
    assert r.compared == 1
    assert len(r.disagreements) == 1
    d = r.disagreements[0]
    assert d.stored_topic_id == 11 and d.exact_topic_id == 10
    assert d.gap == pytest.approx(1.0)


def test_probe_collection_ties_within_tolerance_are_not_disagreements() -> None:
    """Two foreign centroids at (numerically) near-equal distance: the
    engine's own tie-break (lower topic_id) picks 10, and a stored pick
    of 11 within :data:`SIMILARITY_TIE_TOLERANCE` is float noise, not a
    real disagreement."""

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    tiny = SIMILARITY_TIE_TOLERANCE / 100

    class _Centroid:
        def get_foreign(self, name):
            return {
                "embeddings": [[1.0, 0.0], [1.0 - tiny, tiny]],
                "metadatas": [{"topic_id": 10}, {"topic_id": 11}],
            }

    class _Taxo:
        _centroid = _Centroid()

        def get_assignment_details(self, doc_ids):
            return [{"doc_id": "a" * 64, "topic_id": 11, "assigned_by": "projection",
                      "source_collection": "c", "similarity": 0.0,
                      "assigned_at": "2026-01-01T00:00:00Z"}]

    r = probe_collection(_Taxo(), _T3(), "c", size=1, projection_count=1,
                          sample=20, rng=random.Random(1), topic_created_at={})

    assert r.error is None
    assert r.compared == 1
    assert r.disagreements == []


def test_probe_collection_reports_a_deleted_foreign_topic_without_erroring() -> None:
    """A stored projection row referencing a topic that no longer has a
    live foreign centroid (deleted/rebuilt) is excluded from ``compared``
    rather than raising or counting as a disagreement."""

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    class _Centroid:
        def get_foreign(self, name):
            return {"embeddings": [[1.0, 0.0]], "metadatas": [{"topic_id": 10}]}

    class _Taxo:
        _centroid = _Centroid()

        def get_assignment_details(self, doc_ids):
            return [{"doc_id": "a" * 64, "topic_id": 999, "assigned_by": "projection",
                      "source_collection": "c", "similarity": 0.0,
                      "assigned_at": "2026-01-01T00:00:00Z"}]

    r = probe_collection(_Taxo(), _T3(), "c", size=1, projection_count=1,
                          sample=20, rng=random.Random(1), topic_created_at={})

    assert r.error is None
    assert r.compared == 0
    assert r.no_foreign_centroids == 1
    assert r.disagreements == []


def test_probe_collection_excludes_a_topic_created_after_the_cutoff() -> None:
    """Pure-logic proof of the eligibility filter itself: a foreign topic
    whose created_at postdates the stored pick's assigned_at is excluded
    from the exact recompute's candidate set, even though it is a
    strictly BETTER (cosine 1.0) match than the stored pick -- this is
    the mechanism ``test_a_topic_added_after_assignment_does_not_alarm``
    (real engine substrate, below) exercises end to end."""

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    class _Centroid:
        def get_foreign(self, name):
            return {
                # topic 10: the stored (older) pick, a weaker match.
                # topic 11: a PERFECT match, but created AFTER the stored
                # pick's assigned_at -- must be excluded.
                "embeddings": [[0.6, 0.8], [1.0, 0.0]],
                "metadatas": [{"topic_id": 10}, {"topic_id": 11}],
            }

    class _Taxo:
        _centroid = _Centroid()

        def get_assignment_details(self, doc_ids):
            return [{"doc_id": "a" * 64, "topic_id": 10, "assigned_by": "projection",
                      "source_collection": "c", "similarity": 0.6,
                      "assigned_at": "2026-01-01T00:00:00Z"}]

    topic_created_at = {10: "2020-01-01T00:00:00Z", 11: "2026-06-01T00:00:00Z"}

    r = probe_collection(_Taxo(), _T3(), "c", size=1, projection_count=1,
                          sample=20, rng=random.Random(1), topic_created_at=topic_created_at)

    assert r.error is None
    assert r.compared == 1
    assert r.disagreements == [], (
        "topic 11 postdates the stored pick's assigned_at and must be "
        f"excluded from the candidate set: {r.disagreements}"
    )


# ── real engine substrate ────────────────────────────────────────────────────


def test_clean_cross_collection_projection_agrees_with_the_exact_recompute(t2_service_env) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)

    r = probe_collection(tax, t3, _SRC, size=len(ids), projection_count=len(ids),
                          sample=20, rng=random.Random(1), topic_created_at=_topic_created_at(tax))

    assert r.error is None, r.error
    assert r.compared == len(ids)
    assert r.disagreements == [], r.disagreements


def test_a_planted_wrong_projection_is_flagged(t2_service_env) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)
    victim, real_topic, wrong_topic = _plant_wrong_projection(tax, ids, topic_a, topic_b)

    r = probe_collection(tax, t3, _SRC, size=len(ids), projection_count=len(ids),
                          sample=20, rng=random.Random(1), topic_created_at=_topic_created_at(tax))

    assert r.error is None, r.error
    assert len(r.disagreements) == 1, r.disagreements
    d = r.disagreements[0]
    assert d.doc_id == victim
    assert d.stored_topic_id == wrong_topic
    assert d.exact_topic_id == real_topic


def test_a_topic_added_after_assignment_does_not_alarm(t2_service_env) -> None:
    """THE round-1 regression case: DST starts with ONE topic (topic_a),
    every SRC chunk is genuinely, correctly assigned to it (it is the
    ONLY candidate). AFTER that, a second DST topic (topic_b) is
    discovered whose centroid happens to be a PERFECT match for one of
    the already-assigned chunks -- healthy taxonomy growth, not an ANN
    defect. An unrestricted "exact recompute over today's live
    centroids" would wrongly flag every one of those chunks now that a
    strictly closer topic exists; the eligibility cutoff (topic_b's
    created_at postdates every stored assigned_at) must not.
    """
    ids = [_chash(t) for t in _TEXTS]
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    t3.upsert_chunks_with_embeddings(
        collection_name=_SRC, ids=ids, documents=_TEXTS,
        embeddings=[[] for _ in ids], metadatas=[{}] * len(ids),
    )
    real = t3.get_embeddings(_SRC, ids)

    topic_a = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-only", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_LONG_AGO,
        review_status="pending", terms=None,
    )
    tax._centroid.upsert([{
        "collection": _DST, "topic_id": topic_a, "embedding": real[0].tolist(),
        "label": "v4pj4-only", "doc_count": 0,
    }])

    # Every chunk is assigned NOW, to the ONLY topic that exists -- the
    # correct, unambiguous answer at this moment.
    result = tax.assign_from_chashes(_SRC, ids, cross_collection=True)
    assert result["cross_assigned"] == len(ids), result

    # Taxonomy grows: a SECOND topic is discovered afterward, and it
    # happens to be a perfect match for ids[2] -- created_at is set to
    # _FAR_FUTURE so this holds regardless of real wall-clock timing.
    topic_b_new = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-new", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_FAR_FUTURE,
        review_status="pending", terms=None,
    )
    tax._centroid.upsert([{
        "collection": _DST, "topic_id": topic_b_new, "embedding": real[2].tolist(),
        "label": "v4pj4-new", "doc_count": 0,
    }])

    r = probe_collection(tax, t3, _SRC, size=len(ids), projection_count=len(ids),
                          sample=20, rng=random.Random(1), topic_created_at=_topic_created_at(tax))

    assert r.error is None, r.error
    assert r.compared == len(ids)
    assert r.disagreements == [], (
        "topic_b_new was discovered AFTER every stored assignment and must "
        f"not count against them: {r.disagreements}"
    )


def test_a_collection_with_no_cross_projection_population_is_not_applicable(t2_service_env) -> None:
    """A single-collection tenant (no OTHER collection to project onto) has
    a genuinely empty projection population -- 'nothing to audit', not a
    failure."""
    from nexus.doctor_assignments import run_check_assignments

    ids = [_chash(f"v4pj4 bare chunk {i}") for i in range(3)]
    t3 = make_t3()
    t3.upsert_chunks_with_embeddings(
        collection_name=_BARE, ids=ids,
        documents=[f"v4pj4 bare chunk {i}" for i in range(3)],
        embeddings=[[] for _ in ids], metadatas=[{}] * len(ids),
    )

    run_check_assignments(sample=20, collections=(_BARE,), seed=1)  # must not raise SystemExit


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_exit_codes(t2_service_env) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)
    runner = CliRunner()

    clean = runner.invoke(main, ["doctor", "--check-assignments", "--assignments-collection", _SRC])
    assert clean.exit_code == 0, clean.output
    assert f"{len(ids)} assignment(s) compared" in clean.output

    _plant_wrong_projection(tax, ids, topic_a, topic_b)
    drifted = runner.invoke(main, ["doctor", "--check-assignments", "--assignments-collection", _SRC])
    assert drifted.exit_code == 1, drifted.output
    assert f"{_SRC}: 1/" in drifted.output

    unknown = runner.invoke(
        main,
        ["doctor", "--check-assignments", "--assignments-collection",
         "knowledge__nope__bge-base-en-v15-768__v1"],
    )
    assert unknown.exit_code == 1, unknown.output
    assert "no such collection" in unknown.output


def test_json_is_refused_with_check_assignments() -> None:
    result = CliRunner().invoke(main, ["doctor", "--check-assignments", "--json"])
    assert result.exit_code != 0
    assert "--check-assignments" in result.output


@pytest.mark.parametrize("sample", [0, 301])
def test_sample_size_is_bounded(sample: int) -> None:
    result = CliRunner().invoke(main, ["doctor", "--check-assignments", "--assignments-sample", str(sample)])
    assert result.exit_code == 2, result.output
