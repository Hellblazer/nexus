# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-v4pj4 (substantive-critic follow-on to nexus-f3yxx/nexus-iygza):
sampled audit of cross-collection ("projection") topic assignments,
comparing the engine's LIVE ANN pick (``POST /v1/taxonomy/assignments/
cross-preview``, taxonomy-021) against an exact Python recompute over the
SAME live foreign-centroid snapshot, at the SAME moment.

ROUND-1 REVIEW (both reviewers, not-justified): the first cut compared a
STORED historical pick against TODAY's live centroids, so healthy taxonomy
growth (or a centroid revised via rebuild/merge) could read as ANN drift.

ROUND-2 (this version, coordinator decision: build the engine route now,
in this bead): a new read-only engine route, ``cross_preview_<dim>``
(``nexus.doctor_assignments``'s module docstring has the full design and
the investigation that ruled out every existing route), makes the
comparison genuinely same-moment. The round-1 eligibility-cutoff
mitigation (excluding foreign topics created after a stored assignment)
is REMOVED here -- it is moot: there is no more "stored decision" the
recompute needs to be scoped against; every sampled chunk's engine
answer and exact answer are both computed fresh, in this run, over
whatever foreign centroids exist right now.

Same shape as ``nx doctor --check-embeddings``
(``tests/test_doctor_embeddings.py``): pure-part unit tests, real-engine
substrate tests, and CLI exit-code tests.
"""
from __future__ import annotations

import hashlib
import itertools
import random

import httpx
import pytest
from click.testing import CliRunner

import nexus.doctor_assignments as doctor_assignments
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

#: Well before any test's own wall-clock "now" -- used for a topic's
#: created_at where the test wants it to read as "an older topic", purely
#: for narrative/context; the round-2 oracle no longer treats this as
#: eligibility data (see the module docstring).
_LONG_AGO = "2020-01-01T00:00:00Z"
#: Well after any test's own wall-clock "now" -- same purpose, reversed.
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


def _seed_src_with_two_dst_topics(t3, tax) -> tuple[list[str], int, int]:
    """SRC collection with real chunks; DST collection with two topics
    whose centroids are two of SRC's own real vectors (a deterministic,
    exact-recomputable nearest-neighbor setup).

    Returns ``(chashes, topic_a, topic_b)``; ``ids[0]``'s nearest DST
    topic is unambiguously ``topic_a`` (cosine 1.0 against its own vector),
    and ``ids[-1]``'s is unambiguously ``topic_b``.
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

    return ids, topic_a, topic_b


def _plant_wrong_ann_pick(monkeypatch, tax, victim: str, wrong_topic: int) -> None:
    """Monkeypatch ``HttpTaxonomyStore.cross_preview`` at the CLASS level (so
    it takes effect even through a fresh instance, e.g. inside a CLI
    invocation or ``run_check_assignments``) so *victim*'s entry in the
    real response is overwritten with *wrong_topic* -- a "client fake"
    forced disagreement, per the round-2 review's own suggested mechanism
    (the engine's real ANN, at this fixture's tiny scale, agrees with exact
    essentially always, so there is no way to force a genuine engine-side
    miss in a lightweight test; faking the wire response is the documented
    alternative).
    """
    original = HttpTaxonomyStore.cross_preview

    def _patched(self, collection, chashes):
        result = original(self, collection, chashes)
        if victim in result:
            result[victim] = (wrong_topic, 0.5)
        return result

    monkeypatch.setattr(HttpTaxonomyStore, "cross_preview", _patched)


# ── pure parts ──────────────────────────────────────────────────────────────


def test_cosine_matches_identical_and_orthogonal_vectors() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_report_is_not_clean_when_nothing_was_compared() -> None:
    lines, ok = format_report([CollectionAssignmentDrift(collection="c", size=3)], sample=20, seed=1)
    assert not ok
    assert any("nothing was compared" in line for line in lines)


def test_report_flags_a_disagreement_with_its_gap() -> None:
    r = CollectionAssignmentDrift(collection="c", size=5, compared=1)
    r.disagreements.append(Disagreement(
        doc_id="a" * 64, ann_topic_id=1, ann_reported_similarity=0.5,
        ann_recomputed_similarity=0.5, exact_topic_id=2, exact_similarity=0.9, gap=0.4,
    ))
    lines, ok = format_report([r], sample=20, seed=1)
    assert not ok
    assert any("1/1 disagree" in line for line in lines)
    assert any("ann=1 exact=2" in line for line in lines)


