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
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import structlog

from nexus.plans.match import Match

_log = structlog.get_logger(__name__)

__all__ = [
    "PlanResult",
    "PlanRunBindingError",
    "PlanRunEmbeddingDomainError",
    "PlanRunOperatorArgMissingError",
    "PlanRunOperatorOutputError",
    "PlanRunOperatorSchemaVersionError",
    "PlanRunOperatorUnavailableError",
    "PlanRunStepRefError",
    "PlanRunToolNotFoundError",
    "PlanRunUnresolvedVarError",
    "StepRecord",
    "ToolDispatcher",
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
            "Run `claude auth login` or set ANTHROPIC_API_KEY to enable "
            "operator-backed plan steps; retrieval-only plans still work."
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

    ``model`` derivation rule (196-R3 / audit fold, binding): always taken
    VERBATIM from the underlying ``DispatchUsage.model`` — which is
    itself already "the single modelUsage entry's canonical id, or
    ``None`` when zero or ≥2 entries make a single value ambiguous" by
    construction in ``dispatch._parse_dispatch_usage``. This field is
    NEVER re-derived from ``model_usage`` keys here, NEVER defaulted to
    a requested/alias model string, and NEVER coerced to ``""`` when
    absent — ``None`` is the only "absent" spelling, matching RDR-196
    risk #1 (a silently-zero/empty telemetry field reads as "this was
    free", which is the exact measurement bug this arc exists to fix).

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
     "search_metadata_scoped", "search_topic_scoped", "search_graph_hop"},
)

#: Args keys that may carry a collection name. The runner extracts
#: candidates from these to validate the cross-embedding guard, and
#: skips corpus injection when any of these are populated.
_COLLECTION_ARG_KEYS: tuple[str, ...] = ("collection", "collections")

#: Binding key used by ``nx_answer`` to propagate the caller-supplied
#: ``scope`` parameter through ``plan_run``. Nexus-zs1d Phase 1: gives
#: the runner a way to honour caller-supplied corpus intent without
#: requiring plan-library schema changes. Plans that pin their own
#: corpus still win; this binding only fills in the gap.
_CALLER_SCOPE_BINDING: str = "_nx_scope"


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
from nexus.plans.bundle import DEFERRED_REF_KEY as _DEFERRED_REF_KEY  # noqa: E402
import nexus.operators.dispatch as _dispatch_mod  # noqa: E402


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
    honest, not fabricated. ``model`` follows the same ambiguous-population
    rule ``DispatchUsage.model`` already applies within a single call: the
    shared canonical id if every entry agrees, else ``None``. Any field
    that is ``None`` on ANY entry makes the summed field ``None`` too
    (never silently treat "unknown" as "zero" mid-sum). The per-model
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
        return prior[field_name]

    m = _VAR_RE.match(value)
    if m is not None:
        var_name = m.group(1)
        # Unknown $var stays as a literal — required-binding validation
        # runs upfront so we can trust everything left here is intentional.
        return bindings.get(var_name, value)

    return value


