# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Operator-bundle execution — nexus-nxa-perf.

A contiguous run of 2+ operator steps (``extract``, ``rank``, ``compare``,
``summarize``, ``generate``) collapses into a single ``claude -p`` invocation
instead of one subprocess spawn per step. Intermediate step outputs stay
inside the model's reasoning — the host only sees the terminal step's
output.

Rationale: each ``claude_dispatch`` call pays ~5s of fixed subprocess-spawn
cost (process fork + auth + model init) on top of the actual LLM work.
On a typical research plan ``search → extract → summarize``, bundling the
two operator steps saves one spawn (~5s, ~24% of a ~21s baseline).

Retrieval steps (``search``, ``query``, ``traverse``) and ``store_get_many``
auto-hydration stay isolated — they're cheap, deterministic, and the bundle
needs their real host-side outputs as inputs.

Contract with the runner:

* ``segment_steps`` walks the resolved step list and emits a sequence of
  ``Segment`` records. A segment is either a single isolated step or an
  ``OperatorBundle`` of ≥2 consecutive operators.
* ``dispatch_bundle`` issues one ``claude_dispatch`` for an entire bundle
  and returns the terminal step's output. The runner stamps that output
  into ``step_outputs`` at the bundle's LAST step index; intermediate
  slots receive a ``_BUNDLED_INTERMEDIATE`` sentinel so any downstream
  ``$stepN.<field>`` reference aimed at an intermediate raises loudly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUNDLEABLE_OPERATORS",
    "OPERATOR_BUNDLE_NAMES",  # legacy alias — see BUNDLEABLE_OPERATORS
    "OperatorBundleStep",
    "OperatorBundle",
    "OperatorBundleSlice",
    "IsolatedStep",
    "Segment",
    "BUNDLED_INTERMEDIATE",
    "DEFERRED_REF_KEY",
    "MAX_BUNDLE_PROMPT_CHARS",
    "is_operator_tool",
    "segment_steps",
    "compose_bundle_prompt",
    "dispatch_bundle",
]


#: Shared sentinel key — written by ``nexus.plans.runner._resolve_value``
#: when a ``$stepN.field`` reference points at a still-pending bundled
#: step, read by ``_deferred_ref_prose`` in this module. One authoritative
#: definition so a rename or typo on one side doesn't silently corrupt
#: the bundle prompt (substantive-critic C2).
DEFERRED_REF_KEY: str = "__nexus_deferred_step_ref__"


#: Maximum prompt-char budget for a composed bundle. Bundles whose
#: composite prompt exceeds this fall back to per-step dispatch so we
#: don't overflow the ``claude -p`` context window or tip a bundle's
#: output quality off a cliff. 200k chars ≈ 50k tokens, comfortably
#: inside Claude's 1M window while leaving room for reasoning overhead
#: and response. Tuned empirically — raise if bundles are falling back
#: on plans that are clearly within capacity; lower if bundles are
#: producing truncated/degenerate output. (substantive-critic Obs B)
MAX_BUNDLE_PROMPT_CHARS: int = 200_000


#: Operators eligible for bundling into a single ``claude -p`` subprocess
#: (substantive-critic Obs A). An operator may be added here only when:
#:
#: 1. It is *pure* — no side effects beyond returning a JSON dict.
#:    Writes, external API calls, and catalog mutations disqualify.
#: 2. Its per-call cost is bounded by its direct inputs. Operators
#:    whose cost is non-linear in input size (e.g. pairwise-compare
#:    across a large corpus) would starve a bundle's context budget.
#: 3. Its failure mode is meaningful to the bundle as a whole. A cheap
#:    early step failing inside a bundle can't short-circuit an
#:    expensive later step — the whole bundle either succeeds or fails.
#:    Don't bundle an operator whose failure must be retried in
#:    isolation.
#:
#: Today the eight AgenticScholar operators (extract / rank / compare /
#: summarize / generate / filter / check / verify) all satisfy (1),
#: (2), and (3). Bare and resolved forms are both accepted because
#: plan YAMLs use either.
BUNDLEABLE_OPERATORS: frozenset[str] = frozenset({
    "extract", "rank", "compare", "summarize", "generate",
    "filter", "check", "verify", "groupby",
    "operator_extract", "operator_rank", "operator_compare",
    "operator_summarize", "operator_generate", "operator_filter",
    "operator_check", "operator_verify", "operator_groupby",
})

#: Legacy alias — older call sites / docs may reference the prior name.
#: Prefer ``BUNDLEABLE_OPERATORS`` for new code.
OPERATOR_BUNDLE_NAMES: frozenset[str] = BUNDLEABLE_OPERATORS

