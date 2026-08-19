# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h33x8.6 a1: seed retrieval-only (single ``query``-step) plans so
the documented ``nx_answer`` fast path is actually reachable.

Finding (deep-analyst, 2026-08-19, T2 nexus/nx-answer-capability-analysis-
2026-08-19): ``_nx_answer_classify_plan`` reroutes a matched plan straight
to ``query()`` (mean 2.1s, no ``claude -p``) ONLY when the plan has exactly
one step whose tool is ``query`` -- but every one of the 21 plans in the
library at the time ended in an operator (``extract``/``rank``/``compare``/
``summarize``/``generate``/...), so the fast path was structurally
unreachable in practice. This test seeds two new single-``query``-step
builtin templates (``conexus/plans/builtin/document-discovery.yml``,
``corpus-coverage-check.yml``) for the "which documents discuss X" /
"what does the corpus have on X" question shape, and proves, against a
REAL cosine-similarity plan match (bundled ONNX MiniLM, offline, no
network -- ``PlanSessionCache`` + ``InMemoryVectorClient``) competing
against the full real builtin-template pool, that natural phrasings of
that shape actually route onto the fast-path classification.

This is a document-DISCOVERY shape ("which documents exist on X"), not a
synthesized-answer shape ("what does the evidence say about X") -- the
latter genuinely needs composition and is out of scope for a single-step
reroute; see the narrowed guidance (nexus-h33x8.6, DO step 3) for the
do-NOT list.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

# The natural phrasings this seed is meant to catch. Each covers one of
# the three shapes named in the bead: "which documents ... discuss X",
# "find the papers/RDRs about X", "what does the corpus have on X".
_PHRASINGS = [
    "which documents discuss vector search",
    "find the papers about query planning",
    "is there anything in the corpus about release notes",
    "does the corpus cover session memory",
    "list the documents that mention hybrid retrieval",
    "does the corpus have anything about the release process",
]

# Names of the new seed templates -- a hit on either is a pass; the two
# templates cover close-but-distinct phrasings of the same discovery
# shape and the matcher may prefer either depending on the exact wording.
_TARGET_PLAN_NAMES = {"document-discovery", "corpus-coverage-check"}


@pytest.fixture()
def seeded_library(tmp_path: Path):
    """A fresh HttpPlanLibrary seeded from the REAL builtin template
    directory -- the same directory ``nx plan reseed`` loads in
    production. Proves the new templates parse, validate, and load
    alongside the other 15+, not in isolation."""
    from nexus.db.t2 import T2Database
    from nexus.plans.seed_loader import load_seed_directory

    library = T2Database(tmp_path / "m.db").plans
    result = load_seed_directory(_BUILTIN_DIR, library=library)
    assert not result.errors, f"builtin template load errors: {result.errors}"
    return library


@pytest.fixture()
def real_plan_cache(seeded_library):
    """A REAL cosine-similarity PlanCache (bundled ONNX MiniLM, offline)
    populated from the seeded library -- the same combination
    ``test_plan_session_cache_query.py`` uses, so this exercises the
    genuine T1 embedding path plan_match takes in production, not a
    scripted stand-in."""
    from tests.conftest import make_vector_test_client
    from nexus.plans.session_cache import PlanSessionCache

    cache = PlanSessionCache(
        client=make_vector_test_client(), session_id="test-h33x8-a1-seed",
    )
    loaded = cache.populate(seeded_library, project="")
    assert loaded > 0, "PlanSessionCache.populate loaded zero rows from the seeded library"
    return cache


class TestDocumentDiscoverySeedPlansExist:
    def test_both_new_templates_present_in_builtin_dir(self) -> None:
        names = {p.stem for p in _BUILTIN_DIR.glob("*.yml")}
        missing = _TARGET_PLAN_NAMES - names
        assert not missing, f"expected new seed templates not found: {missing}"

    def test_new_templates_are_single_query_step(self) -> None:
        """Structural pin: each new template must classify as
        single_query -- a second step, or a non-query tool, would
        silently defeat the whole point of this seed."""
        import yaml

        for stem in _TARGET_PLAN_NAMES:
            path = _BUILTIN_DIR / f"{stem}.yml"
            template = yaml.safe_load(path.read_text())
            steps = template["plan_json"]["steps"]
            assert len(steps) == 1, f"{stem}: expected exactly 1 step, got {len(steps)}"
            assert steps[0]["tool"] == "query", (
                f"{stem}: expected tool 'query', got {steps[0]['tool']!r}"
            )