def test_report_flags_a_disagreement_and_shows_stored_context() -> None:
    r = CollectionAssignmentDrift(collection="c", size=5, compared=1)
    r.disagreements.append(Disagreement(
        doc_id="a" * 64, ann_topic_id=1, ann_reported_similarity=0.5,
        ann_recomputed_similarity=0.5, exact_topic_id=2, exact_similarity=0.9, gap=0.4,
        stored_topic_id=7,
    ))
    lines, ok = format_report([r], sample=20, seed=1)
    assert not ok
    assert any("(stored=7)" in line for line in lines)


def test_report_names_unprobed_collections() -> None:
    probed = CollectionAssignmentDrift(collection="good", size=2, compared=2)
    failed = CollectionAssignmentDrift(collection="bad", size=2, error="VectorServiceError: 503")
    lines, ok = format_report([probed, failed], sample=20, seed=1)
    assert not ok
    assert any("bad: NOT PROBED" in line for line in lines)


def test_report_names_not_applicable_collections_and_stays_ok() -> None:
    not_applicable = CollectionAssignmentDrift(collection="lonely", size=2, not_applicable=True)
    clean = CollectionAssignmentDrift(collection="busy", size=2, compared=2)
    lines, ok = format_report([not_applicable, clean], sample=20, seed=1)
    assert ok
    assert any("not applicable" in line and "lonely" in line for line in lines)


def test_an_inconclusive_probe_is_named_not_fatal_alone() -> None:
    inconclusive = CollectionAssignmentDrift(collection="quiet", size=2, compared=0, inconclusive=True)
    clean = CollectionAssignmentDrift(collection="busy", size=2, compared=2)
    lines, ok = format_report([inconclusive, clean], sample=20, seed=1)
    assert ok
    assert any("INCONCLUSIVE" in line and "quiet" in line for line in lines)


def test_probe_collection_flags_a_planted_disagreement_with_fake_stores() -> None:
    """Pure-logic proof, no engine: the engine's (faked) live ANN pick names
    a topic that is provably NOT the nearest one over the fetched
    foreign-centroid snapshot."""

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

        def cross_preview(self, name, ids):
            return {"a" * 64: (11, 0.0)}  # wrong: topic 10 is the true nearest

        def get_assignment_details(self, ids):
            return []

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.compared == 1
    assert len(r.disagreements) == 1
    d = r.disagreements[0]
    assert d.ann_topic_id == 11 and d.exact_topic_id == 10
    assert d.gap == pytest.approx(1.0)


def test_probe_collection_ties_within_tolerance_are_not_disagreements() -> None:
    """Two foreign centroids at (numerically) near-equal distance: exact
    picks 10 (the true argmax, if only by a hair), and an engine pick of
    11 within :data:`SIMILARITY_TIE_TOLERANCE` is float noise, not a real
    disagreement."""

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

        def cross_preview(self, name, ids):
            return {"a" * 64: (11, 0.0)}

        def get_assignment_details(self, ids):
            return []

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.compared == 1
    assert r.disagreements == []


def test_probe_collection_reports_a_raced_foreign_centroid_without_erroring() -> None:
    """The engine's live ANN pick names a topic this probe's OWN
    foreign-centroid fetch no longer carries (a rebuild/deletion raced
    between the two calls) -- excluded from ``compared``, never a
    disagreement, never an error."""

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

        def cross_preview(self, name, ids):
            return {"a" * 64: (999, 0.5)}  # topic 999 not in the fetched snapshot

        def get_assignment_details(self, ids):
            return []

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.compared == 0
    assert r.no_foreign_centroids == 1
    assert r.disagreements == []


