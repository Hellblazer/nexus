# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan schema helpers for RDR-078.

Two surfaces:

* :func:`canonical_dimensions_json` (P4c) — load-bearing primitive that
  produces the stable string identity used by the ``UNIQUE (project,
  dimensions)`` index on the ``plans`` table. Any caller that persists
  a plan MUST route its dimension map through this function so
  byte-identical identities collapse to byte-identical keys.
* :func:`validate_plan_template` + :class:`PlanTemplateLoader` (P4a) —
  the YAML/JSON template validator that scoped loaders run before
  persisting any plan. Catches malformed templates with named errors
  and rejects identity collisions across loader sources.

Schema reference: RDR-078 §Phase 4a (description, dimensions, parent,
default_bindings, required_bindings, optional_bindings, tags,
plan_json{steps[*]}). Lenient by default (unknown dimensions warn);
``strict=True`` upgrades the warning to a raise.

SC-16, SC-18, SC-19.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

__all__ = [
    "TYPED_BINDING_DOMAINS",
    "TYPED_FILTER_BINDINGS",
    "PlanTemplateDuplicateError",
    "PlanTemplateLoader",
    "PlanTemplateSchemaError",
    "canonical_dimensions_json",
    "unsatisfiable_typed_binding",
    "validate_plan_steps",
    "validate_plan_template",
]

_log = logging.getLogger(__name__)

#: Plan bindings whose domain is enumerated or numeric rather than free
#: text — catalog metadata filters and retrieval knobs. There is no
#: defensible way to derive one from a question string: ``content_type``
#: accepts ``code`` / ``paper`` / ``rdr`` / ``knowledge``, so a
#: 90-character sentence matches no document at all (observed 2026-07-25
#: on builtin plan 14, which returned zero results while the identical
#: query with no ``content_type`` returned the correct paper as its top
#: hit). Lives here rather than in ``nexus.mcp.core`` so the matcher can
#: consult it without importing the MCP layer.
TYPED_FILTER_BINDINGS: frozenset[str] = frozenset({
    "content_type", "author", "subtree", "year",
    "corpus", "collection", "follow_links", "depth", "limit",
})

#: Legal values for the typed bindings with a small closed domain,
#: surfaced in errors so a caller knows what a satisfying value is.
TYPED_BINDING_DOMAINS: dict[str, str] = {
    # NOT a closed domain — the live catalog also carries prose,
    # blog_post and others, and grows as new content is indexed. Phrased
    # as examples so the hint cannot become quietly wrong.
    "content_type": "a catalog content type, e.g. code / prose / rdr / paper / knowledge",
    "year": "a four-digit year",
    "depth": "a positive integer",
    "limit": "a positive integer",
}


#: Step-argument slots that consume a typed value even though the slot
#: name is not itself a binding name. ``seeds`` takes catalog tumblers;
#: prose aliased into it matches nothing, exactly like ``subtree``.
_TYPED_ARG_SLOTS: frozenset[str] = TYPED_FILTER_BINDINGS | frozenset({"seeds"})


#: ``$name`` anywhere in a string. Anchoring to the start would miss an
#: interpolated reference like ``"repo/$area"``, which reaches the typed
#: slot just as completely as a bare one.
_BINDING_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _binding_refs(value: Any) -> set[str]:
    """Binding names *value* feeds into whatever slot holds it.

    Recurses through lists and dicts, and scans within strings rather
    than requiring the whole value to be one reference. The first version
    checked ``isinstance(value, str) and value.startswith("$")`` with no
    recursion, which review found blind to two real shapes — a
    list-valued slot (``ids: [$a, $b]``) and an interpolated one
    (``subtree: "prefix/$area"``). Either would have let a template
    requiring an underivable typed binding pass the offerability gate
    silently, which is the gate failing at exactly its job.

    ``$stepN.field`` is a step reference the runner resolves, not a
    binding, so a name followed by a dot is skipped.
    """
    if isinstance(value, str):
        return {
            m.group(1) for m in _BINDING_REF_RE.finditer(value)
            if not value[m.end():m.end() + 1] == "."
        }
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            out |= _binding_refs(item)
        return out
    if isinstance(value, dict):
        out = set()
        for item in value.values():
            out |= _binding_refs(item)
        return out
    return set()