def _resolve_args(
    args: dict[str, Any],
    *,
    bindings: dict[str, Any],
    step_outputs: list[dict[str, Any]],
    deferred_step_indices: set[int] | None = None,
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """Return *args* with caller-supplied scope forwarded as ``corpus``
    when the plan step is corpus-agnostic (nexus-zs1d Phase 1).

    When ``nx_answer`` is invoked with a ``scope`` argument, it is
    propagated as the ``_nx_scope`` binding. This helper reads that
    binding and fills in ``args["corpus"]`` for retrieval tools that
    haven't already pinned one. Behaviour:

      * Only retrieval tools in :data:`_RETRIEVAL_TOOLS` are affected.
      * Plan-declared ``corpus`` / ``collection`` / ``collections`` win
        (``_collections_in_args`` covers the latter two); this helper
        does not overwrite them.
      * Plan-declared ``scope.taxonomy_domain`` still wins too, because
        :func:`_apply_scope_to_args` runs first and populates ``corpus``
        before this helper sees the args.
      * Empty / missing binding → no-op, existing behaviour preserved.
    """
    if tool not in _RETRIEVAL_TOOLS:
        return args
    override = bindings.get(_CALLER_SCOPE_BINDING)
    if not override:
        return args
    if "corpus" in args or _collections_in_args(args):
        return args
    out = dict(args)
    out["corpus"] = override
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

    if resolved_tool in _OPERATOR_RESOLVED_TOOLS and "ids" in args:
        ids = args["ids"]
        collections = args.get("collections", "knowledge")
        hydrated = mcp_core.store_get_many(
            ids=ids, collections=collections, structured=True,
        )
        contents = hydrated.get("contents", []) if isinstance(hydrated, dict) else []
        non_empty = [c for c in contents if c]
        original_count = len(non_empty)
        truncation_metadata: dict[str, Any] | None = None
        if original_count > _OPERATOR_MAX_INPUTS:
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
    target_key = _INPUTS_TARGET.get(resolved_tool)
    if target_key and "inputs" in args and target_key not in args:
        value = args.pop("inputs")
        if target_key == "items" and isinstance(value, list):
            value = json.dumps(value)
        args[target_key] = value

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

    # Auto-hydration + arg normalization: shared with the bundle path.
    resolved_tool, args = _hydrate_operator_args(tool, args)

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
            # Retrieval error strings usually indicate a plan-binding
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


# ── Public API ──────────────────────────────────────────────────────────────


async def plan_run(
    match: Match,
    bindings: dict[str, Any] | None = None,
    *,
    dispatcher: ToolDispatcher | None = None,
    bundle_operators: bool = True,
    deadline: float | None = None,
    budget_usd_remaining: float | None = None,
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
    :attr:`PlanResult.budget_exhausted_kind` to ``"time"``. Retrieval
    steps are never budget-cut mid-flight — they don't accept a
    ``timeout`` kwarg — only the pre-segment deadline check can stop
    the loop before one starts.

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
        segment_steps,
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
        elif source == "sql":
            model, in_tok, out_tok, cost = None, 0, 0, 0.0
        else:
            model, in_tok, out_tok, cost = None, None, None, None
        step_records.append(StepRecord(
            step_index=step_index, operator=operator, source=source,
            model=model, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=cost, elapsed_ms=elapsed_ms, ok=ok,
            bundled_steps=list(bundled_steps) if bundled_steps else [],
        ))

    # One authoritative segmentation. When bundling is off or the caller
    # supplied a dispatcher that doesn't opt into bundling, flatten the
    # slices back into isolated steps so the per-step path handles
    # everything. Gate is attribute-based so decorator wrappers survive.
    segments: list = segment_steps(steps)
    use_bundle_path = bundle_operators and getattr(
        dispatch, _SUPPORTS_BUNDLING_ATTR, False,
    )
    if not use_bundle_path:
        flat: list = []
        for seg in segments:
            if isinstance(seg, OperatorBundleSlice):
                for pi in seg.plan_indices:
                    flat.append(IsolatedStep(plan_index=pi, step=steps[pi]))
            else:
                flat.append(seg)
        segments = flat

    try:
        for seg in segments:
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

            if isinstance(seg, OperatorBundleSlice):
                # ── Bundle path: ≥2 contiguous operator steps → single dispatch ──
                deferred_indices = set(seg.plan_indices)
                bundle_steps: list[OperatorBundleStep] = []
                for bi in seg.plan_indices:
                    bstep = steps[bi]
                    btool = _extract_tool(bstep)
                    b_raw_args = bstep.get("args", {}) or {}
                    b_resolved = _resolve_args(
                        b_raw_args, bindings=merged, step_outputs=step_outputs,
                        deferred_step_indices=deferred_indices,
                    )
                    # Capture source collection(s) BEFORE hydration strips
                    # them from args, so the composer can attach a "source:"
                    # line to the prompt for parallel-branch attribution.
                    source_collections = (
                        b_resolved.get("collections") if "ids" in b_resolved else None
                    )
                    # Operators skip _check_embedding_domain / scope / caller-
                    # scope injection — those are retrieval-tool concerns.
                    _, b_prepared = _hydrate_operator_args(btool, b_resolved)
                    # RDR-093 S-1: strip the runner-internal truncation
                    # marker so it never leaks into the bundled prompt.
                    # Surface-on-bundle is out of scope for this RDR
                    # (nexus-3j6b tracks cross-operator generalisation
                    # including bundle-aware metadata propagation); the
                    # structlog warning still fires from _hydrate.
                    b_prepared.pop("_truncation_metadata", None)
                    bundle_steps.append(OperatorBundleStep(
                        plan_index=bi, tool=btool, args=b_prepared,
                        source_collections=source_collections,
                    ))
                bundle = OperatorBundle(steps=tuple(bundle_steps))

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
                        bstep = steps[bi]
                        btool = _extract_tool(bstep)
                        b_raw_args = bstep.get("args", {}) or {}
                        b_resolved = _resolve_args(
                            b_raw_args, bindings=merged,
                            step_outputs=step_outputs,
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
                raw_args, bindings=merged, step_outputs=step_outputs,
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
            # fills in the corpus when the plan step is agnostic. Runs after
            # _apply_scope_to_args so plan-declared corpus / taxonomy_domain
            # always wins.
            resolved = _apply_caller_scope_to_args(
                tool, resolved, bindings=merged,
            )
            # nexus-h3e2: ``mode: broad`` is an authoring affordance for
            # abstract / community-summary plans whose per-corpus default
            # threshold drops 100% of candidates. Runs last so an explicit
            # ``threshold`` set by the plan author always wins.
            resolved = _apply_mode_to_args(tool, resolved)

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
            try:
                with _dispatch_mod.ambient_usage_sink(_step_usage):
                    raw = dispatch(tool, resolved)
                    if inspect.iscoroutine(raw):
                        result = await raw
                    else:
                        result = raw
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
        exc.step_records = list(step_records)
        raise

    return PlanResult(
        steps=step_outputs,
        final=step_outputs[-1] if step_outputs else None,
        budget_exhausted_at_step=budget_exhausted_at_step,
        budget_exhausted_kind=budget_exhausted_kind,
        total_planned_steps=len(steps),
        step_records=step_records,
    )