def test_probe_collection_reports_changed_during_probe_not_a_disagreement() -> None:
    """Round-2 review (critic, Significant + code-review Minor): a foreign
    centroid appearing (or otherwise changing) between this probe's own
    before/after ``get_foreign`` reads must not read as a disagreement --
    the snapshot never stabilizes across the first attempt or its one
    retry, so the collection is reported CHANGED DURING PROBE instead of
    being compared against a snapshot the engine's own answer may never
    have seen.
    """

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    class _Centroid:
        """Every ``get_foreign`` call returns a snapshot ONE topic LARGER
        than the last -- a new, closer centroid keeps appearing -- so no
        two consecutive before/after reads (this attempt's pair, or the
        retry's) ever match, exhausting the one retry."""

        def __init__(self):
            self.calls = 0

        def get_foreign(self, name):
            self.calls += 1
            return {
                "embeddings": [[1.0, 0.0]] * self.calls,
                "metadatas": [{"topic_id": 10 + i} for i in range(self.calls)],
            }

    class _Taxo:
        _centroid = _Centroid()

        def cross_preview(self, name, ids):
            return {"a" * 64: (10, 1.0)}

        def get_assignment_details(self, ids):
            return []

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.compared == 0
    assert r.disagreements == []
    assert r.changed_during_probe is True
    assert r.inconclusive is False, "changed_during_probe and inconclusive are mutually exclusive"


def _mismatch_then_match_centroid_factory():
    """A fresh ``_Centroid`` whose first ``get_foreign`` call returns a
    ONE-topic snapshot, and every later call returns a TWO-topic snapshot
    that stays constant from then on -- so the FIRST attempt's before/after
    pair mismatches (topic 11 appeared mid-attempt), but the RETRY's
    before/after pair matches (the snapshot already stabilized by call 3).
    """

    class _Centroid:
        def __init__(self):
            self.calls = 0

        def get_foreign(self, name):
            self.calls += 1
            if self.calls == 1:
                return {"embeddings": [[1.0, 0.0]], "metadatas": [{"topic_id": 10}]}
            return {
                "embeddings": [[1.0, 0.0], [0.0, 1.0]],
                "metadatas": [{"topic_id": 10}, {"topic_id": 11}],
            }

    return _Centroid


def test_probe_collection_recovers_on_retry_after_a_first_attempt_mismatch(monkeypatch) -> None:
    """Round-3 review (Minor): the retry actually matters, not just the
    detection. When the snapshot mismatches on the first attempt but has
    already stabilized by the retry, the run must recover -- comparing
    against the stabilized snapshot, no false disagreement, and
    ``changed_during_probe`` must stay False. Then, forced down to
    ``_MAX_SNAPSHOT_ATTEMPTS=1`` (no retry allowed), the IDENTICAL race
    must NOT recover: this proves the retry is load-bearing, not a no-op.
    """

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}

    _Centroid = _mismatch_then_match_centroid_factory()

    class _Taxo:
        def __init__(self):
            self._centroid = _Centroid()

        def cross_preview(self, name, ids):
            return {"a" * 64: (10, 1.0)}

        def get_assignment_details(self, ids):
            return []

    # Default _MAX_SNAPSHOT_ATTEMPTS=2: attempt 0 mismatches (1 topic vs 2),
    # the retry's before/after pair both see the stabilized 2-topic
    # snapshot -- recovers, no false disagreement.
    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))
    assert r.error is None
    assert r.compared > 0
    assert r.disagreements == []
    assert r.changed_during_probe is False

    # Non-vacuity: the SAME race, with NO retry allowed, must NOT recover --
    # proving the recovery above genuinely came from the retry and not from
    # some other path.
    monkeypatch.setattr(doctor_assignments, "_MAX_SNAPSHOT_ATTEMPTS", 1)
    r_no_retry = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))
    assert r_no_retry.error is None
    assert r_no_retry.compared == 0
    assert r_no_retry.changed_during_probe is True


