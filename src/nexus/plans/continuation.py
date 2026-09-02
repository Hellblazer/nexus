# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The continuation cut — RDR-200 Phase 1a (nexus-4e75w.3).

Pure cut-selection: given a plan's ``steps``, classify the maximal
terminal (trailing) suffix of operator steps (``bundle.is_operator_tool``)
by how many ``claude -p`` dispatch units it would resolve to. This is the
foundation the continuation envelope (nexus-4e75w.4) and the handoff
telemetry (nexus-4e75w.5) build on — this module decides WHERE to cut and
WHETHER the cut is safe to hand off; it builds nothing.

Classification derives from the real dispatch machinery
(:func:`nexus.plans.bundle.resolve_dispatch_segments`, which itself wraps
:func:`nexus.plans.bundle.segment_steps` and mirrors ``plan_run``'s own
``use_bundle_path`` gate) — never a parallel reimplementation of that
logic. See :func:`classify_continuation_cut`'s docstring for the four
shapes and the reachability proof for :attr:`CutShape.MULTI_UNIT`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from nexus.plans.bundle import (
    IsolatedStep,
    OperatorBundleSlice,
    Segment,
    is_operator_tool,
    resolve_dispatch_segments,
)
# Deliberate cross-module reuse of two bundle.py internals — the
# "operator_" prefix stripper and the step-dict tool-name extractor —
# so this module never re-derives tool-name parsing in parallel (same
# convention as bundle.py's own import of core.py's
# _CHECK_EVIDENCE_ITEM_SCHEMA).
from nexus.plans.bundle import _bare as _bare_verb
from nexus.plans.bundle import _extract_tool_name

__all__ = [
    "CutShape",
    "ContinuationCut",
    "classify_continuation_cut",
]


class CutShape(str, Enum):
    """The four possible outcomes of continuation-cut classification."""

    #: No terminal operator suffix — nothing to continue. The plan
    #: returns exactly what it returns today, single_query fast path
    #: included (that path never reaches the classifier at all — it
    #: returns before Step 4's ``plan_run`` call).
    NO_SUFFIX = "no_suffix"

    #: A run of >=2 contiguous trailing operators that resolves to
    #: exactly ONE dispatch unit — an :class:`OperatorBundleSlice`
    #: composed via ``compose_bundle_prompt``. Fidelity reference:
    #: ``nexus.plans.bundle.compose_bundle_prompt``.
    SHAPE_A = "shape_a"

    #: A suffix of exactly ONE trailing operator — an
    #: :class:`IsolatedStep` dispatched through the standalone
    #: ``operator_*`` MCP tool. Fidelity reference: the RDR-200 Phase 0
    #: ``build_<op>_request`` builder in
    #: ``nexus.mcp.operator_requests`` (this is the likely-most-common
    #: continuation shape — a plan ending in one ``summarize`` or
    #: ``generate``).
    SHAPE_B = "shape_b"

    #: The trailing operator suffix resolves to TWO OR MORE dispatch
    #: units. The envelope carries exactly one (prompt, schema) pair, so
    #: this shape falls back to headless for that call — never a partial
    #: envelope. See :func:`classify_continuation_cut`'s docstring for
    #: when this is actually reachable (it is NOT reachable via
    #: ``segment_steps``'s own structural splitting when bundling is
    #: engaged — only when the ``use_bundle_path`` gate is closed).
    MULTI_UNIT = "multi_unit"


@dataclass(frozen=True)
class ContinuationCut:
    """The result of classifying a plan's continuation suffix.

    ``plan_indices`` and ``operators`` are always in plan order and are
    empty tuples for :attr:`CutShape.NO_SUFFIX`. ``cut_at_step`` is the
    0-based index into ``plan_json.steps`` where the suffix begins —
    ``None`` when there is no suffix.
    """

    shape: CutShape
    cut_at_step: int | None
    plan_indices: tuple[int, ...]
    operators: tuple[str, ...]