def typed_by_usage(plan_json: str | dict[str, Any] | None) -> frozenset[str]:
    """Bindings a plan feeds into a TYPED argument slot, whatever their name.

    A binding is typed because of what it DOES, not what it is called, and
    keying the gate on the name alone let the check be bypassed by
    choosing a free-text-sounding one (nexus-7y4v0). ``debug-default``
    declares ``failing_path`` and passes it as ``subtree: $failing_path``;
    ``review-default`` declares ``changed_paths`` and does the same.
    Neither name is in :data:`TYPED_FILTER_BINDINGS`, so the gate passed
    them, ``nx_answer`` aliased the raw question into the slot, and the
    plan ran with a ``subtree`` filter that can match no tumbler — an
    empty result presented as an answer.

    That is the identical failure this module's own docstring records for
    ``content_type`` on plan 14 (zero results, 2026-07-25); the only new
    thing here is that a free-text-looking name hid it from the gate.

    Reference extraction is delegated to :func:`_binding_refs`, which
    recurses through lists and dicts and scans within strings.
    ``$stepN.field`` is a step reference the runner resolves, not a
    binding, and is skipped.
    """
    if not plan_json:
        return frozenset()
    if isinstance(plan_json, str):
        try:
            plan = json.loads(plan_json)
        except (TypeError, ValueError):
            return frozenset()
    else:
        plan = plan_json
    if not isinstance(plan, dict):
        return frozenset()

    found: set[str] = set()
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for slot, value in (step.get("args") or {}).items():
            if slot in _TYPED_ARG_SLOTS:
                found.update(_binding_refs(value))
    return frozenset(found)


def unsatisfiable_typed_binding(
    *,
    required: list[str],
    defaults: dict[str, Any] | None,
    available: frozenset[str],
    plan_json: str | dict[str, Any] | None = None,
) -> str | None:
    """Return the first typed binding nothing can supply, else ``None``.

    A binding is satisfiable when the caller supplies it or the plan
    carries a default for it. Free-text bindings are always satisfiable —
    ``nx_answer`` aliases them from the question text — so only TYPED
    bindings can make a plan unrunnable.

    Typed means either a name in :data:`TYPED_FILTER_BINDINGS` or, when
    *plan_json* is supplied, a binding the plan feeds into a typed
    argument slot (see :func:`typed_by_usage`). *plan_json* is optional
    so pre-existing callers keep working, but omitting it re-opens the
    hole: pass it wherever the plan is available.
    """
    have = defaults or {}
    typed = TYPED_FILTER_BINDINGS | typed_by_usage(plan_json)
    for req in required:
        if req in typed and req not in available and req not in have:
            return req
    return None


# ── Canonical-JSON primitive (P4c) ──────────────────────────────────────────


def canonical_dimensions_json(dimensions: dict[str, Any]) -> str:
    """Serialise a dimensional identity map to canonical JSON.

    ``{"verb":"r","scope":"g"}`` and ``{"scope":"g","verb":"r"}`` both
    produce ``'{"scope":"g","verb":"r"}'`` — same bytes, same dedup key.

    Rules:
      * Keys are sorted and lowercased.
      * String values are lowercased; non-string values (int/bool) are
        preserved as-is so dimensions like ``depth: 3`` stay typed.
      * JSON output has no whitespace.
    """
    normalised: dict[str, Any] = {}
    for key, value in dimensions.items():
        norm_key = key.lower()
        norm_value = value.lower() if isinstance(value, str) else value
        normalised[norm_key] = norm_value
    return json.dumps(normalised, sort_keys=True, separators=(",", ":"))