class TestDocumentDiscoveryPlanMatchReroute:
    """The load-bearing assertion: real cosine match + real classify."""

    @pytest.mark.parametrize("question", _PHRASINGS)
    def test_phrasing_hits_seed_plan_and_classifies_single_query(
        self, question: str, seeded_library, real_plan_cache,
    ) -> None:
        from nexus.mcp.core import _nx_answer_classify_plan
        from nexus.plans.matcher import plan_match

        matches = plan_match(
            intent=question,
            library=seeded_library,
            cache=real_plan_cache,
            dimensions={"verb": "research"},
            min_confidence=0.40,
            n=5,
        )
        assert matches, f"no match >= 0.40 confidence for {question!r}"

        top = matches[0]
        row = seeded_library.get_plan(top.plan_id)
        assert row is not None
        assert row["name"] in _TARGET_PLAN_NAMES, (
            f"{question!r} matched {row['name']!r} (confidence="
            f"{top.confidence}), not one of the new discovery seeds "
            f"{_TARGET_PLAN_NAMES} -- top-5: "
            f"{[(seeded_library.get_plan(m.plan_id)['name'], m.confidence) for m in matches]}"
        )
        assert top.confidence is not None and top.confidence >= 0.40, (
            f"{question!r} matched {row['name']!r} but confidence "
            f"{top.confidence} is below the RDR-079 gate"
        )

        plan_class = _nx_answer_classify_plan(top)
        assert plan_class == "single_query", (
            f"{question!r} matched {row['name']!r} but classified as "
            f"{plan_class!r}, not single_query -- the fast-path reroute "
            f"in nx_answer would NOT fire"
        )


# Synthesis-shaped questions genuinely need composition (search + rank/
# compare/generate) -- the answer has to be REDUCED FROM multiple sources,
# not just found. Each covers one of the synthesis phrasings the bead's
# narrowed guidance (DO step 3) still routes to nx_answer's composed path,
# as distinct from the document-DISCOVERY shape a1 targets.
_SYNTHESIS_PHRASINGS = [
    "compare the approaches to caching across these papers",
    "what are the tradeoffs of eventual consistency in the corpus",
    "synthesize how the retry strategy evolved across the RDRs",
    "what themes emerge across the papers on distributed consensus",
    "reconcile the conflicting recommendations in these documents",
    "summarize the evolution of the plan-match algorithm across the RDRs",
    # Adversarial: deliberately mix a coverage-shaped NOUN (corpus/
    # collection) with a synthesis-shaped VERB (compare/summarize/favour)
    # -- the exact stress case the discriminator must resolve on verb
    # shape, not noun co-occurrence (Sam's directive, 2026-08-19).
    "compare what the corpus says about caching and consistency",
    "summarize the corpus's position on eventual consistency",
    "why does the collection favour optimistic concurrency",
]


class TestDocumentDiscoverySeedPlansDoNotCatchSynthesisQuestions:
    """Negative guard (nexus-h33x8.6 critic, T2 substantive-critique-
    h33x8.6-a3-a1-2026-08-19): a synthesis-shaped question ("compare X
    across these papers", "what are the tradeoffs of Y") must NOT
    false-reroute onto the new single-query seeds. A synthesis question
    genuinely needs composition (search + rank/compare/generate) --
    rerouting it onto a bare ``query()`` would silently DROP the
    requested synthesis and hand back raw chunks instead, which is worse
    than the pre-a1 status quo (no fast path at all) because it looks
    like an answer rather than failing loud.

    Same offline/deterministic setup as the positive tests above (real
    cosine match, bundled ONNX MiniLM, the full real 17-plan builtin
    pool) -- this is the critic's own probe (6 synthesis phrasings, 0/6
    reroutes) made permanent.

    FALSIFIABILITY, stated so a future edit can tell whether it broke
    this guard on purpose or by accident: this test is NOT vacuous
    today (these 6 phrasings score below the 0.40 gate against both new
    seeds, or land on a differently-classified plan). It WOULD go RED
    if either ``document-discovery.yml`` or ``corpus-coverage-check.yml``
    had its ``description`` broadened toward generic synthesis language
    -- e.g. dropping the "not a synthesized answer" framing in the YAML
    comment header, or adding phrase-bag entries like "compare X",
    "synthesize", "across these", "tradeoffs of" alongside the existing
    discovery phrasings. That is the intended tripwire: a seed
    description drifting toward synthesis vocabulary is exactly the
    failure mode this guard exists to catch before it reaches
    production.
    """

    @pytest.mark.parametrize("question", _SYNTHESIS_PHRASINGS)
    def test_synthesis_phrasing_does_not_reroute_to_document_discovery(
        self, question: str, seeded_library, real_plan_cache,
    ) -> None:
        from nexus.mcp.core import _nx_answer_classify_plan
        from nexus.plans.matcher import plan_match

        matches = plan_match(
            intent=question,
            library=seeded_library,
            cache=real_plan_cache,
            dimensions={"verb": "research"},
            min_confidence=0.40,
            n=5,
        )

        for m in matches:
            row = seeded_library.get_plan(m.plan_id)
            if row is None or row["name"] not in _TARGET_PLAN_NAMES:
                continue
            # The seed matched above the gate. That alone is not
            # necessarily a foul -- only a foul if it would actually fire
            # the single_query fast-path reroute in nx_answer, silently
            # dropping the requested synthesis.
            plan_class = _nx_answer_classify_plan(m)
            assert plan_class != "single_query", (
                f"synthesis-shaped question {question!r} matched seed "
                f"{row['name']!r} at confidence {m.confidence} AND "
                f"classified as single_query -- this would silently "
                f"reroute to a bare query() call, dropping the requested "
                f"synthesis"
            )