# Sentinel stamped into step_outputs for intermediate bundled steps. Any
# $stepN.<field> reference that tries to look into this dict falls through
# the normal "missing field" path in the runner's _resolve_args — which
# raises PlanRunStepRefError — so a YAML author who references a bundled
# intermediate sees a clear failure.
BUNDLED_INTERMEDIATE: dict[str, Any] = {
    "_bundled_intermediate": True,
    "_note": (
        "This step is an intermediate inside an operator bundle; its output "
        "is consumed inline by the next operator and is not exposed on the "
        "host side. Reference the final step of the bundle instead."
    ),
}


def is_operator_tool(tool: str) -> bool:
    """Return True when *tool* is one of the LLM operators eligible for bundling.

    See :data:`BUNDLEABLE_OPERATORS` for the authoritative set + the
    criteria a new operator must satisfy to be added.
    """
    return tool in BUNDLEABLE_OPERATORS


# ── Segmentation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OperatorBundleStep:
    """One operator inside a bundle — already resolved.

    ``source_collections`` is the pre-hydration ``collections`` arg
    value when the step's ``inputs`` came from ``store_get_many`` auto-
    hydration. Captured before hydration strips ``ids``/``collections``
    from the args so the bundle composer can attach a ``source:`` line
    to the prompt — without it, parallel-branch bundles (two extracts
    each hydrating from a distinct retrieval step) produce hydrated
    content the LLM can't attribute back to its source corpus.
    ``None`` means the step's ``inputs`` were provided literally by the
    plan author or chained via a deferred ref.
    """

    plan_index: int                             # index in the original plan
    tool: str                                   # bare or operator_* form
    args: dict[str, Any]                        # post-substitution + post-hydration
    source_collections: Any = None              # pre-hydration collections arg


@dataclass(frozen=True)
class OperatorBundle:
    """A run of 2+ consecutive operator steps fused into one dispatch."""

    steps: tuple[OperatorBundleStep, ...]

    @property
    def start_index(self) -> int:
        return self.steps[0].plan_index

    @property
    def end_index(self) -> int:
        return self.steps[-1].plan_index


@dataclass(frozen=True)
class IsolatedStep:
    """A single non-bundled step (retrieval, ad-hoc, or lone operator)."""

    plan_index: int
    step: dict[str, Any]  # the ORIGINAL step dict (not yet resolved)


@dataclass(frozen=True)
class OperatorBundleSlice:
    """Structural marker: a contiguous run of ≥2 operator steps, pre-resolution.

    The runner resolves args + runs auto-hydration for each step at
    dispatch time (with the correct ``deferred_step_indices`` set for
    intra-bundle refs) and builds :class:`OperatorBundleStep` instances
    there. This class just names the bundle boundary — where to feed
    the args-resolved steps.
    """

    plan_indices: tuple[int, ...]

    @property
    def start_index(self) -> int:
        return self.plan_indices[0]

    @property
    def end_index(self) -> int:
        return self.plan_indices[-1]


Segment = OperatorBundleSlice | IsolatedStep


def _extract_tool_name(step: dict[str, Any]) -> str:
    """Normalize a plan step's tool name (accepts ``tool``/``op``/``operation``,
    strips any ``mcp__...__`` prefix)."""
    tool = step.get("tool") or step.get("op") or step.get("operation") or ""
    if tool.startswith("mcp__"):
        tool = tool.rsplit("__", 1)[-1]
    return tool


def segment_steps(steps: list[dict[str, Any]]) -> list[Segment]:
    """Walk *steps* and emit the sequence of Segments.

    Contiguous operator runs of length ≥ 2 collapse into a single
    :class:`OperatorBundleSlice` naming the bundle's plan indices.
    Lone operators and non-operator steps each yield :class:`IsolatedStep`.

    Pure structural analysis — no argument resolution, no dispatch.
    The runner handles both at execution time, where the relevant
    ``step_outputs`` list and ``deferred_step_indices`` are available.
    """
    segments: list[Segment] = []
    buffer: list[int] = []

    def _flush() -> None:
        if not buffer:
            return
        if len(buffer) >= 2:
            segments.append(OperatorBundleSlice(plan_indices=tuple(buffer)))
        else:
            idx = buffer[0]
            segments.append(IsolatedStep(plan_index=idx, step=steps[idx]))
        buffer.clear()

    for i, step in enumerate(steps):
        if is_operator_tool(_extract_tool_name(step)):
            buffer.append(i)
        else:
            _flush()
            segments.append(IsolatedStep(plan_index=i, step=step))
    _flush()
    return segments


# ── Prompt composition ────────────────────────────────────────────────────────