# ── Errors (P4a) ────────────────────────────────────────────────────────────


class PlanTemplateSchemaError(ValueError):
    """Raised when a plan template fails validation."""


class PlanTemplateDuplicateError(ValueError):
    """Raised when two plan templates collide on canonical identity.

    Carries both source labels so the operator can locate the conflict.
    """

    def __init__(
        self, *, identity: str, original: str, duplicate: str,
    ) -> None:
        self.identity = identity
        self.original = original
        self.duplicate = duplicate
        super().__init__(
            f"plan template identity collision on {identity!r}: "
            f"already declared by {original!r}; rejected duplicate at "
            f"{duplicate!r}"
        )


# ── Constants ───────────────────────────────────────────────────────────────

#: Dimensions every plan template must pin (RDR-078 §Phase 4a).
_REQUIRED_DIMENSIONS: tuple[str, ...] = ("verb", "scope")

#: Tools that take graph-traversal arguments. Only these may carry
#: ``link_types`` / ``purpose``; the SC-16 mutual-exclusion check
#: applies here.
_TRAVERSAL_TOOLS: frozenset[str] = frozenset({"traverse"})


# ── Template validator (P4a) ────────────────────────────────────────────────


def validate_plan_template(
    template: dict[str, Any],
    *,
    registered_dimensions: set[str] | None = None,
    strict: bool = False,
) -> None:
    """Raise :class:`PlanTemplateSchemaError` if *template* is malformed.

    Required: ``description`` (non-empty string), ``dimensions`` dict
    with at minimum ``verb`` and ``scope``, ``plan_json`` with a
    ``steps`` list (may be empty).

    SC-16: traverse steps must declare *either* ``link_types`` or
    ``purpose`` — never both.

    SC-19: when *registered_dimensions* is supplied, dimensions
    outside the set produce a structured warning by default. With
    ``strict=True`` they upgrade to a raise — used by CI gates.
    """
    if not isinstance(template, dict):
        raise PlanTemplateSchemaError(
            f"plan template must be a mapping, got {type(template).__name__}"
        )

    description = template.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PlanTemplateSchemaError(
            "plan template requires a non-empty 'description'"
        )

    dimensions = template.get("dimensions")
    if not isinstance(dimensions, dict):
        raise PlanTemplateSchemaError(
            "plan template requires a 'dimensions' mapping"
        )
    for required in _REQUIRED_DIMENSIONS:
        if not dimensions.get(required):
            raise PlanTemplateSchemaError(
                f"plan template dimensions must pin {required!r} "
                f"(got {sorted(dimensions.keys())})"
            )

    if registered_dimensions is not None:
        unknown = set(dimensions.keys()) - registered_dimensions
        if unknown:
            msg = (
                f"plan template uses unregistered dimension(s) "
                f"{sorted(unknown)}; registered: {sorted(registered_dimensions)}"
            )
            if strict:
                raise PlanTemplateSchemaError(msg)
            _log.warning("plan_template_unknown_dimension: %s", msg)

    plan_json = template.get("plan_json")
    if not isinstance(plan_json, dict) or "steps" not in plan_json:
        raise PlanTemplateSchemaError(
            "plan template requires a 'plan_json' object with a 'steps' list"
        )
    validate_plan_steps(plan_json)


