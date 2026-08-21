# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 .p2a operator quality proxy — tier-agreement vs strong, with
positive AND negative controls (nexus-nyry9.14, the Phase 2 GATE).

196-R4 (T2 ``nexus_rdr/196-research-4``) found no operator-level quality
proxy anywhere in-tree; ``scripts/bench/{runner,metrics,schema,paths}.py``
scores RETRIEVAL (RDR-090), not operator output. This module is the
proxy: run an operator TWICE at the strong tier on a fixed input, score
structured agreement between the two runs (the metric functions live in
``scripts/bench/operator_proxy_metrics.py``), and separately score
agreement between one real run and a synthetically DEGRADED copy of it.
A proxy that can't fail the degraded arm below threshold is not a gate —
see ``THRESHOLDS`` in the metrics module. Thresholds, metrics, and scope
were RECORDED BEFORE MEASUREMENT (before this module existed) in the
FROZEN, write-once T2 title
``nexus_rdr/196-phase2-quality-proxy-REGISTERED-2026-08-21`` — that title
is never upserted again, specifically so its own "Updated" timestamp is
a mechanically-checkable pre-registration proof. Results, revisions, and
variance measurements go to the separate, mutable T2 working entry
``nexus_rdr/196-phase2-quality-proxy`` (round-2 code-review/critic
finding, 2026-08-21: citing the mutable working title as "recorded before
measurement" undermines the mechanical-checkability the frozen title
exists to provide — every such reference in this codebase now cites the
frozen title specifically for the "before measurement" claim).

Scope: 6 of the 10 ``operator_*`` MCP tools have a structured-enough
output shape to score without an LLM judge — filter, groupby, rank,
extract, check, verify. summarize/aggregate/compare/generate are
explicitly OUT (free-text output, see the metrics module docstring).

Prompt/schema builders below are COPIED VERBATIM from the corresponding
``@mcp.tool``-registered functions in ``src/nexus/mcp/core.py`` — the
same convention ``tests/test_operator_dispatch.py::
TestClaudeDispatchPerOperatorSchemaCheapTier`` already established for
RDR-196 .p2b (read-only source copies, not re-derived, so a schema/prompt
drift in core.py is a genuine signal here rather than noise from two
independently-maintained abstractions). A change to an operator's
prompt/schema in core.py is NOT auto-reflected here; re-sync by hand and
note the sync in a commit.

CLI entry (spends real money — 2 live strong-tier dispatches per operator
run; see T2 ``nexus_rdr/196-phase2-quality-proxy`` for the budget
accounting)::

    uv run python scripts/bench/operator_proxy.py --model sonnet
    uv run python scripts/bench/operator_proxy.py --model sonnet --out report.json