def _bare(tool: str) -> str:
    """Strip the ``operator_`` prefix so prompts use the user-facing verb."""
    return tool[len("operator_"):] if tool.startswith("operator_") else tool


def _deferred_ref_prose(
    v: Any,
    *,
    plan_to_local: dict[int, int] | None = None,
) -> str | None:
    """If *v* is a deferred-step-ref sentinel, return the prompt-ready
    prose for it; otherwise None.

    The sentinel's ``step_index`` is 0-based over the PLAN's step list,
    but the bundle prompt numbers its own steps 1..N (where N is bundle
    size). ``plan_to_local`` maps a plan_index to its 1-based position
    inside the current bundle — pass it so ``$step3.extractions`` in a
    plan where step 3 is the first bundled step renders as "STEP 1" in
    the prompt (matching the composer's own step numbering). Without
    it, we fall back to the plan-index display which would desync the
    prompt's cross-references.
    """
    if isinstance(v, dict) and v.get(DEFERRED_REF_KEY):
        plan_idx = int(v["step_index"])
        field = v.get("field") or ""
        if plan_to_local and plan_idx in plan_to_local:
            step_n = plan_to_local[plan_idx]
        else:
            step_n = plan_idx + 1  # fallback — should not fire in practice
        if field:
            return f"the `{field}` output from STEP {step_n}"
        return f"the output from STEP {step_n}"
    return None


def _fmt_value(
    v: Any,
    *,
    plan_to_local: dict[int, int] | None = None,
) -> str:
    """Render an operator arg value for embedding in the composite prompt."""
    prose = _deferred_ref_prose(v, plan_to_local=plan_to_local)
    if prose is not None:
        return prose
    if isinstance(v, (list, dict)):
        return json.dumps(v, indent=2, default=str)
    return v if isinstance(v, str) else str(v)


def _render_input_line(
    *,
    label: str,
    value: Any,
    first: bool,
    position: int,
    default_prose: str,
    plan_to_local: dict[int, int] | None = None,
) -> list[str]:
    """Render one "inputs: ..." line of a bundled step.

    Resolution priority (top wins):

    1. Deferred-step-ref sentinel → "STEP M's `field` output" prose.
       This is how bundled steps reference outputs of earlier bundled
       steps — the model chains internally.
    2. Concrete non-empty value → inline the value block REGARDLESS of
       bundle position. A later bundled step may receive concrete input
       from auto-hydration (parallel-branch shape: two extracts each
       hydrating from a different retrieval step, then a compare that
       consumes both). Under the prior "first-only inlining" rule, the
       second extract's hydrated content was silently dropped in favor
       of "chain from STEP N-1" prose — which collapsed parallel
       branches into a fake sequence and caused the LLM to hallucinate
       identical-source extractions. The fix: if the value exists,
       show it; chaining prose is only for genuinely-empty steps that
       rely on implicit prior-step chaining.
    3. First step with empty value → use default chaining prose (this
       should be rare — typically first steps have concrete inputs).
    4. Non-first empty value → default chaining prose.
    """
    prose = _deferred_ref_prose(value, plan_to_local=plan_to_local)
    if prose is not None:
        return [f"  {label}: {prose}"]
    if value not in (None, "", [], {}):
        return [
            f"  {label}:",
            _indent(_fmt_value(value, plan_to_local=plan_to_local)),
        ]
    return [f"  {label}: {default_prose}"]