def test_a_centroid_change_in_an_unrelated_dim_does_not_flag_this_collection() -> None:
    """Round-3 review (Minor), tightened by round-4 review (Minor: the
    first version was vacuous -- its one-step unrelated-dim change
    stabilized by the retry EVEN WITHOUT dim scoping, so it passed for the
    wrong reason). ``get_foreign`` returns every OTHER collection's
    centroids across every embedding dim a tenant's collections use, not
    just the sampled source collection's own dim (here, dim 2 -- the
    fixture's chunk vector is ``[1.0, 0.0]``).

    This fixture's dim-2 topic (10) is CONSTANT across every call, so the
    DIM-SCOPED comparison matches on the very first attempt (no retry
    needed) -- but its dim-3 topic gets a NEW id on every single call, so
    an UNSCOPED comparison would never stabilize across ANY two calls,
    including the retry's, and would report ``changed_during_probe=True``.
    The two predictions genuinely diverge on this fixture, so it actually
    exercises the scoping rather than merely being consistent with it.
    """

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {"a" * 64: [1.0, 0.0]}  # dim 2

    class _Centroid:
        """dim-2 topic 10 is STABLE across every call. dim-3 gets a BRAND
        NEW topic id on every single call (900 + call number) -- so an
        unscoped comparison never sees the same dim-3 snapshot twice, on
        the first attempt OR the retry, and would never stabilize."""

        def __init__(self):
            self.calls = 0

        def get_foreign(self, name):
            self.calls += 1
            return {
                "embeddings": [[1.0, 0.0], [0.0, 0.0, 1.0]],
                "metadatas": [{"topic_id": 10}, {"topic_id": 900 + self.calls}],
            }

    class _Taxo:
        _centroid = _Centroid()

        def cross_preview(self, name, ids):
            return {"a" * 64: (10, 1.0)}

        def get_assignment_details(self, ids):
            return []

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.changed_during_probe is False
    assert r.compared == 1
    assert r.disagreements == []


def test_report_names_changed_during_probe_collections_and_is_not_a_clean_pass() -> None:
    changed = CollectionAssignmentDrift(collection="racy", size=2, changed_during_probe=True)
    clean = CollectionAssignmentDrift(collection="busy", size=2, compared=2)
    lines, ok = format_report([changed, clean], sample=20, seed=1)
    assert ok  # `busy` alone already makes this run clean; `racy` is not a failure
    assert any("CHANGED DURING PROBE" in line and "racy" in line for line in lines)


def test_a_changed_during_probe_only_run_is_not_a_clean_result() -> None:
    """Mirrors the existing all-inconclusive case: a run where the ONLY
    thing that happened is a snapshot race is not a clean pass either --
    nothing was actually compared."""
    changed = CollectionAssignmentDrift(collection="racy", size=2, changed_during_probe=True)
    lines, ok = format_report([changed], sample=20, seed=1)
    assert not ok
    assert any("CHANGED DURING PROBE" in line and "racy" in line for line in lines)
    assert any("nothing was compared" in line for line in lines)


def test_probe_collection_no_candidate_answered_is_not_applicable() -> None:
    """The engine's cross-preview returns nothing at all for every sampled
    candidate (no live chunk vector at this dim, or no foreign centroid to
    project onto) -- reported not_applicable, never a failure."""

    class _Col:
        def get(self, **kw):
            return {"ids": ["a" * 64]}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

        def get_embeddings_by_id(self, name, ids):
            return {}

    class _Centroid:
        def get_foreign(self, name):
            return {"embeddings": [], "metadatas": []}

    class _Taxo:
        _centroid = _Centroid()

        def cross_preview(self, name, ids):
            return {}

    r = probe_collection(_Taxo(), _T3(), "c", size=1, sample=20, rng=random.Random(1))

    assert r.error is None
    assert r.not_applicable is True
    assert r.compared == 0
    assert r.disagreements == []


# ── real engine substrate ────────────────────────────────────────────────────


def test_clean_cross_collection_projection_agrees_with_the_exact_recompute(t2_service_env) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)

    r = probe_collection(tax, t3, _SRC, size=len(ids), sample=20, rng=random.Random(1))

    assert r.error is None, r.error
    assert r.compared == len(ids)
    assert r.disagreements == [], r.disagreements


def test_a_planted_wrong_ann_pick_is_flagged(t2_service_env, monkeypatch) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)

    # A MIDDLE chunk (not ids[0]/ids[-1], which ARE the two centroids and so
    # score a real cosine of exactly 1.0 to their own topic -- nothing else
    # could plausibly beat that, defeating the point of forcing a MISMATCH).
    victim = ids[2]
    real_ann = tax.cross_preview(_SRC, [victim])
    real_topic_id, _real_sim = real_ann[victim]
    wrong_topic = topic_b if real_topic_id == topic_a else topic_a
    _plant_wrong_ann_pick(monkeypatch, tax, victim, wrong_topic)

    r = probe_collection(tax, t3, _SRC, size=len(ids), sample=20, rng=random.Random(1))

    assert r.error is None, r.error
    assert len(r.disagreements) == 1, r.disagreements
    d = r.disagreements[0]
    assert d.doc_id == victim
    assert d.ann_topic_id == wrong_topic
    assert d.exact_topic_id == real_topic_id