"""
from __future__ import annotations

# Same sys.path bootstrap as scripts/bench/runner.py: invokable directly
# or via the test pythonpath (``pythonpath = ["scripts"]`` in
# pyproject.toml), which is why ``bench`` must be importable as a
# top-level package either way.
import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import asyncio
import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from bench.operator_proxy_metrics import (
    THRESHOLDS,
    bool_plus_jaccard,
    citation_tokens,
    exact_membership_jaccard,
    field_value_bag_f1,
    rand_index,
    spearman_rank_correlation,
)

#: Fixed, seeded input set (RDR-196 .p2a DO 5's "cheap set of inputs" +
#: "operator tests' existing fixtures + spike_5q corpus hydrations").
#: Hydrated from ``bench/queries/spike_5q.yaml``'s ground_truth
#: annotations — real, in-tree, deterministic RDR facts, no network call.
FIXTURE_ITEMS: list[dict[str, Any]] = [
    {"id": "rdr-049", "title": "Catalog tumblers as hierarchical document addresses", "category": "catalog", "year": 2026},
    {"id": "rdr-053", "title": "Xanadu fidelity follow-up to tumblers", "category": "catalog", "year": 2026},
    {"id": "rdr-086", "title": "Content-hash chunk index (chash) for stable chunk identity", "category": "storage", "year": 2026},
    {"id": "rdr-061", "title": "Earlier content-hash chunk discussion", "category": "storage", "year": 2026},
    {"id": "rdr-070", "title": "BERTopic-based taxonomy with HDBSCAN clustering", "category": "taxonomy", "year": 2026},
    {"id": "rdr-067", "title": "Adjacent observability and audit work", "category": "observability", "year": 2026},
    {"id": "rdr-095", "title": "Batch post-store hook contract", "category": "hooks", "year": 2026},
    {"id": "rdr-089", "title": "Document-grain hook chain for aspect extraction", "category": "hooks", "year": 2026},
]

_CHECK_EVIDENCE_ITEM_SCHEMA: dict = {
    "type": "object",
    "required": ["item_id", "quote", "role"],
    "properties": {
        "item_id": {"type": "string"},
        "quote": {"type": "string"},
        "role": {"type": "string", "enum": ["supports", "contradicts", "neutral"]},
    },
}


def _items_json() -> str:
    return json.dumps(FIXTURE_ITEMS)


def build_filter() -> tuple[str, dict]:
    """Verbatim from ``operator_filter`` (core.py) with a fixed criterion
    that has a known-nontrivial true answer (2 of 8 items: rdr-095, rdr-089)."""
    criterion = "the item's category is 'hooks'"
    prompt = (
        f"Filter the following items by this criterion: {criterion}\n"
        f"Return only the items that satisfy the criterion in the 'items' "
        f"array. Populate 'rationale' with one entry per input item, "
        f"keyed by the item's id, giving the reason each item was kept "
        f"or rejected. The output 'items' array must be a subset of the "
        f"input; never add synthetic items.\n\n"
        f"Items:\n{_items_json()}"
    )
    schema: dict = {
        "type": "object",
        "required": ["items", "rationale"],
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}},
            "rationale": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "reason"],
                    "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                },
            },
        },
    }
    return prompt, schema


def build_groupby() -> tuple[str, dict]:
    """Verbatim from ``operator_groupby`` (core.py), partitioning by the
    fixture's own ``category`` field."""
    key = "category"
    prompt = (
        f"Partition the following items by this key: {key}\n"
        f"Output a list of groups. Each group has a string `key_value` "
        f"(the partition label, e.g. a year, a fault model, a system "
        f"property) and an `items` array carrying each item's full "
        f"content INLINE — preserve the original `id` field and any "
        f"other fields verbatim. Every input item appears in exactly "
        f"one group's `items`. Items the partition cannot confidently "
        f"assign go in a group with `key_value` of \"unassigned\".\n\n"
        f"Do not reference items by id-only — carry the full item "
        f"dicts in each group's `items` array so downstream operators "
        f"see the content without a separate lookup.\n\n"
        f"Items:\n{_items_json()}"
    )
    schema: dict = {
        "type": "object",
        "required": ["groups"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key_value", "items"],
                    "properties": {
                        "key_value": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
        },
    }
    return prompt, schema


def build_rank() -> tuple[str, dict]:
    """BYTE-IDENTICAL prompt template to ``operator_rank`` (core.py
    :4695-4699) — no added instruction text. Code-review finding
    (nexus-nyry9.14 review round, 2026-08-21): an earlier version of this
    builder appended "Preserve each item's leading '[id]' tag verbatim in
    your output." to steer the model into keeping the id-matching tag
    (see ``operator_proxy_metrics._extract_id_tag``) — a real, undisclosed
    deviation from "verbatim" (core.py never gives that instruction in
    production), and it measured rank under friendlier conditions than
    production ever provides.

    Fixed: the ``[rdr-XXX]`` tag is fixture DATA embedded in each item
    STRING (legitimate — ``items`` is caller-supplied opaque content in
    production too), not a template instruction. The template itself is
    now exactly core.py's three lines. If the model paraphrases far
    enough to drop a tag, ``_extract_id_tag`` returns ``None`` for that
    entry and ``spearman_rank_correlation`` scores over the smaller common
    subsequence (or returns ``None`` if fewer than 2 tags survive) —
    handled honestly, not steered around.
    """
    criterion = "how directly the RDR bears on storage/identity mechanisms (most direct first)"
    items = [f"[{it['id']}] {it['title']}" for it in FIXTURE_ITEMS]
    prompt = (
        f"Rank the following items by {criterion}.\n"
        f"Return them in ranked order, best first.\n\n"
        f"Items:\n{json.dumps(items)}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ranked"],
        "properties": {"ranked": {"type": "array", "items": {"type": "string"}}},
    }
    return prompt, schema