def classify_continuation_cut(
    steps: list[dict[str, Any]],
    *,
    bundle_operators: bool = True,
    supports_bundling: bool = True,
) -> ContinuationCut:
    """Classify *steps*'s maximal terminal operator suffix.

    **Definition (RDR-200 § The continuation cut):** the continuation
    suffix is the maximal terminal suffix of ``steps`` consisting solely
    of operator steps (:func:`nexus.plans.bundle.is_operator_tool`).
    Everything before it runs server-side exactly as today; plans
    interleave, so a mid-plan operator whose output a later retrieval
    depends on (``search -> extract -> search($step.ids) -> generate``)
    is NOT part of the suffix — only the trailing ``[generate]`` is.

    **Shape derivation.** This walks
    :func:`nexus.plans.bundle.resolve_dispatch_segments` (steps'
    segmentation AS THE RUNNER WOULD ACTUALLY DISPATCH IT, accounting for
    the ``use_bundle_path``/``bundle_operators``/``supports_bundling``
    gate — not just ``segment_steps``'s structural output) from the end,
    accumulating segments while they are wholly operator-composed. Because
    that walk can encounter at most one trailing run of operator
    segments, and because ``segment_steps`` fuses EVERY contiguous run of
    >=2 operators into exactly one :class:`~nexus.plans.bundle.
    OperatorBundleSlice` (never two adjacent slices, never a lone
    operator step directly adjacent to a slice of the same run — its own
    buffer-then-flush algorithm makes both impossible), the trailing run
    resolved under ``resolve_dispatch_segments(bundle_operators=True,
    supports_bundling=True)`` (the production default) can only ever be:
    empty (NO_SUFFIX), one ``OperatorBundleSlice`` (SHAPE_A), or one
    ``IsolatedStep`` wrapping a single operator (SHAPE_B).

    **MULTI_UNIT reachability (nexus-4e75w.3 audit round-2 residual).**
    The original design text worried MULTI_UNIT would fire from
    ``segment_steps`` splitting an operator run across "unbundleable
    operators" or a post-composition oversize check. Both are false for
    THIS function: ``is_operator_tool`` is exactly frozenset membership
    in ``BUNDLEABLE_OPERATORS`` (all ten shipped operators qualify — see
    that constant's own docstring), so no operator in a suffix is ever
    unbundleable; and the size cap
    (``MAX_BUNDLE_PROMPT_CHARS``/``MAX_CONTINUATION_PROMPT_CHARS``) is
    checked POST-composition at dispatch time (runner.py, and the
    envelope-size cap nexus-4e75w.4 adds) — it is a SEPARATE fallback
    layered on top of a cut this function already classified, not a
    classification input. So under the production bundling gate
    (``bundle_operators=True`` and a bundling-capable dispatcher — what
    every real ``nx_answer`` call uses today), MULTI_UNIT is
    UNREACHABLE for a pure trailing operator suffix — proved above, not
    asserted. It is genuinely reachable through exactly ONE path this
    function still covers: ``resolve_dispatch_segments`` with the
    bundling gate CLOSED (``bundle_operators=False``, or a dispatcher
    that does not advertise ``supports_bundling``) flattens a >=2-operator
    ``OperatorBundleSlice`` into N separate ``IsolatedStep`` entries —
    N real per-step ``claude -p`` dispatches, not one — which this
    classifier correctly reports as MULTI_UNIT. ``nx_answer``'s own call
    site always resolves the production gate open (bundling enabled), so
    this branch is defensive there (logged via structlog if it ever
    fires) rather than a path real traffic exercises; the test suite
    proves it with ``bundle_operators=False``, a real, non-vacuous
    construction, not a hypothetical.

    Args:
        steps: ``plan_json.steps`` — a list of raw (pre-resolution) step
            dicts, exactly as parsed from a matched or grown plan.
        bundle_operators: Mirrors ``plan_run``'s own parameter of the
            same name. ``True`` (the production default) — a >=2-operator
            trailing run composes into one dispatch (SHAPE_A eligible).
        supports_bundling: Mirrors the real dispatcher's
            ``_SUPPORTS_BUNDLING_ATTR`` flag (``getattr(dispatcher,
            "supports_bundling", False)`` in ``plan_run``). ``True`` (the
            production default — the runner's ``_default_dispatcher`` sets
            this) alongside ``bundle_operators=True`` is what production
            ``nx_answer`` calls actually resolve to.

    Returns:
        A :class:`ContinuationCut` naming the shape, the 0-based start
        index of the suffix (``None`` for ``NO_SUFFIX``), and the plan
        indices / bare operator verbs the suffix comprises.
    """
    if not steps:
        return ContinuationCut(
            shape=CutShape.NO_SUFFIX, cut_at_step=None,
            plan_indices=(), operators=(),
        )

    segments: list[Segment] = resolve_dispatch_segments(
        steps,
        bundle_operators=bundle_operators,
        supports_bundling=supports_bundling,
    )

    def _is_operator_segment(seg: Segment) -> bool:
        if isinstance(seg, OperatorBundleSlice):
            return True
        # IsolatedStep — check the ORIGINAL step dict's tool name.
        return is_operator_tool(_extract_tool_name(seg.step))

    trailing: list[Segment] = []
    for seg in reversed(segments):
        if _is_operator_segment(seg):
            trailing.append(seg)
        else:
            break
    trailing.reverse()

    if not trailing:
        return ContinuationCut(
            shape=CutShape.NO_SUFFIX, cut_at_step=None,
            plan_indices=(), operators=(),
        )

    plan_indices: list[int] = []
    operators: list[str] = []
    for seg in trailing:
        if isinstance(seg, OperatorBundleSlice):
            plan_indices.extend(seg.plan_indices)
            for pi in seg.plan_indices:
                operators.append(_bare_verb(_extract_tool_name(steps[pi])))
        else:
            assert isinstance(seg, IsolatedStep)
            plan_indices.append(seg.plan_index)
            operators.append(_bare_verb(_extract_tool_name(seg.step)))

    cut_at_step = plan_indices[0]

    if len(trailing) == 1:
        seg = trailing[0]
        shape = CutShape.SHAPE_A if isinstance(seg, OperatorBundleSlice) else CutShape.SHAPE_B
    else:
        shape = CutShape.MULTI_UNIT

    return ContinuationCut(
        shape=shape,
        cut_at_step=cut_at_step,
        plan_indices=tuple(plan_indices),
        operators=tuple(operators),
    )