def _describe_step(
    position: int,
    step: OperatorBundleStep,
    *,
    first: bool,
    plan_to_local: dict[int, int] | None = None,
) -> str:
    """Return the prompt fragment for one step in the bundle.

    The first step typically receives its literal inputs from the args.
    Subsequent steps pull input from the prior step — either via a
    deferred ``$stepN.field`` sentinel (plan author made the chain
    explicit) or via a generic "output from STEP N-1" fallback.
    """
    verb = _bare(step.tool)
    lines = [f"STEP {position} — {verb}"]

    if verb == "extract":
        fields = step.args.get("fields", "")
        lines.append(f"  fields: {fields}")
        if step.source_collections:
            lines.append(f"  source: {step.source_collections}")
        lines.extend(_render_input_line(
            label="inputs", value=step.args.get("inputs"),
            first=first, position=position,
            default_prose=f"the `extractions` array from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append("  Emit a JSON object with key `extractions` holding "
                     "one record per input item.")

    elif verb == "rank":
        criterion = step.args.get("criterion", "")
        lines.append(f"  criterion: {criterion!r}")
        lines.extend(_render_input_line(
            label="items", value=step.args.get("items"),
            first=first, position=position,
            default_prose=f"the output list from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append("  Emit a JSON object with key `ranked` holding the "
                     "items re-ordered best-first.")

    elif verb == "compare":
        focus = step.args.get("focus", "")
        focus_line = f" Focus on: {focus}." if focus else ""
        lines.append(f"  focus: {focus!r}{focus_line}")
        items_a = step.args.get("items_a")
        items_b = step.args.get("items_b")
        label_a = step.args.get("label_a", "A")
        label_b = step.args.get("label_b", "B")
        # Two-sided compare is valid at any bundle position — both deferred
        # sentinels render cleanly via _fmt_value, concrete values render
        # inline. Order: if either side is present, treat as two-sided.
        if items_a or items_b:
            lines.append(f"  set {label_a}:")
            lines.append(_indent(_fmt_value(items_a, plan_to_local=plan_to_local)))
            lines.append(f"  set {label_b}:")
            lines.append(_indent(_fmt_value(items_b, plan_to_local=plan_to_local)))
        else:
            lines.extend(_render_input_line(
                label="items", value=step.args.get("items"),
                first=first, position=position,
                default_prose=f"the output from STEP {position - 1}",
                plan_to_local=plan_to_local,
            ))
        lines.append("  Emit a JSON object with key `comparison` holding "
                     "the synthesis as a single string.")

    elif verb == "summarize":
        cited = bool(step.args.get("cited"))
        cite_line = " Include citations." if cited else ""
        lines.append(f"  cited: {cited}{cite_line}")
        lines.extend(_render_input_line(
            label="content", value=step.args.get("content"),
            first=first, position=position,
            default_prose=f"the output from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append("  Emit a JSON object with key `summary` (and "
                     "`citations` array when cited=true).")

    elif verb == "generate":
        template = step.args.get("template", "")
        lines.append(f"  template: {template!r}")
        lines.extend(_render_input_line(
            label="context", value=step.args.get("context"),
            first=first, position=position,
            default_prose=f"the output from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append("  Emit a JSON object with key `output` holding the "
                     "rendered content.")

    elif verb == "filter":
        criterion = step.args.get("criterion", "")
        lines.append(f"  criterion: {criterion!r}")
        lines.extend(_render_input_line(
            label="items", value=step.args.get("items"),
            first=first, position=position,
            default_prose=f"the output list from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append(
            "  Emit a JSON object with keys `items` (subset of the input "
            "items that satisfy the criterion) and `rationale` (one "
            "`{id, reason}` record per input explaining the keep/reject "
            "decision). The `items` array must be a subset of the input; "
            "never synthesize new entries."
        )

    elif verb == "check":
        instruction = step.args.get("check_instruction", "")
        lines.append(f"  check_instruction: {instruction!r}")
        lines.extend(_render_input_line(
            label="items", value=step.args.get("items"),
            first=first, position=position,
            default_prose=f"the output list from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append(
            "  Emit a JSON object with keys `ok` (boolean: true when "
            "every item supports the claim, false when at least one "
            "contradicts) and `evidence` (array of `{item_id, quote, "
            "role}` records where role is one of `supports`, "
            "`contradicts`, `neutral`)."
        )

    elif verb == "verify":
        claim = step.args.get("claim", "")
        lines.append(f"  claim: {claim!r}")
        lines.extend(_render_input_line(
            label="evidence", value=step.args.get("evidence"),
            first=first, position=position,
            default_prose=f"the output text from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        lines.append(
            "  Emit a JSON object with keys `verified` (boolean), "
            "`reason` (short string explaining the verdict), and "
            "`citations` (array of locator strings pinpointing the "
            "passages that ground the verdict)."
        )

    elif verb == "groupby":
        key = step.args.get("key", "")
        lines.append(f"  key: {key!r}")
        lines.extend(_render_input_line(
            label="items", value=step.args.get("items"),
            first=first, position=position,
            default_prose=f"the output list from STEP {position - 1}",
            plan_to_local=plan_to_local,
        ))
        # RDR-093 C-1: the inline-items contract is load-bearing for
        # bundled groupby → aggregate. The aggregate step runs inside
        # the same `claude -p` dispatch with no host-side retrieval,
        # so groupby must carry full item dicts (not id-only refs)
        # so aggregate can read resolvable content. The wording here
        # mirrors operator_groupby's standalone prompt so a future
        # change that drops the inline-items invariant in core.py
        # also has to update this prompt — keeping the two in sync.
        lines.append(
            "  Emit a JSON object with key `groups` holding a list "
            "of `{key_value, items}` records. Carry each item INLINE "
            "in its group's `items` array (preserve `id` and any "
            "other fields verbatim) — do NOT reference items by id-"
            "only. Every input item appears in exactly one group; "
            "items that cannot be confidently assigned go in a group "
            "with `key_value` of \"unassigned\"."
        )

    else:
        # Unknown operator — fall back to a verbose dump of args. This
        # should not fire in practice since segment_steps gates on
        # OPERATOR_BUNDLE_NAMES, but keep the path explicit.
        for k, v in step.args.items():
            lines.append(f"  {k}: {_fmt_value(v, plan_to_local=plan_to_local)}")

    return "\n".join(lines)


def _indent(text: str, *, by: int = 4) -> str:
    pad = " " * by
    return "\n".join(pad + line for line in text.splitlines())


def _terminal_schema(tool: str) -> dict[str, Any]:
    """Return the JSON schema for the bundle's LAST step — what the caller sees."""
    verb = _bare(tool)
    if verb == "extract":
        return {
            "type": "object",
            "required": ["extractions"],
            "properties": {
                "extractions": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
        }
    if verb == "rank":
        return {
            "type": "object",
            "required": ["ranked"],
            "properties": {
                "ranked": {"type": "array", "items": {"type": "string"}},
            },
        }
    if verb == "compare":
        return {
            "type": "object",
            "required": ["comparison"],
            "properties": {"comparison": {"type": "string"}},
        }
    if verb == "summarize":
        return {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
        }
    if verb == "generate":
        return {
            "type": "object",
            "required": ["output"],
            "properties": {"output": {"type": "string"}},
        }
    if verb == "filter":
        return {
            "type": "object",
            "required": ["items", "rationale"],
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
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
    if verb == "check":
        # Share the evidence-item schema with operator_check's standalone
        # definition so a role-enum or required-key change only lands
        # once. Local import avoids a module-load cycle between runner /
        # bundle / mcp.core.
        from nexus.mcp.core import _CHECK_EVIDENCE_ITEM_SCHEMA
        return {
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
    if verb == "verify":
        return {
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
    if verb == "groupby":
        # RDR-093 C-1: the `items` field on each group is a list of
        # OBJECTS (full input dicts inline), not strings (id-only).
        # The terminal schema MUST pin this so the bundle's final
        # `claude -p` enforcement rejects an id-only revert before
        # downstream consumers see it.
        return {
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
    # Generic fallback.
    return {"type": "object"}


def compose_bundle_prompt(bundle: OperatorBundle) -> tuple[str, dict[str, Any]]:
    """Return ``(prompt, json_schema)`` for executing the bundle in one shot.

    The prompt lays out all N steps in order and instructs the model to
    emit only the final step's output. The schema IS the final step's
    schema — intermediate outputs are internal.

    Deferred ``$stepN.field`` references inside bundled step args are
    translated via ``plan_to_local`` so the prompt's "STEP K" labels and
    the model's cross-references always refer to positions inside THIS
    bundle (1..N) rather than to positions in the outer plan (which the
    model has no visibility into).
    """
    # Plan-index → 1-based bundle-local position. A bundled step whose
    # plan_index is 4 might sit at bundle position 2 (e.g. plan had two
    # retrieval steps before the operator run).
    plan_to_local: dict[int, int] = {
        s.plan_index: i + 1 for i, s in enumerate(bundle.steps)
    }
    header = (
        "Execute the following analytical pipeline of "
        f"{len(bundle.steps)} steps in order. Each step's output may feed a "
        "later step via the explicit 'from STEP K' references — steps with "
        "their own concrete `inputs`/`items`/`content`/`context` block are "
        "independent branches, not chained from the previous step. Return "
        "ONLY the output of the final step as a single JSON object "
        "conforming to the schema. Do not emit intermediate results."
    )
    body = "\n\n".join(
        _describe_step(
            i + 1, s, first=(i == 0), plan_to_local=plan_to_local,
        )
        for i, s in enumerate(bundle.steps)
    )
    footer = (
        "\n\nFinal step's output schema:\n"
        + json.dumps(_terminal_schema(bundle.steps[-1].tool), indent=2)
    )
    prompt = f"{header}\n\n{body}{footer}"
    return prompt, _terminal_schema(bundle.steps[-1].tool)


# ── Dispatch ──────────────────────────────────────────────────────────────────


async def dispatch_bundle(
    bundle: OperatorBundle,
    *,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Issue a single ``claude_dispatch`` for the whole bundle.

    Returns the terminal step's output dict — the caller stamps it at the
    bundle's ``end_index`` slot in ``step_outputs``.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt, schema = compose_bundle_prompt(bundle)
    return await claude_dispatch(prompt, schema, timeout=timeout)
