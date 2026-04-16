# SPDX-License-Identifier: AGPL-3.0-or-later
"""Labeled paraphrase intents for RDR-079 P5 min_confidence calibration.

Each entry is (intent, expected_plan_name). ``expected_plan_name`` refers
to the seed file's identity (dimensions.verb + dimensions.strategy). The
calibration harness looks up the plan_id by loading the builtin plans
and matching on ``(verb, scope, strategy)``.

Coverage: 8 intents per verb for the 5 scenario verbs (analyze, debug,
document, research, review) = 40 positive examples. Plus 2 intents
per meta-verb (plan-author, plan-inspect-default, plan-inspect-dimensions,
plan-promote-propose) = 8 positive examples. Total positive: 48.

Plus 6 "adversarial" intents that don't map to any plan in the library
— they should score below min_confidence for all 9 plans.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Paraphrase:
    intent: str
    expected_verb: str
    expected_strategy: str

    @property
    def is_positive(self) -> bool:
        return self.expected_verb != ""


# ── Scenario-verb positives (8 each = 40) ──────────────────────────────────

_RESEARCH = [
    "how does the retrieval layer work end-to-end",
    "I want to understand the architecture of the plan runner",
    "design notes for cross-embedding enforcement",
    "walk me through the catalog link graph from RDR to code",
    "research the pool worker spawn protocol",
    "plan a new feature for auto-linking chunks to RDRs",
    "architectural overview of the three-tier storage model",
    "explain the SessionStart hook chain from the RDR perspective",
]

_REVIEW = [
    "audit the recent changes against the original design",
    "critique this PR for drift from the RDR",
    "review change set against decisions we already locked in",
    "did the implementation keep faith with the RDR?",
    "check the diff for decision evolution conflicts",
    "review PR #167 for RDR alignment",
    "audit the changed files for scope creep",
    "compare the change set against prior RDRs",
]

_ANALYZE = [
    "analyze how embedding model selection propagates across the system",
    "synthesize what we know about the taxonomy projector",
    "compare approaches for cross-collection semantic routing",
    "give me a ranked reading list on contextual chunking",
    "analysis of pool survival under worker retirement",
    "synthesis of three competing ideas for auto-linker triggers",
    "rank evidence for and against BERTopic in our pipeline",
    "analyse the tradeoffs of FastMCP vs raw stdio",
]

_DEBUG = [
    "why is the integration test failing on macOS",
    "debug the ChromaDB quota exceeded error on upsert",
    "track down the flaky test in test_pool_retirement",
    "investigate why auth check fails silently in CI",
    "what's breaking in the RDR indexer when a PDF has no text",
    "why does plan_match return empty on quoted FTS5 queries",
    "debug the session file leak after SIGKILL",
    "investigate the cross-embedding dispatch false-positive",
]

_DOCUMENT = [
    "document the pool worker lifecycle for the architecture doc",
    "write docs for the nx plan promote subcommand",
    "audit existing documentation for the catalog subsystem",
    "flag coverage gaps in the operator tool documentation",
    "write a how-to for indexing PDFs with MinerU",
    "document the SessionStart hook chain for new contributors",
    "write release notes for RDR-079",
    "audit docs vs code for the TAxonomy rebuild command",
]


# ── Meta-verb positives (2 each = 8) ───────────────────────────────────────

_PLAN_AUTHOR = [
    "help me author a new plan template for batch labeling",
    "draft a brand new plan template — my verb is 'triage'",
]

_PLAN_INSPECT_DEFAULT = [
    "inspect the runtime metrics of plan id 42",
    "show me use_count and success rate for the research-default plan",
]

_PLAN_INSPECT_DIMENSIONS = [
    "list every registered dimension and how many plans use each",
    "which axes are canonical vs specialization in the current library",
]

_PLAN_PROMOTE_PROPOSE = [
    "which plans look promotable based on metrics so far",
    "survey the library and rank promotion candidates",
]


# ── Adversarial negatives (6) — should NOT match any seed above threshold ─

_NEGATIVES = [
    "what's the weather in tokyo",
    "book a flight to san francisco for next tuesday",
    "convert 37 celsius to fahrenheit",
    "who won the 1994 world cup",
    "compose a limerick about a cat",
    "what is 2 plus 2",
]


# ── Assembled dataset ──────────────────────────────────────────────────────


def paraphrase_dataset() -> list[Paraphrase]:
    out: list[Paraphrase] = []
    for intent in _RESEARCH:
        out.append(Paraphrase(intent, "research", "default"))
    for intent in _REVIEW:
        out.append(Paraphrase(intent, "review", "default"))
    for intent in _ANALYZE:
        out.append(Paraphrase(intent, "analyze", "default"))
    for intent in _DEBUG:
        out.append(Paraphrase(intent, "debug", "default"))
    for intent in _DOCUMENT:
        out.append(Paraphrase(intent, "document", "default"))
    for intent in _PLAN_AUTHOR:
        out.append(Paraphrase(intent, "plan-author", "default"))
    for intent in _PLAN_INSPECT_DEFAULT:
        out.append(Paraphrase(intent, "plan-inspect", "default"))
    for intent in _PLAN_INSPECT_DIMENSIONS:
        out.append(Paraphrase(intent, "plan-inspect", "dimensions"))
    for intent in _PLAN_PROMOTE_PROPOSE:
        out.append(Paraphrase(intent, "plan-promote", "propose"))
    for intent in _NEGATIVES:
        out.append(Paraphrase(intent, "", ""))
    return out
