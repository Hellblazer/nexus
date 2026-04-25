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
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import structlog

from nexus.plans.match import Match

_log = structlog.get_logger(__name__)

__all__ = [
    "PlanResult",
    "PlanRunBindingError",
    "PlanRunEmbeddingDomainError",
    "PlanRunOperatorOutputError",
    "PlanRunOperatorSchemaVersionError",
    "PlanRunOperatorUnavailableError",
    "PlanRunStepRefError",
    "PlanRunToolNotFoundError",
    "ToolDispatcher",
    "plan_run",
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
class PlanResult:
    """Captured output of a :func:`plan_run` execution."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] | None = None


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
    {"search", "query", "store_get_many"},
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
    from nexus.corpus import index_model_for_collection

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
        except Exception:
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
    from nexus.mcp import core as mcp_core

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
    from nexus.mcp import core as mcp_core

    # Auto-hydration + arg normalization: shared with the bundle path.
    resolved_tool, args = _hydrate_operator_args(tool, args)

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
    try:
        sig = inspect.signature(fn)
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
    except (TypeError, ValueError):
        # Builtins / C-level callables can't always be inspected; leave
        # args untouched and let the call site surface any TypeError.
        pass

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
    # step still conforms to ``{ids, tumblers, distances, collections}``
    # and downstream ``$stepN.tumblers`` refs don't crash; preserve the
    # error text in an ``error`` key for visibility.
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
    """
    from nexus.plans.bundle import (
        BUNDLED_INTERMEDIATE,
        IsolatedStep,
        MAX_BUNDLE_PROMPT_CHARS,
        OperatorBundle,
        OperatorBundleSlice,
        OperatorBundleStep,
        compose_bundle_prompt,
        dispatch_bundle,
        segment_steps,
    )

    def _extract_tool(step: dict[str, Any]) -> str:
        t = step.get("tool") or step.get("op") or step.get("operation") or ""
        if t.startswith("mcp__"):
            t = t.rsplit("__", 1)[-1]
        return t

    caller = bindings or {}
    merged: dict[str, Any] = {**match.default_bindings, **caller}
    _validate_bindings(match, merged)

    plan = json.loads(match.plan_json)
    steps = plan.get("steps", []) or []

    dispatch: ToolDispatcher = dispatcher or _default_dispatcher
    step_outputs: list[dict[str, Any]] = []

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

    for seg in segments:
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
                    raw = dispatch(btool, b_resolved)
                    if inspect.iscoroutine(raw):
                        result = await raw
                    else:
                        result = raw
                    if not isinstance(result, dict):
                        raise PlanRunStepRefError(
                            ref=f"step{bi + 1}",
                            reason=(
                                f"tool {btool!r} returned "
                                f"{type(result).__name__}; expected dict "
                                "(bundle fallback path)"
                            ),
                        )
                    step_outputs.append(result)
                continue

            bundle_result = await dispatch_bundle(bundle)
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

        # Dispatcher may be async (default path, RDR-079 P4) or sync
        # (legacy test fixtures + any caller that prefers the simpler
        # contract). Detect a returned coroutine and await it; treat a
        # returned dict/mapping as the direct result.
        raw = dispatch(tool, resolved)
        if inspect.iscoroutine(raw):
            result = await raw
        else:
            result = raw
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
        step_outputs.append(result)

    return PlanResult(
        steps=step_outputs,
        final=step_outputs[-1] if step_outputs else None,
    )