def validate_plan_steps(
    plan_json: dict[str, Any], *, require_steps: bool = False,
) -> None:
    """Validate that ``plan_json`` carries an EXECUTABLE steps list
    (nexus-vtp8h, extracted from :func:`validate_plan_template`).

    ``require_steps=True`` additionally rejects a missing/empty steps list —
    the bead-dump signature (the drift audit's plan 138 matched at 0.66-0.70
    then crashed the runner with unknown tool ``''``). Template loaders keep
    the historical may-be-empty semantics (``require_steps=False``).

    Raises :class:`PlanTemplateSchemaError` with a named reason.
    """
    if not isinstance(plan_json, dict):
        raise PlanTemplateSchemaError(
            f"plan_json must be a mapping, got {type(plan_json).__name__}"
        )
    steps = plan_json.get("steps")
    if steps is None and require_steps:
        raise PlanTemplateSchemaError(
            "plan_json has no 'steps' list — not an executable retrieval "
            "plan (implementation/phased plans belong in beads + T2 "
            "memory, not the plan library)"
        )
    # steps=None without require_steps falls through to the isinstance check
    # below, preserving the pre-extraction error text byte-for-byte
    # ("plan_json.steps must be a list, got NoneType") — reviewer Low.
    if not isinstance(steps, list):
        raise PlanTemplateSchemaError(
            f"plan_json.steps must be a list, got {type(steps).__name__}"
        )
    if require_steps and not steps:
        raise PlanTemplateSchemaError(
            "plan_json.steps is empty — not an executable retrieval plan"
        )

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise PlanTemplateSchemaError(
                f"plan_json.steps[{index}] must be a mapping, "
                f"got {type(step).__name__}"
            )
        tool = step.get("tool")
        if not isinstance(tool, str) or not tool:
            raise PlanTemplateSchemaError(
                f"plan_json.steps[{index}] requires a non-empty 'tool'"
            )
        if tool in _TRAVERSAL_TOOLS:
            # SC-16: traverse steps carry 'link_types' or 'purpose' either at
            # the step top-level (legacy test shape) or inside 'args' (current
            # YAML convention). Check both so the invariant holds either way.
            args = step.get("args") if isinstance(step.get("args"), dict) else {}
            has_link_types = bool(step.get("link_types")) or bool(args.get("link_types"))
            has_purpose = bool(step.get("purpose")) or bool(args.get("purpose"))
            if has_link_types and has_purpose:
                raise PlanTemplateSchemaError(
                    f"plan_json.steps[{index}] (traverse) declares both "
                    f"'link_types' and 'purpose'; SC-16 requires exactly one"
                )
            if not has_link_types and not has_purpose:
                raise PlanTemplateSchemaError(
                    f"plan_json.steps[{index}] (traverse) must declare "
                    f"either 'link_types' or 'purpose' (SC-16)"
                )


# ── Loader (P4a) ────────────────────────────────────────────────────────────


class PlanTemplateLoader:
    """Validate templates and detect canonical-identity collisions.

    A loader instance accumulates templates from one or more source
    locations (``.nexus/plans/*.yml``, ``conexus/plans/builtin/*.yml``,
    etc.). Any two templates whose canonical dimension JSON matches
    raise :class:`PlanTemplateDuplicateError` naming both sources.

    SC-18 — the dedup key is :func:`canonical_dimensions_json`, so
    declaration order within ``dimensions`` doesn't affect identity.
    """

    def __init__(
        self,
        *,
        registered_dimensions: set[str] | None = None,
        strict: bool = False,
    ) -> None:
        self._registered = registered_dimensions
        self._strict = strict
        self._seen: dict[str, str] = {}

    def add(self, template: dict[str, Any], *, source: str) -> str:
        """Validate *template* and register its canonical identity.

        Returns the canonical identity JSON for the template. Raises
        :class:`PlanTemplateSchemaError` on a malformed template and
        :class:`PlanTemplateDuplicateError` on a canonical-identity
        collision with a previously-added template.
        """
        validate_plan_template(
            template,
            registered_dimensions=self._registered,
            strict=self._strict,
        )
        identity = canonical_dimensions_json(template["dimensions"])
        if identity in self._seen:
            raise PlanTemplateDuplicateError(
                identity=identity,
                original=self._seen[identity],
                duplicate=source,
            )
        self._seen[identity] = source
        return identity

    def sources(self) -> dict[str, str]:
        """Return ``{canonical_identity: source_label}`` for all templates."""
        return dict(self._seen)