def build_extract() -> tuple[str, dict]:
    """Verbatim from ``operator_extract`` (core.py)."""
    fields = "id,category,year"
    prompt = (
        f"Extract the following fields from each item: {fields}\n\n"
        f"Items:\n{_items_json()}"
    )
    schema: dict = {
        "type": "object",
        "required": ["extractions"],
        "properties": {"extractions": {"type": "array", "items": {"type": "object"}}},
    }
    return prompt, schema


def build_check() -> tuple[str, dict]:
    """Verbatim from ``operator_check`` (core.py). ``ok`` has a known-false
    true answer: not every item is catalog/storage-related (taxonomy,
    observability, hooks items also present)."""
    claim = "every item's category is either 'catalog' or 'storage'"
    prompt = (
        f"Check whether the following items are consistent with this "
        f"claim or question: {claim}\n"
        f"Set ok=true when every item supports the claim, false when at "
        f"least one item contradicts it. Populate 'evidence' with a "
        f"record per item containing a short grounding 'quote' and a "
        f"'role' of 'supports', 'contradicts', or 'neutral'. Keep quotes "
        f"short enough to be verifiable against the source item.\n\n"
        f"Items:\n{_items_json()}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ok", "evidence"],
        "properties": {
            "ok": {"type": "boolean"},
            "evidence": {"type": "array", "items": _CHECK_EVIDENCE_ITEM_SCHEMA},
        },
    }
    return prompt, schema


