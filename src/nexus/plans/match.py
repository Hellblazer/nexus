# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan match record (RDR-078 P1).

The :class:`Match` dataclass is the unit of currency between the two
matching paths and the runner:

  * **T1 cosine path** — :func:`plan_match` queries the
    ``plans__session`` ChromaDB collection and constructs a ``Match``
    with ``confidence`` populated.
  * **T2 FTS5 fallback path** — when T1 is unavailable,
    :meth:`Match.from_plan_row` builds a ``Match`` from a SQLite row,
    setting ``confidence=None`` as the sentinel for "FTS5 hit; no
    cosine score available".

The runner (:mod:`nexus.plans.runner`) treats ``confidence=None`` as an
implicit pass; skill-level gates that compare against a threshold MUST
check ``is not None`` first.

Reference: RDR-078 §Phase 1 Vocabulary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = ["Match"]


def _decode_json_dict(value: Any) -> dict[str, Any]:
    """Best-effort decode of a SQLite JSON column into a dict.

    Returns ``{}`` for ``None``, an empty string, or malformed JSON so
    the caller never has to defend against shape errors. Surfaces any
    deeper issue downstream when the runner tries to use the values.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


@dataclass(frozen=True)
class Match:
    """A single ``plan_match`` result.

    ``confidence`` is either a cosine score in ``[0.0, 1.0]`` from the
    T1 query path or ``None`` from the T2 FTS5 fallback path.
    """

    plan_id: int
    name: str
    description: str
    confidence: float | None
    dimensions: dict[str, Any]
    tags: str
    plan_json: str
    required_bindings: list[str]
    optional_bindings: list[str]
    default_bindings: dict[str, Any]
    parent_dims: dict[str, Any] | None

    @classmethod
    def from_plan_row(
        cls,
        row: dict[str, Any],
        confidence: float | None = None,
    ) -> Match:
        """Build a :class:`Match` from a raw ``plans`` table row.

        Used by the FTS5 fallback path. ``confidence`` defaults to
        ``None`` (FTS5 has no cosine equivalent); pass an explicit
        value when called from the T1 cosine path with a row joined
        back from T2.

        Tolerates legacy RDR-042 rows where the dimensional columns are
        all ``NULL``.
        """
        plan_json = row.get("plan_json") or "{}"
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError:
            plan = {}

        required = list(plan.get("required_bindings", []) or [])
        optional = list(plan.get("optional_bindings", []) or [])

        parent = _decode_json_dict(row.get("parent_dims"))

        return cls(
            plan_id=int(row["id"]),
            name=row.get("name") or "",
            description=row.get("query") or "",
            confidence=confidence,
            dimensions=_decode_json_dict(row.get("dimensions")),
            tags=row.get("tags") or "",
            plan_json=plan_json,
            required_bindings=required,
            optional_bindings=optional,
            default_bindings=_decode_json_dict(row.get("default_bindings")),
            parent_dims=parent or None,
        )
