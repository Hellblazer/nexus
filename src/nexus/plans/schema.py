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
from typing import Any

__all__ = [
    "PlanTemplateDuplicateError",
    "PlanTemplateLoader",
    "PlanTemplateSchemaError",
    "canonical_dimensions_json",
    "validate_plan_template",
]

_log = logging.getLogger(__name__)


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

    steps = plan_json.get("steps")
    if not isinstance(steps, list):
        raise PlanTemplateSchemaError(
            f"plan_json.steps must be a list, got {type(steps).__name__}"
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
            has_link_types = bool(step.get("link_types"))
            has_purpose = bool(step.get("purpose"))
            if has_link_types and has_purpose:
                raise PlanTemplateSchemaError(
                    f"plan_json.steps[{index}] (traverse) declares both "
                    f"'link_types' and 'purpose'; SC-16 requires exactly one"
                )


# ── Loader (P4a) ────────────────────────────────────────────────────────────


class PlanTemplateLoader:
    """Validate templates and detect canonical-identity collisions.

    A loader instance accumulates templates from one or more source
    locations (``.nexus/plans/*.yml``, ``nx/plans/builtin/*.yml``,
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
