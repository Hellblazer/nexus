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

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from nexus.plans.match import Match

__all__ = [
    "PlanResult",
    "PlanRunBindingError",
    "PlanRunStepRefError",
    "PlanRunEmbeddingDomainError",
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
    """Callable that invokes an MCP tool by name with a kwargs dict."""

    def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


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

#: Args keys that may carry a collection name. The runner extracts
#: candidates from these to validate the cross-embedding guard, and
#: skips corpus injection when any of these are populated.
_COLLECTION_ARG_KEYS: tuple[str, ...] = ("collection", "collections")


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


def _resolve_value(
    value: Any,
    *,
    bindings: dict[str, Any],
    step_outputs: list[dict[str, Any]],
) -> Any:
    """Substitute one arg value.

    ``$var`` and ``$stepN.field`` substitutions only fire on values
    that are exactly that single token — no inline interpolation.
    Lists are resolved element-wise so callers can write e.g.
    ``seeds: [$step1.tumblers, $step2.tumblers]`` — each element
    resolves independently, list-valued elements are flattened one
    level so the final ``seeds`` is a flat list of tumblers. Non-list,
    non-string values pass through unchanged.
    """
    if isinstance(value, list):
        resolved: list[Any] = []
        for item in value:
            r = _resolve_value(item, bindings=bindings, step_outputs=step_outputs)
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
        if step_idx < 0 or step_idx >= len(step_outputs):
            raise PlanRunStepRefError(
                ref=value,
                reason=f"step {step_idx + 1} has not produced output yet",
            )
        prior = step_outputs[step_idx]
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
) -> dict[str, Any]:
    return {
        key: _resolve_value(val, bindings=bindings, step_outputs=step_outputs)
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


# ── Bindings ────────────────────────────────────────────────────────────────


def _validate_bindings(match: Match, bindings: dict[str, Any]) -> None:
    missing = [
        name for name in match.required_bindings
        if name not in bindings
    ]
    if missing:
        raise PlanRunBindingError(missing=missing)


# ── Default dispatcher (lazy MCP-tool wiring) ───────────────────────────────


def _default_dispatcher(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch *tool* against the live MCP tool registry.

    Resolves the tool callable lazily from :mod:`nexus.mcp.core` so the
    runner module imports cheaply and tests can swap it out without
    touching the MCP server.
    """
    from nexus.mcp import core as mcp_core

    fn = getattr(mcp_core, tool, None)
    if fn is None or not callable(fn):
        raise PlanRunToolNotFoundError(
            tool=tool,
            reason=f"not present in nexus.mcp.core",
        )
    return fn(**args)  # type: ignore[no-any-return]


# ── Public API ──────────────────────────────────────────────────────────────


def plan_run(
    match: Match,
    bindings: dict[str, Any] | None = None,
    *,
    dispatcher: ToolDispatcher | None = None,
) -> PlanResult:
    """Execute the steps in *match* and return the captured outputs.

    ``bindings`` are the caller's substitutions. They are merged on top
    of ``match.default_bindings`` (caller wins on conflict).
    """
    caller = bindings or {}
    merged: dict[str, Any] = {**match.default_bindings, **caller}
    _validate_bindings(match, merged)

    plan = json.loads(match.plan_json)
    steps = plan.get("steps", []) or []

    dispatch: ToolDispatcher = dispatcher or _default_dispatcher
    step_outputs: list[dict[str, Any]] = []

    for index, step in enumerate(steps):
        tool = step.get("tool", "")
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

        result = dispatch(tool, resolved)
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
