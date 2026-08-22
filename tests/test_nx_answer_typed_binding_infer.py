# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed bindings derived from the question (nexus-7y4v0 follow-through).

nx_answer binds only `intent`, so any plan requiring a typed value was
unofferable to it forever — including two templates written for question
shapes that carry the value they need: find-by-author for "papers by
Grossberg about ART resonance", type-scoped-search for "which RDRs
mention X". A template unreachable for the question it exists to serve is
an unreachable feature.

The asymmetry that shapes every rule here: an UNDERIVED binding leaves the
plan unofferable and the caller falls through to the inline planner —
today's behaviour, and fine. A WRONG one produces a confident empty
answer. schema.py records the measurement (plan 14, zero results on a bad
content_type, while the same query without it returned the right paper
first). So the tests below spend most of their weight on what must NOT be
derived.
"""
from __future__ import annotations

import pytest

from nexus.mcp.core import _nx_answer_caller_bindings
from nexus.plans.binding_infer import infer_typed_bindings


class TestContentType:
    @pytest.mark.parametrize(("question", "expected"), [
        ("Which RDRs mention chunk identity?", "rdr"),
        ("Find papers about membership churn", "paper"),
        ("What does the code do about retries?", "code"),
        ("Search the knowledge notes for tumblers", "knowledge"),
    ])
    def test_named_type_is_derived(self, question, expected):
        assert infer_typed_bindings(question)["content_type"] == expected

    @pytest.mark.parametrize("question", [
        # Two types named: the filter takes ONE value, so picking either
        # would silently discard half of what was asked for.
        "Compare the papers and the RDRs on churn",
        "Which RDRs and papers discuss tumblers?",
        # No type named at all.
        "How does the catalog resolve a tumbler?",
        "Explain the plan matcher",
        "",
    ])
    def test_ambiguous_or_absent_derives_nothing(self, question):
        assert "content_type" not in infer_typed_bindings(question)


class TestAuthor:
    @pytest.mark.parametrize(("question", "expected"), [
        ("Find papers by Grossberg about ART resonance", "Grossberg"),
        ("What was written by Van Renesse on gossip?", "Van Renesse"),
        ("Show work authored by K. Birman", "K. Birman"),
    ])
    def test_named_author_is_derived(self, question, expected):
        assert infer_typed_bindings(question)["author"] == expected

    @pytest.mark.parametrize("question", [
        # Lowercase "by" phrases.
        "Sort the results by default",
        "Explain it by contrast with the old design",
        "Fix it by hand",
        "How is the catalog searched by the matcher?",
        "What happens by the way?",
        # CAPITALISED nouns after "by" — the class that broke the first
        # version of this rule. It accepted any capitalised word after
        # "by", defended by a blocklist of prose words, and review found
        # all four of these immediately. A blocklist can only exclude the
        # prose someone thought of, so the rule now requires a positive
        # authorship context instead.
        "results sorted by Relevance",
        "search by Author",
        "which papers are grouped by Category",
        "list RDRs blocked by Dependency",
        "entries ordered by Score",
        "rows filtered by Type",
        # Authorship-adjacent but NOT authorship: a citation relation.
        "Which papers cited by Lamport matter?",
    ])
    def test_prose_by_is_not_an_author(self, question):
        assert "author" not in infer_typed_bindings(question), question

    @pytest.mark.parametrize(("question", "expected"), [
        # A bibliographic noun immediately before "by" is the positive
        # signal that makes bare "by" safe.
        ("papers by Grossberg", "Grossberg"),
        ("the RDR by Hildebrand", "Hildebrand"),
        ("documents by Van Renesse", "Van Renesse"),
    ])
    def test_bibliographic_noun_before_by_is_authorship(self, question, expected):
        assert infer_typed_bindings(question)["author"] == expected


class TestWiredIntoNxAnswer:
    def test_derived_bindings_are_both_bound_and_declared(self):
        """run and available are derived together so they cannot disagree —
        a binding declared available but not bound would let the matcher
        offer a plan that then fails at dispatch."""
        run, available = _nx_answer_caller_bindings(
            question="Find papers by Grossberg about ART resonance",
            scope="", bindings=None,
        )
        assert run["author"] == "Grossberg"
        assert run["content_type"] == "paper"
        assert {"author", "content_type", "intent"} <= available
        assert set(run) == set(available)

    def test_an_explicit_caller_value_always_wins(self):
        run, _ = _nx_answer_caller_bindings(
            question="Find papers by Grossberg",
            scope="", bindings={"author": "Explicitly Supplied"},
        )
        assert run["author"] == "Explicitly Supplied"

    def test_nothing_derived_leaves_todays_behaviour(self):
        run, available = _nx_answer_caller_bindings(
            question="How does the catalog work?", scope="", bindings=None,
        )
        assert set(run) == {"intent"}
        assert available == frozenset({"intent"})


def test_find_by_author_becomes_offerable_for_its_own_question_shape():
    """The end-to-end point. This template existed for exactly this
    question and could never be offered for it."""
    import json
    import pathlib

    import yaml

    from nexus.plans.schema import unsatisfiable_typed_binding

    template = yaml.safe_load(
        (pathlib.Path(__file__).parent.parent
         / "conexus/plans/builtin/find-by-author.yml").read_text()
    )
    plan_json = dict(template["plan_json"])
    plan_json["required_bindings"] = list(template.get("required_bindings") or [])

    _, available = _nx_answer_caller_bindings(
        question="Find papers by Grossberg about ART resonance",
        scope="", bindings=None,
    )
    assert unsatisfiable_typed_binding(
        required=plan_json["required_bindings"],
        defaults=template.get("default_bindings"),
        available=available,
        plan_json=json.dumps(plan_json),
    ) is None
