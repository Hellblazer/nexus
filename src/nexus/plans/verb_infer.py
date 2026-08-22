# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Derive a plan-library verb from a natural-language question.

Design of record: T2 ``nexus/design-dimension-routed-category-plans-2026-08-21``.

WHY THIS EXISTS. ``plan_match``'s category route selects builtin
templates by verb, but nx_answer's callers almost never pin one — the
``dimensions`` parameter is optional and in practice omitted. Without a
derived verb the route would only ever help the five scenario skills that
already pass a verb, i.e. it would fix a surface nx_answer does not use.

WHY IT IS LEXICAL AND NOT A MODEL CALL. This runs on the hot path ahead
of every plan match, including the ones that go on to hit the cache in
milliseconds. A model call here would cost more than the plan reuse it is
trying to enable. The classifier is allowed to be wrong: a wrong verb
selects a small wrong pool whose members all sit below the cosine floor
anyway, and the caller falls through to the inline planner exactly as it
does today. Returning ``None`` is always safe and is the default whenever
the question does not clearly present as one of the known shapes.

WHAT THE OUTPUT IS FOR. The returned verb is matched against builtin
plans with ``_verbs_compatible``, which treats {query, research, lookup}
as one class and {analyze, review, compare} as another. So this function
does not need to distinguish ``research`` from ``lookup`` — that
distinction is made afterwards, by relative cosine inside the selected
pool. It only needs to pick the right CLASS: retrieval, critique, debug,
document, or plan-lifecycle.

It deliberately does NOT read ``conexus/plans/dimensions.yml``. That
file's verb enumeration is prose, is already stale (it omits ``query``
and ``lookup``, both of which three shipped templates use), and the
schema validator never checks dimension VALUES against it.
"""
from __future__ import annotations

import re

__all__ = ["infer_verb"]


#: Ordered most-specific first: the first class whose pattern matches
#: wins. Order is the whole design — "review why the parser fails" is a
#: debug question that happens to contain "review", and a question about
#: documentation is a document question even when phrased as "what is".
#:
#: Patterns match anywhere in the casefolded question unless anchored.
#: They are kept deliberately narrow: a miss costs one inline-planner
#: call, which is today's behaviour, while a false positive routes to a
#: pool that cannot answer.
_VERB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Plan lifecycle. Anchored on the plan-library nouns so an ordinary
    # question that merely contains "plan" does not land here — that
    # over-capture is exactly why the meta plans outrank real ones on
    # the cosine path today.
    ("plan-author", re.compile(
        r"\b(author|write|draft|create)\s+(a\s+)?(new\s+)?plan\b"
        r"|\bplan\s+template\b"
    )),
    ("plan-promote", re.compile(r"\bpromot\w*\b.{0,20}\bplan\b|\bplan\b.{0,20}\bpromot")),
    ("plan-inspect", re.compile(
        r"\bplan\s+(librar|metric|dimension|match\s+histor)"
        r"|\b(inspect|show)\s+(the\s+)?plan\b"
    )),
    # Failure investigation. The strongest signal in the set, and the
    # reason it precedes critique and retrieval: these questions are
    # usually phrased as "why does X ..." which would otherwise read as
    # research.
    ("debug", re.compile(
        r"\bdebug\b|\btraceback\b|\bstack\s?trace\b|\bregression\b"
        r"|\b(fail|failing|failure|crash|crashes|crashing|broken|breaks)\b"
        r"|\b(error|exception)s?\b"
        r"|\bwhy\s+(does|is|are|did|do|isn't|doesn't|won't|can't)\b"
    )),
    # Documentation authoring / gap-finding. Must precede retrieval:
    # "what documentation exists for X" is a document question, and the
    # retrieval class would otherwise swallow it on "what".
    #
    # The line drawn here is between documentation-the-artifact and
    # documents-the-stored-items, and it is worth stating because the two
    # read alike: "documentation", "docs", "doc" and "docstring" all mean
    # the artifact, so they route to the document verb; bare "document" /
    # "documents" is the noun for a thing in the corpus, so it stays in
    # the retrieval class where document-discovery lives. Getting this
    # backwards would send "which documents discuss X" to document-default
    # and make the single-query fast path unreachable all over again.
    ("document", re.compile(
        r"\bdocumentation\b|\bdocument(ed|ing)\b|\bdocstrings?\b|\bdocs?\b"
    )),
    # Critique / synthesis. Covers the {analyze, review, compare} class.
    ("analyze", re.compile(
        r"\banalyz\w+\b|\banalys\w+\b|\bcritique\b|\baudit\b"
        r"|\breview\b|\bcompare\b|\bcomparison\b|\btrade-?offs?\b"
        r"|\bversus\b|\s+vs\.?\s+"
        r"|\bpros\s+and\s+cons\b"
    )),
    # Retrieval. Last, and the broadest — it is the class the other four
    # are carved out of.
    ("research", re.compile(
        r"\bresearch\b|\bexplain\b|\barchitecture\b|\bdesign\s+of\b"
        r"|^\s*(what|which|who|where|how|when)\b"
        r"|\b(find|list|show|search\s+for|look\s+up)\b"
        r"|\bwhat\s+(is|are|does|do)\b"
        r"|\bhow\s+(does|do|is|are)\b"
    )),
)


def infer_verb(question: str) -> str | None:
    """Return the plan verb *question* presents as, or ``None``.

    ``None`` means "no confident reading" and disables the category
    route for this call, leaving today's behaviour untouched. That is
    the correct answer for anything that is not recognisably one of the
    known shapes — a bare topic, a fragment, an empty string.
    """
    if not question or not question.strip():
        return None
    text = " ".join(question.casefold().split())
    for verb, pattern in _VERB_PATTERNS:
        if pattern.search(text):
            return verb
    return None
