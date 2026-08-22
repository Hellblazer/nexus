# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Derive TYPED plan bindings from a natural-language question.

Sibling to :mod:`nexus.plans.verb_infer`, and the same argument one level
down. Verb derivation let a category-level plan be SELECTED; this lets a
plan that needs a typed value be OFFERED at all.

WHY IT EXISTS. ``nx_answer`` can bind only ``intent``, so any plan
requiring a typed value — ``content_type``, ``author`` — is unofferable
to it, permanently. Two shipped templates are in exactly that state and
both were written for question shapes that CARRY the value they need:
``find-by-author`` for "papers by Grossberg about ART resonance", and
``type-scoped-search`` for "which RDRs mention chunk identity". A
template that can never be offered for the question it was written for is
not a safe default, it is an unreachable feature.

WHY IT IS CONSERVATIVE TO THE POINT OF STUBBORN. Getting a typed filter
WRONG is worse than not deriving one. ``nexus.plans.schema`` records the
measurement: builtin plan 14 returned zero results with a bad
``content_type`` while the identical query without it returned the
correct paper as its top hit. An unfilled binding leaves a plan
unofferable and the caller falls through to the inline planner — today's
behaviour, and fine. A WRONG binding produces a confident empty answer.
So every rule here fires only on an explicit, unambiguous mention, and
anything else derives nothing.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["infer_typed_bindings"]


#: Content types worth deriving: each is a word a question uses plainly
#: about the thing it wants. Deliberately NOT the full catalog domain —
#: the live catalog also carries prose, blog_post and others, and a type
#: nobody names in a question cannot be derived from one anyway.
_CONTENT_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rdr", re.compile(r"\brdrs?\b")),
    ("paper", re.compile(r"\bpapers?\b")),
    ("code", re.compile(r"\bcode\b|\bsource\s+file")),
    ("knowledge", re.compile(r"\bknowledge\s+(notes?|entry|entries|base)\b")),
)

#: "by Grossberg", "authored by Van Renesse", "written by K. Birman".
#: Capitalisation is required and is what separates an author from "by
#: default" or "by the way"; the question is matched in its ORIGINAL case
#: for exactly that reason.
_AUTHOR_RE = re.compile(
    r"\b(?:authored\s+by|written\s+by|by)\s+"
    r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2})"
)

#: Words that follow "by" in ordinary prose and are not people.
_AUTHOR_STOPWORDS: frozenset[str] = frozenset({
    "The", "This", "That", "A", "An", "It", "Its", "I", "We", "You",
    "Default", "Design", "Contrast", "Comparison", "Hand", "Now", "Then",
    "Way", "Far", "Definition", "Convention", "Construction",
})


def _infer_content_type(text: str) -> str | None:
    """The content type the question names, or ``None``.

    A question naming two types ("papers and RDRs") derives nothing: the
    filter takes one value, and picking either would silently discard
    half of what was asked for.
    """
    hits = [name for name, pat in _CONTENT_TYPE_PATTERNS if pat.search(text)]
    return hits[0] if len(hits) == 1 else None


def _infer_author(question: str) -> str | None:
    """The author the question names, or ``None``.

    Matched against the ORIGINAL casing — capitalisation is the whole
    signal separating "by Grossberg" from "by default".
    """
    match = _AUTHOR_RE.search(question)
    if match is None:
        return None
    name = match.group(1).strip()
    if name.split()[0] in _AUTHOR_STOPWORDS:
        return None
    return name


def infer_typed_bindings(question: str) -> dict[str, Any]:
    """Return the typed bindings *question* unambiguously supplies.

    An empty dict means "derived nothing", which leaves any plan needing
    those bindings unofferable — exactly today's behaviour, and always
    the safe answer.
    """
    if not question or not question.strip():
        return {}
    lowered = " ".join(question.casefold().split())
    out: dict[str, Any] = {}
    content_type = _infer_content_type(lowered)
    if content_type is not None:
        out["content_type"] = content_type
    author = _infer_author(question)
    if author is not None:
        out["author"] = author
    return out
