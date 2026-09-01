# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure prompt/schema builders for the ten ``operator_*`` MCP tools —
RDR-200 Phase 0 (nexus-5mft0.1).

Every ``operator_*`` tool in ``src/nexus/mcp/core.py`` used to build its
``claude_dispatch`` prompt and JSON schema inline, immediately before the
dispatch call. RDR-200's continuation mode needs the caller-visible
(prompt, schema) pair the standalone tool would have built for the SAME
args, without paying a subprocess round trip to get it — so this module
hoists each construction site into a pure ``build_<op>_request(args) ->
(prompt, schema)`` function, callable from both the tool (this phase)
and the continuation path (RDR-200 Phase 1).

Pure by construction: no I/O, no ``claude_dispatch``, no model-tier
logic, no imports of ``nexus.mcp.core`` or anything that imports it —
this module sits below ``core.py`` in the import graph so ``core.py``
can import it at module scope with zero cycle risk. The model pin
(``_pin_default_model``) and the ``operator=`` dispatch label stay at
the tool call site; only the (prompt, schema) construction moves here.

Every body below is a byte-for-byte transcription of the construction
that used to live inline in the corresponding ``operator_*`` function
(verified against develop 3c3c10321) — this is a pure refactor with a
guard (``tests/test_operator_request_builders.py``), not a rewrite.
Three of the ten (``operator_filter``, ``operator_groupby``,
``operator_aggregate``) also have a SQL fast path ahead of the LLM
prompt; that branch stays at the tool, unmoved — only the LLM-path
prompt/schema construction the SQL path falls through to is hoisted
here.

Do NOT unify these schemas with ``nexus.plans.bundle._terminal_schema``
— they are deliberately NOT byte-identical (e.g. ``build_generate_
request``'s schema carries ``citations``; ``_terminal_schema("generate")``
does not, RDR-200 P0 audit round 2 residual). ``bundle.py``'s prompt
shape (positional pipeline fragments via ``compose_bundle_prompt``) is
also a structurally different, deliberately separate builder for the
``>=2``-contiguous-operator bundle path — this module is the fidelity
reference for the single, lone-terminal-operator (``IsolatedStep``)
path only.
"""
from __future__ import annotations

from typing import Any, Callable

#: Shared evidence-item schema for ``operator_check`` and (via a deferred
#: import) ``nexus.plans.bundle._terminal_schema("check")`` — one
#: authoritative definition so a role-enum or required-key change lands
#: once. ``nexus.mcp.core`` re-exports this name at module scope (a plain
#: import, not a copy) so ``bundle.py``'s existing
#: ``from nexus.mcp.core import _CHECK_EVIDENCE_ITEM_SCHEMA`` keeps
#: working unchanged.
_CHECK_EVIDENCE_ITEM_SCHEMA: dict = {
    "type": "object",
    "required": ["item_id", "quote", "role"],
    "properties": {
        "item_id": {"type": "string"},
        "quote": {"type": "string"},
        "role": {
            "type": "string",
            "enum": ["supports", "contradicts", "neutral"],
        },
    },
}


def build_extract_request(inputs: str, fields: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_extract``. Verbatim from core.py."""
    prompt = (
        f"Extract the following fields from each item: {fields}\n\n"
        f"Items:\n{inputs}"
    )
    schema: dict = {
        "type": "object",
        "required": ["extractions"],
        "properties": {
            "extractions": {
                "type": "array",
                "items": {"type": "object"},
            }
        },
    }
    return prompt, schema


