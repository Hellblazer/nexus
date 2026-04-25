# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-088 Spike B — ambiguous-band queries for LLM rerank precision probe.

Bead ``nexus-ac40.8``. This module publishes 20 synthetic queries crafted
to land in the **ambiguous cosine band (0.40-0.65)** of the ``plan_match``
MiniLM similarity distribution against the live 50-row plan library
snapshot at ``scripts/spikes/plan_library_snapshot.json`` (2026-04-24).

The spike runner will:

1. Feed each ``intent`` through the matcher and record top-k raw cosine
   scores.
2. Keep only queries that actually land in the (0.50, 0.65] rerank band
   (per RDR-092 Phase 2: verb skills can pin ``min_confidence=0.50``).
3. Score an LLM rerank pass against the ``expected_plan_id`` ground
   truth, reporting precision-at-1 and recall changes versus the
   MiniLM-only baseline.

Category split (RDR-088 design, matches Spike B spec):

- ``true-positive-ambiguous`` (10): the correct plan *is* in the library
  but the query is paraphrased / indirect enough that cosine sits in the
  ambiguous band rather than above the confident-match threshold.
- ``hard-negative`` (6): no correct match exists, but a structurally
  similar attractor plan would plausibly pull cosine into the band.
- ``genuinely-ambiguous`` (4): a human could reasonably argue either
  way. Ground truth is None (matcher should decline / fall through).

Every ``expected_plan_id`` that is not None corresponds to a plan ``id``
present in ``plan_library_snapshot.json``.

