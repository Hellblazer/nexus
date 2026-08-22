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

#: Authorship phrasing. Two positive forms only:
#:
#:   * an explicit authorship verb — "authored by X", "written by X";
#:   * a bibliographic noun immediately before the "by" — "papers by X",
#:     "work by X", "the RDR by X".
#:
#: Bare "by X" is NOT enough, and the first version of this rule learned
#: that the expensive way. It accepted any Capitalised word after "by",
#: defended by a blocklist of prose words — which is unbounded by
#: construction, so review found four false positives immediately:
#: "results sorted by Relevance", "search by Author", "papers grouped by
#: Category", "RDRs blocked by Dependency". Every one would have put a
#: nonsense name into an author filter and returned an empty answer that
#: reads like a real one. A blocklist can only exclude the prose someone
#: thought of; an allowlist of authorship CONTEXTS excludes everything
#: nobody vouched for, which is the right default when being wrong is
#: worse than being silent.
_BIBLIOGRAPHIC_NOUNS = (
    r"papers?|articles?|publications?|books?|works?|writings?|notes?"
    r"|documents?|docs?|rdrs?|preprints?|theses|thesis"
)
_AUTHOR_RE = re.compile(
    r"\b(?:"
    r"(?:authored|written|published|co-authored)\s+by"
    # Case-insensitive on the NOUN only ("the RDR by X" is the same
    # question as "the rdr by X"), while the NAME stays case-sensitive.
    # No filler word is allowed between noun and "by": "papers cited by
    # Lamport" is a citation relation, not authorship, and "papers
    # written by X" is already covered by the verb branch above.
    rf"|(?i:{_BIBLIOGRAPHIC_NOUNS})\s+by"
    r")\s+"
    r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2})"
)

#: Capitalised words that follow an authorship context but are not names.
#: A short backstop, NOT the primary defence — the allowlist above is.
_AUTHOR_STOPWORDS: frozenset[str] = frozenset({
    "The", "This", "That", "A", "An", "It", "Its", "I", "We", "You",
    "Anyone", "Someone", "Whom", "Who", "Hand", "Default",
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

    Matched against the ORIGINAL casing: capitalisation is necessary
    (a lowercase word after "by" is never a name) but, as
    :data:`_AUTHOR_RE` records, nowhere near sufficient on its own.
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