def build_rank_request(items: str, criterion: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_rank``. Verbatim from core.py."""
    prompt = (
        f"Rank the following items by {criterion}.\n"
        f"Return them in ranked order, best first.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ranked"],
        "properties": {
            "ranked": {"type": "array", "items": {"type": "string"}},
        },
    }
    return prompt, schema


def build_compare_request(
    items: str = "",
    focus: str = "",
    *,
    items_a: str = "",
    items_b: str = "",
    label_a: str = "A",
    label_b: str = "B",
) -> tuple[str, dict]:
    """Prompt/schema for ``operator_compare``. Verbatim from core.py,
    including both prompt branches: the two-sided cross-set compare
    (``items_a``/``items_b`` both given) and the one-sided compare
    (``items`` only)."""
    import json as _json  # noqa: PLC0415 — matches core.py's original deferred import

    def _fmt(v) -> str:
        if isinstance(v, (list, dict)):
            return _json.dumps(v, indent=2, default=str)
        return v if isinstance(v, str) else str(v)

    focus_clause = f" Focus on: {focus}." if focus else ""
    if items_a and items_b:
        a_text = _fmt(items_a)
        b_text = _fmt(items_b)
        prompt = (
            f"Compare two sets of items across corpora.{focus_clause}\n\n"
            f"Set {label_a}:\n{a_text}\n\n"
            f"Set {label_b}:\n{b_text}\n\n"
            "Name:\n"
            f"  * **Shared axes**: concerns both {label_a} and {label_b} "
            "address with comparable intent (even if mechanism differs).\n"
            f"  * **Divergent decisions**: places where {label_a} and {label_b} "
            "take different approaches on the same question; attribute each "
            "choice to its side.\n"
            f"  * **Side-only axes**: concerns that appear in {label_a} or "
            f"{label_b} but not both.\n"
            "  * **Philosophy difference**: one or two sentences on the "
            "underlying stance difference, if one emerges from the evidence."
        )
    else:
        items_text = _fmt(items)
        prompt = (
            f"Compare the following items.{focus_clause}\n\n"
            f"Items:\n{items_text}"
        )
    schema: dict = {
        "type": "object",
        "required": ["comparison"],
        "properties": {
            "comparison": {"type": "string"},
        },
    }
    return prompt, schema


def build_summarize_request(content: str, cited: bool = False) -> tuple[str, dict]:
    """Prompt/schema for ``operator_summarize``. Verbatim from core.py."""
    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = f"Summarize the following content concisely.{cite_clause}\n\n{content}"
    schema: dict = {
        "type": "object",
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return prompt, schema


def build_generate_request(
    template: str, context: str, cited: bool = False,
) -> tuple[str, dict]:
    """Prompt/schema for ``operator_generate``. Verbatim from core.py.

    NOT byte-identical to ``bundle._terminal_schema("generate")`` — this
    schema carries ``citations``, that one does not (RDR-200 P0 audit
    round 2 residual). Deliberate; do not "fix" the discrepancy here."""
    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = (
        f"Generate a {template}.{cite_clause}\n\n"
        f"Context:\n{context}"
    )
    schema: dict = {
        "type": "object",
        "required": ["output"],
        "properties": {
            "output": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return prompt, schema


def build_filter_request(items: str, criterion: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_filter``'s LLM path. Verbatim from
    core.py. The SQL fast path (``try_filter``) stays at the tool and
    never reaches this builder."""
    prompt = (
        f"Filter the following items by this criterion: {criterion}\n"
        f"Return only the items that satisfy the criterion in the 'items' "
        f"array. Populate 'rationale' with one entry per input item, "
        f"keyed by the item's id, giving the reason each item was kept "
        f"or rejected. The output 'items' array must be a subset of the "
        f"input; never add synthetic items.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["items", "rationale"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object"},
            },
            "rationale": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }
    return prompt, schema


def build_check_request(items: str, check_instruction: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_check``. Verbatim from core.py."""
    prompt = (
        f"Check whether the following items are consistent with this "
        f"claim or question: {check_instruction}\n"
        f"Set ok=true when every item supports the claim, false when at "
        f"least one item contradicts it. Populate 'evidence' with a "
        f"record per item containing a short grounding 'quote' and a "
        f"'role' of 'supports', 'contradicts', or 'neutral'. Keep quotes "
        f"short enough to be verifiable against the source item.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ok", "evidence"],
        "properties": {
            "ok": {"type": "boolean"},
            "evidence": {
                "type": "array",
                "items": _CHECK_EVIDENCE_ITEM_SCHEMA,
            },
        },
    }
    return prompt, schema


def build_verify_request(claim: str, evidence: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_verify``. Verbatim from core.py."""
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
            "citations": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }
    return prompt, schema


