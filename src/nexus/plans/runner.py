# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Deterministic plan runner — RDR-078 P1.

:func:`plan_run` executes a :class:`~nexus.plans.match.Match` as a
straight-line DAG of MCP-tool dispatches. There is no agent dispatch,
no LLM call, no decision logic — every transformation is pure
substitution + tool dispatch + result stash.

Per-step execution loop:

1. Resolve ``$var`` placeholders in ``step.args`` from the merged
   binding map ``{**default_bindings, **caller_bindings}`` (caller
   wins on conflict).
2. Resolve ``$stepN.<field>`` references against prior step outputs
   captured by this runner.
3. Validate ``step.scope.taxonomy_domain`` against the collection
   embedding model implied by ``step.args`` — the SC-10 cross-embedding
   guard. ``traverse`` is exempt: it operates on tumblers, never on
   embeddings.
4. Dispatch the named tool via the injected ``dispatcher`` callable,
   capture the result, stash it as ``stepN`` for downstream references.

Errors:

* :class:`PlanRunBindingError` — required bindings unresolved at
  start. Carries ``missing: list[str]``.
* :class:`PlanRunStepRefError` — a ``$stepN.<field>`` reference points
  at a step that has not run yet, or at a field absent from the
  prior step's output dict.
* :class:`PlanRunUnresolvedVarError` — a ``$var`` reference has no
  default and no caller-supplied value. Checked for every step, before
  any step dispatches (nexus-pucte) — no silent literal-token fallback.
* :class:`PlanRunEmbeddingDomainError` — a step's
  ``scope.taxonomy_domain`` mismatches the embedding model of the
  collection it dispatches against.

The runner is deliberately decoupled from MCP wiring via the
``dispatcher`` parameter so it can be exercised in tests without
spinning up the FastMCP server. The default dispatcher (lazy-loaded)
calls into :mod:`nexus.mcp.core`.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os as _os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field, replace as _dc_replace
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable, Protocol

import structlog

from nexus import call_deadline
from nexus.plans.match import Match
from nexus.plans.owner_narrowing import OWNERS_BINDING

if TYPE_CHECKING:
    from nexus.plans.coverage_ledger import CoverageBoundary, CoverageResult
    from nexus.plans.evidence import CorpusNarrowing
    from nexus.plans.owner_narrowing import OwnerNarrowing

_log = structlog.get_logger(__name__)

#: nexus-5ezgn: a SYNC retrieval tool called directly on the event loop
#: blocks it, and no ``wait_for`` can cut it: one search step ran 310 s
#: against a 120 s budget that way. While a deadline is armed
#: (:mod:`nexus.call_deadline`, set by :func:`plan_run` around the
#: dispatch), :func:`_default_dispatcher` runs the tool on this executor
#: instead. Its OWN pool, not the loop's default executor: a cut thread
#: cannot be killed, and abandoned searches must not be able to starve the
#: default executor that T1 minting and channel waits share. Bounded, so
#: a burst of over-budget calls queues here; a queued call whose wait is
#: cut is cancelled before it starts. A running one stops at its next
#: ``call_deadline.check`` (search_cross_corpus checks between stages).
_RETRIEVAL_EXECUTOR_WORKERS: int = 4
_retrieval_executor: ThreadPoolExecutor | None = None


def _get_retrieval_executor() -> ThreadPoolExecutor:
    global _retrieval_executor
    if _retrieval_executor is None:
        _retrieval_executor = ThreadPoolExecutor(
            max_workers=_RETRIEVAL_EXECUTOR_WORKERS,
            thread_name_prefix="nx-retrieval",
        )
    return _retrieval_executor

__all__ = [
    "PlanResult",
    "PlanRunBindingError",
    "PlanRunEmbeddingDomainError",
    "PlanRunOperatorArgMissingError",
    "PlanRunOperatorOutputError",
    "PlanRunOperatorSchemaVersionError",
    "PlanRunOperatorUnavailableError",
    "PlanRunRetrievalRefusedError",
    "PlanRunStepRefError",
    "PlanRunToolNotFoundError",
    "PlanRunUnresolvedVarError",
    "StepRecord",
    "ToolDispatcher",
    "find_embedded_step_ref",
    "merge_bindings",
    "plan_run",
    "resolve_step_bindings",
]


# ── Errors ──────────────────────────────────────────────────────────────────