def build_verify() -> tuple[str, dict]:
    """Verbatim from ``operator_verify`` (core.py). ``verified`` has a
    known-true true answer grounded directly in the evidence text."""
    claim = "RDR-086 added the content-hash chunk index for stable chunk identity"
    evidence = (
        "rdr-086: Content-hash chunk index (chash) for stable chunk "
        "identity. rdr-061: an earlier, less complete content-hash chunk "
        "discussion that predates rdr-086's full index."
    )
    prompt = (
        f"Verify whether the following claim is grounded in the evidence "
        f"provided.\n\n"
        f"Claim: {claim}\n\n"
        f"Evidence:\n{evidence}\n\n"
        f"Set verified=true only when the claim is directly supported by "
        f"the evidence. Provide a concise 'reason' explaining the "
        f"verdict. Populate 'citations' with locators (section, page, "
        f"table, or quoted span snippets) that pinpoint the supporting "
        f"or contradicting passages."
    )
    schema: dict = {
        "type": "object",
        "required": ["verified", "reason", "citations"],
        "properties": {
            "verified": {"type": "boolean"},
            "reason": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return prompt, schema


BUILDERS: dict[str, Any] = {
    "operator_filter": build_filter,
    "operator_groupby": build_groupby,
    "operator_rank": build_rank,
    "operator_extract": build_extract,
    "operator_check": build_check,
    "operator_verify": build_verify,
}


# ── synthetic degradation (negative control; NO extra dispatch) ────────────

def degrade_filter(output: dict) -> dict:
    """Invert membership: return the complement of the kept-item ids
    against the full fixture universe."""
    kept_ids = {it.get("id") for it in output.get("items", []) if isinstance(it, dict)}
    universe = {it["id"] for it in FIXTURE_ITEMS}
    inverted_ids = universe - kept_ids
    degraded = copy.deepcopy(output)
    degraded["items"] = [{"id": i} for i in sorted(inverted_ids)]
    return degraded


def degrade_groupby(output: dict) -> dict:
    """Invert the real clustering so it is guaranteed below threshold BY
    CONSTRUCTION, for any real output shape — not just the multi-group
    case (code-review finding 2026-08-21: merging into one group is a
    NO-OP, and the negative control silently PASSES, when the real run
    already returned a single group).

    Real output has >= 2 groups: merge everything into one group — turns
    every cross-group pair (the majority of pairs for a genuinely
    multi-group assignment) into a same-group disagreement.

    Real output has <= 1 group (all items already co-grouped): the merge
    degradation is a no-op (rand_index would stay 1.0), so instead split
    every item into its OWN singleton group — turns every real same-group
    pair (100% of pairs, since there was only one group) into a
    disagreement instead.

    Either branch inverts whichever pairwise relationship the real
    output actually has, so at least one of the two guaranteed-changed
    pair classes dominates the total -- there's no real-output shape for
    which this degradation leaves rand_index at 1.0.
    """
    groups = output.get("groups", [])
    degraded = copy.deepcopy(output)
    if len(groups) >= 2:
        all_items: list[dict] = []
        for group in groups:
            all_items.extend(group.get("items", []))
        degraded["groups"] = [{"key_value": "everything", "items": all_items}]
    else:
        singleton_groups = []
        for group in groups:
            for item in group.get("items", []):
                singleton_groups.append({
                    "key_value": f"solo-{item.get('id', id(item))}",
                    "items": [item],
                })
        degraded["groups"] = singleton_groups
    return degraded


def degrade_rank(output: dict) -> dict:
    """Reverse the ranking. Reversing a full permutation against itself
    is its OWN exact anti-correlate: Spearman rho = -1.0 always, for any
    base ordering — a deterministic, seed-free negative control."""
    degraded = copy.deepcopy(output)
    degraded["ranked"] = list(reversed(output.get("ranked", [])))
    return degraded


def degrade_extract(output: dict) -> dict:
    """Drop all but one field from every extraction record — a large,
    deterministic recall cut against the field-value bag."""
    degraded_extractions = []
    for record in output.get("extractions", []):
        if isinstance(record, dict) and record:
            first_key = sorted(record.keys())[0]
            degraded_extractions.append({first_key: record[first_key]})
        else:
            degraded_extractions.append(record)
    degraded = copy.deepcopy(output)
    degraded["extractions"] = degraded_extractions
    return degraded


_ROLE_FLIP = {"supports": "contradicts", "contradicts": "supports", "neutral": "contradicts"}


def degrade_check(output: dict) -> dict:
    """Invert ``ok`` and flip every evidence role."""
    degraded = copy.deepcopy(output)
    degraded["ok"] = not bool(output.get("ok"))
    degraded["evidence"] = [
        {**e, "role": _ROLE_FLIP.get(e.get("role", "neutral"), "neutral")}
        for e in output.get("evidence", [])
        if isinstance(e, dict)
    ]
    return degraded


def degrade_verify(output: dict) -> dict:
    """Invert ``verified`` and drop all citations."""
    degraded = copy.deepcopy(output)
    degraded["verified"] = not bool(output.get("verified"))
    degraded["citations"] = []
    return degraded


DEGRADERS: dict[str, Any] = {
    "operator_filter": degrade_filter,
    "operator_groupby": degrade_groupby,
    "operator_rank": degrade_rank,
    "operator_extract": degrade_extract,
    "operator_check": degrade_check,
    "operator_verify": degrade_verify,
}


# ── same-verdict-wrong-grounding degradation (substantive-critic finding,
# 2026-08-21): degrade_check/degrade_verify above ALWAYS flip the boolean
# verdict together with the grounding, and bool_plus_jaccard caps the
# composite at (1 - bool_weight) = 0.5 on ANY bool mismatch REGARDLESS of
# the Jaccard term -- so the Jaccard/grounding term the 2026-08-21
# citation-matching revision fixed has never been independently exercised
# below threshold. These two hold the verdict FIXED and empty the
# grounding instead, isolating the grounding term as the sole cause of
# failure. Provable with pure synthetic fixtures, no live dispatch: for
# any nonempty real grounding set, bool_plus_jaccard(v, v, S, {}) =
# 0.5*1 + 0.5*0 = 0.5, which is < both operators' 0.70 threshold. ─────────

class DegenerateGroundingError(ValueError):
    """Raised by ``degrade_check_wrong_grounding`` /
    ``degrade_verify_wrong_grounding`` when the REAL output's grounding
    (evidence/citations) is ALREADY empty (round-2 substantive-critic
    finding, 2026-08-21 — the same structural class as round-1's
    ``degrade_groupby`` single-group no-op).

    Degrading "empty" to "empty" would score
    ``exact_membership_jaccard(∅, ∅) = 1.0`` — a VACUOUS pass that tests
    nothing, not a real negative control. Rather than silently produce
    that pass, this raises an explicit, named marker so a caller (test or
    harness) must handle the degenerate case deliberately — skip it with
    a stated reason, never fold it into a score as if it meant something.
    """


def degrade_check_wrong_grounding(output: dict) -> dict:
    """Same ``ok`` verdict, evidence emptied -- isolates the evidence-
    Jaccard term (verdict-agreement term is deliberately left untouched,
    unlike ``degrade_check``).

    Raises:
        DegenerateGroundingError: *output*'s evidence is already empty.
    """
    evidence = output.get("evidence", [])
    if not evidence:
        raise DegenerateGroundingError(
            "operator_check: real output's evidence is already empty -- "
            "wrong-grounding degradation would be an empty-vs-empty "
            "comparison (vacuous pass); this input is not a valid case "
            "for this degrader"
        )
    degraded = copy.deepcopy(output)
    degraded["evidence"] = []
    return degraded


def degrade_verify_wrong_grounding(output: dict) -> dict:
    """Same ``verified`` verdict, citations emptied -- isolates the
    citation-token-Jaccard term (verdict-agreement term is deliberately
    left untouched, unlike ``degrade_verify``).

    Raises:
        DegenerateGroundingError: *output*'s citations are already empty.
    """
    citations = output.get("citations", [])
    if not citations:
        raise DegenerateGroundingError(
            "operator_verify: real output's citations are already empty "
            "-- wrong-grounding degradation would be an empty-vs-empty "
            "comparison (vacuous pass); this input is not a valid case "
            "for this degrader"
        )
    degraded = copy.deepcopy(output)
    degraded["citations"] = []
    return degraded


WRONG_GROUNDING_DEGRADERS: dict[str, Any] = {
    "operator_check": degrade_check_wrong_grounding,
    "operator_verify": degrade_verify_wrong_grounding,
}


# ── graded (near-miss) degradation (substantive-critic finding, 2026-08-21):
# every DEGRADERS entry above is a caricature-level full inversion. That
# proves the gate rejects obvious garbage but says nothing about the
# smallest realistic quality loss it can actually detect -- the magnitude
# a real cheap-vs-strong FLIP decision (.p2c/.p2d) needs resolved. These
# perturb ONE element instead of all of them; TestGradedDegrade (see the
# test suite) reports the resulting score for each operator as a stated
# "smallest loss visible to this gate" data point, pure/no dispatch. ─────

def graded_degrade_filter(output: dict) -> dict:
    """Drop exactly ONE kept item (if any), instead of inverting the
    whole membership set."""
    degraded = copy.deepcopy(output)
    items = degraded.get("items", [])
    if items:
        degraded["items"] = items[1:]
    return degraded


def graded_degrade_groupby(output: dict) -> dict:
    """Move exactly ONE item from its group into a different existing
    group (a single misclassification), instead of merging/splitting
    everything."""
    degraded = copy.deepcopy(output)
    groups = degraded.get("groups", [])
    if len(groups) >= 2 and groups[0].get("items"):
        moved = groups[0]["items"].pop(0)
        groups[1].setdefault("items", []).append(moved)
    return degraded


def graded_degrade_rank(output: dict) -> dict:
    """Swap exactly ONE adjacent pair, instead of reversing the whole
    ranking."""
    degraded = copy.deepcopy(output)
    ranked = degraded.get("ranked", [])
    if len(ranked) >= 2:
        ranked[0], ranked[1] = ranked[1], ranked[0]
    return degraded


def graded_degrade_extract(output: dict) -> dict:
    """Drop ONE field from ONE record, instead of all-but-one field from
    every record."""
    degraded = copy.deepcopy(output)
    extractions = degraded.get("extractions", [])
    if extractions and isinstance(extractions[0], dict) and extractions[0]:
        drop_key = sorted(extractions[0].keys())[0]
        del extractions[0][drop_key]
    return degraded


def graded_degrade_check(output: dict) -> dict:
    """Flip the role of exactly ONE evidence entry; verdict and every
    other entry untouched."""
    degraded = copy.deepcopy(output)
    evidence = degraded.get("evidence", [])
    if evidence:
        e = evidence[0]
        e["role"] = _ROLE_FLIP.get(e.get("role", "neutral"), "neutral")
    return degraded


def graded_degrade_verify(output: dict) -> dict:
    """Drop ONE of N citations; verdict untouched."""
    degraded = copy.deepcopy(output)
    citations = degraded.get("citations", [])
    if citations:
        degraded["citations"] = citations[1:]
    return degraded


GRADED_DEGRADERS: dict[str, Any] = {
    "operator_filter": graded_degrade_filter,
    "operator_groupby": graded_degrade_groupby,
    "operator_rank": graded_degrade_rank,
    "operator_extract": graded_degrade_extract,
    "operator_check": graded_degrade_check,
    "operator_verify": graded_degrade_verify,
}


# ── scoring: dispatches two structured outputs of the SAME operator ────────

def score(operator_name: str, output_a: dict, output_b: dict) -> float | None:
    """Score agreement between two structured outputs of *operator_name*.

    Returns ``None`` only for ``operator_rank`` when fewer than 2 common
    tagged ids exist across both orderings (undefined correlation) — every
    other operator's metric is total over well-formed input.
    """
    if operator_name == "operator_filter":
        a = {it.get("id") for it in output_a.get("items", []) if isinstance(it, dict)}
        b = {it.get("id") for it in output_b.get("items", []) if isinstance(it, dict)}
        return exact_membership_jaccard(a, b)

    if operator_name == "operator_groupby":
        assign_a = {
            it.get("id"): g.get("key_value")
            for g in output_a.get("groups", [])
            for it in g.get("items", [])
            if isinstance(it, dict) and it.get("id") is not None
        }
        assign_b = {
            it.get("id"): g.get("key_value")
            for g in output_b.get("groups", [])
            for it in g.get("items", [])
            if isinstance(it, dict) and it.get("id") is not None
        }
        return rand_index(assign_a, assign_b)

    if operator_name == "operator_rank":
        return spearman_rank_correlation(
            output_a.get("ranked", []), output_b.get("ranked", []),
        )

    if operator_name == "operator_extract":
        return field_value_bag_f1(
            output_a.get("extractions", []), output_b.get("extractions", []),
        )

    if operator_name == "operator_check":
        evidence_a = {
            (e.get("item_id", ""), e.get("role", ""))
            for e in output_a.get("evidence", []) if isinstance(e, dict)
        }
        evidence_b = {
            (e.get("item_id", ""), e.get("role", ""))
            for e in output_b.get("evidence", []) if isinstance(e, dict)
        }
        return bool_plus_jaccard(
            bool(output_a.get("ok")), bool(output_b.get("ok")),
            {f"{i}:{r}" for i, r in evidence_a}, {f"{i}:{r}" for i, r in evidence_b},
        )

    if operator_name == "operator_verify":
        # Token-level Jaccard on citations, not whole-string Jaccard --
        # see citation_tokens' docstring for why (2026-08-21 revision
        # after a live strong-vs-strong positive control failed under
        # the original exact-string design).
        tokens_a = citation_tokens(output_a.get("citations", []))
        tokens_b = citation_tokens(output_b.get("citations", []))
        return bool_plus_jaccard(
            bool(output_a.get("verified")), bool(output_b.get("verified")),
            tokens_a, tokens_b,
        )

    raise ValueError(f"no scorer for operator {operator_name!r}")


@dataclass
class ControlResult:
    operator: str
    threshold: float
    positive_score: float | None
    positive_passed: bool | None
    negative_score: float | None
    negative_passed: bool | None
    dispatch_count: int
    total_cost_usd: float | None
    canonical_models: list[str] = field(default_factory=list)
    #: Raw structured outputs, persisted so a run's numbers can be
    #: audited or a metric change re-scored WITHOUT a fresh dispatch
    #: (nexus-nyry9.14 2026-08-21 addendum, after the first live run's
    #: transcripts had to be re-derived from memory rather than replayed).
    output_a: dict = field(default_factory=dict)
    output_b: dict = field(default_factory=dict)
    degraded: dict = field(default_factory=dict)


async def run_operator_controls(
    operator_name: str, *, model: str, timeout: float = 90.0,
) -> ControlResult:
    """Two live strong-tier dispatches on the SAME fixed input for
    *operator_name* (positive control + the variance sample), plus one
    synthetic degradation of the first run (negative control, no extra
    dispatch)."""
    from nexus.operators.dispatch import DispatchUsage, claude_dispatch

    prompt, schema = BUILDERS[operator_name]()
    usage_sink: list[DispatchUsage] = []

    output_a = await claude_dispatch(
        prompt, schema, timeout=timeout, model=model, operator=operator_name,
        usage_sink=usage_sink,
    )
    output_b = await claude_dispatch(
        prompt, schema, timeout=timeout, model=model, operator=operator_name,
        usage_sink=usage_sink,
    )

    positive_score = score(operator_name, output_a, output_b)
    degraded = DEGRADERS[operator_name](output_a)
    negative_score = score(operator_name, output_a, degraded)

    threshold = THRESHOLDS[operator_name]
    total_cost = None
    if usage_sink and all(u.cost_usd is not None for u in usage_sink):
        total_cost = sum(u.cost_usd for u in usage_sink)  # type: ignore[misc]

    return ControlResult(
        operator=operator_name,
        threshold=threshold,
        positive_score=positive_score,
        positive_passed=(positive_score >= threshold) if positive_score is not None else None,
        negative_score=negative_score,
        negative_passed=(negative_score < threshold) if negative_score is not None else None,
        dispatch_count=len(usage_sink),
        total_cost_usd=total_cost,
        canonical_models=[u.model for u in usage_sink if u.model],
        output_a=output_a,
        output_b=output_b,
        degraded=degraded,
    )


async def run_all(*, model: str, only: list[str] | None = None) -> list[ControlResult]:
    names = only if only is not None else list(BUILDERS)
    results = []
    for operator_name in names:
        results.append(await run_operator_controls(operator_name, model=model))
    return results


def _print_table(results: list[ControlResult]) -> None:
    print(f"{'operator':<18} {'thr':>5} {'pos':>7} {'pos_ok':>7} {'neg':>7} {'neg_ok':>7} {'$':>7}")
    for r in results:
        pos = f"{r.positive_score:.3f}" if r.positive_score is not None else "n/a"
        neg = f"{r.negative_score:.3f}" if r.negative_score is not None else "n/a"
        cost = f"{r.total_cost_usd:.3f}" if r.total_cost_usd is not None else "n/a"
        print(
            f"{r.operator:<18} {r.threshold:>5.2f} {pos:>7} "
            f"{str(r.positive_passed):>7} {neg:>7} {str(r.negative_passed):>7} {cost:>7}"
        )


def exit_code_for_results(results: list[ControlResult]) -> tuple[int, str | None]:
    """Pure aggregate/exit-code decision over a completed run's
    ``ControlResult`` list -- extracted out of ``main()`` (round-2
    code-review finding, 2026-08-21) so it is directly unit-testable
    without a live dispatch: nothing in the test suite previously
    imported or exercised ``main()``/``run_all()``/``ControlResult``, so
    a regression in the None-hard-fail logic below would have gone
    uncaught.

    Returns ``(exit_code, error_message_or_None)``. A ``None``
    (undefined) score on ANY result is a HARD FAILURE, never silently
    excluded from the aggregate -- matches
    ``tests/test_operator_proxy_controls.py``'s own ``pytest.fail()`` on
    ``None``. Only ``operator_rank`` can legitimately return ``None``
    (fewer than 2 common tagged ids across both orderings).
    """
    undefined = [r.operator for r in results if r.positive_score is None or r.negative_score is None]
    if undefined:
        return 1, (
            f"ERROR: undefined (None) score for {undefined} -- treated as "
            f"a FAILURE, not skipped (non-vacuity doctrine)"
        )

    all_positive_pass = all(r.positive_passed is True for r in results)
    all_negative_pass = all(r.negative_passed is True for r in results)
    if all_positive_pass and all_negative_pass:
        return 0, None
    return 1, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sonnet", help="--model alias to pass to claude_dispatch (default: sonnet, the 'strong' tier alias)")
    parser.add_argument("--out", type=str, default="", help="optional path to write a JSON report")
    parser.add_argument("--only", type=str, default="", help="comma-separated subset of operator names to run (default: all 6 in-scope operators)")
    args = parser.parse_args()

    only = [n.strip() for n in args.only.split(",") if n.strip()] or None
    if only is not None:
        unknown = [n for n in only if n not in BUILDERS]
        if unknown:
            print(
                f"ERROR: --only names unrecognized operator(s) {unknown} -- "
                f"known in-scope operators: {sorted(BUILDERS)}",
                file=_sys.stderr,
            )
            return 2
    results = asyncio.run(run_all(model=args.model, only=only))
    _print_table(results)

    if args.out:
        payload = [asdict(r) for r in results]
        _Path(args.out).write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.out}")

    exit_code, error_message = exit_code_for_results(results)
    if error_message:
        print(error_message, file=_sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