No execution side effects. Import ``QUERIES`` and iterate.
"""
from __future__ import annotations

QUERIES: list[dict] = [
    # -------------------------------------------------------------------------
    # True-positive ambiguous (10): correct plan exists, paraphrased intent.
    # -------------------------------------------------------------------------
    {
        "id": "qb-01",
        "intent": "walk the references of a paper both inward and outward to see what surrounds it",
        "expected_plan_id": 54,
        "expected_plan_name": "citation-traversal",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Paraphrases plan 54 (citation-traversal) without using the "
            "words 'citation' or 'chain'; cosine should soften into the "
            "ambiguous band rather than hit a clean lexical overlap."
        ),
    },
    {
        "id": "qb-02",
        "intent": "look up everything written by a particular researcher",
        "expected_plan_id": 57,
        "expected_plan_name": "find-by-author",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Hits plan 57 (find-by-author) via 'researcher' instead of "
            "'author'; competing personal research plans dilute the cosine."
        ),
    },
    {
        "id": "qb-03",
        "intent": "restrict a semantic query to only the papers bucket",
        "expected_plan_id": 64,
        "expected_plan_name": "type-scoped-search",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 64 is type-scoped-search; the query uses 'bucket' "
            "informally and omits 'content-type', so the match_text overlap "
            "is partial — likely 0.45-0.60."
        ),
    },
    {
        "id": "qb-04",
        "intent": "draft a brand-new reusable plan template for some verb",
        "expected_plan_id": 58,
        "expected_plan_name": "default (plan-author)",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 58 is the plan-author default; 'draft a brand-new "
            "reusable plan template' paraphrases without matching the "
            "'from scratch' / 'plan-authoring guide' phrasing."
        ),
    },
    {
        "id": "qb-05",
        "intent": "look up usage metrics and recent match traces for one plan",
        "expected_plan_id": 59,
        "expected_plan_name": "default (plan-inspect)",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 59 covers single-plan runtime inspection; competes with "
            "plan 60 (dimensions enumeration) and plan 61 (promote propose) "
            "which share the 'inspect the library' surface."
        ),
    },
    {
        "id": "qb-06",
        "intent": "rank plans that could be lifted to a broader scope",
        "expected_plan_id": 61,
        "expected_plan_name": "propose (plan-promote)",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 61 promote-propose; 'lifted to a broader scope' avoids "
            "the 'promotion candidates' / 'usage patterns' phrasing, "
            "lowering cosine into the band."
        ),
    },
    {
        "id": "qb-07",
        "intent": "investigate a regression where a unit test suddenly fails",
        "expected_plan_id": 26,
        "expected_plan_name": "debug-failing-tests-broken-behavior",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 26 is the debug-failing-tests plan. 'Regression' and "
            "'unit test' are synonyms not in the match_text; competes "
            "with plan 55 (global debug default) which shares the space."
        ),
    },
    {
        "id": "qb-08",
        "intent": "gather information about a subject and put the notes somewhere permanent",
        "expected_plan_id": 24,
        "expected_plan_name": "research-topic-store-findings",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 24 is 'research topic and store findings'. The intent "
            "is a paraphrase that also partially collides with plan 28 "
            "(research-synthesize-knowledge), placing it in the band."
        ),
    },
    {
        "id": "qb-09",
        "intent": "inspect a pull request and make sure its tests actually cover the change",
        "expected_plan_id": 29,
        "expected_plan_name": "code-review-validate-tests-review",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 29 combines code-review + validate-tests. Competitors: "
            "plan 25 (review-validate-implemented-code) and plan 63 "
            "(review default). Pull-request vocabulary is absent from "
            "all three match_texts, damping cosine into the band."
        ),
    },
    {
        "id": "qb-10",
        "intent": "explain what the nx_answer pipeline does and how its runs get logged",
        "expected_plan_id": 41,
        "expected_plan_name": "explain-nx_answer-trunk-records-runs",
        "category": "true-positive-ambiguous",
        "rationale": (
            "Plan 41 is the exact match; however plan 44 "
            "(explain-nx_answer-trunk-plan_match-plan_run) is a near-twin "
            "that will likely tie for top-1 — a canonical ambiguous-band "
            "rerank decision."
        ),
    },
    # -------------------------------------------------------------------------
    # Hard negatives (6): no correct plan, attractor lives in the band.
    # -------------------------------------------------------------------------
    {
        "id": "qb-11",
        "intent": "what papers and circuits cover the Auditory module in Grossberg's ChatSOME architecture",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "No 'Auditory module' plan exists; plans 3-8 cover Vision / "
            "Language / Emotion / Motor / Temporal / Spatial. Cosine will "
            "rank one of those near-misses highly — a textbook attractor."
        ),
    },
    {
        "id": "qb-12",
        "intent": "explain how the RDR-049 catalog handles permission checks on read",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "Plan 17 (rdr-049-git-backed-xanadu) is the attractor — the "
            "RDR-049 phrase dominates cosine — but permission-checks is "
            "not part of that implementation plan's surface, so the "
            "correct answer is 'no plan matches'."
        ),
    },
    {
        "id": "qb-13",
        "intent": "RDR-090 says what about matcher threshold calibration",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "Plan 45 (rdr-080-say-boundary-between-45) and plan 38 "
            "(plan-match-first-retrieval-work in RDR-078) are twin "
            "attractors, but no plan covers RDR-090; an LLM rerank "
            "should recognise the RDR number mismatch."
        ),
    },
    {
        "id": "qb-14",
        "intent": "what did Deutsch's 2001 paper say about incremental maintenance of views",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "Plan 65 (compare-fagin-s-chase-procedure) name-drops "
            "Deutsch 2001 and will attract cosine, but the query is "
            "about a different topic (incremental view maintenance) — "
            "rerank should decline."
        ),
    },
    {
        "id": "qb-15",
        "intent": "the Delos papers' take on log-structured storage latency",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "Plans 49 and 50 both concern Delos-paper membership churn; "
            "cosine will pull one of them into the band even though "
            "log-structured-storage latency is an unrelated slice."
        ),
    },
    {
        "id": "qb-16",
        "intent": "summarise what the CMRB chapter on colour perception says about opponent processing",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "hard-negative",
        "rationale": (
            "Plan 10 (cmrb-chapter-index-each-chapter) is an index-style "
            "plan, not a chapter-content query. The CMRB/chapter overlap "
            "will raise cosine; the intent asks for chapter content."
        ),
    },
    # -------------------------------------------------------------------------
    # Genuinely ambiguous (4): reasonable either way.
    # -------------------------------------------------------------------------
    {
        "id": "qb-17",
        "intent": "how does plan matching decide when to fall back to an inline planner",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "genuinely-ambiguous",
        "rationale": (
            "Plan 47 (nx_answer-plan-match-gate-decide) is nearly this "
            "question verbatim, but plan 38 (plan-match-first-retrieval) "
            "is also defensible. Whether cosine picks the right one is "
            "a coin-flip — classic rerank stress-test."
        ),
    },
    {
        "id": "qb-18",
        "intent": "review the lifecycle of taking an RDR from idea to merged code",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "genuinely-ambiguous",
        "rationale": (
            "Plans 22 (research-design-review-research-gate) and 27 "
            "(rdr-chain-research-design-review) both paraphrase this. "
            "Either could be 'right'; human judgement splits."
        ),
    },
    {
        "id": "qb-19",
        "intent": "what is ChatSOME's overall resonance architecture for cognition",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "genuinely-ambiguous",
        "rationale": (
            "Plan 15 (resonance-feedback-loops-grossberg-s) and plan 9 "
            "(core-art-computational-primitives) both plausibly match; "
            "whether 'overall architecture' ranks primitives or "
            "feedback-loops higher is a judgement call."
        ),
    },
    {
        "id": "qb-20",
        "intent": "audit an implementation against the decisions that led to it",
        "expected_plan_id": None,
        "expected_plan_name": None,
        "category": "genuinely-ambiguous",
        "rationale": (
            "Plans 63 (review default — decision-evolution) and 23 "
            "(plan-then-audit-then-implement) are both reasonable. "
            "Rerank behaviour on this is genuinely a design choice, "
            "not a clean answer."
        ),
    },
]