class PlanRunBindingError(ValueError):
    """Raised when one or more required bindings are unresolved at start."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            f"plan_run: missing required bindings: {sorted(missing)}"
        )


class PlanRunStepRefError(ValueError):
    """Raised when a ``$stepN.<field>`` reference cannot be resolved."""

    def __init__(self, ref: str, reason: str) -> None:
        self.ref = ref
        self.reason = reason
        super().__init__(f"plan_run: bad step reference {ref!r}: {reason}")


class PlanRunToolNotFoundError(ValueError):
    """Raised when a step names a tool not present on the dispatcher."""

    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"plan_run: unknown tool {tool!r}: {reason}")


class PlanRunOperatorArgMissingError(ValueError):
    """Raised when an operator step has no way to supply its required
    positional content argument (nexus-nyry9.4 review-fix, T2 [23036]).

    Not present as the operator's own arg key, not present as ``ids``
    (which ``_hydrate_operator_args`` would hydrate FROM), and not
    present as an ``inputs`` alias (nexus-yis0) — no hydration or
    substitution path can ever satisfy the call, so this is a plan-
    authoring bug, not a runtime condition. The unfixed shape reaches
    ``fn(**args)`` in ``_default_dispatcher`` and raises a bare
    ``TypeError`` instead (nx_answer_runs id=4: "operator_summarize()
    missing 1 required positional argument: 'content'").
    """

    def __init__(self, *, step_index: int, tool: str, missing_arg: str) -> None:
        self.step_index = step_index
        self.tool = tool
        self.missing_arg = missing_arg
        super().__init__(
            f"plan_run: steps[{step_index}] ({tool!r}) has no "
            f"{missing_arg!r} — not present in args, not hydratable via "
            "'ids', no 'inputs' alias present. Malformed plan, rejected "
            "before any step dispatched."
        )


class PlanRunOperatorOutputError(ValueError):
    """Raised when an operator MCP tool receives malformed structured output.

    Most commonly: the pool worker did not emit a ``StructuredOutput``
    tool_use event (model ignored the schema), the emitted JSON does
    not match the operator's contract shape, or a required field is
    missing. RDR-079 P3.
    """

    def __init__(self, operator: str, reason: str) -> None:
        self.operator = operator
        self.reason = reason
        super().__init__(f"operator_{operator}: {reason}")


class PlanRunOperatorSchemaVersionError(ValueError):
    """Raised when an operator dispatch uses an unsupported schema version.

    Operator contracts pin ``$schema_version: 1``. If a caller passes a
    different version (e.g. future v2 shape), the tool refuses rather
    than silently mis-validating.
    """

    def __init__(self, operator: str, received: str | int, expected: str | int = 1) -> None:
        self.operator = operator
        self.received = received
        self.expected = expected
        super().__init__(
            f"operator_{operator}: unsupported $schema_version "
            f"{received!r} (expected {expected!r})"
        )


class PlanRunOperatorUnavailableError(RuntimeError):
    """Raised when operator steps cannot run because the pool has no auth.

    SC-10 (graceful degradation): when ``claude auth status --json``
    reports ``loggedIn: false`` (or the CLI is missing), operator-
    requiring MCP tools convert the underlying ``PoolAuthUnavailableError``
    into this named error. Callers (plan_run + downstream skills) can
    branch on it without importing the pool's private exception type.

    Retrieval-only plans continue to work — this error is exclusive to
    steps that dispatch through the operator pool.
    """

    def __init__(self, operator: str, reason: str) -> None:
        self.operator = operator
        self.reason = reason
        super().__init__(
            f"operator_{operator}: unavailable — {reason}. "
            "Run `claude auth login` (Claude Code) or `nx config set anthropic_api_key <key>` "
            "(a direct Anthropic API key; `nx doctor` names the backend in use) to enable "
            "operator-backed plan steps; retrieval-only plans still work."
        )


class PlanRunRetrievalRefusedError(RuntimeError):
    """Raised when a retrieval step's tool refused outright rather than
    returning results (nexus-vply6 fix round 2, SHIP-BLOCKER).

    ``_default_dispatcher`` normally synthesizes an empty structured
    result ``{ids: [], ..., error: <text>}`` for a retrieval tool's bare
    ``"Error: ..."`` return (a bad subtree, an uninitialized catalog, an
    unresolvable filter — a plan-BINDING issue the next operator step can
    still degrade past, e.g. via ``_hydrate_operator_args``'s zero-
    evidence short-circuit). A search/query whose embedder cannot serve
    one of its targeted collections is a DIFFERENT class: the collection
    exists and may hold exactly the evidence the plan needs, but this
    install's current embedding mode cannot reach it. Synthesizing an
    empty result here reproduces the EXACT silent-empty-result shape
    nexus-vply6 exists to close, one layer up from the MCP tool boundary
    the bead's own fix already covers — ``nx_answer`` would confidently
    answer "no evidence" while never having searched the collection that
    held it (critique T2 [26773], ship-blocker 1).

    Recognized via :func:`nexus.errors.is_search_embedding_profile_mismatch_text`
    against the bare error STRING (not the exception object — by the time
    a retrieval tool's result reaches ``_default_dispatcher`` it has
    already been rendered to text by ``_mcp_tool_error``, at the MCP tool
    boundary), so this fires for every retrieval tool in
    :data:`_RETRIEVAL_TOOLS` — ``search``/``query`` (via
    ``search_cross_corpus``) and the four combined-query tools (via
    ``_grouped_combined_query`` / ``search_topic_scoped`` in
    :mod:`nexus.mcp.core`) alike, since fix round 2 point 3 made all of
    them raise the identical named error.
    """

    def __init__(self, *, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__(
            f"plan_run: retrieval step ({tool!r}) refused rather than "
            f"returning results: {message}"
        )


class PlanRunEmbeddingDomainError(ValueError):
    """Raised when a step would cross the embedding-model boundary (SC-10).

    Covers two failure modes:
    * ``actual_model == "<unknown>"`` — the declared ``taxonomy_domain``
      is not in the domain→model registry (refuse rather than guess).
    * Otherwise — the declared domain's expected model does not match
      the collection's actual model.
    """

    def __init__(
        self,
        step_index: int,
        declared_domain: str,
        collection: str,
        actual_model: str,
    ) -> None:
        self.step_index = step_index
        self.declared_domain = declared_domain
        self.collection = collection
        self.actual_model = actual_model
        if actual_model == "<unknown>":
            super().__init__(
                f"plan_run: step {step_index} declares unrecognized "
                f"taxonomy_domain={declared_domain!r}; cross-embedding "
                f"boundary guard refuses ambiguous dispatch"
            )
        else:
            super().__init__(
                f"plan_run: step {step_index} declares "
                f"taxonomy_domain={declared_domain!r} but dispatches to "
                f"collection {collection!r} (embedding model "
                f"{actual_model!r}); cross-embedding boundary violation"
            )


class PlanRunUnresolvedVarError(ValueError):
    """Raised when a step's args reference a ``$var`` binding that the
    merged bindings (``{**default_bindings, **caller}``) cannot resolve —
    checked at validation time, BEFORE any step dispatches (nexus-pucte).

    Previously :func:`_resolve_value` resolved ``$var`` via
    ``bindings.get(var_name, value)``, so a binding with no default and
    no caller-supplied value silently reached the dispatched tool as the
    literal string ``'$var'`` — the same silent-literal class
    nexus-nyry9.5 fixed for ``nx_answer``'s ``single_query`` fast path
    (it was querying the literal string ``'$question'``).
    ``$stepN.<field>`` references already raise
    :class:`PlanRunStepRefError` on an unresolved reference; this closes
    the gap for the other reference kind so both fail loud alike — no
    silent fallback for either.
    """

    def __init__(self, *, step_index: int, var_name: str) -> None:
        self.step_index = step_index
        self.var_name = var_name
        super().__init__(
            f"plan_run: steps[{step_index}] references unresolved "
            f"binding '${var_name}' — not present in default_bindings "
            "or caller-supplied bindings. Malformed plan or missing "
            "binding; rejected before any step dispatched."
        )


# ── Tool dispatcher protocol ────────────────────────────────────────────────


class ToolDispatcher(Protocol):
    """Awaitable callable that invokes an MCP tool by name with a kwargs dict.

    Async since RDR-079 P4 — the runner awaits dispatcher results so the
    underlying async ``operator_*`` tools (subprocess-backed pool workers)
    can run on the current event loop without thread-bridge loop-boundary
    crashes. Test dispatchers may be plain ``async def`` functions.
    """

    async def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


# ── Result ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepRecord:
    """One cost/quality telemetry record for one EXECUTED dispatch segment
    (RDR-196 .p1b, nexus-nyry9.8).

    "One executed step" here means one dispatch *segment* as
    :func:`plan_run` actually ran it, not one entry in ``plan_json``'s
    ``steps`` array: a fused bundle of N plan-steps (see
    :mod:`nexus.plans.bundle`) is ONE claude -p subprocess call and
    therefore produces exactly ONE record, never N — inventing N
    per-step costs by dividing the bundle's real cost would fabricate
    data (the same principle the RDR states for bundle attribution).

    ``step_index`` vs the run-level ``PlanResult.budget_exhausted_at_step``
    (CROSS-REF, .r2 critique 2026-08-20): this field is 0-BASED (0 = the
    first executed step), matching every existing 0-based ``plan_index``/
    ``bi`` usage throughout this module. ``budget_exhausted_at_step`` is
    1-INDEXED with ``0`` reserved as "no plan step ran at all" (core.py's
    ``_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN`` sentinel). Same underlying
    value space, OPPOSITE meaning at 0 and an off-by-one everywhere else —
    never join or compare these two columns by raw value.

    ``model`` derivation rule, binding: always taken VERBATIM from the
    underlying ``DispatchUsage.model``. Prior to nexus-xepsr (2026-09-14)
    that field was itself "the single modelUsage entry's canonical id, or
    ``None`` when zero or ≥2 entries make a single value ambiguous" by
    construction in ``dispatch._parse_dispatch_usage`` — cited here at the
    time as "196-R3", a mislabeling corrected by nexus-xepsr (196-R3 is
    RDR-196's ``--strict-mcp-config`` spike, unrelated to this field; the
    rule itself was never written into RDR-196's own text at all, only
    into this docstring and ``DispatchUsage.model``'s). Since nexus-xepsr,
    ``DispatchUsage.model`` is "the single entry's canonical id when
    ``modelUsage`` has exactly one, or the HIGHEST-costUSD entry's
    canonical id when it has two or more (tie-broken by outputTokens,
    then by ``modelUsage``'s own key order), or ``None`` only when
    ``modelUsage`` is absent, empty, or wholly malformed" — see that
    dataclass's own docstring for the full rule and its rationale. This
    field is still NEVER re-derived from ``model_usage`` keys here, NEVER
    defaulted to a requested/alias model string, and NEVER coerced to
    ``""`` when absent — ``None`` is the only "absent" spelling, matching
    RDR-196 risk #1 (a silently-zero/empty telemetry field reads as "this
    was free", which is the exact measurement bug this arc exists to fix).

    ``source`` is one of three values, kept to the RDR's own sketch
    (no fourth value invented): ``"bundle"`` for a real fused
    ``dispatch_bundle`` call; ``"llm"`` for an isolated (or
    bundle-fallback) ``claude -p`` dispatch; ``"sql"`` for BOTH Gap 5's
    ``operator_filter``/``groupby``/``aggregate`` SQL fast path AND any
    non-operator (retrieval/traversal) tool dispatch — on this substrate
    every retrieval call already runs a real SQL/pgvector query
    server-side, so labelling it "sql" is accurate, not a stretch, and
    it reuses the RDR's own framing of the llm/sql split as "NOMA's
    deterministic-vs-stochastic engine choice" rather than adding an
    unspecified fourth bucket.

    ``input_tokens`` / ``output_tokens`` / ``cost_usd`` are ``| None``
    — a DELIBERATE widening of the RDR's illustrative non-Optional
    sketch, required by the RDR's OWN risk register (risk 1: "absent
    fields record None, never 0.0"). Two distinct zero-vs-unknown
    populations exist: a ``"sql"`` step is a TRUE, known zero (no
    claude -p call happened at all) — ``0``/``0``/``0.0``. An ``"llm"``
    step whose dispatch reached a parsed result envelope carries the
    real figures: the ``"bundle"`` path threads an explicit ``usage_sink``
    into ``dispatch_bundle`` -> ``claude_dispatch``, and the isolated /
    bundle-fallback path (dispatched through the :class:`ToolDispatcher`
    abstraction into an ``operator_*`` MCP tool that owns its own
    ``claude_dispatch`` call) is captured by the ambient ContextVar sink
    (``dispatch.ambient_usage_sink``) the runner sets around each step.
    Only a dispatch that produced no envelope at all (timeout, non-zero
    exit, empty stdout) records ``None`` — unknown, not a fabricated ``0``.

    ``run_id`` defaults to ``""`` — :func:`plan_run` has no concept of
    a persisted run id (the PG primary key ``.p1c``/``.p1d`` will add
    doesn't exist until AFTER this function returns and its caller
    persists the run row). The caller re-stamps the real id via
    ``dataclasses.replace(record, run_id=...)`` once known; this keeps
    ``plan_run``'s signature unchanged for that future wiring, per this
    bead's own DO instruction.
    """

    run_id: str = ""
    step_index: int = 0
    operator: str = ""
    source: str = "llm"
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: nexus-ndoke. The Anthropic usage envelope reports CACHED input
    #: separately: a cached prompt records ``input_tokens=2`` with the real
    #: size in ``cache_creation_input_tokens`` (cold) or
    #: ``cache_read_input_tokens`` (warm). Measured: cold 2/26068/0, warm
    #: 2/-/16324. Without these, ``input_tokens`` is 2 on essentially every
    #: recorded step and every per-plan cost aggregate mixes cache-warm and
    #: cache-cold runs with nothing recorded that can separate them.
    #: ``DispatchUsage`` already parses both; they were dropped at the
    #: persistence boundary. ``None`` (never 0) for absence, matching the
    #: sibling token fields: a stored 0 reads as "used no cached input",
    #: which is a measurement claim the run never made.
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: float | None = None
    elapsed_ms: int = 0
    ok: bool = True
    #: Populated ONLY on a ``source="bundle"`` record — the full set of
    #: 0-based plan indices this one dispatch fused. Empty for every
    #: other source.
    bundled_steps: list[int] = field(default_factory=list)

    # Same landmine as DispatchUsage (dispatch.py): frozen=True + a
    # mutable list field would auto-generate a __hash__ that raises
    # lazily on the first hash() call. Declare it honestly unhashable.
    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PlanResult:
    """Captured output of a :func:`plan_run` execution."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] | None = None
    #: nexus-h33x8.6 a4: set (1-indexed) when a caller-supplied ``deadline``
    #: cut the run short — either the wall-clock check before a segment
    #: started, or an ``OperatorTimeoutError`` raised while the deadline
    #: was active. ``None`` (the default) means the run completed or
    #: failed through the pre-a4 sentinel-and-continue path unchanged.
    budget_exhausted_at_step: int | None = None
    #: RDR-196 .p3c (nexus-nyry9.21): which budget axis cut the run short
    #: — ``"time"`` (the ``deadline`` mechanism above) or ``"cost"`` (the
    #: ``budget_usd_remaining`` pre-segment check). ``None`` whenever
    #: ``budget_exhausted_at_step`` is also ``None``. Threaded into the
    #: single marker emitter's text (``core.py``'s
    #: ``_budget_exhausted_response``) so a caller can tell which budget
    #: ran out without inventing a second marker shape.
    budget_exhausted_kind: str | None = None
    #: Total steps in the plan as parsed, regardless of how many actually
    #: ran. Lets a caller render "step N of M" without re-parsing
    #: ``match.plan_json`` itself.
    total_planned_steps: int = 0
    #: RDR-196 .p1b (nexus-nyry9.8): one :class:`StepRecord` per executed
    #: dispatch segment — see that class's docstring for the bundle/sql/
    #: llm attribution rules. In-process only; the engine-side
    #: ``nx_answer_steps`` write is `.p1c`/`.p1d`, not this bead.
    step_records: list[StepRecord] = field(default_factory=list)
    #: RDR-200 .p1c (nexus-4e75w.5): ``True`` when a caller-supplied
    #: ``continuation_cut_at_step`` actually stopped this run before
    #: dispatching the segment at/after that index — ``False`` (the
    #: default) whenever ``continuation_cut_at_step`` was ``None``, or
    #: the run stopped/exhausted/errored for some OTHER reason before
    #: ever reaching the cut point. Lets a caller distinguish "the
    #: suffix was withheld as designed" from "the suffix never got a
    #: chance to run" without re-deriving it from ``len(steps)`` vs
    #: ``total_planned_steps``.
    continuation_cut_applied: bool = False
    #: nexus-ivv4d code-review follow-up (T2 [24198]): one
    #: ``{step_index, tool, corpus, query}`` dict per ISOLATED dispatch
    #: whose tool is in :data:`_CORPUS_QUERY_TOOLS`, captured with the
    #: fully RESOLVED args — after the plan/scope/caller-scope/default
    #: corpus fall-through (``_apply_scope_to_args`` ->
    #: ``_apply_caller_scope_to_args`` -> ``_apply_default_corpus_to_args``)
    #: has already run. A plan template that leaves ``corpus`` unset
    #: shows the runner's OWN fill-in here, not an empty string — the
    #: static ``plan_json`` a caller could re-parse instead only ever
    #: shows what the plan AUTHOR wrote, not what actually dispatched.
    #: In-process only, same precedent as :attr:`step_records` — no
    #: wire/telemetry shape carries this.
    resolved_step_args: list[dict[str, Any]] = field(default_factory=list)
    #: nexus-4h0oh (RDR-200 Phase 1c): 1-indexed plan-step positions
    #: (matching :attr:`budget_exhausted_at_step`'s convention) of
    #: executed operator ("reduce") steps whose output is never
    #: referenced, directly or transitively, by the plan's terminal
    #: step — evidence the runner retrieved, reduced, and paid for,
    #: then silently discarded because no fan-in step folded the
    #: parallel branches back together. Empty when every reduce step
    #: feeds the terminal step (the common case), the plan has no
    #: reduce steps, or the run produced no output at all. Visibility
    #: only: the runner WARNS (structlog ``nx_answer_discarded_reduce_
    #: steps``) rather than refusing the run — see
    #: :func:`_discarded_reduce_steps`'s docstring for the "smallest
    #: change that makes the loss visible" rationale.
    dropped_reduce_steps: list[int] = field(default_factory=list)
    #: RDR-232 Phase 1 (nexus-jypmj.1.3): one dict per corpus-taking retrieval step the caller's
    #: ``scope`` reached, in step order -- ``{step_index, tool, plan_corpus, caller_scope,
    #: scope_kind, outcome, resolved_corpus}``. ``outcome`` is a
    #: :data:`nexus.plans.evidence.NarrowStatus`: ``conflict`` (the step was skipped),
    #: ``narrowed``, ``within_scope``, ``scope_applied``, ``scope_all``,
    #: ``subtree_filtered`` (a catalog subtree became the step's own ``subtree`` filter, whatever its
    #: corpus; nexus-jypmj.1.16), ``subtree_not_applied`` (the subtree could not filter the step, which ran
    #: as it would without a scope, with a ``detail``) or ``unscoped_collection_pin`` (a step pinned through
    #: ``collection``/``collections`` keeps today's behaviour). An explicit owner (the
    #: ``_nx_owners`` binding, nexus-jypmj.1.18) adds one more entry per step with ``scope_kind``
    #: ``owner`` (the owners and ``removed_collections`` ride along; a ``conflict`` skips the step).
    #: Empty when the caller gave neither scope nor owner, so an unscoped run reports nothing.
    scope_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    #: RDR-232 (nexus-jypmj.1.6): what the reserved primary stream did in this run
    #: (:meth:`ReservedStream.report`); ``None`` when no stream was passed.
    reserved_report: dict[str, Any] | None = None
    #: RDR-232 Phase 3 (nexus-jypmj.3.1): the coverage boundary's result for the terminal reduction --
    #: the ledger, ``coverage.state`` and whether generation was skipped. ``None`` when no
    #: ``coverage_boundary`` was passed, or the run never reached a terminal operator segment (a retrieval-only
    #: plan, a budget stop). A continuation cut at that segment runs the boundary first (nexus-jypmj.3.2), so a
    #: hand-off carries the state a headless run would.
    coverage: CoverageResult | None = None
    #: RDR-232 Phase 3 step 2 (nexus-jypmj.3.2): the repair evidence the coverage boundary merged into the
    #: terminal reduction's input. ``None`` when no ``coverage_boundary`` was passed. A continuation hand-off
    #: re-enters it (:func:`nexus.plans.repair_stream.supplement_scope`) so the envelope's prompt is built
    #: from the evidence the boundary assessed; its ``items`` are empty when no repair merged anything.
    repair_supplement: RepairSupplement | None = None


# ── Embedding-domain mapping ────────────────────────────────────────────────

#: Maps the high-level taxonomy domain to the embedding model that any
#: collection in that domain must use. Mirrors ``nexus.corpus.
#: index_model_for_collection``.
_DOMAIN_TO_MODEL: dict[str, str] = {
    "code": "voyage-code-3",
    "prose": "voyage-context-3",
}

#: Maps the taxonomy domain to the comma-separated ``corpus`` prefix
#: string accepted by the ``search`` / ``query`` MCP tools.
#: Forwarded by :func:`_apply_scope_to_args` when the step doesn't
#: pin a specific collection.
_DOMAIN_TO_CORPUS: dict[str, str] = {
    "code": "code",
    "prose": "knowledge,docs,rdr,paper",
}

#: Tools that operate on tumblers/ids/link-types and never embeddings.
#: They are exempt from the cross-embedding guard *and* from the
#: corpus-injection helper (no embedding space to route into).
_NON_EMBEDDING_TOOLS: frozenset[str] = frozenset({"traverse"})

#: Tools that accept the ``structured=True`` kwarg to opt into the retrieval
#: step-output contract (RDR-079 P1). ``traverse`` is deliberately excluded —
#: it returns dict natively without needing the flag. The runner's
#: ``_default_dispatcher`` auto-injects ``structured=True`` for the tools in
#: this set so plan steps can reference ``$stepN.ids`` / ``$stepN.tumblers``.
#: Retrieval tools auto-promoted to ``structured=True`` by the
#: default dispatcher. ``store_get_many`` is listed here (rather
#: than as a plain MCP tool) because plan steps feeding it into an
#: operator need ``{contents, missing}`` shape — the seeds would
#: otherwise have to thread ``structured=True`` into every YAML
#: hydration step.
_RETRIEVAL_TOOLS: frozenset[str] = frozenset(
    {"search", "query", "store_get_many",
     # RDR-156 P4 (nexus-joesk): combined-query primitives — structured=True so
     # $stepN.ids/tumblers resolve from the {ids, tumblers, distances, collections} shape.
     # search_aspect_scoped (RDR-156 Decision 5, nexus-ubnwk) is the fourth shape.
     "search_metadata_scoped", "search_topic_scoped", "search_graph_hop",
     "search_aspect_scoped"},
)

#: :data:`_RETRIEVAL_TOOLS` minus ``store_get_many`` (nexus-ivv4d code
#: review, T2 [24198]): the corpus/query provenance capture below
#: (``resolved_step_args``) exists to answer "what did this retrieval
#: step search" — ``store_get_many`` is a hydration call keyed on
#: ``ids``/``collections`` from a PRIOR step's output, never a
#: ``corpus``/``query`` argument of its own, so including it would
#: render a step basis line of ``corpus='' query=''`` that looks like a
#: missing-scope defect rather than the tool simply having no such args.
_CORPUS_QUERY_TOOLS: frozenset[str] = _RETRIEVAL_TOOLS - {"store_get_many"}

#: The :data:`_CORPUS_QUERY_TOOLS` that take a catalog ``subtree`` filter (RDR-232 section 2a,
#: nexus-jypmj.1.16). A caller's catalog-subtree scope reaches a step of one of these through that
#: argument, whatever its corpus; any other retrieval tool runs as before and the step is disclosed.
#: ``tests/test_rdr232_caller_scope.py`` pins this set to the tools' real signatures.
_SUBTREE_FILTER_TOOLS: frozenset[str] = frozenset({"query", "search_metadata_scoped"})

#: Args keys that may carry a collection name. The runner extracts
#: candidates from these to validate the cross-embedding guard, and
#: skips corpus injection when any of these are populated.
_COLLECTION_ARG_KEYS: tuple[str, ...] = ("collection", "collections")

#: Binding key used by ``nx_answer`` to propagate the caller-supplied
#: ``scope`` parameter through ``plan_run``. Nexus-zs1d Phase 1: gives
#: the runner a way to honour caller-supplied corpus intent without
#: requiring plan-library schema changes. RDR-232 section 2a
#: (nexus-jypmj.1.3): a corpus-prefix or collection scope NARROWS the
#: corpus a plan declares (a conflict skips the step); it fills in when the
#: plan declares none. A catalog-subtree scope is never a corpus
#: (nexus-jypmj.1.16): it filters a step through the tool's ``subtree``
#: argument (:func:`_apply_caller_scope_to_args`).
_CALLER_SCOPE_BINDING: str = "_nx_scope"

#: Last-resort ``corpus`` default for a ``search``/``query`` retrieval
#: step that reaches dispatch with no corpus pinned by the plan, no
#: ``scope.taxonomy_domain``, and no caller-supplied ``_nx_scope``
#: binding (RDR-200 Phase 1b, nexus-rl59s). Deliberately NOT ``"all"``:
#: ``"all"`` also admits the ``quarantine-*`` collections (see
#: ``nexus.mcp.core``'s corpus docs), which a fall-through default must
#: never reach into. Measured cause of the reach gap this closes: with
#: no default here the runner fell through to the bare MCP tool
#: default (``search`` = ``knowledge,code,docs``, ``query`` =
#: ``knowledge``), which omits ``rdr__`` entirely — plan-based
#: retrieval reached the caller's primary source on only 7-9 of 24
#: gate questions where a flat search naming this same four-prefix set
#: reached 24/24 (T2 ``nexus/gate-result-rdr200-phase1b-2026-09-01``).
_PLAN_STEP_DEFAULT_CORPUS: str = "knowledge,code,docs,rdr"


def _collections_in_args(args: dict[str, Any]) -> list[str]:
    """Pull every collection name out of the args dict for validation."""
    out: list[str] = []
    for key in _COLLECTION_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out


def _embedding_model_for(collection: str) -> str:
    """Resolve the embedding model used by *collection*.

    Lazy-imported from :mod:`nexus.corpus` so the runner module stays
    cheap to import. Raises ``ValueError`` if the collection name is
    unrecognised — the caller's responsibility to plumb a real name.
    """
    from nexus.corpus import index_model_for_collection  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    return index_model_for_collection(collection)


# ── Substitution ────────────────────────────────────────────────────────────

#: ``$var`` and ``$stepN.field`` are the only two reference forms.
#: ``$stepN`` (with no field) is intentionally NOT supported — every
#: downstream consumer must name the field it wants. This keeps the
#: contract observable and grep-able.
_VAR_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")
_STEPREF_RE = re.compile(r"^\$step(\d+)\.([A-Za-z_][A-Za-z0-9_]*)$")


#: Sentinel returned when a ``$stepN.field`` reference is DEFERRED — the
#: referenced step hasn't produced output yet because it's bundled
#: alongside the step doing the referencing. The bundle prompt composer
#: rewrites this into "the output from STEP M" in the prompt; the LLM
#: carries the chain internally. The KEY string lives in
#: :mod:`nexus.plans.bundle` (``DEFERRED_REF_KEY``) so a rename or typo
#: on either side can't silently drop the sentinel (substantive-critic C2).
from nexus.plans.bundle import DEFERRED_REF_KEY as _DEFERRED_REF_KEY, _extract_tool_name  # noqa: E402
import nexus.operators.dispatch as _dispatch_mod  # noqa: E402
from nexus.plans.evidence_diagnostics import (  # noqa: E402
    active_recorder,
    capture_reducer_input,
    note_reducer_input,
    set_recorder_step,
)
from nexus.plans.reserved_stream import (  # noqa: E402
    EVIDENCE_KEYS as _RESERVED_EVIDENCE_KEYS,
    ReservedStream,
    active_reserved_stream,
    reserved_scope,
    set_reserved_step,
)
from nexus.plans.repair_stream import (  # noqa: E402
    RepairSupplement,
    SupplementItem,
    active_supplement,
    set_supplement_step,
    supplement_scope,
)

#: RDR-232 Phase 3 (nexus-jypmj.3.1). A dispatcher carrying this attribute runs :func:`_hydrate_operator_args`
#: itself (the default dispatcher does). The coverage boundary has to read the terminal reduction's hydrated
#: input before the reduction runs, so for such a dispatcher the runner hydrates ONCE, hands over the prepared
#: args and sets :data:`_PREHYDRATED` for that one dispatch; the dispatcher then skips its own hydration. A
#: second hydration would fetch twice and merge the reserved stream twice. A dispatcher without the
#: attribute never hydrates (a test fake): it is handed the resolved args as it always was.
_HYDRATES_ATTR: str = "hydrates_operator_args"
_PREHYDRATED: ContextVar[bool] = ContextVar("nexus_operator_args_prehydrated", default=False)


def operator_args_prehydrated() -> bool:
    """Whether the dispatch in progress was handed args the runner already hydrated."""
    return _PREHYDRATED.get()


def _is_operator_error(exc: BaseException) -> bool:
    """Identify ``OperatorError`` from ``nexus.operators.dispatch`` even
    when its class identity drifts across test patches.

    Pre-fix the runner caught ``except _OperatorError`` where
    ``_OperatorError`` was bound at module import time. When
    ``test_nx_answer.py``'s ``patch.object(_dispatch_mod, "claude_dispatch", ...)``
    chains run, the dispatch module's attribute table can briefly
    expose a different ``OperatorError`` class identity (observed
    intermittently on the ubuntu-latest CI runner; a previously
    passing local suite reproduced the same fault under full-test-set
    ordering after the new operator-failure tests landed). Catching
    by name + module fingerprint is identity-drift-proof.
    """
    if isinstance(exc, _dispatch_mod.OperatorError):
        return True
    cls = type(exc)
    return cls.__name__ == "OperatorError" and cls.__module__ == "nexus.operators.dispatch"


# nexus-l0yh: sentinel that replaces a step's output when the
# operator dispatch raised :class:`OperatorError`. The runner stays
# alive, downstream ``$stepN.<field>`` references resolve to ``""``
# via the dict fall-through (no KeyError), and the final result
# carries enough breadcrumbs for the operator to triage in the
# logs. Mirrors the retrieval-tool short-circuit shape (e.g.
# catalog_not_initialized => ``{ids:[], error:...}``).
_OPERATOR_FAILED_SENTINEL_KEYS = ("error", "status", "tool", "step_index")
def _operator_failed_sentinel(
    *, tool: str, step_index: int, message: str,
) -> dict[str, Any]:
    return {
        "error": message,
        "status": "failed",
        "tool": tool,
        "step_index": step_index,
        # Common downstream fields tools usually return; substituting
        # empty values lets ``$stepN.text`` / ``$stepN.summary`` /
        # ``$stepN.aggregates`` style refs resolve to a falsy value
        # instead of raising PlanRunStepRefError.
        "text": "",
        "summary": "",
        "aggregates": [],
    }


def _operator_timeout_sentinel(
    *, tool: str, step_index: int, exc: "_dispatch_mod.OperatorTimeoutError",
) -> dict[str, Any]:
    """Sentinel for an operator step cut off by the a4 wall-clock budget.

    Unlike :func:`_operator_failed_sentinel`, this carries the
    RECONSTRUCTED partial content off ``OperatorTimeoutError`` (the a3
    precondition, nexus-h33x8.6 a4) so ``nx_answer`` can build a
    partial-answer marker instead of discarding whatever the subprocess
    produced before the budget ran out. Only reached when a ``deadline``
    is active — see the call sites in :func:`plan_run`.
    """
    return {
        "error": str(exc),
        "status": "timeout",
        "tool": tool,
        "step_index": step_index,
        "text": exc.partial_text,
        "summary": "",
        "aggregates": [],
        "partial_text": exc.partial_text,
        "event_count": exc.event_count,
    }


def _retrieval_timeout_sentinel(
    *, tool: str, step_index: int, budget_s: float,
) -> dict[str, Any]:
    """Sentinel for a retrieval step cut off by the wall-clock budget.

    Retrieval has no partial content to salvage (a search either returns
    its page or not), so ``text`` and ``partial_text`` are empty; the shape
    matches :func:`_operator_timeout_sentinel` so ``nx_answer`` builds the
    same ``[budget exhausted (time) ...]`` marker for either (nexus-5ezgn).
    """
    return {
        "error": f"{tool} step exceeded the remaining budget of {budget_s:.1f}s",
        "status": "timeout",
        "tool": tool,
        "step_index": step_index,
        "text": "",
        "summary": "",
        "aggregates": [],
        "partial_text": "",
        "event_count": 0,
    }


def _rollup_step_usage(entries: list) -> Any:
    """Reduce the ``DispatchUsage`` list captured via
    :func:`nexus.operators.dispatch.ambient_usage_sink` for ONE isolated
    or bundle-fallback step into a single usage record (RDR-196 .p1b
    Gap-1 addendum, nexus-nyry9.8).

    Zero entries: ``None`` — no claude -p call happened inside this step
    (or the operator's own SQL fast path served it). One entry: returned
    AS-IS. More than one entry (an operator that internally issues >1
    claude_dispatch call — none do today, but the mechanism must not
    silently drop or mis-divide a future one): the entries are SUMMED,
    never divided — unlike a fused bundle's ONE real dispatch, these are
    N SEPARATELY MEASURED real dispatches, so summing their real costs is
    honest, not fabricated.

    ``model`` here is THIS function's own agreement rule over the list of
    per-dispatch ``DispatchUsage`` entries — the shared canonical id if
    every entry's ``.model`` agrees, else ``None`` — which is a distinct
    axis from ``DispatchUsage.model``'s own internal selection among a
    SINGLE dispatch's ``modelUsage`` entries (see ``dispatch._parse_
    dispatch_usage`` and ``DispatchUsage.model``'s docstring). Before
    nexus-xepsr (2026-09-14), ``DispatchUsage.model`` was itself ``None``
    whenever one dispatch's ``modelUsage`` carried more than one entry, so
    the set-comprehension below (which filters out ``None`` before
    checking agreement) was effectively absorbing both rules into one:
    almost any multi-dispatch step's entries would each already be
    ``None``, so "do they agree" rarely had two real values to compare.
    Since nexus-xepsr, a single dispatch's ``.model`` is populated even
    when its own ``modelUsage`` had several entries (the highest-cost one
    wins), so this function's agreement check now runs against real,
    non-``None`` values far more often — and can therefore now RESOLVE a
    shared model across N separately-measured dispatches that previously
    collapsed to ``None`` purely because each dispatch's own CLI side-call
    entry made its individual ``.model`` ``None``. No change was needed to
    the logic below for this: the set-based agreement check was already
    correct — it only ever compares whatever ``.model`` values it is
    handed, and those values are simply populated more often now. Any
    field that is ``None`` on ANY entry makes the summed field ``None``
    too (never silently treat "unknown" as "zero" mid-sum). The per-model
    breakdown across entries is NOT preserved at StepRecord granularity —
    the flat schema matches ``.p1c``'s ``nx_answer_steps`` sketch (no
    ``model_usage`` column); this is a deliberate, stated roll-up, not a
    silent drop.
    """
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    models = {e.model for e in entries if e.model is not None}
    model = next(iter(models)) if len(models) == 1 else None

    def _sum(attr: str) -> Any:
        vals = [getattr(e, attr) for e in entries]
        if any(v is None for v in vals):
            return None
        return sum(vals)

    return _dispatch_mod.DispatchUsage(
        model=model,
        cost_usd=_sum("cost_usd"),
        input_tokens=_sum("input_tokens"),
        output_tokens=_sum("output_tokens"),
        cache_creation_input_tokens=_sum("cache_creation_input_tokens"),
        cache_read_input_tokens=_sum("cache_read_input_tokens"),
        duration_ms=_sum("duration_ms"),
        duration_api_ms=_sum("duration_api_ms"),
        num_turns=_sum("num_turns"),
        model_usage={},
    )


class _StepId(str):
    """A string that came out of a ``$stepN.ids`` / ``.tumblers`` reference inside a list arg
    (nexus-jypmj.4). It compares and serialises as the plain string; the type is the provenance, so
    evidence hydration can tell a chunk id from fetched text that merely looks like one."""

    __slots__ = ()


#: Step output fields that hold identifiers, never evidence text.
_ID_FIELDS: frozenset[str] = frozenset({"ids", "tumblers"})


def _is_id_field_ref(item: Any) -> bool:
    """True when *item* is a whole-value ``$stepN.ids`` / ``$stepN.tumblers`` reference."""
    if not isinstance(item, str):
        return False
    m = _STEPREF_RE.match(item)
    return m is not None and m.group(2) in _ID_FIELDS


def _resolve_value(
    value: Any,
    *,
    bindings: dict[str, Any],
    step_outputs: list[dict[str, Any]],
    deferred_step_indices: set[int] | None = None,
) -> Any:
    """Substitute one arg value.

    ``$var`` and ``$stepN.field`` substitutions only fire on values
    that are exactly that single token — no inline interpolation.
    Lists are resolved element-wise so callers can write e.g.
    ``seeds: [$step1.tumblers, $step2.tumblers]`` — each element
    resolves independently, list-valued elements are flattened one
    level so the final ``seeds`` is a flat list of tumblers. Non-list,
    non-string values pass through unchanged.

    *deferred_step_indices* names step indices (0-based) whose outputs
    won't exist on the host side because they're bundled alongside the
    step being resolved. ``$step{M+1}.field`` with ``M ∈ deferred``
    returns a sentinel marker the bundle composer translates into
    "from STEP (M+1)" prose. Non-deferred references resolve normally
    or raise if the target hasn't run.
    """
    if isinstance(value, list):
        resolved: list[Any] = []
        for item in value:
            r = _resolve_value(
                item, bindings=bindings, step_outputs=step_outputs,
                deferred_step_indices=deferred_step_indices,
            )
            if isinstance(r, list):
                if _is_id_field_ref(item):
                    r = [_StepId(x) if isinstance(x, str) else x for x in r]
                resolved.extend(r)
            else:
                resolved.append(r)
        return resolved

    if not isinstance(value, str):
        return value

    m = _STEPREF_RE.match(value)
    if m is not None:
        step_idx = int(m.group(1)) - 1
        field_name = m.group(2)
        if deferred_step_indices is not None and step_idx in deferred_step_indices:
            # Preserve the intent — the bundle composer will describe
            # this as chaining from STEP M's output in the prompt.
            return {
                _DEFERRED_REF_KEY: True,
                "step_index": step_idx,         # 0-based; +1 for display
                "field": field_name,
            }
        if step_idx < 0 or step_idx >= len(step_outputs):
            raise PlanRunStepRefError(
                ref=value,
                reason=f"step {step_idx + 1} has not produced output yet",
            )
        prior = step_outputs[step_idx]
        # Named error when the target slot is a bundled-intermediate
        # sentinel. Without this check the generic "output has no field"
        # message below surfaces the sentinel's internal keys
        # (``_bundled_intermediate``, ``_note``) which most YAML authors
        # will not recognize as a bundling artifact. (substantive-critic S6)
        if prior.get("_bundled_intermediate"):
            raise PlanRunStepRefError(
                ref=value,
                reason=(
                    f"step {step_idx + 1} is an intermediate inside an operator "
                    "bundle; its output is consumed inline by the next operator "
                    "and is not exposed on the host side. Reference the FINAL "
                    "step of the bundle instead, or pass `bundle_operators=False` "
                    "to plan_run to disable bundling for this invocation."
                ),
            )
        if field_name not in prior:
            raise PlanRunStepRefError(
                ref=value,
                reason=(
                    f"step {step_idx + 1} output has no field "
                    f"{field_name!r} (have: {sorted(prior.keys())})"
                ),
            )
        # nexus-onn7s: a ``$stepN.contents`` reference into an explicit
        # store_get_many step gets that step's source notes prefixed, the
        # same way auto-hydration prefixes them, so the pre-hydrated pipe
        # (plan 57 find-by-author's shape) reaches the operator annotated.
        # Any other field, or a step without notes, resolves verbatim.
        if field_name == "contents":
            notes = prior.get("source_notes")
            contents = prior[field_name]
            if isinstance(notes, list) and notes and isinstance(contents, list):
                from nexus.context_annotations import with_source_note  # noqa: PLC0415 — deferred; runner imports stay lazy

                return [
                    with_source_note(c, str(notes[i])) if i < len(notes) and isinstance(c, str) else c
                    for i, c in enumerate(contents)
                ]
        return prior[field_name]

    m = _VAR_RE.match(value)
    if m is not None:
        var_name = m.group(1)
        # Unknown $var stays as a literal — required-binding validation
        # runs upfront so we can trust everything left here is intentional.
        return bindings.get(var_name, value)

    return value


#: A ``$stepN.field`` token anywhere in a string (:data:`_STEPREF_RE` is the anchored, whole-value form).
_EMBEDDED_STEPREF_RE = re.compile(r"\$step\d+\.[A-Za-z_][A-Za-z0-9_]*")


def _find_unsubstitutable_step_ref(value: Any, *, in_dict: bool = False) -> str | None:
    """The first ``$stepN.field`` token in *value* that :func:`_resolve_value` would leave as text.

    It substitutes a string only when the whole string is the one token, and does not look inside a
    dict at all; lists resolve element-wise. So a token inside a longer string, or anywhere inside a
    dict (at any depth, whole-value or not), reaches the tool as the literal ``$stepN.field``.
    """
    if isinstance(value, str):
        found = _EMBEDDED_STEPREF_RE.search(value)
        if found is None or (not in_dict and _STEPREF_RE.match(value)):
            return None
        return found.group(0)
    if isinstance(value, list):
        children: Any = value
    elif isinstance(value, dict):
        children, in_dict = value.values(), True
    else:
        return None
    for child in children:
        if (token := _find_unsubstitutable_step_ref(child, in_dict=in_dict)) is not None:
            return token
    return None


def _reject_unsubstitutable_step_refs(raw_args: dict[str, Any], step_index: int | None) -> None:
    """Fail loud before dispatch on a step arg that holds a ``$stepN.field`` the runner cannot
    substitute (nexus-jypmj.1.19). Reads the RAW plan args, never the resolved ones: ``$intent``
    resolves to the caller's question, which may itself contain ``$step`` text."""
    for key, val in raw_args.items():
        token = _find_unsubstitutable_step_ref(val)
        if token is not None:
            where = "a step" if step_index is None else f"step {step_index + 1}"
            raise PlanRunStepRefError(
                ref=token,
                reason=(
                    f"{where} arg {key!r} holds it inside other text or an object, where it is "
                    "not substituted; an arg is exactly one $stepN.<field> reference or a list of them"
                ),
            )


def find_embedded_step_ref(plan: "str | dict[str, Any] | None") -> PlanRunStepRefError | None:
    """The first unsubstitutable ``$stepN.field`` in a whole plan, as the error ``plan_run`` would
    raise for it, else ``None`` (nexus-jypmj.4).

    The pre-run form of :func:`_reject_unsubstitutable_step_refs`, which it calls on each step's raw
    args: ``plan_save``, the plan matcher, ``nx plan hygiene`` and the inline planner's output all
    ask it rather than carry a second copy of the rule. *plan* is the ``plan_json`` text or its
    decoded mapping. Anything that is not a plan with a ``steps`` list of mappings returns ``None``:
    shape problems belong to the schema validator, not to this scan.
    """
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (TypeError, ValueError):
            return None
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return None
    for index, step in enumerate(steps):
        args = step.get("args") if isinstance(step, dict) else None
        if not isinstance(args, dict):
            continue
        try:
            _reject_unsubstitutable_step_refs(args, index)
        except PlanRunStepRefError as exc:
            return exc
    return None


def _resolve_args(
    args: dict[str, Any],
    *,
    bindings: dict[str, Any],
    step_outputs: list[dict[str, Any]],
    deferred_step_indices: set[int] | None = None,
    step_index: int | None = None,
) -> dict[str, Any]:
    _reject_unsubstitutable_step_refs(args, step_index)
    return {
        key: _resolve_value(
            val, bindings=bindings, step_outputs=step_outputs,
            deferred_step_indices=deferred_step_indices,
        )
        for key, val in args.items()
    }


def merge_bindings(
    default_bindings: dict[str, Any] | None,
    caller_bindings: dict[str, Any] | None,
) -> dict[str, Any]:
    """The plan-execution binding-precedence formula: caller wins over
    plan defaults.

    Extracted (nexus-nyry9.5, RDR-196 .r5 review-fix, code-review
    SIGNIFICANT, T2 review-nexus-nyry9.5) so this precedence lives in
    exactly ONE place. ``plan_run`` below computes its ``merged``
    binding dict via this function; ``nexus.mcp.core``'s ``nx_answer``
    single-query fast path (Step 2, which bypasses ``plan_run``
    entirely by design) used to hand-replicate
    ``{**default_bindings, **caller}`` as an independently-maintained
    second copy with no test cross-checking the two stayed in sync — a
    future edit to this precedence here would have silently NOT
    propagated to that copy. It now calls :func:`resolve_step_bindings`
    below, which calls this.
    """
    return {**(default_bindings or {}), **(caller_bindings or {})}


def resolve_step_bindings(
    raw_args: dict[str, Any],
    *,
    default_bindings: dict[str, Any] | None,
    caller_bindings: dict[str, Any] | None,
    step_outputs: list[dict[str, Any]] | None = None,
    deferred_step_indices: set[int] | None = None,
) -> dict[str, Any]:
    """Merge bindings (:func:`merge_bindings`) then resolve ONE step's
    raw ``args`` dict against them (:func:`_resolve_args`).

    ``plan_run`` itself doesn't need this exact shape — it merges once
    via :func:`merge_bindings` and resolves MANY steps against the same
    merged dict across the run, calling :func:`_resolve_args` directly
    at each call site. This is what a caller resolving exactly ONE
    step's args outside of a full ``plan_run`` invocation should call
    instead — currently ``nexus.mcp.core``'s ``nx_answer`` single-query
    fast path (nexus-nyry9.5, RDR-196 .r5 review-fix).

    Raises :class:`PlanRunUnresolvedVarError` on any ``$var`` in
    ``raw_args`` the merged bindings can't resolve (nexus-pucte). The
    single-query fast path bypasses :func:`plan_run` — and therefore
    its :func:`_validate_var_refs` pre-dispatch check — entirely by
    design, so without a guard HERE the exact same silent-literal-token
    bug the fast path was already fixed for once (nexus-nyry9.5,
    querying the literal string ``'$question'``) stays fully
    reproducible for any OTHER var a future single-query template might
    reference. Treats ``raw_args`` as a synthetic one-step plan
    (``step_index`` is always ``0`` in the raised error) — there is no
    ``scope`` at this call site, so the ``scope.topic`` half of
    ``_validate_var_refs`` never fires here.
    """
    merged = merge_bindings(default_bindings, caller_bindings)
    _validate_var_refs([{"args": raw_args}], merged)
    return _resolve_args(
        raw_args, bindings=merged, step_outputs=step_outputs or [],
        deferred_step_indices=deferred_step_indices,
    )


# ── Cross-embedding guard ───────────────────────────────────────────────────


def _check_embedding_domain(
    step_index: int,
    tool: str,
    scope: dict[str, Any] | None,
    args: dict[str, Any],
) -> None:
    """Raise if the step crosses the embedding boundary (SC-10)."""
    if not scope:
        return
    domain = scope.get("taxonomy_domain")
    if not domain:
        return
    if tool in _NON_EMBEDDING_TOOLS:
        return  # traverse: tumblers in, tumblers out, no embeddings.

    expected_model = _DOMAIN_TO_MODEL.get(domain)
    if expected_model is None:
        # Unknown domain — refuse. Better than silently letting through.
        raise PlanRunEmbeddingDomainError(
            step_index=step_index,
            declared_domain=str(domain),
            collection="<none>",
            actual_model="<unknown>",
        )

    for collection in _collections_in_args(args):
        try:
            actual = _embedding_model_for(collection)
        except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
            # Collection name didn't resolve — leave that for the
            # tool dispatcher to surface; the embedding guard is not
            # the place to enforce naming.
            continue
        if actual != expected_model:
            raise PlanRunEmbeddingDomainError(
                step_index=step_index,
                declared_domain=str(domain),
                collection=collection,
                actual_model=actual,
            )


# ── Step ``mode`` flag (nexus-h3e2) ───────────────────────────────────────
#
# Plan authors writing abstract / community-summary plans need to disable
# the per-corpus default threshold (e.g. 0.65 for prose) which is tuned
# for narrow-target search and drops 100% of candidates on broad-phrasing
# queries. Before this helper they had to memorise ``threshold: 2.0``
# (cosine distance maxes at 2.0, effectively no filter). The ``mode``
# field is the authoring affordance: ``mode: broad`` translates to
# ``threshold = 2.0`` at dispatch time. ``mode: narrow`` is the default
# and a no-op.
#
# Implemented as a runner-side argument-shaping step so the authoring
# concept stays out of the MCP search/query tool API. An explicit
# ``threshold`` in the same step wins (operator override).

_BROAD_THRESHOLD: float = 2.0
_VALID_MODES: frozenset[str] = frozenset({"narrow", "broad"})


def _apply_mode_to_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Map a plan-step ``mode`` field to concrete tool kwargs.

    * ``mode: broad`` on a retrieval tool sets ``threshold=2.0`` unless
      the step already specified one (operator override always wins).
    * ``mode: narrow`` is a no-op (the per-corpus default applies).
    * ``mode`` on a non-retrieval tool is silently dropped.
    * Unknown mode values log at warning and are dropped without
      changing the threshold (no silent typo masking).

    The ``mode`` key is consumed in every case so the dispatch
    kwarg-drop guard does not log it as an unhandled extra.
    """
    if "mode" not in args:
        return args
    out = dict(args)
    mode = out.pop("mode")
    if not isinstance(mode, str):
        _log.warning(
            "plan_step_mode_invalid_type",
            tool=tool, mode=mode, kind=type(mode).__name__,
        )
        return out
    mode = mode.strip().lower()
    if mode not in _VALID_MODES:
        _log.warning(
            "plan_step_mode_unknown",
            tool=tool, mode=mode, valid=sorted(_VALID_MODES),
        )
        return out
    if tool not in _RETRIEVAL_TOOLS:
        # mode applies to retrieval semantics only; drop quietly.
        return out
    if mode == "broad" and "threshold" not in out:
        out["threshold"] = _BROAD_THRESHOLD
    return out


# ── Scope forwarding (RDR-078 P2) ──────────────────────────────────────────


def _apply_scope_to_args(
    tool: str,
    scope: dict[str, Any] | None,
    args: dict[str, Any],
    *,
    bindings: dict[str, Any],
    step_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return *args* with ``scope.taxonomy_domain`` and ``scope.topic``
    forwarded into the dispatched tool call (RDR-078 P2 / SC-3).

    Behaviour:

      * ``scope.taxonomy_domain`` populates ``args["corpus"]`` with the
        prefix string for the domain — only when (a) the tool is an
        embedding-domain tool (``traverse`` is exempt — operates on
        tumblers) and (b) the caller hasn't already pinned a corpus
        or specific collection. Caller-pinned values always win; the
        SC-10 cross-embedding guard separately enforces consistency.
      * ``scope.topic`` is var/stepref-resolved and forwarded as
        ``args["topic"]`` when not already set by the caller.

    Returns a new dict; ``args`` is not mutated.
    """
    out = dict(args)
    if not scope:
        return out

    domain = scope.get("taxonomy_domain")
    if (
        domain
        and tool not in _NON_EMBEDDING_TOOLS
        and "corpus" not in out
        and not _collections_in_args(out)
    ):
        corpus = _DOMAIN_TO_CORPUS.get(str(domain))
        if corpus is not None:
            out["corpus"] = corpus

    topic = scope.get("topic")
    if topic is not None and "topic" not in out:
        out["topic"] = _resolve_value(
            topic, bindings=bindings, step_outputs=step_outputs,
        )

    return out


def _apply_caller_scope_to_args(
    tool: str,
    args: dict[str, Any],
    *,
    bindings: dict[str, Any],
) -> tuple[dict[str, Any], "CorpusNarrowing | None"]:
    """Apply the caller's ``scope`` (the ``_nx_scope`` binding) to a corpus-taking
    retrieval step's resolved *args*; returns ``(args, narrowing)``.

    nexus-zs1d Phase 1 filled ``corpus`` from the scope only when the plan left it
    unset, so a plan-declared corpus beat the caller. RDR-232 section 2a (nexus-jypmj.1.3)
    narrows it instead (:func:`nexus.plans.evidence.narrow_corpus`):

      * Only tools in :data:`_CORPUS_QUERY_TOOLS` are affected (``store_get_many`` takes no
        corpus; ``traverse`` and operators never did).
      * The step's corpus here is whatever it has after binding resolution and
        ``scope.taxonomy_domain`` (:func:`_apply_scope_to_args` runs first). None gives the
        scope, ``all`` gives the scope, entries under the scope (or a scope under an entry)
        give the narrower of the two, and no overlap is a ``conflict``: the returned args are
        the unchanged input and the CALLER skips the step. The step is never run unscoped.
      * A step pinned through ``collection``/``collections`` keeps today's behaviour (the plan
        wins) and is reported as ``unscoped_collection_pin``.
      * A catalog-subtree scope is a filter and never a corpus (nexus-jypmj.1.16; forwarded as
        ``corpus`` it resolved to no collection), so it does not depend on the step's corpus
        (none, ``all``, a list). On :data:`_SUBTREE_FILTER_TOOLS` (``query``,
        ``search_metadata_scoped``) it becomes the step's ``subtree`` argument
        (``subtree_filtered``) and the corpus stays what it was, or the default when it had none.
        A tool with no subtree filter runs unchanged and is reported ``subtree_not_applied``; so
        is a document-depth address (not a subtree filter) and a step that sets its own
        ``subtree``, which it keeps. :func:`nexus.plans.evidence.narrow_subtree` decides, and the
        ``single_query`` fast path in ``nx_answer`` calls the same function. A subtree never
        narrows by owner (nexus-jypmj.1.18).
      * No scope binding is a no-op and returns ``(args, None)``.

    The narrowing is ``None`` exactly when the scope did not reach the step. The same helper
    serves ``plan_run``'s isolated path and the continuation path
    (:func:`nexus.plans.continuation_envelope._compose_shape_b`).
    """
    if tool not in _CORPUS_QUERY_TOOLS:
        return args, None
    override = bindings.get(_CALLER_SCOPE_BINDING)
    if not override:
        return args, None
    from nexus.plans.evidence import (  # noqa: PLC0415 — deferred: keeps this hot-path module's import light
        CorpusNarrowing,
        ScopeConstraint,
        narrow_corpus,
        narrow_subtree,
    )

    scope = ScopeConstraint.parse(override)
    if _collections_in_args(args):
        plan_corpus = args.get("corpus")
        return args, CorpusNarrowing(
            "unscoped_collection_pin", plan_corpus or None, plan_corpus or None, scope,
        )
    if scope.kind == "subtree":
        # A subtree is a filter and never a corpus: the step's corpus (none, ``all`` or a list) is left as it is.
        narrowing, subtree = narrow_subtree(
            args.get("corpus"), scope, tool=tool, takes_subtree=tool in _SUBTREE_FILTER_TOOLS,
            own_subtree=str(args.get("subtree") or ""),
        )
        out = dict(args)
        if subtree:
            out["subtree"] = subtree
        if narrowing.status == "subtree_filtered" and tool == "search_metadata_scoped" and not out.get("corpus"):
            # the tool's own corpus default has no rdr; a plan step reads the same default ``query`` does
            out["corpus"] = _PLAN_STEP_DEFAULT_CORPUS
        return out, narrowing
    narrowing = narrow_corpus(args.get("corpus"), scope)
    if narrowing.conflict:
        return args, narrowing
    out = dict(args)
    if narrowing.corpus is not None:
        out["corpus"] = narrowing.corpus
    return out, narrowing


async def _apply_owner_narrowing_to_args(
    tool: str, args: dict[str, Any], *, bindings: dict[str, Any],
) -> tuple[dict[str, Any], "OwnerNarrowing | None"]:
    """Narrow a corpus-taking retrieval step to the request's explicit owners (RDR-232,
    nexus-jypmj.1.18); returns ``(args, narrowing)``.

    Runs after the section 2a scope narrowing and the default corpus, so it sees the corpus the step
    would actually search. The owners ride in the ``_nx_owners`` binding (set by ``nx_answer`` only for
    an explicit, resolved owner qualifier); without it, or for a tool outside
    :data:`_CORPUS_QUERY_TOOLS`, this is a no-op returning ``(args, None)``. See
    :func:`nexus.plans.owner_narrowing.narrow_args_by_owner` for the rule; a ``conflict`` is skipped
    by the caller exactly like a section 2a conflict.

    The corpus expansion reads the collection listing, which on a cold cache is a blocking T3 call, so
    it runs in a worker thread (a copy of the context goes with it); the no-owner path never leaves
    the loop.
    """
    owners = bindings.get(OWNERS_BINDING)
    if not owners or tool not in _CORPUS_QUERY_TOOLS:
        return args, None
    from nexus.plans.owner_narrowing import narrow_args_by_owner  # noqa: PLC0415 — deferred: keeps this hot-path module's import light

    return await asyncio.to_thread(narrow_args_by_owner, args, owners)


def _apply_traversal_policy_to_args(
    tool: str, args: dict[str, Any], *, bindings: dict[str, Any],
) -> dict[str, Any]:
    """Give a ``traverse`` step its evidence policy (RDR-232 section 3, nexus-jypmj.1.5).

    Every ``traverse`` step the runner dispatches carries a policy derived from the caller's scope
    (the ``_nx_scope`` binding; none means no containment) and the step's own link types. A policy
    the plan declared is replaced, never merged: the plan does not loosen the caller's scope.
    Any other tool is returned unchanged.
    """
    if tool != "traverse":
        return args
    from nexus.plans.traversal_policy import derive_policy  # noqa: PLC0415 — deferred: keeps this hot-path module's import light

    return {**args, "policy": derive_policy(
        args, bindings.get(_CALLER_SCOPE_BINDING), bindings.get(OWNERS_BINDING),
    )}


def _mark_reserved_step(index: int, steps: list[dict[str, Any]]) -> None:
    """Tell the reserved stream which plan step is about to hydrate, and which tools produced its
    evidence args (the ``$stepN`` refs in the step's raw args), so only a producer that keeps or
    reorders items can be blamed for a missing reserved item. The reducer-input recorder
    (nexus-jypmj.1.8) is told the step too. No-op without a stream or a recorder."""
    set_recorder_step(index)
    set_supplement_step(index)
    if active_reserved_stream() is None:
        return
    producers: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, str) and (m := _STEPREF_RE.match(value)):
            ref = int(m.group(1)) - 1
            if 0 <= ref < len(steps):
                producers.append(_extract_tool_name(steps[ref]))

    raw = steps[index].get("args", {}) or {}
    for key in _RESERVED_EVIDENCE_KEYS:
        _walk(raw.get(key))
    set_reserved_step(index, feeds_from=producers)


def _scope_conflict_output(
    narrowing: "CorpusNarrowing | OwnerNarrowing", *, step_index: int, tool: str,
) -> dict[str, Any]:
    """The output of a retrieval step skipped for a scope conflict (RDR-232 S2).

    The empty structured retrieval shape: every key ``query``/``search`` return in structured mode
    (``hybrid_scores``, ``also_in`` and ``also_in_ids`` included) plus the runner's ``contents``,
    so a later ``$stepN.<key>`` reference resolves to the empty value and never raises
    :class:`PlanRunStepRefError`; plus the ``scope_conflict`` marker carrying the plan's corpus
    and the caller's scope.
    """
    return {
        "ids": [], "tumblers": [], "distances": [], "hybrid_scores": [], "collections": [],
        "chunk_collections": [], "also_in": [], "also_in_ids": [], "chunk_text_hash": [],
        "contents": [],
        "scope_conflict": {
            "step_index": step_index,
            "tool": tool,
            "plan_corpus": narrowing.plan_corpus,
            "caller_scope": narrowing.scope.value,
            "scope_kind": narrowing.scope.kind,
        },
    }


#: Tools eligible for the :func:`_apply_default_corpus_to_args`
#: fall-through. Deliberately narrower than :data:`_RETRIEVAL_TOOLS`:
#: the catalog-routed combined-query tools (``search_metadata_scoped``,
#: ``search_topic_scoped``, ``search_graph_hop``, ``search_aspect_scoped``)
#: and ``store_get_many`` keep their own existing corpus/collection
#: semantics, and ``traverse`` is non-embedding — none of those five
#: benefit from (or should be forced into) the plain ``search``/``query``
#: prefix default.
_DEFAULT_CORPUS_TOOLS: frozenset[str] = frozenset({"search", "query"})


def _apply_default_corpus_to_args(
    tool: str, args: dict[str, Any],
) -> dict[str, Any]:
    """Return *args* with :data:`_PLAN_STEP_DEFAULT_CORPUS` filled in when
    a ``search``/``query`` step reaches dispatch with no corpus scoping
    at all (RDR-200 Phase 1b, nexus-rl59s).

    This is the LAST step in the corpus-resolution fall-through:
    plan-declared ``corpus``/``collection``/``collections`` win, then
    ``scope.taxonomy_domain`` (:func:`_apply_scope_to_args`), then the
    caller's ``_nx_scope`` binding (:func:`_apply_caller_scope_to_args`).
    Only when all three leave ``corpus`` unset does this helper apply —
    otherwise the step would fall through to the bare MCP tool default
    (``search`` = ``knowledge,code,docs``, ``query`` = ``knowledge``),
    which structurally excludes ``rdr__`` and was the measured cause of
    plan-based retrieval missing the caller's primary source on 7-9 of
    24 Phase 1b gate questions where a flat search reached 24/24 by
    naming this same four-prefix set (T2
    ``nexus/analysis-rdr200-phase1b-retrieval-reach-2026-09-01``).
    """
    if tool not in _DEFAULT_CORPUS_TOOLS:
        return args
    if "corpus" in args or _collections_in_args(args):
        return args
    out = dict(args)
    out["corpus"] = _PLAN_STEP_DEFAULT_CORPUS
    return out


# ── Bindings ────────────────────────────────────────────────────────────────


def _validate_bindings(match: Match, bindings: dict[str, Any]) -> None:
    missing = [
        name for name in match.required_bindings
        if name not in bindings
    ]
    if missing:
        raise PlanRunBindingError(missing=missing)


def _scan_var_refs(value: Any, found: set[str]) -> None:
    """Collect every ``$var`` token name referenced by *value* into
    *found*, IN PLACE.

    Mirrors :func:`_resolve_value`'s own traversal exactly — list
    elements are recursed into, a string is checked only when it is
    EXACTLY a ``$var`` token (no inline interpolation), and dict values
    are NOT recursed into. This keeps "what counts as a $var reference"
    identical between validation and resolution — anything this scan
    would miss is, by construction, also something the runtime never
    resolves (nexus-pucte scope: the two reference kinds this bug
    concerns are exactly the shapes ``_resolve_value`` substitutes).
    """
    if isinstance(value, list):
        for item in value:
            _scan_var_refs(item, found)
        return
    if isinstance(value, str):
        m = _VAR_RE.match(value)
        if m is not None:
            found.add(m.group(1))


def _scan_step_refs(value: Any, found: set[int]) -> None:
    """Collect every ``$stepN.<field>`` 0-based step index referenced by
    *value* into *found*, IN PLACE.

    Mirrors :func:`_resolve_value`'s own traversal exactly (see
    :func:`_scan_var_refs`'s docstring for the same rationale) — list
    elements are recursed into, a string is checked only when it is
    EXACTLY a ``$stepN.field`` token, and dict values are NOT recursed
    into.
    """
    if isinstance(value, list):
        for item in value:
            _scan_step_refs(item, found)
        return
    if isinstance(value, str):
        m = _STEPREF_RE.match(value)
        if m is not None:
            found.add(int(m.group(1)) - 1)


def _discarded_reduce_steps(
    steps: list[dict[str, Any]],
    step_outputs: list[dict[str, Any]],
    *,
    extract_tool: "Callable[[dict[str, Any]], str]",
    is_operator_tool: "Callable[[str], bool]",
) -> list[int]:
    """0-based indices of executed operator ("reduce") steps whose
    output is never referenced, directly or transitively, by the plan's
    terminal (last-executed) step (nexus-4h0oh).

    A parallel branch with no fan-in step folding it back in is exactly
    this: the runner retrieved and reduced the content, paid the real
    ``claude -p`` cost for it, and then ``PlanResult.final`` — always
    ``step_outputs[-1]`` — never surfaces it. RDR-200 Phase 1c q23/q24
    hit this live: three independent summarize steps under "return only
    the final step" dropped two of three.

    Computed by a backward reachability walk from the terminal step
    through ``$stepN.<field>`` references in each step's ORIGINAL
    (unresolved) ``args`` — the same reference shape
    :func:`_resolve_value` substitutes, scanned via
    :func:`_scan_step_refs`.

    Excludes:
      * the terminal step itself (trivially reachable from itself);
      * any step whose recorded output is the bundle-intermediate
        sentinel (``_bundled_intermediate``) — its content WAS
        consumed, inline, by the next operator in its own bundle, not
        dropped;
      * anything past what actually executed — a budget/continuation
        cut ending the run early is a different, already-visible
        condition (``PlanResult.budget_exhausted_at_step`` /
        ``continuation_cut_applied``), not evidence loss.
    """
    if not step_outputs:
        return []
    terminal_index = len(step_outputs) - 1
    reachable: set[int] = set()
    frontier = [terminal_index]
    while frontier:
        idx = frontier.pop()
        if idx in reachable or idx < 0 or idx >= len(step_outputs):
            continue
        reachable.add(idx)
        refs: set[int] = set()
        # _resolve_args applies _resolve_value PER ARG VALUE (see its
        # own body: ``{key: _resolve_value(val, ...) for key, val in
        # args.items()}``) — mirror that here rather than scanning the
        # whole args dict as one value, since _scan_step_refs (like
        # _resolve_value) does not recurse into dicts.
        for _arg_value in (steps[idx].get("args", {}) or {}).values():
            _scan_step_refs(_arg_value, refs)
        frontier.extend(r for r in refs if r not in reachable)
    reduce_indices = {
        i for i in range(len(step_outputs))
        if is_operator_tool(extract_tool(steps[i]))
        and not step_outputs[i].get("_bundled_intermediate")
    }
    return sorted(reduce_indices - reachable)


def _validate_var_refs(
    steps: list[dict[str, Any]], bindings: dict[str, Any],
) -> None:
    """Fail loud on any ``$var`` token a step's args reference that
    *bindings* (the merged ``{**default_bindings, **caller}`` map)
    cannot resolve — BEFORE any step dispatches (nexus-pucte).

    Closes the gap between the two reference kinds: ``$stepN.<field>``
    already raises :class:`PlanRunStepRefError` on an unresolved
    reference at DISPATCH time; an unresolved ``$var`` previously had no
    equivalent check at all and silently reached the tool as the literal
    token string. ``_validate_bindings`` above only checks
    ``match.required_bindings`` NAMES — it never looks at what a step's
    args actually reference, so an optional (or simply undeclared) var
    with no default and no caller value sailed through unnoticed. This
    walks every step once, mirroring the existing malformed-step
    validation loop in :func:`plan_run` (nexus-nyry9.4) that already
    runs a single pass over every step before the first dispatch — and
    reuses that loop's own :class:`PlanRunToolNotFoundError` for
    shape problems (non-dict ``args``) for the same reason: this file
    already treats that class as the general "malformed plan step,
    rejected before any dispatch" error, not a tool-lookup-specific one
    (see the ``_step is not a mapping`` check just above, in
    :func:`plan_run` itself).

    Also covers ``step.scope.topic`` (e.g. ``scope: {topic: $area}`` —
    a real pattern in the ``analyze-default``, ``document-default``, and
    ``research-default`` builtin templates). It is resolved through the
    SAME :func:`_resolve_value` ``$var`` branch as ``args``, via
    :func:`_apply_scope_to_args`, and forwarded into the dispatched
    call's ``args["topic"]`` — an unresolved var there is the identical
    silent-literal risk. ``scope.taxonomy_domain`` is never
    ``$var``-substituted (read as a literal domain name), so it is not
    scanned.

    ``_apply_scope_to_args`` only resolves ``scope.topic`` when the
    step's own ``args`` has NOT already set ``"topic"`` — a caller-set
    ``args["topic"]`` wins outright and ``scope.topic`` is never touched
    by :func:`_resolve_value` at all. This scan mirrors that exact
    precedence: it skips ``scope.topic`` whenever ``"topic"`` is already
    a key in ``args`` (checked on the RAW, pre-resolution args — key
    presence never changes across resolution, only values do, so this is
    equivalent to checking the resolved dict without needing one).
    Without this guard, a step shaped
    ``{"args": {"topic": "explicit"}, "scope": {"topic": "$unset"}}`` is
    a perfectly valid plan (``scope.topic`` is dead) that this validator
    would otherwise reject with a spurious hard failure.
    """
    for step_index, step in enumerate(steps):
        args = step.get("args") or {}
        if not isinstance(args, dict):
            raise PlanRunToolNotFoundError(
                tool="",
                reason=(
                    f"plan_json.steps[{step_index}] has non-dict args "
                    f"{args!r} ({type(args).__name__}) — malformed plan, "
                    "rejected before any step dispatched"
                ),
            )
        _reject_unsubstitutable_step_refs(args, step_index)
        found: set[str] = set()
        for val in args.values():
            _scan_var_refs(val, found)
        if "topic" not in args:
            scope = step.get("scope") or {}
            _scan_var_refs(scope.get("topic"), found)
        unresolved = found - bindings.keys()
        if unresolved:
            raise PlanRunUnresolvedVarError(
                step_index=step_index, var_name=sorted(unresolved)[0],
            )


# ── Default dispatcher (lazy MCP-tool wiring) ───────────────────────────────


#: Plan-step operator names → their MCP-tool counterparts in nexus.mcp.core.
#: Seed YAMLs use bare names (``tool: extract``, ``tool: rank``, etc.); the
#: dispatcher maps those to the async ``operator_*`` MCP tools registered
#: in RDR-079 P3. (Review flagged this as required for P4 to compose with
#: the scenario seeds shipped by RDR-078 P4b.)
_OPERATOR_TOOL_MAP: dict[str, str] = {
    "extract": "operator_extract",
    "rank": "operator_rank",
    "compare": "operator_compare",
    "summarize": "operator_summarize",
    "generate": "operator_generate",
    "filter": "operator_filter",
    "check": "operator_check",
    "verify": "operator_verify",
    "groupby": "operator_groupby",
    "aggregate": "operator_aggregate",
}

#: Maximum inputs to pass to an operator before auto-inserting a rank
#: winnow step. RDR-080 §Auto-hydration.
_OPERATOR_MAX_INPUTS: int = 100

#: Set of resolved operator tool names for auto-hydration detection.
_OPERATOR_RESOLVED_TOOLS: frozenset[str] = frozenset(_OPERATOR_TOOL_MAP.values())

#: Translation table for the ``inputs`` → operator-specific positional arg
#: rename (nexus-yis0). Pre-hydrated steps that passed ``$stepN.contents``
#: through ``inputs:`` get their value remapped to the operator's expected
#: arg name.
#:
#: Deliberately omitted (a stray ``inputs:`` on these operators must
#: surface as an authoring bug rather than be silently renamed):
#:
#: - ``operator_verify`` (RDR-088): takes scalar ``claim`` and ``evidence``.
#: - ``operator_aggregate`` (RDR-093): takes ``groups`` (a JSON-serialised
#:   list[{key_value, items}] from a prior groupby step), not ``items``.
#:   Renaming inputs->items here would silently dispatch with the wrong
#:   arg shape and make the resulting TypeError much harder to attribute.
#:   nexus-3j6b is the proper place to revisit cross-operator inputs.
#:
#: Hoisted to module scope per nexus-4o2z (RDR-088 Phase 1 gate
#: review observation).
_INPUTS_TARGET: dict[str, str] = {
    "operator_summarize": "content",
    "operator_generate": "context",
    "operator_rank": "items",
    "operator_compare": "items",
    "operator_filter": "items",
    "operator_check": "items",
    "operator_groupby": "items",
}


#: nexus-mm5tx (nexus-zekpl class): a catalog tumbler is 3+ dot-separated
#: non-negative integers (``store.owner.document[.chunk]``); a T3 chash is
#: 64 lowercase hex characters. A real sha256 chash containing only
#: decimal digits is astronomically unlikely (1 in 16**64), so this
#: heuristic has no practical false positive against real data.
_TUMBLER_ID_RE = re.compile(r"^\d+(?:\.\d+){2,}$")


def _is_tumbler_shaped(value: str) -> bool:
    """True when *value* looks like a catalog document tumbler rather
    than a T3 chunk chash. See :data:`_TUMBLER_ID_RE`."""
    return bool(_TUMBLER_ID_RE.match(value))


def _drop_step_ids_from_evidence(
    values: list[Any], *, tool: str, resolved_tool: str, branch: str,
) -> list[Any]:
    """Drop the chunk ids from an evidence list that also holds fetched text (nexus-jypmj.4).

    ``[$step3.contents, $step5.ids]`` flattens to fetched texts and ids. An id is an identifier,
    not evidence: only the ``ids=`` arg hydrates one, and that needs the paired ``collections`` a
    flat list no longer has (the default collection would hydrate a code or rdr chash to ""). So a
    list that mixes texts with ids loses the ids, as the pass-through loses empty strings, and says
    so. Provenance decides, not shape: only a string :func:`_resolve_value` marked as coming from an
    ``.ids`` / ``.tumblers`` reference is an id, so fetched text that reads like one ("1.2.3") is
    kept. A list of ids alone is the plan author's explicit choice and is returned as it is.
    """
    ids = [v for v in values if isinstance(v, _StepId)]
    if not ids or not any(v and not isinstance(v, _StepId) for v in values):
        return values
    kept = [v for v in values if not isinstance(v, _StepId)]
    _log.warning(
        "evidence_step_ids_dropped",
        tool=tool, resolved_tool=resolved_tool, branch=branch,
        dropped=len(ids), kept=len(kept), sample=[str(v)[:24] for v in ids[:3]],
    )
    return kept


#: The text of the terminal output when the coverage boundary skips the terminal reduction
#: (RDR-232 Phase 3, nexus-jypmj.3.1). The shape, the notice and the counters are nexus-jypmj.3.3's; the
#: marker key is what they key on.
_COVERAGE_SKIPPED_TEXT: str = "[generation skipped: no required facet has supporting evidence]"


def _coverage_skipped_output() -> dict[str, Any]:
    return {"text": _COVERAGE_SKIPPED_TEXT, "coverage_skipped": True}


def _hydrate_capturing(
    tool: str, args: dict[str, Any], *, capture: bool,
) -> tuple[str, dict[str, Any], list[list[str]]]:
    """:func:`_hydrate_operator_args`, and the lists its hydration reported when *capture* is set."""
    if not capture:
        resolved_tool, prepared = _hydrate_operator_args(tool, args)
        return resolved_tool, prepared, []
    with capture_reducer_input() as captured:
        resolved_tool, prepared = _hydrate_operator_args(tool, args)
    return resolved_tool, prepared, captured


def _boundary_evidence(args: dict[str, Any], captured: list[list[str]]) -> tuple[list[str], bool]:
    """``(texts, observed)``: what an operator step is handed as evidence, for the coverage boundary.

    The last list the step's hydration reported (:func:`~nexus.plans.evidence_diagnostics.capture_reducer_input`)
    is the exact input, reserved stream merged and cap applied. With none (a dispatcher that does not
    hydrate, or an arg the hydration does not handle) the evidence args are read as they stand: a list of
    texts, a JSON list of texts or items, or one text (a condensed earlier output). A deferred bundle ref
    and a chunk id are not evidence. ``observed`` is False only when the step has no evidence arg at all.
    """
    if captured:
        return list(captured[-1]), True
    texts: list[str] = []
    observed = False
    for key in _RESERVED_EVIDENCE_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.lstrip().startswith("["):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                value = [p.get("text") if isinstance(p, dict) else p for p in parsed]
        if isinstance(value, list):
            observed = True
            texts.extend(v for v in value if isinstance(v, str) and v and not isinstance(v, _StepId))
        elif isinstance(value, str) and value.strip() and not isinstance(value, _StepId):
            observed = True
            texts.append(value)
    return texts, observed


#: nexus-mm5tx follow-up (code-review T2 [24199]): fixed, machine-
#: recognisable prefix for the short-circuit result text emitted when
#: an operator's hydrated evidence filters to zero items (either the
#: ids-auto-hydration branch or the nexus-yis0 pass-through branch).
#: A downstream classifier (e.g. answer-runs) keys on this constant
#: instead of parsing an operator's own free-text refusal prose —
#: compare's "No comparison performed" was the live q12 symptom this
#: closes. Kept as a bare prefix (not a full templated sentence) so a
#: caller can reliably ``str.startswith`` it regardless of which
#: operator or how many items were filtered.
_NO_EVIDENCE_RESULT_PREFIX = "No evidence to reduce:"


def _no_evidence_result_text(*, tool: str, filtered_count: int) -> str:
    """Build the short-circuit result text for a zero-evidence hydration.

    *filtered_count* is the number of hydrated items that were
    attempted and came back empty (never a fabricated 0 when the count
    is genuinely unknown — every call site below only calls this when
    it has a real count in scope). Step-index attribution is
    deliberately NOT baked into this text: neither
    ``_hydrate_operator_args`` nor ``_default_dispatcher`` knows the
    plan-step index (the same limitation documented on
    ``PlanRunOperatorArgMissingError``'s call site), and ``plan_run``'s
    own per-step ``nx_answer_step_start``/``nx_answer_step_complete``
    structlog events already carry it — a caller correlates by log
    timestamp/step context rather than by re-parsing this string.
    """
    return (
        f"{_NO_EVIDENCE_RESULT_PREFIX} tool={tool}, "
        f"{filtered_count} hydrated item(s) filtered to 0 before dispatch"
    )


def _hydrate_tumbler_ids(tumbler_ids: list[str]) -> dict[str, Any]:
    """Resolve document tumblers to T3 content via the catalog manifest
    (nexus-mm5tx / nexus-zekpl class).

    ``search_metadata_scoped`` / ``search_graph_hop`` /
    ``search_aspect_scoped`` return document-level results keyed by
    TUMBLER, not chash — feeding those ids straight into the chash-keyed
    ``store_get_many`` hydrates every row to an empty string. This
    resolves each tumbler's ``document_chunks`` manifest via the catalog
    (chash + collection per chunk, position-ordered), batch-hydrates
    every real chash through the existing ``store_get_many``, then
    reassembles each document's content by joining its own chunks back
    together in order — the full document, not just one matched
    snippet.

    Returns the same ``{contents, missing}`` shape ``store_get_many``
    returns (aligned 1:1 with *tumbler_ids*), so it slots into the same
    ``non_empty`` filtering the caller already does for the chash path.
    """
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.mcp_infra import get_catalog as _get_catalog  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    cat = _get_catalog()
    if cat is None:
        # code-review finding (T2 [24200]): get_catalog() is documented
        # to return None when the catalog isn't initialized. An
        # unguarded .get_manifests() call here would raise
        # AttributeError, which _is_operator_error does not recognize —
        # it would escape both the isolated dispatch path and the
        # bundle-composition path (which sits outside any try/except)
        # and crash the whole plan_run. Mirror core.py's own
        # `if cat is None:` guard (e.g. query()'s catalog-param branch)
        # and degrade to the same empty-content shape the zero-
        # manifest-rows case below already returns.
        _log.warning(
            "hydrate_tumbler_ids_catalog_unavailable",
            tumbler_count=len(tumbler_ids),
        )
        return {"contents": ["" for _ in tumbler_ids], "missing": list(tumbler_ids)}
    manifests = cat.get_manifests(tumbler_ids)

    # nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, review round 3):
    # best-effort non_evidentiary-stamp exclusion — one extra batched
    # round trip (``resolve_many``, the same batched primitive
    # ``search_engine.py``'s ``_attach_display_paths`` uses) alongside the
    # ``get_manifests`` call this function already makes. A
    # ``resolve_many`` failure, or a tumbler it cannot resolve, degrades
    # to "no stamp for this id" — never raises, never excludes a document
    # it cannot classify (see ``_entry_is_non_evidentiary``).
    try:
        catalog_entries = cat.resolve_many(tumbler_ids)
    except Exception:  # noqa: BLE001 — best-effort exclusion; a lookup failure must degrade to "leave every candidate in", never crash the hydration
        _log.warning(
            "hydrate_tumbler_ids_resolve_many_failed",
            tumbler_count=len(tumbler_ids),
        )
        catalog_entries = {}
    excluded_doc_ids = {
        doc_id for doc_id, entry in catalog_entries.items()
        if _entry_is_non_evidentiary(entry)
    }

    flat_ids: list[str] = []
    flat_collections: list[str] = []
    owner: list[str] = []
    # nexus-tko2w (RDR-108): the label of a chunk is a fact about its position in ONE
    # document. This path reads each document's own manifest, so it has the document: the
    # row's ``section_type`` is that document's label, where the chunk row's (read below
    # through store_get_many) is whichever document wrote it last. An entry is ``None`` for a
    # row written before the manifest carried labels (``labels_written`` false: the hydrated
    # value decides, as it always did); otherwise the document's own label, "" included.
    flat_manifest_types: list[str | None] = []
    for doc_id in tumbler_ids:
        if doc_id in excluded_doc_ids:
            # Contributes no chunks -- per_doc[doc_id] stays empty, so
            # the existing contents/missing construction below already
            # reports it exactly like any other unusable document.
            continue
        rows = manifests.get(doc_id) or []
        for row in sorted(rows, key=lambda r: r.position):
            if not row.collection:
                # code-review finding (T2 [24200]): log the skip rather
                # than dropping it silently, mirroring the sibling
                # drop-path logging convention elsewhere in this
                # function family (e.g. auto_hydration_overflow) — a
                # developer debugging thin document content should not
                # have to guess why a chunk went missing.
                _log.warning(
                    "hydrate_tumbler_ids_row_missing_collection",
                    doc_id=doc_id, chash=row.chash, position=row.position,
                )
                continue
            flat_ids.append(row.chash)
            flat_collections.append(row.collection)
            owner.append(doc_id)
            flat_manifest_types.append(
                (getattr(row, "section_type", None) or "")
                if getattr(row, "labels_written", False) else None
            )

    if not flat_ids:
        return {"contents": ["" for _ in tumbler_ids], "missing": list(tumbler_ids)}

    # The notes below come from each document's own catalog entry, so the chunk-level
    # ``source_notes`` (an owner ``resolve_many`` over the same documents) is not asked for.
    with mcp_core.without_source_notes():
        hydrated = mcp_core.store_get_many(
            ids=flat_ids, collections=flat_collections, structured=True,
        )
    chunk_contents = hydrated.get("contents", []) if isinstance(hydrated, dict) else []
    # nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, Sam's decision 3):
    # ``section_types`` rides the SAME store_get_many call above -- no
    # extra round trip. A package/import-only chunk (``section_type ==
    # "imports"``, stamped at index time by
    # ``nexus.code_indexer._is_import_only_chunk``) is structural, not
    # evidentiary; drop it from the reassembled document exactly like an
    # empty content string already is below. Missing/malformed
    # ``section_types`` (an older cached hydration shape, a test double
    # stubbing only ``contents``) degrades to "" per position -- never
    # excludes a chunk it cannot classify.
    chunk_section_types = (
        hydrated.get("section_types", []) if isinstance(hydrated, dict) else []
    )
    per_doc: dict[str, list[str]] = {doc_id: [] for doc_id in tumbler_ids}
    # nexus-onn7s: a document's note is prefixed once on its reassembled text.
    # nexus-6pfgb.11: it is built from THIS document's own catalog entry, never
    # from the chunk's ``source_notes`` line: that line speaks for a bare chunk
    # id, and a chunk two documents share would name the same document (or no
    # field at all) for both. The collection is the first kept chunk's.
    doc_collection: dict[str, str] = {}
    for i, (doc_id, content) in enumerate(zip(owner, chunk_contents)):
        if not content:
            continue
        section_type = flat_manifest_types[i]
        if section_type is None:
            section_type = chunk_section_types[i] if i < len(chunk_section_types) else ""
        if section_type == "imports":
            continue
        per_doc[doc_id].append(content)
        doc_collection.setdefault(doc_id, flat_collections[i])

    from nexus.context_annotations import (  # noqa: PLC0415 — deferred; runner imports stay lazy
        agreed_facts,
        entry_facts,
        source_note,
        with_source_note,
    )

    def _doc_note(doc_id: str) -> str:
        # A document the catalog did not resolve (the lookup failed, or it is
        # gone) gets no note: nothing here may speak for it, and the chunk
        # rows' values are the last writer's.
        entry = catalog_entries.get(doc_id) if isinstance(catalog_entries, dict) else None
        if entry is None:
            return ""
        return source_note(agreed_facts([entry_facts(entry)]), collection=doc_collection.get(doc_id, ""))

    contents = [
        with_source_note("\n\n".join(per_doc[doc_id]), _doc_note(doc_id) if per_doc[doc_id] else "")
        for doc_id in tumbler_ids
    ]
    missing = [doc_id for doc_id in tumbler_ids if not per_doc[doc_id]]
    return {"contents": contents, "missing": missing}


#: RDR-200 Phase 1c evidence hygiene (nexus-4jj40). Review round 3
#: replaces round 2's hard-coded source_uri path-prefix rule (a global
#: ``"/tests/"`` marker drops any conexus USER's real test-file evidence
#: in ANY indexed repo, not just this one's) with a per-document catalog
#: stamp: :data:`nexus.catalog.types.NON_EVIDENTIARY_META_KEY` in
#: ``Document.meta``. An operator sets it via the EXISTING
#: ``nx catalog update --meta '{"non_evidentiary": true}'`` command (no
#: new CLI); ``nx index repo`` auto-stamps it at register time ONLY for
#: files under ``tests/fixtures/`` (fixture DATA, never a sibling test
#: MODULE — see ``nexus.catalog.types.is_fixture_path``'s docstring;
#: ``nexus.indexer._register_time_meta`` is the write side). A document
#: with no resolvable catalog entry, or whose ``meta`` carries no stamp
#: at all, is left in untouched — best-effort exclusion, never a silent
#: drop of evidence this cannot classify.
#:
#: Round 1 of this bead's review shipped a category-based rule (drop
#: "code" whenever "paper" shared the batch); round 2 shipped the
#: source_uri path-prefix rule above. Both removed entirely: category
#: dropped EVERY code-category candidate whenever a paper candidate
#: shared the batch (breaking a legitimate "paper's algorithm plus its
#: implementation" plan); source_uri's global "/tests/" marker is unsafe
#: for any OTHER conexus user's repo, not just this one's fixtures.


def _entry_is_non_evidentiary(entry: Any) -> bool:
    """True when *entry* (a ``CatalogEntry``) carries the non_evidentiary
    stamp in its ``meta`` dict. A missing/malformed ``meta`` is never
    non-evidentiary — a caller with no resolvable stamp must leave the
    candidate in, never exclude it."""
    from nexus.catalog.types import NON_EVIDENTIARY_META_KEY  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    meta = getattr(entry, "meta", None)
    if not isinstance(meta, dict):
        return False
    return bool(meta.get(NON_EVIDENTIARY_META_KEY))


def _resolve_non_evidentiary_chashes(chashes: list[str]) -> set[str]:
    """Best-effort, batched chash -> "belongs to a non_evidentiary-
    stamped document" set, for the chash-shaped hydration path (plain
    ``search()`` results).

    Two batched round trips total for the WHOLE input list, never one
    per chash: ``docs_for_chashes`` (chash -> [doc_id, ...], the same
    primitive ``search_engine.py``'s ``_attach_doc_ids_from_catalog``
    already uses) then ``resolve_many`` (doc_id -> ``CatalogEntry``, the
    same primitive :func:`_hydrate_tumbler_ids` uses for the tumbler-
    shaped path). Either call failing, the catalog being unavailable, or
    *chashes* being empty degrades to an empty set — never raises, never
    excludes a chash it cannot classify.
    """
    from nexus.mcp_infra import get_catalog as _get_catalog  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    if not chashes:
        return set()
    cat = _get_catalog()
    if cat is None:
        return set()
    try:
        chash_to_docs = cat.docs_for_chashes(chashes)
        if not isinstance(chash_to_docs, dict) or not chash_to_docs:
            return set()
        all_doc_ids = sorted({
            doc_id for docs in chash_to_docs.values() for doc_id in (docs or [])
        })
        if not all_doc_ids:
            return set()
        entries = cat.resolve_many(all_doc_ids)
        if not isinstance(entries, dict):
            return set()
    except Exception:  # noqa: BLE001 — best-effort exclusion; ANY failure here (a raised exception or a malformed/unconfigured catalog return) must degrade to "leave every candidate in", never crash the hydration
        _log.warning(
            "resolve_non_evidentiary_chashes_failed",
            chash_count=len(chashes),
        )
        return set()
    non_evidentiary_doc_ids = {
        doc_id for doc_id, entry in entries.items()
        if _entry_is_non_evidentiary(entry)
    }
    if not non_evidentiary_doc_ids:
        return set()
    return {
        chash for chash, docs in chash_to_docs.items()
        # nexus-4jj40 (review round 4): all(), not any() -- a chash
        # SHARED by a stamped and an unstamped document (identical
        # chunk text indexed in more than one document, RDR-180) is
        # excluded only when EVERY owning document is stamped. any()
        # would drop a genuinely evidentiary chash just because it
        # happens to ALSO belong to one non_evidentiary document.
        # all() on an empty (docs or []) is vacuously True in Python,
        # but that can never fire here: chash_to_docs only ever
        # contains keys docs_for_chashes itself returned with at
        # least one doc_id, never an empty list.
        if docs and all(doc_id in non_evidentiary_doc_ids for doc_id in docs)
    }


def _import_only_ids_from_hydrated(
    hydrated: Any, id_list: list[Any],
) -> set[str]:
    """Return the subset of *id_list* whose hydrated chunk carries the
    import-only ``section_type`` stamp (RDR-200 Phase 1c evidence
    hygiene, nexus-4jj40 Sam's decision 3): a chunk consisting only of a
    package/module declaration plus import statements
    (``nexus.code_indexer._is_import_only_chunk``, index time) is
    structural, not evidentiary -- near-identical header boilerplate
    embeds close together across unrelated, self-indexed repos and
    crowds out real matches.

    ``section_types`` rides the SAME ``store_get_many`` fetch *hydrated*
    already came from (see that function's ``structured=True`` return) --
    zero extra round trips, unlike :func:`_resolve_non_evidentiary_chashes`
    which needs its own catalog lookups because the non_evidentiary stamp
    lives on the catalog Document, not the T3 chunk.

    Degrades to an empty set for anything it cannot classify: a
    non-dict *hydrated* (a raw string, an error payload), a missing or
    malformed ``section_types`` list (an older cached hydration shape, a
    test double stubbing only ``contents``/``missing``) -- never raises,
    never excludes a chash it cannot classify.
    """
    if not isinstance(hydrated, dict):
        return set()
    section_types = hydrated.get("section_types")
    if not isinstance(section_types, list):
        return set()
    return {
        str(doc_id) for doc_id, st in zip(id_list, section_types)
        if st == "imports"
    }


def _drop_intent_from_list_args(
    args: dict[str, Any], *, intent: Any,
) -> dict[str, Any]:
    """Drop any list-arg element equal to the caller's raw *intent* text.

    RDR-200 Phase 1c evidence hygiene (nexus-4jj40): a composed or
    ad-hoc-grown plan step can reference ``$intent`` inside an
    items/inputs-shaped list arg to give an operator step (rank,
    compare, ...) context -- e.g.
    ``{"items": ["$intent", "$step2.contents"]}``. Once ``$intent``
    resolves (``plan_run``'s ``_resolve_args``), that list contains the
    literal question STRING sitting beside real retrieved evidence, and
    an items-consuming operator has no way to tell "this is context" from
    "this is a candidate" -- it ranks/compares the question against
    itself. Measured: the question string appeared as a rank candidate in
    5 of 15 composed Phase 1c runs (T2 [24082]).

    A flat membership check against every list-valued arg in *args* -- no
    tool-name special-casing, no additional round trip. A non-string or
    empty *intent* is a no-op (``$intent`` unbound, or bound to something
    that could never collide with real evidence).
    """
    if not isinstance(intent, str) or not intent:
        return args
    out = dict(args)
    for key, value in args.items():
        if isinstance(value, list) and intent in value:
            out[key] = [v for v in value if v != intent]
    return out


def _merge_reserved_explicit_inputs(
    value: list[Any], args: dict[str, Any], tool: str, resolved_tool: str,
) -> list[Any]:
    """Merge the reserved stream ahead of an operator's explicit ``inputs`` list and cap the merged
    list the way the ``ids`` branch does (RDR-232, nexus-jypmj.1.6).

    The cap runs ONLY when a stream or a repair supplement merges (S4): a run without either keeps
    today's uncapped inputs. A cap that fires sets ``args["_truncation_metadata"]``. Returns *value*
    unchanged when there is neither or each has already merged at an earlier step.
    """
    rs, supp = active_reserved_stream(), active_supplement()
    if rs is not None and not rs.should_merge():
        rs = None
    if supp is not None and not supp.should_merge():
        supp = None
    if rs is None and supp is None:
        return value
    if supp is not None:
        value = supp.merge(value)  # RDR-232 3.2: repair evidence ahead of the plan's own items ...
    if rs is not None:
        value = rs.merge(value)  # ... and the reserved stream ahead of both
    if len(value) > _OPERATOR_MAX_INPUTS:
        if rs is not None:
            rs.note_cap(len(value), _OPERATOR_MAX_INPUTS)
        _log.warning(
            "explicit_inputs_overflow_with_reserved_stream",
            tool=tool, resolved_tool=resolved_tool,
            input_count=len(value), max_inputs=_OPERATOR_MAX_INPUTS,
        )
        args["_truncation_metadata"] = {
            "truncated": True, "original_count": len(value), "kept_count": _OPERATOR_MAX_INPUTS,
        }
        value = value[:_OPERATOR_MAX_INPUTS]
    return value


def _hydrate_operator_args(
    tool: str, args: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Run auto-hydration and arg-name translation for an operator call.

    Shared between :func:`_default_dispatcher` (isolated operator steps)
    and the bundle execution path. Takes the plan-step ``tool`` name
    (bare or ``operator_*``) and returns ``(resolved_tool, prepared_args)``
    ready for either ``claude_dispatch`` (bundled) or direct MCP-tool call
    (isolated). Non-operator tools pass through untouched.

    Hydration rules (RDR-080 Option C):
      * operator tool + ``ids`` in args → call ``store_get_many``, replace
        ``ids``/``collections`` with the operator's expected content arg
      * ``_OPERATOR_MAX_INPUTS`` positional cap
      * RF-13: ``template`` dict → ``fields`` CSV for extract
      * list-valued ``content``/``context`` joined for summarize/generate
    """
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    resolved_tool = _OPERATOR_TOOL_MAP.get(tool, tool)
    # RDR-232 (nexus-jypmj.1.20): set once the explicit-``inputs`` pass-through below has merged the
    # reserved stream and recorded the evidence, so the Pattern A site does not merge it again (a
    # second merge would count the primary items as deduplicated secondary ones).
    _evidence_handled = False

    # nexus-mm5tx follow-up (code-review T2 [24199]): an EXPLICIT plan
    # step whose tool is literally "store_get_many" is a DIFFERENT code
    # path from the operator ids-auto-hydration branch below —
    # "store_get_many" is never in _OPERATOR_RESOLVED_TOOLS, so the
    # tumbler routing added just above for the operator path never
    # fired for it. This is gate q12's ACTUAL repro shape: an explicit
    # store_get_many step hydrates tumbler ids to "" directly, and the
    # nexus-yis0 pass-through further down then filters those to
    # items == [], and compare refuses. Route the SAME way: when the
    # step's own ids are confidently tumbler-shaped, precompute the
    # {contents, missing} result via the catalog manifest and stash it
    # under a marker _default_dispatcher checks immediately after
    # calling this function — short-circuiting the real store_get_many
    # dispatch entirely, matching store_get_many's own documented
    # structured=True return shape so no caller sees a difference.
    # store_get_many is never a bundleable operator (BUNDLEABLE_OPERATORS
    # doesn't include it), so this never fires inside the bundle-
    # composition path.
    if resolved_tool == "store_get_many" and "ids" in args:
        ids = args["ids"]
        id_list_raw = ids if isinstance(ids, list) else [ids]
        if id_list_raw and all(_is_tumbler_shaped(str(i)) for i in id_list_raw):
            _log.info(
                "store_get_many_tumbler_route",
                tool=tool, id_count=len(id_list_raw),
            )
            return resolved_tool, {
                "_precomputed_result": _hydrate_tumbler_ids(
                    [str(i) for i in id_list_raw],
                ),
            }

    if resolved_tool in _OPERATOR_RESOLVED_TOOLS and "ids" in args:
        ids = args["ids"]
        collections = args.get("collections", "knowledge")
        id_list_raw = ids if isinstance(ids, list) else [ids]
        # nexus-mm5tx (nexus-zekpl class): a tumbler-only id list is
        # document-level output from search_metadata_scoped /
        # search_graph_hop / search_aspect_scoped — route it through
        # the catalog manifest instead of the chash-keyed
        # store_get_many, which would hydrate every id to "".  A mixed
        # or empty list is not confidently tumbler-shaped and falls
        # through to the direct path unchanged.
        if id_list_raw and all(_is_tumbler_shaped(str(i)) for i in id_list_raw):
            _log.info(
                "auto_hydration_tumbler_route",
                tool=tool, resolved_tool=resolved_tool,
                id_count=len(id_list_raw),
            )
            hydrated = _hydrate_tumbler_ids([str(i) for i in id_list_raw])
            excluded_ids: set[str] = set()
        else:
            hydrated = mcp_core.store_get_many(
                ids=ids, collections=collections, structured=True,
            )
            # nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, review
            # round 3): the chash-shaped path -- the far more common
            # shape (plain search() results) round 2 left uncovered.
            # docs_for_chashes + resolve_many, one batched call each
            # (never one per chash), mirroring _hydrate_tumbler_ids'
            # own resolve_many use for the tumbler-shaped path.
            excluded_ids = _resolve_non_evidentiary_chashes(
                [str(i) for i in id_list_raw],
            )
            # nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, Sam's
            # decision 3): import-only chunks, resolved from the
            # section_types the store_get_many call above already
            # fetched -- no extra round trip.
            excluded_ids |= _import_only_ids_from_hydrated(hydrated, id_list_raw)
        contents_raw = hydrated.get("contents", []) if isinstance(hydrated, dict) else []
        if excluded_ids:
            contents = [
                "" if str(cid) in excluded_ids else c
                for cid, c in zip(id_list_raw, contents_raw)
            ]
        else:
            contents = list(contents_raw)
        # nexus-onn7s: prefix each kept chunk with its source note so the
        # operator prompt sees what the search render shows a person:
        # document, collection, indexed date, publication year, expiry.
        # The tumbler path above already prefixed per document. A missing
        # or short ``source_notes`` list leaves the content untouched.
        notes = hydrated.get("source_notes", []) if isinstance(hydrated, dict) else []
        if isinstance(notes, list) and notes and not (
            id_list_raw and all(_is_tumbler_shaped(str(i)) for i in id_list_raw)
        ):
            from nexus.context_annotations import with_source_note  # noqa: PLC0415 — deferred; runner imports stay lazy

            contents = [
                with_source_note(c, str(notes[i])) if i < len(notes) else c
                for i, c in enumerate(contents)
            ]
        # RDR-232 (nexus-jypmj.1.6): the reserved primary stream enters ahead of the plan's own
        # items, so the positional cap below cuts only secondary ones. A vector hit that is a
        # reserved chunk (same chash) is dropped; the primary copy keeps its attribution header.
        _rs = active_reserved_stream()
        _rs_merge = _rs is not None and _rs.should_merge()
        if _rs_merge:
            _rs_dups = [k for k, (cid, c) in enumerate(zip(id_list_raw, contents)) if c and _rs.is_primary_chash(str(cid))]
            for k in _rs_dups:
                contents[k] = ""
        non_empty = [c for c in contents if c]
        _supp = active_supplement()
        if _supp is not None and _supp.should_merge():
            non_empty = _supp.merge(non_empty)  # RDR-232 3.2: repair evidence ahead of the plan's own items
        if _rs_merge:
            non_empty = _rs.merge(non_empty, pre_deduped=len(_rs_dups))
        original_count = len(non_empty)
        truncation_metadata: dict[str, Any] | None = None
        if original_count > _OPERATOR_MAX_INPUTS:
            if _rs_merge:
                _rs.note_cap(original_count, _OPERATOR_MAX_INPUTS)
            _log.warning(
                "auto_hydration_overflow",
                tool=tool, resolved_tool=resolved_tool,
                input_count=original_count, max_inputs=_OPERATOR_MAX_INPUTS,
                action="positional truncation to max_inputs",
            )
            # RDR-093 S-1 + nexus-3j6b: when the cap fires the runner
            # surfaces a {truncated, original_count, kept_count} block
            # on the operator's return envelope so plan authors see
            # the cap hit rather than silently losing items. Originally
            # scoped to operator_groupby in RDR-093; generalised in
            # nexus-3j6b to every operator that runs through this
            # auto-hydration branch (extract / rank / compare /
            # summarize / generate / filter / check / verify / groupby
            # — any operator with `ids in args`).
            #
            # Attachment chosen: runner-attaches (option a) — the
            # operator's JSON schema is unchanged; the dispatcher
            # (and bundle path) merge this metadata into the operator's
            # return dict post-dispatch via the _truncation_metadata
            # private marker. Operators whose return shape collides
            # with one of the metadata keys (truncated, original_count,
            # kept_count) would have an issue; the existing operator
            # family does not collide.
            truncation_metadata = {
                "truncated": True,
                "original_count": original_count,
                "kept_count": _OPERATOR_MAX_INPUTS,
            }
            non_empty = non_empty[:_OPERATOR_MAX_INPUTS]
        note_reducer_input(non_empty)  # nexus-jypmj.1.8: reads the final list, changes nothing
        args = {k: v for k, v in args.items() if k not in ("ids", "collections")}
        if resolved_tool == "operator_summarize":
            args.setdefault("content", "\n\n".join(non_empty))
        elif resolved_tool == "operator_generate":
            args.setdefault("context", "\n\n".join(non_empty))
        elif resolved_tool in ("operator_rank", "operator_compare"):
            args.setdefault("items", json.dumps(non_empty))
        elif resolved_tool in (
            "operator_filter", "operator_check", "operator_groupby",
        ):
            args.setdefault("items", json.dumps(non_empty))
        else:
            args.setdefault("inputs", json.dumps(non_empty))
        if truncation_metadata is not None:
            args["_truncation_metadata"] = truncation_metadata
        # nexus-mm5tx follow-up (code-review T2 [24199]): every id
        # hydrated to empty content -- dispatching claude -p on an
        # empty prompt wastes a real subprocess call and returns free-
        # text refusal prose a caller can only detect by parsing
        # English. Short-circuit BEFORE dispatch instead; see
        # _default_dispatcher for where this marker is consumed.
        if not non_empty:
            args["_zero_evidence_text"] = _no_evidence_result_text(
                tool=tool, filtered_count=len(contents),
            )

    if resolved_tool == "operator_extract" and "template" in args and "fields" not in args:
        template = args.pop("template")
        if isinstance(template, dict):
            args["fields"] = ",".join(template.keys())

    # nexus-yis0: translate step-passed ``inputs`` to the operator's
    # expected arg name when a prior explicit ``store_get_many`` step
    # already materialized content. The auto-hydration branch above
    # handles the ``ids in args`` case; this handles the pre-hydrated
    # case where the operator's args reference ``$stepN.contents``.
    # Without this, isolated dispatch of summarize / rank / compare /
    # generate fires with no positional arg and raises TypeError
    # (plan 57 ``find-by-author`` is the canonical repro).
    # RDR-232 (nexus-jypmj.1.6): ``operator_extract`` takes ``inputs`` as it is, so it is not in
    # ``_INPUTS_TARGET``; when the plan passed it a list it is still the first evidence-consuming
    # operator in the ``review-default`` shape (extract, then compare), and merges the stream here.
    if resolved_tool == "operator_extract" and isinstance(args.get("inputs"), list):
        args["inputs"] = _drop_step_ids_from_evidence(
            args["inputs"], tool=tool, resolved_tool=resolved_tool, branch="extract_inputs",
        )
    if resolved_tool == "operator_extract" and isinstance(args.get("inputs"), list):
        _rs, _sp = active_reserved_stream(), active_supplement()
        if (_rs is not None and _rs.should_merge()) or (_sp is not None and _sp.should_merge()):
            args["inputs"] = _merge_reserved_explicit_inputs(
                [v for v in args["inputs"] if v], args, tool, resolved_tool,
            )
    if resolved_tool == "operator_extract" and isinstance(args.get("inputs"), list):
        note_reducer_input([v for v in args["inputs"] if v])

    target_key = _INPUTS_TARGET.get(resolved_tool)
    if target_key and "inputs" in args and target_key not in args:
        _evidence_handled = True
        value = args.pop("inputs")
        _passthrough_count = len(value) if isinstance(value, list) else (1 if value else 0)
        if isinstance(value, list):
            # nexus-mm5tx (nexus-1lfk8 critique addendum): the inline
            # ids-auto-hydration branch above already filters falsy
            # content (``non_empty = [c for c in contents if c]``); this
            # pass-through branch did not, so a tumbler/chash mismatch
            # upstream (nexus-zekpl class) could hand an operator a list
            # of literal empty strings verbatim. Filter here too, and
            # warn loudly when anything was dropped — gate q12's compare
            # step took exactly this path with ten empty strings.
            original_count = len(value)
            value = [v for v in value if v]
            if len(value) != original_count:
                _log.warning(
                    "yis0_passthrough_empty_filtered",
                    tool=tool, resolved_tool=resolved_tool,
                    original_count=original_count, kept_count=len(value),
                )
            value = _drop_step_ids_from_evidence(
                value, tool=tool, resolved_tool=resolved_tool, branch="inputs_alias",
            )
            value = _merge_reserved_explicit_inputs(value, args, tool, resolved_tool)
            note_reducer_input(value)  # nexus-jypmj.1.8: the explicit-inputs reducer input, after merge and cap
            if target_key == "items":
                value = json.dumps(value)
        args[target_key] = value
        # nexus-mm5tx follow-up (code-review T2 [24199]): same zero-
        # evidence short-circuit as the ids-auto-hydration branch above
        # — see _default_dispatcher for where this marker is consumed.
        _final_empty = value in ("", "[]") or (isinstance(value, list) and not value)
        if _final_empty:
            args.setdefault("_zero_evidence_text", _no_evidence_result_text(
                tool=tool, filtered_count=_passthrough_count,
            ))

    # RDR-232 (nexus-jypmj.1.20): the planner's documented Pattern A passes the evidence in the
    # operator's own arg (``summarize(content=$stepN.contents)``, ``generate(context=...)``,
    # ``rank(items=...)``), which resolves to a list of fetched texts and reaches the joins below
    # with no ids and no ``inputs``. It is the same evidence, so it takes the same merge, cap and
    # reducer-input record. Only a list of strings counts: a string is a condensed prior output
    # (``$step5.summary``) and a deferred bundle ref is a dict. A run without a stream gets back
    # the very list it passed in (``_merge_reserved_explicit_inputs`` returns it unchanged).
    if target_key and not _evidence_handled:
        _listed = args.get(target_key)
        if isinstance(_listed, list) and all(isinstance(v, str) for v in _listed):
            _texts = [v for v in _listed if v]
            _kept = _drop_step_ids_from_evidence(
                _texts, tool=tool, resolved_tool=resolved_tool, branch="pattern_a",
            )
            if _kept is not _texts:
                _texts = _kept
                args[target_key] = _texts
            _merged = _merge_reserved_explicit_inputs(_texts, args, tool, resolved_tool)
            note_reducer_input(_merged)
            if _merged is not _texts:
                args[target_key] = _merged

    if resolved_tool == "operator_summarize" and isinstance(args.get("content"), list):
        args["content"] = "\n\n".join(str(x) for x in args["content"] if x)
    if resolved_tool == "operator_generate" and isinstance(args.get("context"), list):
        args["context"] = "\n\n".join(str(x) for x in args["context"] if x)
    if resolved_tool in (
        "operator_rank", "operator_compare", "operator_filter",
        "operator_check", "operator_groupby",
    ) and isinstance(args.get("items"), list):
        args["items"] = json.dumps(args["items"])
    # RDR-093 Phase 2 follow-up (code-review S-2): operator_aggregate's
    # positional arg is `groups`, not `items`, so it doesn't share the
    # coercion path above. When a plan step resolves `$stepN.groups`
    # from a prior groupby's output, the runner-side $stepN reference
    # resolution may hand a Python list to this hydration step. Coerce
    # to JSON so the operator_aggregate prompt sees clean JSON rather
    # than a Python repr (which would silently malform via the
    # f-string in claude_dispatch).
    if resolved_tool == "operator_aggregate" and isinstance(args.get("groups"), list):
        args["groups"] = json.dumps(args["groups"])

    # RDR-232 (nexus-jypmj.1.6, critique observation 13): an operator after the merge step is not
    # given the stream again; count the reserved items its input no longer holds, so a drop by an
    # intermediate operator (an unbundled rank) is reported, not silent.
    _rs = active_reserved_stream()
    if (
        _rs is not None and _rs.merged and _rs.merged_step != _rs.current_step
        and resolved_tool in _OPERATOR_RESOLVED_TOOLS
    ):
        _rs.observe(args)

    return resolved_tool, args


#: Attribute set on dispatchers that understand operator-bundle execution.
#: The bundle path in :func:`plan_run` reads this via ``getattr(dispatch,
#: _SUPPORTS_BUNDLING_ATTR, False)``. We don't want an ``is`` identity
#: check because a decorator or timing-wrapper would silently disable
#: bundling in production. (substantive-critic Obs D)
_SUPPORTS_BUNDLING_ATTR: str = "supports_bundling"


async def _default_dispatcher(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch *tool* against the live MCP tool registry.

    Async throughout — RDR-079 P4 review (critic adcddfaad25f760fd) C-1
    established that a thread-bridged dispatcher breaks the persistent
    operator pool: ``asyncio.subprocess`` StreamReader instances are
    loop-bound, so the second bridge-dispatched step crashes on a
    ``readline()`` against a subprocess pipe opened on a different loop.
    Making the dispatcher native-async eliminates the problem. Callers
    must ``await`` the result.

    Resolves tool callables lazily from :mod:`nexus.mcp.core` so the
    runner imports cheaply and tests can swap it out. Maps plan-step
    operator names (bare ``extract``, ``rank``, etc.) to their
    ``operator_*`` MCP-tool counterparts.

    **Auto-hydration (RDR-080 Option C)**: when the resolved tool is an
    operator AND the args contain an ``ids`` key from a prior retrieval
    step, the dispatcher calls ``store_get_many`` to materialize document
    content and injects ``inputs`` (JSON array) into the args before
    dispatch. Plan YAML does NOT need explicit hydration steps.
    """
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    # Auto-hydration + arg normalization: shared with the bundle path. Skipped when the runner already
    # hydrated this terminal step for the coverage boundary (nexus-jypmj.3.1): once only.
    if _PREHYDRATED.get():
        resolved_tool = _OPERATOR_TOOL_MAP.get(tool, tool)
    else:
        resolved_tool, args = _hydrate_operator_args(tool, args)

    # nexus-mm5tx follow-up (code-review T2 [24199]): an explicit
    # store_get_many step with tumbler-shaped ids was resolved via the
    # catalog manifest inside _hydrate_operator_args, which returns the
    # already-complete {contents, missing} result under this marker
    # rather than mutating args for a real dispatch — return it
    # directly, skipping the real store_get_many call entirely.
    _precomputed_result = args.pop("_precomputed_result", None)
    if _precomputed_result is not None:
        return _precomputed_result

    # nexus-mm5tx follow-up (code-review T2 [24199]): hydration filtered
    # this operator's evidence to zero items (either the ids-auto-
    # hydration branch or the nexus-yis0 pass-through branch inside
    # _hydrate_operator_args). Short-circuit BEFORE any model-tiering /
    # kwargs-filtering / real dispatch work below — a claude -p call on
    # an empty prompt is pure waste and its free-text refusal ("No
    # comparison performed") is not machine-classifiable the way this
    # fixed marker is.
    _zero_evidence_text = args.pop("_zero_evidence_text", None)
    if _zero_evidence_text is not None:
        _log.info(
            "nx_answer_zero_evidence_short_circuit",
            tool=tool, resolved_tool=resolved_tool,
        )
        return {"text": _zero_evidence_text}

    # RDR-196 .p2d (nexus-nyry9.17): DEFAULT-ON per-operator tiering, plus
    # the .p2c measurement override and a kill switch — a 3-way branch on
    # ``NX_OPERATOR_MODEL_TIERING``, all gated behind "model" not already
    # supplied by the caller (an explicit step-author override always
    # wins, never touched here):
    #
    #   == "1"  measurement override (.p2c, UNCHANGED): consult the WHOLE
    #           ``OPERATOR_MODEL_TIER`` table, including "strong" entries
    #           — for A/B re-verification, not production traffic. Every
    #           table operator's MCP signature accepts ``model`` since
    #           nexus-3mea3 + nexus-ek8tr (guarded by
    #           test_every_tier_table_operator_mcp_signature_accepts_model).
    #   != "0"  (unset, or any other value) DEFAULT PATH (.p2d as
    #           extended by nexus-ek8tr 2026-08-21): EVERY table operator
    #           gets an explicit model — FLIPPED_OPERATORS the cheap
    #           alias, everything else STRONG_DEFAULT_ALIAS. Nothing on
    #           this path inherits the box CLI default any more.
    #   == "0"  kill switch: neither branch fires, ``args`` stays
    #           untouched — bare dispatch, the true pre-tiering rollback
    #           (the model is then whatever the box CLI defaults to).
    #
    # Each branch's env check is inlined (not hoisted to a local) so the
    # AST structural guard (``TestNotConsultedRepoWide::
    # test_p2c_opt_in_call_sites_are_env_gated``) — which requires the
    # resolver call be lexically nested inside an if/while whose test
    # source names ``NX_OPERATOR_MODEL_TIERING`` — inspects the real
    # branch condition, not a variable read.
    if "model" not in args:
        if _os.environ.get("NX_OPERATOR_MODEL_TIERING") == "1":
            from nexus.operators.model_tiers import (  # noqa: PLC0415 — measurement-only opt-in, not a default-path import
                OPERATOR_MODEL_TIER,
                resolve_model_for_operator,
            )

            if resolved_tool in OPERATOR_MODEL_TIER:
                args = {**args, "model": resolve_model_for_operator(resolved_tool)}
        elif _os.environ.get("NX_OPERATOR_MODEL_TIERING") != "0":
            from nexus.operators.model_tiers import (  # noqa: PLC0415 — default-path resolver, kept adjacent to its measurement-override sibling above
                OPERATOR_MODEL_TIER as _OMT,
                resolve_model_for_default_path,
            )

            # nexus-ek8tr: EVERY known operator gets an explicit model on
            # the default path — flipped -> cheap alias, everything else
            # -> STRONG_DEFAULT_ALIAS. Bare dispatch survives only via
            # the kill switch or an unknown tool name.
            if resolved_tool in _OMT:
                args = {**args, "model": resolve_model_for_default_path(resolved_tool)}

    # RDR-093 S-1: pop runner-attached truncation metadata before the
    # kwargs-drop pass so the operator never sees the marker (it's not
    # part of any operator's signature) and the warn-on-drop log doesn't
    # fire for an intentional runner-internal arg. The metadata gets
    # merged onto the operator's return dict post-dispatch.
    truncation_metadata = args.pop("_truncation_metadata", None)

    fn = getattr(mcp_core, resolved_tool, None)
    if fn is None or not callable(fn):
        available = sorted(
            name for name in dir(mcp_core)
            if not name.startswith("_") and callable(getattr(mcp_core, name, None))
        )[:20]
        raise PlanRunToolNotFoundError(
            tool=tool,
            reason=(
                f"not present in nexus.mcp.core "
                f"(available sample: {', '.join(available[:10])}…)"
            ),
        )
    # RDR-079 P1: inject structured=True for retrieval tools so plan steps
    # receive the runner-contract dict {ids, tumblers, distances, collections}
    # per RDR-078 §Phase 1, rather than a human-readable string wrapped as
    # {"text": str}. Non-retrieval tools keep their default behavior; callers
    # can still pass structured=True explicitly if they want.
    if tool in _RETRIEVAL_TOOLS and "structured" not in args:
        args = {**args, "structured": True}

    # Drop kwargs the resolved tool doesn't accept. Plan YAML carries
    # extra metadata (e.g. ``scope.topic`` forwarded as ``topic=…``,
    # authoring-layer hints like ``mode``, ``target``) that an older
    # tool signature may not implement yet. Silently dropping would
    # mask plan-YAML typos (e.g. ``colllection``) as well as genuinely-
    # forward-compatible kwargs. The compromise: drop the kwarg so the
    # call succeeds, AND log the drop at warning level so misspelled
    # or unwired kwargs stay observable. ``**kwargs``-accepting tools
    # keep every kwarg.
    _sig: inspect.Signature | None
    try:
        _sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Builtins / C-level callables can't always be inspected; leave
        # args untouched and let the call site surface any TypeError.
        _sig = None

    if _sig is not None:
        sig = _sig
        accepts_any_kwarg = any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if not accepts_any_kwarg:
            known = set(sig.parameters.keys())
            dropped = sorted(k for k in args if k not in known)
            if dropped:
                _log.warning(
                    "plan_dispatcher_kwargs_dropped",
                    tool=resolved_tool,
                    dropped=dropped,
                    known_sample=sorted(known)[:12],
                )
            args = {k: v for k, v in args.items() if k in known}

        # nexus-nyry9.4 review-fix (critic finding #3, T2 [23036]): a
        # step whose resolved args have no path to a required parameter
        # (not hydrated via ``ids``, not present directly, no ``inputs``
        # alias — see ``_hydrate_operator_args`` above, which already
        # ran) reaches this call and raises a bare ``TypeError`` today
        # (nx_answer_runs id=4: "operator_summarize() missing 1 required
        # positional argument: 'content'"). Fail loud with a named error
        # instead — a plan-authoring bug, not a runtime condition; a
        # silent default would mask it (no-silent-fallbacks-for-
        # correctness). Deliberately placed HERE, after hydration and
        # AFTER the kwargs-drop pass, and gated on the REAL callable's
        # own signature — not a blanket pre-dispatch check — so a
        # caller-injected fake/test dispatcher (which never reaches this
        # function) is never affected; only a step that will actually be
        # dispatched against the real MCP tool registry is checked.
        # ``step_index=-1`` is a placeholder: this function is not given
        # the step index (the ``ToolDispatcher`` protocol is deliberately
        # ``(tool, args) -> result``), so the two ``plan_run`` call sites
        # that invoke a real dispatcher patch in the correct index before
        # re-raising.
        _missing_required = [
            name for name, p in sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name not in args
        ]
        if _missing_required:
            raise PlanRunOperatorArgMissingError(
                step_index=-1,
                tool=resolved_tool,
                missing_arg=_missing_required[0],
            )

    # RDR-079 P4: await async tools directly, call sync tools inline.
    # No thread bridge — the loop continuity matters for the pool's
    # subprocess StreamReader objects, which are loop-bound.
    if inspect.iscoroutinefunction(fn):
        result = await fn(**args)
    elif call_deadline.get() is not None:
        # nexus-5ezgn: off the loop, so plan_run's wait_for can cut it; in a
        # copy of this context, so the deadline reaches the worker and a cut
        # search stops at its next stage boundary. See _retrieval_executor.
        import contextvars  # noqa: PLC0415 — deadline path only
        import functools  # noqa: PLC0415 — deadline path only

        ctx = contextvars.copy_context()
        result = await asyncio.get_running_loop().run_in_executor(
            _get_retrieval_executor(), functools.partial(ctx.run, fn, **args),
        )
    else:
        result = fn(**args)
    # Most MCP tools return str (human-readable summary); the runner
    # expects dict per RDR-078 §Phase 1. Normalize: wrap string returns
    # as ``{"text": ...}`` so downstream ``$stepN.text`` references
    # resolve. The ``traverse`` tool already returns dict and passes
    # through unchanged.
    #
    # Retrieval tools occasionally short-circuit on error paths
    # (catalog not initialized, subtree too deep, …) with a bare
    # ``"Error: …"`` string even when ``structured=True`` is passed.
    # In that case synthesize the empty structured shape so the plan
    # step still conforms to ``{ids, tumblers, distances, collections,
    # contents}`` and downstream ``$stepN.tumblers`` / ``$stepN.contents``
    # refs don't crash; preserve the error text in an ``error`` key for
    # visibility. ``contents`` is included so combined-query consumers
    # (RDR-156 P4 search_metadata_scoped / search_topic_scoped, whose
    # structured output carries chunk text inline) degrade to an empty
    # summarize in local mode rather than raising PlanRunStepRefError.
    if isinstance(result, str):
        if tool in _RETRIEVAL_TOOLS:
            # nexus-vply6 fix round 2, SHIP-BLOCKER (critique T2 [26773]):
            # an embedding-profile-mismatch refusal is NOT a plan-binding
            # issue the empty-result synthesis below exists for — fail the
            # WHOLE plan loudly instead of handing the next operator step
            # an empty result it cannot distinguish from a genuine
            # no-match. Checked BEFORE the generic synthesis so this
            # specific class never reaches it. See
            # PlanRunRetrievalRefusedError's docstring.
            from nexus.errors import is_search_embedding_profile_mismatch_text  # noqa: PLC0415 — deferred to avoid a module-load-time nexus.errors dependency in this hot-path module

            if is_search_embedding_profile_mismatch_text(result):
                raise PlanRunRetrievalRefusedError(tool=tool, message=result)
            # Other retrieval error strings usually indicate a plan-binding
            # issue (bad subtree, missing catalog, unresolvable filter).
            # Synthesize the empty structured shape so ``$stepN.tumblers``
            # resolves, but log at warning level so the next operator
            # step isn't silently handed empty inputs without anyone
            # noticing. Callers can inspect the ``error`` key on the
            # step output for programmatic branching.
            _log.warning(
                "plan_retrieval_error_synthesized",
                tool=tool,
                error=result[:200],
            )
            return {
                "ids": [], "tumblers": [], "distances": [], "collections": [],
                "contents": [],
                "error": result,
            }
        return {"text": result}
    if isinstance(result, dict):
        # RDR-093 S-1: merge runner-attached truncation metadata onto
        # the operator's return dict so plan authors see when the
        # _OPERATOR_MAX_INPUTS cap fired. Scoped to operator_groupby
        # in this RDR; nexus-3j6b tracks cross-operator generalisation.
        if truncation_metadata is not None:
            result = {**result, **truncation_metadata}
        return result
    # Anything else (list, None, …) — surface explicitly rather than
    # let downstream step-ref resolution silently fail.
    raise PlanRunStepRefError(
        ref=f"tool:{tool}",
        reason=(
            f"default dispatcher received unexpected return type "
            f"{type(result).__name__} from {tool!r}"
        ),
    )


# Mark the default dispatcher as bundle-aware. Wrappers (timing decorator,
# retry wrapper, …) that want bundling enabled can set this attribute
# themselves. The plan_run bundle path gates on this attribute (not on
# identity), so wrapping the dispatcher doesn't silently disable bundling.
# (substantive-critic Obs D)
setattr(_default_dispatcher, _SUPPORTS_BUNDLING_ATTR, True)
setattr(_default_dispatcher, _HYDRATES_ATTR, True)


# ── Public API ──────────────────────────────────────────────────────────────


async def _plan_run_impl(
    match: Match,
    bindings: dict[str, Any] | None = None,
    *,
    dispatcher: ToolDispatcher | None = None,
    bundle_operators: bool = True,
    deadline: float | None = None,
    budget_usd_remaining: float | None = None,
    continuation_cut_at_step: int | None = None,
    coverage_boundary: CoverageBoundary | None = None,
) -> PlanResult:
    """Execute the steps in *match* and return the captured outputs.

    ``bindings`` are the caller's substitutions. They are merged on top
    of ``match.default_bindings`` (caller wins on conflict).

    Async since RDR-079 P4 — callers must ``await`` it. The MCP
    ``plan_run`` tool also went async in the same change so FastMCP
    runs it natively on its event loop without the sync-legacy
    thread-bridge dance that broke loop continuity for pool workers.

    ``bundle_operators`` (nexus-nxa-perf, default ``True``) collapses
    contiguous runs of ≥2 operator steps (extract/rank/compare/summarize/
    generate) into a single ``claude -p`` subprocess via
    :func:`nexus.plans.bundle.dispatch_bundle`. Benchmark (2-op chain,
    3-paper synthetic input): **~55% wall-clock reduction** vs per-step
    isolation. Retrieval steps stay isolated — they're cheap and the
    bundle needs their real host-side outputs as inputs. Pass ``False``
    to recover the per-step dispatch path for debugging or plans that
    need per-step telemetry.

    ``deadline`` (``time.monotonic()`` timestamp, nexus-h33x8.6 a4):
    ``None`` (the default) reproduces the pre-a4 behavior exactly — an
    ``OperatorTimeoutError`` is substituted with a failure sentinel and
    the loop continues, same as any other ``OperatorError``. When set,
    two things change: (1) the deadline is checked before dispatching
    each segment, and if already past, the loop stops WITHOUT
    dispatching that segment; (2) an active operator dispatch (isolated
    or bundled) receives the REMAINING budget as its own ``timeout``
    kwarg, and if it raises ``OperatorTimeoutError`` anyway, the
    reconstructed ``partial_text``/``event_count`` are captured into the
    terminal sentinel and the loop stops. Either trigger sets
    :attr:`PlanResult.budget_exhausted_at_step` (1-indexed) and
    :attr:`PlanResult.budget_exhausted_kind` to ``"time"``. A retrieval
    step takes no ``timeout`` kwarg, so it is cut from outside instead
    (nexus-5ezgn): its dispatch runs under ``asyncio.wait_for`` with the
    remaining budget, and the default dispatcher runs a sync tool in a
    worker thread while that wait is armed, so the wait can fire. A cut
    retrieval step leaves a timeout sentinel and stops the loop exactly
    like an operator timeout. A custom dispatcher that returns a plain
    dict (not a coroutine) runs to completion and can still overrun.

    ``budget_usd_remaining`` (RDR-196 .p3c, nexus-nyry9.21): an OPTIONAL
    USD ceiling on the sum of already-completed steps' ``cost_usd``
    (``StepRecord.cost_usd``, non-``None`` entries only — an unknown-cost
    step never contributes a fabricated 0 to the running sum, and never
    trips the check either). ``None`` (the default) reproduces pre-.p3c
    behavior exactly — no cost check runs. When set, checked at the SAME
    point as the ``deadline`` check above — BEFORE dispatching each
    segment — because a dispatch's real cost is unknowable until it
    returns; unlike ``deadline``, there is no mid-dispatch cost cut. This
    makes the budget a STOP-LINE, not a hard ceiling: the running sum is
    only ever compared to the cap using costs already known when a
    segment STARTS, so the segment that pushes the sum over the cap
    still finishes and its own cost is not counted until AFTER it
    completes — the final executed segment's cost can carry the run
    total above ``budget_usd_remaining``. Treat this as the documented
    contract, not a bug: the caller learns of any overshoot from the
    step's own recorded ``cost_usd``, same as it always could. When the
    check trips, the loop stops (the segment about to start does not
    dispatch) and :attr:`PlanResult.budget_exhausted_at_step` /
    :attr:`PlanResult.budget_exhausted_kind` (``"cost"``) are set exactly
    like the ``deadline`` trigger, sharing the same marker convention on
    the ``nx_answer`` side.

    ``continuation_cut_at_step`` (RDR-200 .p1c, nexus-4e75w.5 — the
    "stop-before-cut" mechanism): a 0-based ``plan_json.steps`` index,
    the SAME convention as :attr:`nexus.plans.continuation.
    ContinuationCut.cut_at_step` (the caller computes this via
    :func:`nexus.plans.continuation.classify_continuation_cut` BEFORE
    calling ``plan_run`` — this module never imports that classifier
    itself, keeping the runner decoupled from continuation-mode policy,
    per the bead's own "the runner must not import MCP-layer state"
    constraint). ``None`` (the default) reproduces pre-.p1c behavior
    exactly — every segment dispatches, unchanged. When set, checked at
    the SAME pre-segment point as ``deadline``/``budget_usd_remaining``
    above: if the segment about to start (its first plan index) is at or
    past ``continuation_cut_at_step``, the loop stops WITHOUT dispatching
    that segment or any segment after it — the terminal continuation
    suffix a caller is about to hand off NEVER executes server-side, the
    exact contract RDR-200 R2 requires ("the terminal suffix MUST NOT
    execute server-side when it is being handed off"). Steps before the
    cut run exactly as they would without this parameter; their
    :class:`StepRecord` entries are real, not fabricated, and
    ``PlanResult.step_records``/``.steps`` reflect only what actually
    ran — the same "step_count counts steps that actually executed
    server-side, never the planned total" contract the RDR states for
    the handoff telemetry row this parameter exists to make possible.
    :attr:`PlanResult.continuation_cut_applied` records whether the cut
    actually fired (``False`` when the run stopped/errored/exhausted its
    budget before ever reaching the cut point, or when the plan simply
    has fewer segments than the cut index — in either case nothing was
    withheld that would otherwise have run).

    ``coverage_boundary`` (RDR-232 Phase 3, nexus-jypmj.3.1): ``None`` (the default) changes nothing.
    When set, the runner stops at the LAST dispatch segment if it is an operator segment (an isolated
    operator step, or a fused operator bundle), reads the input that segment's first evidence-consuming
    step will be handed (hydrated once; see ``_HYDRATES_ATTR``), and calls
    :meth:`~nexus.plans.coverage_ledger.CoverageBoundary.evaluate` on it BEFORE dispatching the segment.
    The result is :attr:`PlanResult.coverage`. When it says ``skip_generation`` the segment is not
    dispatched: its slots get the bundle-intermediate sentinel and, for the terminal slot,
    ``{"text": ..., "coverage_skipped": True}``, so a later ``$stepN`` reference still resolves and no
    ``StepRecord`` is invented. The plan prefix is never re-run. The assessment calls are ``StepRecord``s
    numbered past the plan's own step indices and count against ``deadline`` and ``budget_usd_remaining``.
    A boundary failure never fails the run.

    The boundary also owns the ONE repair (RDR-232 Phase 3 step 2, nexus-jypmj.3.2; see
    :meth:`~nexus.plans.coverage_ledger.CoverageBoundary.run`): when the first ledger has a facet that was
    assessed and nothing held bears on it, targeted retrieval calls (a ``search`` or ``query`` per facet and
    target, then one ``store_get_many`` of the hits) run through the same dispatcher, each a ``StepRecord`` of
    source ``sql`` numbered past the plan like the assessment calls, inside the repair's own allowance and the
    one USD accumulator. The new chunks are installed in the run's :class:`~nexus.plans.repair_stream.
    RepairSupplement`, the terminal segment's input is read a second time with them merged (after the
    reserved items, ahead of the plan's own, under the same positional cap), and the ledger is recomputed once.
    That second reading is what the terminal step is dispatched with; the prefix is not re-run. The supplement
    is returned as :attr:`PlanResult.repair_supplement`.

    With ``continuation_cut_at_step`` at the terminal operator segment, the cut no longer fires before the
    boundary: the boundary and its repair run on the segment's input, the segment stops after them undispatched
    (``continuation_cut_applied``), and the hand-off carries the result. If the ledger skips generation there
    is nothing to hand off: the cut does not fire and the run completes with the skipped terminal output.

    **Validation is WHOLE-PLAN and PRE-DISPATCH (nexus-pucte), which
    runs BEFORE either budget check above ever executes.** Required
    bindings, malformed-step shape, and unresolved ``$var`` references
    are all checked once, up front, across every step in the plan —
    not just the step about to dispatch. This is a deliberate contract,
    not an oversight: previously, a ``deadline``/``budget_usd_remaining``
    cut that stopped the run before a LATE, malformed step was ever
    reached meant that step's problems were harmless — dead code the run
    never got to. Since this validation pass now runs first, a plan with
    an unresolvable ``$var`` in step 5 fails the ENTIRE call with
    :class:`PlanRunUnresolvedVarError` even when a tight ``deadline``
    would have stopped the run at step 1 and never reached step 5 at
    all. The runner refuses malformed input outright, rather than let it
    slip through opportunistically depending on how far execution
    happens to get before a budget cuts it off.
    """
    from nexus.plans.bundle import (  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        BUNDLED_INTERMEDIATE,
        IsolatedStep,
        MAX_BUNDLE_PROMPT_CHARS,
        OperatorBundle,
        OperatorBundleSlice,
        OperatorBundleStep,
        compose_bundle_prompt,
        dispatch_bundle,
        is_operator_tool,
        resolve_dispatch_segments,
    )

    def _extract_tool(step: dict[str, Any]) -> str:
        t = step.get("tool") or step.get("op") or step.get("operation") or ""
        # nexus-nyry9.4 review-fix (code-review-expert + substantive-critic,
        # T2 [23035]/[23036]): a truthy non-string tool value (e.g.
        # ``{"tool": 5}``) previously reached ``t.startswith(...)`` and
        # raised a bare ``AttributeError`` instead of the intended
        # ``PlanRunToolNotFoundError`` — exactly the kind of unhandled
        # crash the validation loop below exists to prevent. Treat a
        # non-string value the same as "no tool resolved" so the loop's
        # own checks (which DO name the step index and the offending
        # value/type) are what the caller sees.
        if not isinstance(t, str):
            return ""
        if t.startswith("mcp__"):
            t = t.rsplit("__", 1)[-1]
        return t

    caller = bindings or {}
    # nexus-nyry9.5 (RDR-196 .r5 review-fix): this precedence formula is
    # now expressed in exactly one place — see merge_bindings's own
    # docstring for why.
    merged: dict[str, Any] = merge_bindings(match.default_bindings, caller)
    _validate_bindings(match, merged)

    plan = json.loads(match.plan_json)
    steps = plan.get("steps", []) or []

    # nexus-nyry9.4 (RDR-196 residual): fail loud at VALIDATION, not
    # dispatch. Historically an empty/missing tool name reached
    # ``_default_dispatcher`` and raised ``PlanRunToolNotFoundError``
    # only after earlier steps in the same plan had already dispatched
    # (nx_answer_runs ids 177/183/184/369 — the plan-138 "unknown tool
    # ''" crash class; the offending stored plans no longer exist in
    # the library, purged by a later reseed, but the runner itself
    # never validated this and would still fail the same way against
    # any malformed plan reaching it today). A caller-supplied fake
    # dispatcher (as in tests) may not replicate the real dispatcher's
    # own unknown-tool check at all, so this cannot be left to the
    # dispatcher layer — it must be checked here, once, for every step,
    # before the first dispatch of the whole plan.
    for _step_index, _step in enumerate(steps):
        if not isinstance(_step, dict):
            raise PlanRunToolNotFoundError(
                tool="",
                reason=(
                    f"plan_json.steps[{_step_index}] is not a mapping "
                    f"(got {type(_step).__name__}) — malformed plan, "
                    "rejected before any step dispatched"
                ),
            )
        if not _extract_tool(_step):
            # nexus-nyry9.4 review-fix: distinguish "missing entirely"
            # from "present but not a string" so the error names the
            # offending value/type, not just a generic absence.
            _raw_tool = (
                _step.get("tool")
                if _step.get("tool") is not None
                else _step.get("op") if _step.get("op") is not None
                else _step.get("operation")
            )
            if _raw_tool is not None and not isinstance(_raw_tool, str):
                raise PlanRunToolNotFoundError(
                    tool="",
                    reason=(
                        f"plan_json.steps[{_step_index}] has a non-string "
                        f"'tool'/'op'/'operation' value {_raw_tool!r} "
                        f"({type(_raw_tool).__name__}) — malformed plan, "
                        "rejected before any step dispatched"
                    ),
                )
            raise PlanRunToolNotFoundError(
                tool="",
                reason=(
                    f"plan_json.steps[{_step_index}] has no resolvable "
                    "'tool'/'op'/'operation' name — malformed plan, "
                    "rejected before any step dispatched"
                ),
            )
    # nexus-pucte: same "validate everything before the first dispatch"
    # discipline as the tool-name loop just above — every step is
    # confirmed to be a dict with a resolvable tool name by this point,
    # so ``step.get("args")`` below is safe.
    _validate_var_refs(steps, merged)
    dispatch: ToolDispatcher = dispatcher or _default_dispatcher
    step_outputs: list[dict[str, Any]] = []
    #: nexus-h33x8.6 a4: set when ``deadline`` cuts the run short. See
    #: :class:`PlanResult` and the ``deadline`` docstring above.
    budget_exhausted_at_step: int | None = None
    #: RDR-196 .p3c: "time" or "cost", set alongside budget_exhausted_at_step
    #: whichever axis cut the run short. See PlanResult.budget_exhausted_kind.
    budget_exhausted_kind: str | None = None
    #: RDR-196 .p1b (nexus-nyry9.8): one StepRecord per executed dispatch
    #: segment. See StepRecord's own docstring for the source/model/cost
    #: attribution rules.
    step_records: list[StepRecord] = []
    #: nexus-ivv4d code-review follow-up (T2 [24198]). See
    #: :attr:`PlanResult.resolved_step_args`'s own docstring.
    resolved_step_args: list[dict[str, Any]] = []
    #: RDR-232 (nexus-jypmj.1.3). See :attr:`PlanResult.scope_diagnostics`.
    scope_diagnostics: list[dict[str, Any]] = []
    #: RDR-200 .p1c (nexus-4e75w.5): set True the moment the
    #: ``continuation_cut_at_step`` pre-segment check actually stops
    #: dispatch. See ``continuation_cut_at_step``'s own docstring
    #: paragraph above and :attr:`PlanResult.continuation_cut_applied`.
    continuation_cut_applied: bool = False

    def _step_source(tool: str, result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify an isolated/bundle-fallback dispatch's ``source`` and
        strip the runner-internal ``_dispatch_source`` marker (Gap 5 —
        core.py's operator_filter/groupby/aggregate SQL fast path) off
        *result* so it never leaks onto ``step_outputs`` / ``$stepN.field``.

        Default: any operator tool (:func:`is_operator_tool`) is "llm";
        any non-operator (retrieval/traversal) tool is "sql" — see
        :class:`StepRecord`'s docstring for why that's accurate on this
        substrate, not a stretch. The marker overrides the default to
        "sql" only when an operator's own SQL fast path actually served
        the request.
        """
        explicit = result.pop("_dispatch_source", None)
        source = explicit or ("llm" if is_operator_tool(tool) else "sql")
        return source, result

    def _record_step(
        *, step_index: int, operator: str, source: str, elapsed_ms: int,
        ok: bool, usage: Any = None,
        bundled_steps: list[int] | None = None,
    ) -> None:
        """Append one :class:`StepRecord`. *usage* is a
        ``nexus.operators.dispatch.DispatchUsage`` or ``None``.

        Priority: real ``usage`` always wins when present (the "bundle"
        path via its explicit sink, the isolated / bundle-fallback path
        via the ambient ContextVar sink). Absent real usage: a "sql" step
        is a TRUE, known zero (no claude -p call happened at all) —
        ``0``/``0``/``0.0``. Any "llm"/"bundle" step whose dispatch never
        produced a result envelope (timeout, non-zero exit, empty stdout)
        is genuinely UNKNOWN — ``None``, never a fabricated ``0``.
        """
        if usage is not None:
            model = usage.model
            in_tok, out_tok, cost = (
                usage.input_tokens, usage.output_tokens, usage.cost_usd,
            )
            # nexus-ndoke: carried through verbatim from the same envelope,
            # never re-derived and never defaulted to 0 — see StepRecord.
            cache_read = usage.cache_read_input_tokens
            cache_creation = usage.cache_creation_input_tokens
        elif source == "sql":
            model, in_tok, out_tok, cost = None, 0, 0, 0.0
            # A SQL step runs no prompt, so there is no cache dimension at all.
            # 0 here would be a claim about caching; None is the absence of one.
            cache_read = cache_creation = None
        else:
            model, in_tok, out_tok, cost = None, None, None, None
            cache_read = cache_creation = None
        step_records.append(StepRecord(
            step_index=step_index, operator=operator, source=source,
            model=model, input_tokens=in_tok, output_tokens=out_tok,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            cost_usd=cost, elapsed_ms=elapsed_ms, ok=ok,
            bundled_steps=list(bundled_steps) if bundled_steps else [],
        ))

    # One authoritative segmentation. When bundling is off or the caller
    # supplied a dispatcher that doesn't opt into bundling,
    # ``resolve_dispatch_segments`` flattens the slices back into
    # isolated steps so the per-step path handles everything. Gate is
    # attribute-based so decorator wrappers survive. Extracted to
    # ``bundle.resolve_dispatch_segments`` (nexus-4e75w.3) so RDR-200's
    # continuation cut classifier reads the same real dispatch-shape
    # resolution this call site uses, rather than a second
    # reimplementation of this gate that could drift from it.
    segments: list = resolve_dispatch_segments(
        steps,
        bundle_operators=bundle_operators,
        supports_bundling=getattr(dispatch, _SUPPORTS_BUNDLING_ATTR, False),
    )

    # RDR-232 Phase 3 (nexus-jypmj.3.1): the coverage boundary sits before the LAST segment when that is an
    # operator segment. Assessment calls are numbered past the plan's own step indices ((run_id, step_index)
    # is the primary key of nx_answer_steps) and ride the existing "llm" step source.
    _terminal_seg = segments[-1] if segments else None
    coverage_result: CoverageResult | None = None
    _coverage_calls = 0
    #: Seconds the last boundary run took (assessment, repair, the reassessment). The terminal step's own
    #: ``elapsed_ms`` starts after it (nexus-jypmj.17): the assessment calls are their own StepRecords, and the run
    #: summary carries ``assessment_latency_ms``, so counting them in the terminal step's time too would charge the
    #: generation with a pre-check it did not run.
    _boundary_s = 0.0

    def _at_boundary(seg: Any) -> bool:
        if coverage_boundary is None or seg is not _terminal_seg:
            return False
        if isinstance(seg, OperatorBundleSlice):
            return True
        return isinstance(seg, IsolatedStep) and is_operator_tool(_extract_tool(seg.step))

    def _remaining_seconds() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    def _remaining_usd() -> float | None:
        if budget_usd_remaining is None:
            return None
        return budget_usd_remaining - sum(r.cost_usd for r in step_records if r.cost_usd is not None)

    async def _coverage_check(args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """One assessment call through the run's own dispatcher, accounted as a ``StepRecord``."""
        nonlocal _coverage_calls
        from nexus.plans.coverage_ledger import ASSESSMENT_OPERATOR  # noqa: PLC0415 — deferred; the ledger module imports this one's neighbours

        step_index = len(steps) + _coverage_calls
        _coverage_calls += 1
        started = time.monotonic()
        usage: list[Any] = []
        try:
            # nexus-jypmj.3.14: the assessment is not a plan operator, so the reserved stream is out of scope
            # for it. Otherwise the dispatcher's operator hydration lets the stream (still marked at the
            # terminal step, fed by a rank) observe this call's payload as the terminal input and record every
            # reserved item dropped, which the repair's second ledger then reads as a missing named source.
            with _dispatch_mod.ambient_usage_sink(usage), reserved_scope(None):
                raw = dispatch(ASSESSMENT_OPERATOR, {**args, "timeout": timeout})
                result = await raw if inspect.iscoroutine(raw) else raw
            if not isinstance(result, dict):
                raise PlanRunStepRefError(
                    ref=ASSESSMENT_OPERATOR,
                    reason=f"returned {type(result).__name__}; expected dict",
                )
        except (Exception, asyncio.CancelledError):
            # A cancelled call counts too (nexus-jypmj.17): the boundary cuts each concurrent call at its own timeout
            # from outside, and a call cut that way still spent what it spent.
            _record_step(
                step_index=step_index, operator=ASSESSMENT_OPERATOR, source="llm",
                elapsed_ms=int((time.monotonic() - started) * 1000), ok=False, usage=_rollup_step_usage(usage),
            )
            raise
        _, result = _step_source(ASSESSMENT_OPERATOR, result)
        _record_step(
            step_index=step_index, operator=ASSESSMENT_OPERATOR, source="llm",
            elapsed_ms=int((time.monotonic() - started) * 1000), ok=True, usage=_rollup_step_usage(usage),
        )
        return result

    async def _coverage_retrieval(tool: str, args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """One repair retrieval call (a targeted query or the chunk fetch) through the run's dispatcher,
        accounted as a known-zero ``sql`` :class:`StepRecord` numbered past the plan like the assessment
        calls. Cut from outside like any retrieval step (``wait_for`` plus ``call_deadline``)."""
        nonlocal _coverage_calls
        step_index = len(steps) + _coverage_calls
        _coverage_calls += 1
        started = time.monotonic()
        usage: list[Any] = []
        token = call_deadline.set_deadline(started + timeout)
        try:
            with _dispatch_mod.ambient_usage_sink(usage):
                raw = dispatch(tool, dict(args))
                result = await asyncio.wait_for(raw, timeout=timeout) if inspect.iscoroutine(raw) else raw
            if not isinstance(result, dict):
                raise PlanRunStepRefError(ref=tool, reason=f"returned {type(result).__name__}; expected dict")
        except Exception:
            _record_step(
                step_index=step_index, operator=tool, source="sql",
                elapsed_ms=int((time.monotonic() - started) * 1000), ok=False, usage=_rollup_step_usage(usage),
            )
            raise
        finally:
            call_deadline.reset(token)
        _, result = _step_source(tool, result)
        _record_step(
            step_index=step_index, operator=tool, source="sql",
            elapsed_ms=int((time.monotonic() - started) * 1000), ok=True, usage=_rollup_step_usage(usage),
        )
        return result

    async def _coverage_fetch(hits: Sequence[Any], *, timeout: float) -> list[SupplementItem]:
        """The chunks of the repair's hits, hydrated the way an operator's ``ids`` are: the same hygiene
        (non-evidentiary documents and import-only chunks dropped) and the same source note."""
        ids = [str(h.id) for h in hits]
        cols = [str(h.collection) for h in hits]
        collections: Any = cols if all(cols) else (",".join(dict.fromkeys(c for c in cols if c)) or "knowledge")
        out = await _coverage_retrieval("store_get_many", {"ids": ids, "collections": collections}, timeout=timeout)
        contents = out.get("contents")
        if not isinstance(contents, list) or len(contents) != len(ids):
            raise PlanRunStepRefError(ref="store_get_many", reason="the repair fetch did not return one content per id")
        by_tumbler = bool(ids) and all(_is_tumbler_shaped(i) for i in ids)
        excluded = set() if by_tumbler else _resolve_non_evidentiary_chashes(ids) | _import_only_ids_from_hydrated(out, ids)
        notes = out.get("source_notes")
        items: list[SupplementItem] = []
        for k, (hit, text) in enumerate(zip(hits, contents)):
            if not isinstance(text, str) or not text or str(hit.id) in excluded:
                continue
            if not by_tumbler and isinstance(notes, list) and k < len(notes):
                from nexus.context_annotations import with_source_note  # noqa: PLC0415 — deferred; runner imports stay lazy

                text = with_source_note(text, str(notes[k]))
            items.append(SupplementItem(
                text=text, chash="" if by_tumbler else (hit.chash or str(hit.id)), collection=hit.collection, tumbler=hit.tumbler,
            ))
        return items

    def _install_supplement(items: Sequence[SupplementItem]) -> None:
        supplement = active_supplement()
        if supplement is not None:
            supplement.install(items)

    def _install_note(note: str) -> None:
        supplement = active_supplement()
        if supplement is not None:
            supplement.install_note(note)

    def _assessment_cost_estimate() -> float | None:
        """The mean known cost of the assessment calls so far, or ``None`` (no call reported a cost)."""
        costs = [r.cost_usd for r in step_records if r.operator == "operator_check" and r.cost_usd is not None]
        return sum(costs) / len(costs) if costs else None

    async def _run_coverage_boundary(
        texts: list[str], observed: bool, terminal_index: int,
        rehydrate: Callable[[], tuple[list[str], bool]] | None = None,
    ) -> CoverageResult | None:
        """Evaluate the boundary (the ledger and its one repair) on the terminal input; a failure is logged
        and leaves the run alone. *rehydrate* re-reads that input after the repair merged its evidence."""
        nonlocal coverage_result, _boundary_s
        from nexus.plans.coverage_ledger import BoundaryView  # noqa: PLC0415 — deferred, as above

        assert coverage_boundary is not None
        can_repair = rehydrate is not None and active_supplement() is not None
        _boundary_started = time.monotonic()
        try:
            coverage_result = await coverage_boundary.run(BoundaryView(
                evidence_texts=texts, observed=observed, step_outputs=list(step_outputs),
                tools=[_extract_tool(s) for s in steps], scope_diagnostics=list(scope_diagnostics),
                resolved_args=list(resolved_step_args), check=_coverage_check,
                remaining_seconds=_remaining_seconds, remaining_usd=_remaining_usd, terminal_step=terminal_index,
                retrieve=_coverage_retrieval if can_repair else None, fetch=_coverage_fetch if can_repair else None,
                install_supplement=_install_supplement if can_repair else None, rehydrate=rehydrate if can_repair else None,
                install_note=_install_note if can_repair else None, assessment_cost_estimate=_assessment_cost_estimate,
            ))
        except Exception as exc:  # noqa: BLE001 — the boundary never fails an answer
            _log.warning("nx_answer_coverage_boundary_failed", error=type(exc).__name__)  # the type only: an exception's text can carry prompt text
            coverage_result = None
        _boundary_s = time.monotonic() - _boundary_started
        return coverage_result

    def _cut_here(at_index: int) -> None:
        """The continuation cut fires at the segment starting at *at_index*."""
        nonlocal continuation_cut_applied
        continuation_cut_applied = True
        _log.info(
            "nx_answer_continuation_cut_applied",
            at_step=at_index + 1, total_steps=len(steps), steps_completed=len(step_outputs),
        )

    try:
        for seg in segments:
            _cut_pending, _cut_start_index = False, 0
            # nexus-h33x8.6 a4: hard budget check BEFORE dispatching this
            # segment. ``deadline is None`` (the default) skips this
            # entirely — pre-a4 behavior is unchanged. Retrieval steps
            # already run get to keep their output; the segment about to
            # start does not.
            if deadline is not None and time.monotonic() >= deadline:
                if isinstance(seg, OperatorBundleSlice):
                    budget_exhausted_at_step = seg.plan_indices[0] + 1
                else:
                    budget_exhausted_at_step = seg.plan_index + 1
                budget_exhausted_kind = "time"
                _log.info(
                    "nx_answer_budget_exhausted",
                    at_step=budget_exhausted_at_step,
                    total_steps=len(steps),
                    steps_completed=len(step_outputs),
                    kind=budget_exhausted_kind,
                )
                break

            # RDR-196 .p3c (nexus-nyry9.21): USD cost check, same
            # pre-segment placement as the deadline check above (a
            # dispatch's real cost is unknowable until it returns, so
            # this can only ever be a stop-line before the NEXT segment,
            # not a hard ceiling -- see budget_usd_remaining's own
            # docstring paragraph above). Sums only the non-None costs
            # of steps already completed in THIS plan_run call; an
            # unknown-cost step contributes nothing to the running sum
            # (never a fabricated 0) and therefore can never trip this
            # check on its own.
            if budget_usd_remaining is not None:
                _spent_so_far = sum(
                    r.cost_usd for r in step_records if r.cost_usd is not None
                )
                if _spent_so_far >= budget_usd_remaining:
                    if isinstance(seg, OperatorBundleSlice):
                        budget_exhausted_at_step = seg.plan_indices[0] + 1
                    else:
                        budget_exhausted_at_step = seg.plan_index + 1
                    budget_exhausted_kind = "cost"
                    _log.info(
                        "nx_answer_budget_exhausted",
                        at_step=budget_exhausted_at_step,
                        total_steps=len(steps),
                        steps_completed=len(step_outputs),
                        kind=budget_exhausted_kind,
                        spent_usd=_spent_so_far,
                        budget_usd_remaining=budget_usd_remaining,
                    )
                    break

            # RDR-200 .p1c (nexus-4e75w.5): "stop-before-cut". Same
            # pre-segment placement as the two checks above — the
            # segment about to start does not dispatch when it is at or
            # past the continuation cut. See continuation_cut_at_step's
            # own docstring paragraph for the full contract.
            if continuation_cut_at_step is not None:
                if isinstance(seg, OperatorBundleSlice):
                    _seg_start_index = seg.plan_indices[0]
                else:
                    _seg_start_index = seg.plan_index
                if _seg_start_index >= continuation_cut_at_step:
                    if _at_boundary(seg):
                        # RDR-232 Phase 3 (nexus-jypmj.3.2): the hand-off carries the coverage state, so the
                        # boundary (the ledger and its one repair) runs on this segment's input first and
                        # the segment stops after it, undispatched, as it always did.
                        _cut_pending, _cut_start_index = True, _seg_start_index
                    else:
                        continuation_cut_applied = True
                        _log.info(
                            "nx_answer_continuation_cut_applied",
                            at_step=_seg_start_index + 1,
                            total_steps=len(steps),
                            steps_completed=len(step_outputs),
                        )
                        break

            # nexus-0qi9: per-step progress visibility. Emit start/complete
            # log events at each segment boundary so the silent claude -p
            # chain becomes audible. Without this, a 4-step plan that takes
            # 10+ minutes is indistinguishable from a hang from the caller's
            # seat. Events flow to structlog which the MCP server's logger
            # routes to ~/.config/nexus/logs/mcp.log; downstream ``nx
            # tier-status``-style commands can join on the same session_id.
            _seg_started_at = time.monotonic()
            if isinstance(seg, OperatorBundleSlice):
                _seg_kind = "bundle"
                _seg_indices = list(seg.plan_indices)
                _seg_tools = [_extract_tool(steps[bi]) for bi in seg.plan_indices]
            else:
                _seg_kind = "isolated"
                _seg_indices = [seg.plan_index]
                _seg_tools = [_extract_tool(seg.step)]
            _log.info(
                "nx_answer_step_start",
                kind=_seg_kind,
                step_indices=_seg_indices,
                tools=_seg_tools,
                total_steps=len(steps),
            )
            # RDR-232 (nexus-jypmj.1.6): the reserved stream merges at one plan step; tell it
            # which one this segment hydrates (the bundle loops below refine it per step).
            _mark_reserved_step(_seg_indices[0], steps)

            if isinstance(seg, OperatorBundleSlice):
                # ── Bundle path: ≥2 contiguous operator steps → single dispatch ──
                deferred_indices = set(seg.plan_indices)
                _bundle_at_boundary = _at_boundary(seg)

                def _build_bundle_steps(capture: bool) -> tuple[list[OperatorBundleStep], list[str], bool]:
                    """Resolve and hydrate each step of the bundle: its steps, and (when *capture*) the texts
                    its evidence-consuming steps were handed and whether any was observed. Run again by the
                    coverage repair after it merged its evidence."""
                    built: list[OperatorBundleStep] = []
                    texts_out: list[str] = []
                    observed_out = False
                    for bi in seg.plan_indices:
                        bstep = steps[bi]
                        btool = _extract_tool(bstep)
                        b_raw_args = bstep.get("args", {}) or {}
                        b_resolved = _resolve_args(
                            b_raw_args, bindings=merged, step_outputs=step_outputs,
                            deferred_step_indices=deferred_indices, step_index=bi,
                        )
                        # nexus-4jj40: strip a literal $intent from any
                        # items/inputs-shaped list arg before it reaches the
                        # bundle composer -- see _drop_intent_from_list_args.
                        b_resolved = _drop_intent_from_list_args(
                            b_resolved, intent=merged.get("intent"),
                        )
                        # Capture source collection(s) BEFORE hydration strips
                        # them from args, so the composer can attach a "source:"
                        # line to the prompt for parallel-branch attribution.
                        source_collections = (
                            b_resolved.get("collections") if "ids" in b_resolved else None
                        )
                        # Operators skip _check_embedding_domain / scope / caller-
                        # scope injection — those are retrieval-tool concerns.
                        _mark_reserved_step(bi, steps)
                        _, b_prepared, _bundle_captured = _hydrate_capturing(btool, b_resolved, capture=capture)
                        if capture:
                            _step_texts, _step_observed = _boundary_evidence(b_prepared, _bundle_captured)
                            texts_out.extend(_step_texts)
                            observed_out = observed_out or _step_observed
                        # RDR-093 S-1: strip the runner-internal truncation
                        # marker so it never leaks into the bundled prompt.
                        # Surface-on-bundle is out of scope for this RDR
                        # (nexus-3j6b tracks cross-operator generalisation
                        # including bundle-aware metadata propagation); the
                        # structlog warning still fires from _hydrate.
                        b_prepared.pop("_truncation_metadata", None)
                        # nexus-mm5tx follow-up (code-review T2 [24199]):
                        # strip the zero-evidence marker so it never leaks
                        # into the bundled prompt, same as the truncation
                        # marker above. KNOWN LIMITATION, disclosed rather
                        # than silently absorbed: unlike the isolated
                        # dispatch path (_default_dispatcher), the bundle
                        # path does NOT skip dispatch for a step with zero
                        # evidence — composing a fused multi-step prompt
                        # with one step excluded is out of scope here (the
                        # composer has no per-step exclusion mechanism, and
                        # q12's actual repro is on the isolated path — a
                        # bare store_get_many step never bundles). Warn so
                        # this is at least observable.
                        if b_prepared.pop("_zero_evidence_text", None) is not None:
                            _log.warning(
                                "nx_answer_zero_evidence_in_bundle_not_short_circuited",
                                step_index=bi, tool=btool,
                            )
                        built.append(OperatorBundleStep(
                            plan_index=bi, tool=btool, args=b_prepared,
                            source_collections=source_collections,
                        ))
                    return built, texts_out, observed_out

                bundle_steps, _boundary_texts, _boundary_observed = _build_bundle_steps(_bundle_at_boundary)
                bundle = OperatorBundle(steps=tuple(bundle_steps))

                # RDR-232 Phase 3 (nexus-jypmj.3.1): the coverage boundary, on the bundle's hydrated input,
                # before the one fused dispatch. A skip leaves one output per plan step. The repair
                # (nexus-jypmj.3.2) re-reads the input through the same builder and replaces the bundle.
                if _bundle_at_boundary:
                    def _rehydrate_bundle() -> tuple[list[str], bool]:
                        nonlocal bundle
                        rebuilt, rebuilt_texts, rebuilt_observed = _build_bundle_steps(True)
                        bundle = OperatorBundle(steps=tuple(rebuilt))
                        return rebuilt_texts, rebuilt_observed

                    _cov = await _run_coverage_boundary(
                        _boundary_texts, _boundary_observed, seg.plan_indices[0], _rehydrate_bundle,
                    )
                    _seg_started_at += _boundary_s
                    if _cov is not None and _cov.skip_generation:
                        for _ in range(len(seg.plan_indices) - 1):
                            step_outputs.append(dict(BUNDLED_INTERMEDIATE))
                        step_outputs.append(_coverage_skipped_output())
                        _log.info(
                            "nx_answer_coverage_generation_skipped", kind="bundle",
                            step_indices=_seg_indices, reason=_cov.reason,
                        )
                        continue
                    if _cut_pending:
                        _cut_here(_cut_start_index)
                        break

                # Pre-dispatch size guard. If the composite prompt would
                # blow past the bundle budget, fall back to per-step
                # dispatch for this segment so we don't overflow the
                # claude -p context or produce truncated output. The
                # fallback re-resolves each step's args WITHOUT deferred
                # indices so intra-bundle $stepN refs resolve against real
                # accumulated step_outputs. (substantive-critic Obs B)
                prompt, _schema = compose_bundle_prompt(bundle)
                if len(prompt) > MAX_BUNDLE_PROMPT_CHARS:
                    _log.warning(
                        "bundle_oversized_fallback_to_per_step",
                        prompt_chars=len(prompt),
                        max_chars=MAX_BUNDLE_PROMPT_CHARS,
                        bundle_plan_indices=list(seg.plan_indices),
                    )
                    for bi in seg.plan_indices:
                        _mark_reserved_step(bi, steps)
                        bstep = steps[bi]
                        btool = _extract_tool(bstep)
                        b_raw_args = bstep.get("args", {}) or {}
                        b_resolved = _resolve_args(
                            b_raw_args, bindings=merged,
                            step_outputs=step_outputs, step_index=bi,
                        )
                        # nexus-4jj40: see the main bundle path's identical
                        # call above -- this is the oversized-bundle,
                        # per-step-dispatch fallback of the same segment.
                        b_resolved = _drop_intent_from_list_args(
                            b_resolved, intent=merged.get("intent"),
                        )
                        # RDR-196 .p1b: per-bi timing, NOT the segment-aggregate
                        # timer below — this loop dispatches N separate
                        # ToolDispatcher calls (the oversized-bundle fallback),
                        # so each gets its own real elapsed_ms rather than a
                        # divided share of the aggregate (same no-fabrication
                        # principle the bead states for cost).
                        _bi_started_at = time.monotonic()
                        # RDR-196 .p1b Gap-1 addendum: same ambient-sink scope
                        # as the isolated path — this loop dispatches through
                        # the identical ToolDispatcher abstraction.
                        _bi_usage: list[Any] = []
                        try:
                            with _dispatch_mod.ambient_usage_sink(_bi_usage):
                                raw = dispatch(btool, b_resolved)
                                if inspect.iscoroutine(raw):
                                    result = await raw
                                else:
                                    result = raw
                        except Exception as exc:
                            # nexus-nyry9.4 review-fix: _default_dispatcher
                            # doesn't know the step index (see its docstring
                            # note); patch in the real one here, where ``bi``
                            # is in scope, before this propagates further.
                            if (
                                isinstance(exc, PlanRunOperatorArgMissingError)
                                and exc.step_index < 0
                            ):
                                raise PlanRunOperatorArgMissingError(
                                    step_index=bi,
                                    tool=exc.tool,
                                    missing_arg=exc.missing_arg,
                                ) from exc
                            if not _is_operator_error(exc):
                                raise
                            # nexus-l0yh: graceful degrade — substitute
                            # sentinel, log, continue. Bundle fallback
                            # path: each step gets its own sentinel so the
                            # plan can still surface partial results.
                            _log.warning(
                                "operator_step_failed",
                                kind="bundle_fallback",
                                step_index=bi,
                                tool=btool,
                                error=str(exc),
                            )
                            step_outputs.append(_operator_failed_sentinel(
                                tool=btool, step_index=bi, message=str(exc),
                            ))
                            # RDR-196 .p1b: a step that errors still produces
                            # a record — ok=False, real usage when a raise
                            # site still appended it (.p1a contract), read
                            # back via the ambient sink.
                            _record_step(
                                step_index=bi, operator=btool,
                                source="llm" if is_operator_tool(btool) else "sql",
                                elapsed_ms=int((time.monotonic() - _bi_started_at) * 1000),
                                ok=False, usage=_rollup_step_usage(_bi_usage),
                            )
                            continue
                        if not isinstance(result, dict):
                            raise PlanRunStepRefError(
                                ref=f"step{bi + 1}",
                                reason=(
                                    f"tool {btool!r} returned "
                                    f"{type(result).__name__}; expected dict "
                                    "(bundle fallback path)"
                                ),
                            )
                        b_source, result = _step_source(btool, result)
                        step_outputs.append(result)
                        _record_step(
                            step_index=bi, operator=btool, source=b_source,
                            elapsed_ms=int((time.monotonic() - _bi_started_at) * 1000),
                            ok=True, usage=_rollup_step_usage(_bi_usage),
                        )
                    _log.info(
                        "nx_answer_step_complete",
                        kind="bundle_fallback",
                        step_indices=_seg_indices,
                        tools=_seg_tools,
                        elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    )
                    continue

                # nexus-h33x8.6 a4: an active deadline threads the REMAINING
                # budget through as the bundle's own timeout, so the
                # subprocess is actually cut at the deadline rather than
                # running to the default 300s regardless.
                _bundle_kwargs: dict[str, Any] = {}
                if deadline is not None:
                    _bundle_kwargs["timeout"] = max(0.0, deadline - time.monotonic())
                # RDR-196 .p1b: the ONE genuine claude_dispatch call site this
                # module owns directly (every operator_* MCP tool owns its
                # own internal call, out of reach from here — see StepRecord's
                # docstring). A fresh list per bundle segment; DispatchUsage is
                # appended whenever a result envelope parsed, even on some
                # subsequent raises (.p1a contract) — read back below.
                _bundle_usage: list[Any] = []
                # nexus-ek8tr: bundles are strong by construction (they fuse
                # synthesis operators) — pin them to STRONG_DEFAULT_ALIAS
                # explicitly on every path except the kill switch, instead
                # of inheriting the box CLI default bare.
                if _os.environ.get("NX_OPERATOR_MODEL_TIERING") != "0":
                    from nexus.operators.model_tiers import STRONG_DEFAULT_ALIAS as _SDA  # noqa: PLC0415 — default-path pin (nexus-ek8tr)

                    _bundle_kwargs["model"] = _SDA
                try:
                    bundle_result = await dispatch_bundle(
                        bundle, usage_sink=_bundle_usage, **_bundle_kwargs,
                    )
                except Exception as exc:
                    if deadline is not None and isinstance(exc, _dispatch_mod.OperatorTimeoutError):
                        # a4: capture the reconstructed partial content on
                        # the terminal slot and STOP — unlike the
                        # deadline-less path below, we do not keep going
                        # past a budget-driven timeout.
                        _log.warning(
                            "nx_answer_budget_operator_timeout",
                            kind="bundle",
                            step_indices=list(seg.plan_indices),
                            partial_chars=len(exc.partial_text),
                            event_count=exc.event_count,
                        )
                        for _ in range(len(seg.plan_indices) - 1):
                            step_outputs.append(dict(BUNDLED_INTERMEDIATE))
                        step_outputs.append(_operator_timeout_sentinel(
                            tool=_extract_tool(steps[seg.plan_indices[-1]]),
                            step_index=seg.plan_indices[-1],
                            exc=exc,
                        ))
                        budget_exhausted_at_step = seg.plan_indices[0] + 1
                        budget_exhausted_kind = "time"
                        # A timeout NEVER reaches claude_dispatch's usage_sink
                        # append (.p1a contract) — usage is genuinely None,
                        # never fabricated. Still one record: one dispatch
                        # was attempted regardless of outcome.
                        _record_step(
                            step_index=seg.plan_indices[0],
                            operator="+".join(_seg_tools), source="bundle",
                            elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                            ok=False, bundled_steps=list(seg.plan_indices),
                        )
                        break
                    if not _is_operator_error(exc):
                        raise
                    # nexus-l0yh: bundle dispatch covers every plan_index
                    # in the segment, so on failure push one sentinel per
                    # index. Downstream refs to the terminal slot now
                    # resolve to the failed-sentinel; the planner sees
                    # ``status: failed`` if it inspects step output, or
                    # falls through to empty fields for $stepN.<field>.
                    _log.warning(
                        "operator_step_failed",
                        kind="bundle",
                        step_indices=list(seg.plan_indices),
                        error=str(exc),
                    )
                    for bi in seg.plan_indices:
                        step_outputs.append(_operator_failed_sentinel(
                            tool=_extract_tool(steps[bi]),
                            step_index=bi,
                            message=str(exc),
                        ))
                    # RDR-196 .p1b: ONE record for the whole segment (one
                    # dispatch_bundle call failed), not N — mirrors the
                    # success-path "never divide/duplicate a fused dispatch's
                    # telemetry" rule. Several raise sites still append real
                    # usage before raising (.p1a contract); read it back when
                    # present rather than assuming None.
                    _record_step(
                        step_index=seg.plan_indices[0],
                        operator="+".join(_seg_tools), source="bundle",
                        elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                        ok=False, bundled_steps=list(seg.plan_indices),
                        usage=_bundle_usage[-1] if _bundle_usage else None,
                    )
                    continue
                # Intermediate slots: sentinel. Terminal slot: the real
                # output. Downstream $stepN.<field> refs to an intermediate
                # raise a specific "inside a bundle" error via
                # _resolve_value's sentinel-aware check.
                for _ in range(len(seg.plan_indices) - 1):
                    step_outputs.append(dict(BUNDLED_INTERMEDIATE))
                if not isinstance(bundle_result, dict):
                    raise PlanRunStepRefError(
                        ref=f"step{seg.end_index + 1}",
                        reason=(
                            f"operator bundle returned {type(bundle_result).__name__}; "
                            "expected dict"
                        ),
                    )
                step_outputs.append(bundle_result)
                _log.info(
                    "nx_answer_step_complete",
                    kind="bundle",
                    step_indices=_seg_indices,
                    tools=_seg_tools,
                    elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                )
                # RDR-196 .p1b: exactly ONE record for the whole fused
                # dispatch — never one per bundled_steps index, and never a
                # per-step cost fabricated by dividing the bundle's real cost.
                _record_step(
                    step_index=seg.plan_indices[0],
                    operator="+".join(_seg_tools), source="bundle",
                    elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    ok=True, bundled_steps=list(seg.plan_indices),
                    usage=_bundle_usage[-1] if _bundle_usage else None,
                )
                continue

            # ── Isolated path: IsolatedStep → one dispatcher call ──
            assert isinstance(seg, IsolatedStep)
            index = seg.plan_index
            step = seg.step
            tool = _extract_tool(step)
            raw_args = step.get("args", {}) or {}
            scope = step.get("scope")

            resolved = _resolve_args(
                raw_args, bindings=merged, step_outputs=step_outputs, step_index=index,
            )
            # nexus-4jj40: strip a literal $intent from any items/inputs-
            # shaped list arg before this step dispatches -- see
            # _drop_intent_from_list_args.
            resolved = _drop_intent_from_list_args(
                resolved, intent=merged.get("intent"),
            )
            _check_embedding_domain(index, tool, scope, resolved)
            # SC-3: forward scope.taxonomy_domain → corpus and scope.topic
            # → topic into the dispatched call. Runs after the cross-
            # embedding guard so guard-violating scopes still raise even
            # when the scope-driven corpus injection would have masked
            # the inconsistency.
            resolved = _apply_scope_to_args(
                tool, scope, resolved,
                bindings=merged, step_outputs=step_outputs,
            )
            # nexus-zs1d Phase 1: caller-supplied scope (from nx_answer's
            # ``scope`` argument, propagated via the ``_nx_scope`` binding)
            # reaches the corpus. Runs after _apply_scope_to_args so a
            # taxonomy_domain-derived corpus is narrowed like a declared one.
            # RDR-232 section 2a (nexus-jypmj.1.3): the same hook now NARROWS a
            # plan-declared corpus to the caller's scope instead of deferring to it.
            # A step whose corpus has nothing under the scope is a scope conflict:
            # it is skipped (never run unscoped) and contributes an empty structured
            # result plus a ``scope_conflict`` marker, so a later ``$stepN.field``
            # reference resolves to the empty value.
            resolved, _narrowing = _apply_caller_scope_to_args(
                tool, resolved, bindings=merged,
            )
            if _narrowing is not None:
                scope_diagnostics.append(
                    _narrowing.to_diagnostic(step_index=index, tool=tool)
                )
                if _narrowing.conflict:
                    _log.warning(
                        "nx_answer_scope_conflict_step_skipped",
                        step_index=index, tool=tool,
                        plan_corpus=_narrowing.plan_corpus,
                        caller_scope=_narrowing.scope.value,
                    )
                    resolved_step_args.append({
                        "step_index": index,
                        "tool": tool,
                        "corpus": _narrowing.plan_corpus or "",
                        "query": resolved.get("query") or resolved.get("question") or "",
                        "skipped": True,
                    })
                    step_outputs.append(_scope_conflict_output(
                        _narrowing, step_index=index, tool=tool,
                    ))
                    _log.info(
                        "nx_answer_step_complete",
                        kind="isolated_skipped_scope_conflict",
                        step_indices=_seg_indices,
                        tools=_seg_tools,
                        elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    )
                    continue
            # RDR-200 Phase 1b (nexus-rl59s): last-resort corpus default for
            # a bare search/query step that still has no corpus after plan,
            # scope, AND caller scope all declined to set one. Runs after
            # _apply_caller_scope_to_args so a plan-declared corpus,
            # scope.taxonomy_domain, and a caller's _nx_scope binding all
            # take precedence over this fall-through.
            resolved = _apply_default_corpus_to_args(tool, resolved)
            # RDR-232 (nexus-jypmj.1.18): an explicit owner narrows the corpus to its own project's
            # collections (knowledge passes). A step left with no collection is skipped like a scope
            # conflict: empty structured result plus a ``scope_conflict`` marker, never run unscoped.
            resolved, _owner_narrowing = await _apply_owner_narrowing_to_args(tool, resolved, bindings=merged)
            if _owner_narrowing is not None:
                scope_diagnostics.append(_owner_narrowing.to_diagnostic(step_index=index, tool=tool))
                if _owner_narrowing.conflict:
                    _log.warning(
                        "nx_answer_owner_narrowing_step_skipped",
                        step_index=index, tool=tool, plan_corpus=_owner_narrowing.plan_corpus,
                        owners=list(_owner_narrowing.owners), detail=_owner_narrowing.detail,
                    )
                    resolved_step_args.append({
                        "step_index": index,
                        "tool": tool,
                        "corpus": _owner_narrowing.plan_corpus or "",
                        "query": resolved.get("query") or resolved.get("question") or "",
                        "skipped": True,
                    })
                    _skipped = _scope_conflict_output(_owner_narrowing, step_index=index, tool=tool)
                    _skipped["scope_conflict"]["owners"] = list(_owner_narrowing.owners)
                    step_outputs.append(_skipped)
                    _log.info(
                        "nx_answer_step_complete",
                        kind="isolated_skipped_owner_narrowing",
                        step_indices=_seg_indices,
                        tools=_seg_tools,
                        elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    )
                    continue
            # RDR-232 section 3 (nexus-jypmj.1.5): a traverse step carries the evidence policy
            # derived from the caller's scope, applied inside ``traverse`` before manifest expansion.
            resolved = _apply_traversal_policy_to_args(tool, resolved, bindings=merged)
            # nexus-h3e2: ``mode: broad`` is an authoring affordance for
            # abstract / community-summary plans whose per-corpus default
            # threshold drops 100% of candidates. Runs last so an explicit
            # ``threshold`` set by the plan author always wins.
            resolved = _apply_mode_to_args(tool, resolved)

            # nexus-ivv4d code-review follow-up (T2 [24198]): capture the
            # FULLY RESOLVED corpus/query — after every fall-through above
            # has run — before dispatch, so a caller reading
            # PlanResult.resolved_step_args afterward sees what actually
            # searched even when the step errors or returns zero evidence
            # (a result envelope's own "collections" field is empty in
            # exactly that case, so it can never substitute for this).
            if tool in _CORPUS_QUERY_TOOLS:
                resolved_step_args.append({
                    "step_index": index,
                    "tool": tool,
                    "corpus": resolved.get("corpus", ""),
                    "query": resolved.get("query") or resolved.get("question") or "",
                    # nexus-jypmj.1.16: a catalog-subtree filter the step ran under is part of what it searched.
                    **({"subtree": resolved["subtree"]} if resolved.get("subtree") else {}),
                    # RDR-232 Phase 3 fix round: so are its metadata filters; the coverage repair asks under the same ones.
                    **({"filters": _flt} if (_flt := {
                        k: resolved[k] for k in ("where", "content_type", "author")
                        if isinstance(resolved.get(k), str) and resolved[k]
                    }) else {}),
                })

            # RDR-232 Phase 3 (nexus-jypmj.3.1): the coverage boundary, on the terminal step's input,
            # BEFORE the timeout below is computed (the assessment spends part of the deadline) and before
            # the step dispatches. A dispatcher that hydrates (the default one) is handed the args this
            # hydration produced, once; any other is handed the resolved args as before.
            _prehydrated = False
            if _at_boundary(seg):
                _captured: list[list[str]] = []
                # The args as they stand before this step's hydration, for the repair's second reading
                # (hydration rewrites an evidence argument in place).
                _unhydrated = dict(resolved)
                if getattr(dispatch, _HYDRATES_ATTR, False):
                    try:
                        _, _hydrated_args, _captured = _hydrate_capturing(tool, resolved, capture=True)
                        resolved, _prehydrated = _hydrated_args, True
                    except Exception as exc:  # noqa: BLE001 — leave it to the dispatch below, which raises where it always did
                        _log.warning("nx_answer_coverage_prehydration_failed", error=type(exc).__name__)
                        _captured = []
                _step_texts, _step_observed = _boundary_evidence(resolved, _captured)

                def _rehydrate_isolated() -> tuple[list[str], bool]:
                    """The terminal step's input with the repair evidence merged. The step is handed the args
                    this reading produced, whatever the dispatcher does (the merge is the runner's)."""
                    nonlocal resolved, _prehydrated
                    _, again, again_captured = _hydrate_capturing(tool, dict(_unhydrated), capture=True)  # noqa: B023
                    resolved, _prehydrated = again, True
                    return _boundary_evidence(again, again_captured)

                _cov = await _run_coverage_boundary(_step_texts, _step_observed, index, _rehydrate_isolated)
                _seg_started_at += _boundary_s
                if _cov is not None and _cov.skip_generation:
                    step_outputs.append(_coverage_skipped_output())
                    _log.info(
                        "nx_answer_coverage_generation_skipped", kind="isolated",
                        step_indices=_seg_indices, reason=_cov.reason,
                    )
                    continue
                if _cut_pending:
                    _cut_here(_cut_start_index)
                    break

            # nexus-h33x8.6 a4: an active deadline threads the REMAINING
            # budget into an operator step's own ``timeout`` kwarg (dropped
            # harmlessly by the dispatcher for non-operator tools that don't
            # accept it — see ``_default_dispatcher``'s kwargs-filter).
            if deadline is not None and is_operator_tool(tool):
                resolved = {**resolved, "timeout": max(0.0, deadline - time.monotonic())}

            # Dispatcher may be async (default path, RDR-079 P4) or sync
            # (legacy test fixtures + any caller that prefers the simpler
            # contract). Detect a returned coroutine and await it; treat a
            # returned dict/mapping as the direct result.
            # RDR-196 .p1b Gap-1 addendum (nexus-nyry9.8, 2026-08-20): scope an
            # ambient usage sink around the dispatch so real DispatchUsage is
            # captured even though ``dispatch`` goes through the
            # ToolDispatcher abstraction into an operator_* MCP tool whose
            # OWN internal claude_dispatch call this module never touches
            # directly. See ``_rollup_step_usage`` for the >1-entry rule.
            _step_usage: list[Any] = []
            # nexus-5ezgn: an operator step gets its remaining budget as a
            # ``timeout`` kwarg above. A retrieval step gets it here, as a
            # wait_for around its dispatch; the call_deadline it sets tells
            # the default dispatcher to run the sync tool off the event loop
            # (so the wait can fire) and lets the tool stop itself at a stage
            # boundary (DeadlineExceeded). Operator steps stay out of this:
            # cancelling their coroutine mid-read would strand the
            # persistent operator pool's pipes.
            _cut_retrieval = deadline is not None and not is_operator_tool(tool)
            _deadline_token = call_deadline.set_deadline(deadline) if _cut_retrieval else None
            _prehydrated_token = _PREHYDRATED.set(True) if _prehydrated else None
            try:
                with _dispatch_mod.ambient_usage_sink(_step_usage):
                    raw = dispatch(tool, resolved)
                    if inspect.iscoroutine(raw):
                        if _cut_retrieval:
                            result = await asyncio.wait_for(
                                raw, timeout=max(0.0, deadline - time.monotonic()),
                            )
                        else:
                            result = await raw
                    else:
                        result = raw
            except (asyncio.TimeoutError, call_deadline.DeadlineExceeded):
                if not _cut_retrieval:
                    raise
                _log.warning(
                    "nx_answer_budget_retrieval_timeout",
                    step_index=index,
                    tool=tool,
                    elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                )
                step_outputs.append(_retrieval_timeout_sentinel(
                    tool=tool, step_index=index,
                    budget_s=deadline - _seg_started_at,
                ))
                budget_exhausted_at_step = index + 1
                budget_exhausted_kind = "time"
                _record_step(
                    step_index=index, operator=tool, source="sql",
                    elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    ok=False, usage=_rollup_step_usage(_step_usage),
                )
                break
            except Exception as exc:
                # nexus-nyry9.4 review-fix: _default_dispatcher doesn't know
                # the step index (see its docstring note); patch in the real
                # one here, where ``index`` is in scope, before this
                # propagates further.
                if (
                    isinstance(exc, PlanRunOperatorArgMissingError)
                    and exc.step_index < 0
                ):
                    raise PlanRunOperatorArgMissingError(
                        step_index=index,
                        tool=exc.tool,
                        missing_arg=exc.missing_arg,
                    ) from exc
                if deadline is not None and isinstance(exc, _dispatch_mod.OperatorTimeoutError):
                    # a4: capture partial content and STOP — unlike the
                    # deadline-less path below, a budget-driven timeout does
                    # not keep going.
                    _log.warning(
                        "nx_answer_budget_operator_timeout",
                        kind="isolated",
                        step_index=index,
                        tool=tool,
                        partial_chars=len(exc.partial_text),
                        event_count=exc.event_count,
                    )
                    step_outputs.append(_operator_timeout_sentinel(
                        tool=tool, step_index=index, exc=exc,
                    ))
                    budget_exhausted_at_step = index + 1
                    budget_exhausted_kind = "time"
                    # RDR-196 .p1b: a timeout never reaches usage_sink's
                    # append (.p1a contract) — usage genuinely None (the
                    # ambient sink is symmetric: it appends at the same site
                    # usage_sink does, so it is equally empty here). Still
                    # one record: a dispatch was attempted.
                    _record_step(
                        step_index=index, operator=tool,
                        source="llm" if is_operator_tool(tool) else "sql",
                        elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                        ok=False, usage=_rollup_step_usage(_step_usage),
                    )
                    break
                if not _is_operator_error(exc):
                    raise
                # nexus-l0yh: graceful degrade for the isolated-step path.
                # Substitute sentinel, log, continue with the next step.
                _log.warning(
                    "operator_step_failed",
                    kind="isolated",
                    step_index=index,
                    tool=tool,
                    error=str(exc),
                )
                step_outputs.append(_operator_failed_sentinel(
                    tool=tool, step_index=index, message=str(exc),
                ))
                # RDR-196 .p1b: a step that errors still produces a record
                # (ok=False) — a telemetry layer that only records successes
                # measures the wrong population. Several raise sites still
                # append real usage before raising (.p1a contract) — read it
                # back via the ambient sink rather than assuming None.
                _record_step(
                    step_index=index, operator=tool,
                    source="llm" if is_operator_tool(tool) else "sql",
                    elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                    ok=False, usage=_rollup_step_usage(_step_usage),
                )
                continue
            finally:
                if _deadline_token is not None:
                    call_deadline.reset(_deadline_token)
                if _prehydrated_token is not None:
                    _PREHYDRATED.reset(_prehydrated_token)
            if not isinstance(result, dict):
                # Tool authors must follow the documented output contract;
                # surface non-dict returns explicitly rather than letting
                # downstream $stepN.field substitution silently fail.
                raise PlanRunStepRefError(
                    ref=f"step{index + 1}",
                    reason=(
                        f"tool {tool!r} returned {type(result).__name__}; "
                        f"expected dict per RDR-078 §Phase 1"
                    ),
                )
            source, result = _step_source(tool, result)
            step_outputs.append(result)
            _log.info(
                "nx_answer_step_complete",
                kind="isolated",
                step_indices=_seg_indices,
                tools=_seg_tools,
                elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
            )
            _record_step(
                step_index=index, operator=tool, source=source,
                elapsed_ms=int((time.monotonic() - _seg_started_at) * 1000),
                ok=True, usage=_rollup_step_usage(_step_usage),
            )

    except Exception as exc:  # noqa: BLE001 — boundary re-raise; attach-and-propagate, see below
        # RDR-196 .p1d critique fold (T2 [23092], nexus-nyry9.11 DO): a
        # mid-loop exception must not discard completed steps' telemetry --
        # failed runs are exactly the population that produced the
        # 45x-wrong latency docstring (nexus-h33x8.6). Attach the
        # step_records completed so far directly to the exception INSTANCE
        # (no new carrier type -- every raise site inside the loop above,
        # present or future, is covered by this single boundary, whether it
        # raises a fresh exception or re-raises a caught one) so core.py's
        # except-handler can record real per-step data instead of a
        # hardcoded []. step_records is empty when the FIRST segment fails
        # before any _record_step call -- that is accurate, not a bug.
        exc.step_records = sorted(step_records, key=lambda r: r.step_index)  # stable: concurrent assessment calls finish out of order
        raise

    # nexus-4h0oh: computed over the EXECUTED prefix only (step_outputs),
    # never the full parsed `steps` — a budget/continuation cut ending the
    # run early is a different, already-visible condition and must not be
    # reported as discarded evidence (see _discarded_reduce_steps's own
    # docstring). WARN, never refuse — visibility is the fix; see the
    # PlanResult field's docstring for why.
    _dropped_reduce_steps = [
        i + 1 for i in _discarded_reduce_steps(
            steps, step_outputs,
            extract_tool=_extract_tool, is_operator_tool=is_operator_tool,
        )
    ]
    if _dropped_reduce_steps:
        _log.warning(
            "nx_answer_discarded_reduce_steps",
            dropped_step_indices=_dropped_reduce_steps,
            terminal_step=len(step_outputs),
            total_steps=len(steps),
        )

    # The concurrent assessment calls (nexus-jypmj.17) are appended as they finish, in no fixed order; step_index is
    # unique and assigned in plan order, so listing by it makes the envelope's step order the same on every run.
    step_records.sort(key=lambda r: r.step_index)
    return PlanResult(
        steps=step_outputs,
        final=step_outputs[-1] if step_outputs else None,
        budget_exhausted_at_step=budget_exhausted_at_step,
        budget_exhausted_kind=budget_exhausted_kind,
        total_planned_steps=len(steps),
        step_records=step_records,
        continuation_cut_applied=continuation_cut_applied,
        resolved_step_args=resolved_step_args,
        dropped_reduce_steps=_dropped_reduce_steps,
        scope_diagnostics=scope_diagnostics,
        coverage=coverage_result,
    )


async def plan_run(
    match: Match,
    bindings: dict[str, Any] | None = None,
    *,
    dispatcher: ToolDispatcher | None = None,
    bundle_operators: bool = True,
    deadline: float | None = None,
    budget_usd_remaining: float | None = None,
    continuation_cut_at_step: int | None = None,
    reserved_stream: ReservedStream | None = None,
    coverage_boundary: CoverageBoundary | None = None,
) -> PlanResult:
    """Execute *match*'s steps; see :func:`_plan_run_impl` for every parameter but one.

    ``reserved_stream`` (RDR-232 Phase 1, nexus-jypmj.1.6): the named source's primary evidence,
    built by ``nx_answer`` from the resolved sources. Operator hydration merges it ahead of the
    plan's own inputs at the first operator that consumes retrieved evidence (see
    :mod:`nexus.plans.reserved_stream`). ``None`` (the default) runs exactly as before and
    leaves :attr:`PlanResult.reserved_report` ``None``.
    """
    if reserved_stream is not None:
        reserved_stream.reset_run()
    _recorder = active_recorder()
    if _recorder is not None:  # nexus-jypmj.1.8: a whole-plan re-run starts from a clean reducer input
        _recorder.reset_run()
    supplement = RepairSupplement() if coverage_boundary is not None else None
    with reserved_scope(reserved_stream), supplement_scope(supplement):
        result = await _plan_run_impl(
            match, bindings, dispatcher=dispatcher, bundle_operators=bundle_operators,
            deadline=deadline, budget_usd_remaining=budget_usd_remaining,
            continuation_cut_at_step=continuation_cut_at_step, coverage_boundary=coverage_boundary,
        )
    if supplement is not None:
        result = _dc_replace(result, repair_supplement=supplement)
    if reserved_stream is not None:
        result = _dc_replace(result, reserved_report=reserved_stream.report())
    return result