def build_groupby_request(items: str, key: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_groupby``'s LLM path. Verbatim from
    core.py. The SQL fast path (``try_groupby``) stays at the tool and
    never reaches this builder."""
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
        f"Items:\n{items}"
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
                        "items": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                },
            },
        },
    }
    return prompt, schema


def build_aggregate_request(groups: str, reducer: str) -> tuple[str, dict]:
    """Prompt/schema for ``operator_aggregate``'s LLM path. Verbatim from
    core.py. The SQL fast path (``try_aggregate``) stays at the tool and
    never reaches this builder."""
    prompt = (
        f"Reduce each group of items into a per-group summary using "
        f"this reducer instruction: {reducer}\n\n"
        f"Output one aggregate per input group, preserving the group's "
        f"`key_value` verbatim. Each `summary` MUST reference only the "
        f"items in that group's `items` array. Do NOT pull content "
        f"from items in other groups, even when vocabulary overlaps "
        f"across groups. The summary is a short paragraph answering "
        f"the reducer instruction USING ONLY this group's items.\n\n"
        f"Groups:\n{groups}"
    )
    schema: dict = {
        "type": "object",
        "required": ["aggregates"],
        "properties": {
            "aggregates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key_value", "summary"],
                    "properties": {
                        "key_value": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
        },
    }
    return prompt, schema


# ── Verb → builder lookup table (RDR-200 Phase 1b, nexus-4e75w.4) ──────────
#
# The continuation envelope's Shape B path (a lone trailing operator —
# nexus.plans.continuation.CutShape.SHAPE_B) needs to go from a bare
# operator verb ("extract", "summarize", ...) to the matching
# ``build_<op>_request`` builder above WITHOUT constructing the builder's
# name as a string and looking it up via ``getattr`` (nexus-4e75w.3
# critic observation 1, T2 [23942]) — a getattr-by-string-name approach
# would silently resolve to nothing (or the wrong thing) on a typo/rename
# instead of failing at import time the way a plain dict literal does.
#
# Each adapter takes the REAL hydrated args dict for that operator (the
# ``prepared_args`` ``_hydrate_operator_args`` in ``nexus.plans.runner``
# already produces for the real isolated dispatch — see
# ``nexus.plans.continuation_envelope``, which is the sole caller) and
# maps it onto the builder's actual positional/keyword signature. Keys
# are read with ``args[...]`` (required) or ``args.get(..., <default>)``
# (optional) exactly mirroring each ``operator_*`` MCP tool's own
# signature defaults in ``nexus.mcp.core`` — this table's only job is
# argument-shape translation, never new defaulting behaviour.
#
# Keyed on the BARE verb (``_bare(tool)`` strips any ``operator_``
# prefix) — the same bare-verb shape ``ContinuationCut.operators`` and
# ``nexus.plans.bundle._bare`` already use, so a caller with either a
# bare or ``operator_*``-prefixed tool name needs only one normalization
# step before indexing this table.
def _extract_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_extract_request(args["inputs"], args["fields"])


def _rank_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_rank_request(args["items"], args["criterion"])


def _compare_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_compare_request(
        args.get("items", ""), args.get("focus", ""),
        items_a=args.get("items_a", ""), items_b=args.get("items_b", ""),
        label_a=args.get("label_a", "A"), label_b=args.get("label_b", "B"),
    )


def _summarize_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_summarize_request(args["content"], args.get("cited", False))


def _generate_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_generate_request(
        args["template"], args["context"], args.get("cited", False),
    )


def _filter_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_filter_request(args["items"], args["criterion"])


def _check_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_check_request(args["items"], args["check_instruction"])


def _verify_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_verify_request(args["claim"], args["evidence"])


def _groupby_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_groupby_request(args["items"], args["key"])


def _aggregate_request_from_args(args: dict[str, Any]) -> tuple[str, dict]:
    return build_aggregate_request(args["groups"], args["reducer"])


#: Bare operator verb -> adapter that maps a real hydrated-args dict onto
#: the matching ``build_<op>_request`` builder's actual call signature.
#: Covers all ten shipped operators (the P0 docstring's own inventory:
#: extract / rank / compare / summarize / generate / filter / check /
#: verify / groupby / aggregate) — a KeyError on lookup names a genuinely
#: unrecognised verb rather than a silent no-op, matching this codebase's
#: fail-loud convention for a malformed/unsupported plan step.
VERB_TO_REQUEST_BUILDER: dict[str, Callable[[dict[str, Any]], tuple[str, dict]]] = {
    "extract": _extract_request_from_args,
    "rank": _rank_request_from_args,
    "compare": _compare_request_from_args,
    "summarize": _summarize_request_from_args,
    "generate": _generate_request_from_args,
    "filter": _filter_request_from_args,
    "check": _check_request_from_args,
    "verify": _verify_request_from_args,
    "groupby": _groupby_request_from_args,
    "aggregate": _aggregate_request_from_args,
}