def test_a_topic_added_after_assignment_does_not_alarm(t2_service_env) -> None:
    """THE round-1 regression case, now closed STRUCTURALLY rather than by
    an eligibility filter: DST starts with one topic; a REAL assignment is
    made (for narrative realism only -- the round-2 oracle does not read
    it); taxonomy then grows a second, strictly closer topic. Since both
    the engine's live ANN pick and the exact recompute are computed FRESH,
    in this run, over whatever foreign centroids exist NOW, the new topic
    is simply a valid candidate for both -- there is no 'old decision' left
    to compare against, so this is not even a distinguishable event any
    more, and it must not alarm.
    """
    ids = [_chash(t) for t in _TEXTS]
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    t3.upsert_chunks_with_embeddings(
        collection_name=_SRC, ids=ids, documents=_TEXTS,
        embeddings=[[] for _ in ids], metadatas=[{}] * len(ids),
    )
    real = t3.get_embeddings(_SRC, ids)

    topic_old = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-old", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_LONG_AGO,
        review_status="pending", terms=None,
    )
    tax._centroid.upsert([{
        "collection": _DST, "topic_id": topic_old, "embedding": real[0].tolist(),
        "label": "v4pj4-old", "doc_count": 0,
    }])
    result = tax.assign_from_chashes(_SRC, ids, cross_collection=True)
    assert result["cross_assigned"] == len(ids), result

    # Taxonomy grows: a second topic, a PERFECT match for ids[2], appears
    # strictly after the assignment above.
    topic_new = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="v4pj4-new", parent_id=None, collection=_DST,
        centroid_hash=None, doc_count=0, created_at=_FAR_FUTURE,
        review_status="pending", terms=None,
    )
    tax._centroid.upsert([{
        "collection": _DST, "topic_id": topic_new, "embedding": real[2].tolist(),
        "label": "v4pj4-new", "doc_count": 0,
    }])

    r = probe_collection(tax, t3, _SRC, size=len(ids), sample=20, rng=random.Random(1))

    assert r.error is None, r.error
    assert r.compared == len(ids)
    assert r.disagreements == [], (
        f"topic_new is a valid live candidate for BOTH the engine's live pick and"
        f" the exact recompute -- neither should disagree: {r.disagreements}"
    )


def test_a_collection_with_no_live_foreign_centroid_is_not_applicable(t2_service_env) -> None:
    """A single-collection tenant (no OTHER collection to project onto) has
    no live cross-collection candidate -- 'nothing to audit', not a
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


def test_an_engine_without_cross_preview_is_named_not_applicable(t2_service_env, monkeypatch) -> None:
    """An engine older than the version that shipped POST .../cross-preview
    404s on the very first call -- reported as one 'not applicable' line,
    exit 0, never N per-collection failures."""
    from nexus.doctor_assignments import run_check_assignments

    ids = [_chash(f"v4pj4 old-engine chunk {i}") for i in range(3)]
    t3 = make_t3()
    t3.upsert_chunks_with_embeddings(
        collection_name=_SRC, ids=ids,
        documents=[f"v4pj4 old-engine chunk {i}" for i in range(3)],
        embeddings=[[] for _ in ids], metadatas=[{}] * len(ids),
    )

    def _404(self, collection, chashes):
        req = httpx.Request("POST", "http://engine/v1/taxonomy/assignments/cross-preview")
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    monkeypatch.setattr(HttpTaxonomyStore, "cross_preview", _404)

    run_check_assignments(sample=20, collections=(_SRC,), seed=1)  # must not raise SystemExit


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_exit_codes(t2_service_env, monkeypatch) -> None:
    t3 = make_t3()
    tax = HttpTaxonomyStore()
    ids, topic_a, topic_b = _seed_src_with_two_dst_topics(t3, tax)
    runner = CliRunner()

    clean = runner.invoke(main, ["doctor", "--check-assignments", "--assignments-collection", _SRC])
    assert clean.exit_code == 0, clean.output
    assert f"{len(ids)} chunk(s) compared" in clean.output

    victim = ids[2]
    real_ann = tax.cross_preview(_SRC, [victim])
    real_topic_id, _real_sim = real_ann[victim]
    wrong_topic = topic_b if real_topic_id == topic_a else topic_a
    _plant_wrong_ann_pick(monkeypatch, tax, victim, wrong_topic)

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
