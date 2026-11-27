# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Scope-tag helpers for RDR-091 plan scope-aware matching.

Lives in :mod:`nexus.plans` rather than :mod:`nexus.db.t2.plan_library`
to break a ``migrations -> plan_library -> migrations`` circular import
path (code-review finding C-1, RDR-091 follow-up). The helpers have no
sqlite dependency; their natural home is the ``plans`` domain.

``_normalize_scope_string`` is the canonical scope-tag normalizer shared
between the save path (``plan_library.save_plan``), the inference helper
(``_infer_scope_tags``), and the match-time re-ranker
(``nexus.plans.matcher._scope_fit``). Changes here touch all three.
"""
from __future__ import annotations

import json
import re

from nexus.label_quote import HASH_SUFFIX_RE, WILDCARD_WORDS, label_needs_quote, unquote_label

__all__ = [
    "_HASH_SUFFIX_RE",
    "_RETRIEVAL_SCOPE_ARGS",
    "_SCOPE_AGNOSTIC_SENTINELS",
    "_infer_scope_tags",
    "_normalize_scope_string",
    "normalize_scope_tags",
]

_HASH_SUFFIX_RE: re.Pattern[str] = HASH_SUFFIX_RE

# Comma-separated ``scope_tags`` entries; also the keys ``_infer_scope_tags``
# looks for in retrieval-step args.
_RETRIEVAL_SCOPE_ARGS: tuple[str, ...] = ("corpus", "collection")

# ``"all"`` is a sentinel the search/query MCP tools document as "search
# every collection". It is scope-agnostic, not a concrete corpus name,
# so it must never contribute to ``scope_tags`` — otherwise the seven
# builtin plans that use ``corpus: all`` would be scope-filtered out of
# the candidate pool whenever ``scope_preference`` is set (RDR-091
# critic follow-up, nexus-dfok).
_SCOPE_AGNOSTIC_SENTINELS: frozenset[str] = WILDCARD_WORDS


def _normalize_scope_string(scope: str) -> str:
    """Return *scope* in canonical scope-tag form.

    Rules (RDR-091 §Proposed Solution → Normalization):
      * Strip a trailing 8-char hex suffix in either case
        (``-deadbeef`` or ``-5AF9BFE0``). Real collection names
        (``code__Delos-5AF9BFE0``) use mixed case and must normalize
        to the same family as their lowercase siblings.
      * Strip a trailing ``*`` or ``-*`` glob.
      * Preserve a bare family prefix (``rdr__``) and tumbler addresses
        (``1.16``) unchanged.
      * Stored case is preserved; only the hex-suffix and glob tails
        are removed. Case folding for comparison happens at match time
        in :func:`nexus.plans.matcher._scope_fit`.
      * A quoted part (``"all"``, ``"foo*"``, ``"$x"``) names the label inside
        exactly (nexus-o5vbo, :mod:`nexus.label_quote`): its glob, hash suffix,
        ``all`` and ``$`` are the label's own, so none is stripped. The quotes stay
        when a reader would misread the bare label (the canonical form, so the
        stored tag still names the label and never the sentinel) and go when none
        would (``"code__foo"`` is ``code__foo``).
      * Empty input is returned unchanged.
    """
    if not scope:
        return scope
    label, quoted = unquote_label(scope)
    if quoted:
        return scope if label_needs_quote(label) else label
    if scope.endswith("-*"):
        return scope[:-2]
    if scope.endswith("*"):
        return scope[:-1]
    return _HASH_SUFFIX_RE.sub("", scope)


def normalize_scope_tags(scope_tags: str) -> str:
    """Return a caller-supplied scope-tag CSV in canonical stored form.

    Splits on commas, applies :func:`_normalize_scope_string` to each entry,
    drops empties and scope-agnostic sentinels (``"all"``), then joins the
    sorted-unique remainder. Idempotent.

    The sentinel drop is load-bearing: a stored literal ``"all"`` is not read
    as "any scope" by the matcher, it is read as a scope named ``all``, which
    then conflicts with every real tag (RDR-091 code-review finding C-3).

    This logic was written out three times — ``PlanLibrary.save_plan``,
    ``PlanLibrary.set_scope_tags``, and ``HttpPlanLibrary.save_plan`` — and
    the fourth site that needed it, ``HttpPlanLibrary.set_scope_tags``, simply
    did not get a copy, so service-mode ``nx answer-plan set-scope`` stored raw
    values (nexus-aqbrk, found porting the plan-library tests to the engine
    substrate). Hence one function.
    """
    parts = [
        _normalize_scope_string(p.strip())
        for p in scope_tags.split(",")
        if p.strip() and p.strip() not in _SCOPE_AGNOSTIC_SENTINELS
    ]
    return ",".join(sorted({p for p in parts if p}))


def _infer_scope_tags(plan_json: str) -> str:
    """Infer scope_tags from a plan by unioning retrieval-step scope args.

    Walks the ``steps`` list in *plan_json*. For every step, collects any
    ``args["corpus"]`` or ``args["collection"]`` value that is a literal
    string (not a ``$var`` binding placeholder and not the ``"all"``
    wildcard sentinel), split on commas so each part is read alone. Each
    collected part is normalized via
    :func:`_normalize_scope_string`. The result is a comma-separated,
    sorted, deduplicated string — ``""`` when the plan names no literal
    retrieval scope (e.g. traverse-only plans or plans that search
    everything).

    Malformed ``plan_json`` yields ``""`` (fail-soft: scope inference is
    best-effort and must never block a save).
    """
    try:
        plan = json.loads(plan_json)
    except (TypeError, json.JSONDecodeError):
        return ""

    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return ""

    collected: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        args = step.get("args")
        if not isinstance(args, dict):
            continue
        for key in _RETRIEVAL_SCOPE_ARGS:
            value = args.get(key)
            if not isinstance(value, str) or not value:
                continue
            # nexus-o5vbo: a corpus value is a comma list, so each part is read on its own; read whole,
            # '"all","$x"' is one quoted label and is stored as the one corrupt tag all","$x.
            for part in (p.strip() for p in value.split(",")):
                if not part or part.startswith("$") or part in _SCOPE_AGNOSTIC_SENTINELS:
                    continue
                collected.add(_normalize_scope_string(part))

    return ",".join(sorted(tag for tag in collected if tag))
