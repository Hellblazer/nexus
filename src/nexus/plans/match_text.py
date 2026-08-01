# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan match-text synthesis (nexus-i711w Stage 2, Phase 0).

Lifted out of ``nexus.db.t2.plan_library`` so the SURVIVING consumers —
``HttpPlanLibrary`` and ``nexus.plans.session_cache`` — do not import from a
module the SQLite retirement deletes. The function's own docstring already
called itself "the single source of truth for match-text synthesis"; this gives
it a home that matches the claim, and one that is not tied to a substrate.

(The SQLite ``plan_library`` re-exported it until that module was deleted in
nexus-i711w Stage 2 sub-stage A3; every surviving caller imports from here.)
"""
from __future__ import annotations


def _synthesize_match_text(
    *,
    description: str | None,
    verb: str | None,
    name: str | None,
    scope: str | None,
) -> str:
    """Hybrid match-text synthesiser. RDR-092 Phase 3 / Phase 1.

    Shape: ``"<description>. <verb> <name> scope <scope>"`` when both
    *verb* and *name* are provided. Scope is optional and only
    appended when present. A trailing ``.`` on *description* is
    collapsed so the output does not carry ``..``.

    When verb or name is missing, returns the raw description so
    legacy NULL-dimension rows still carry a usable FTS payload.
    R10 validates the hybrid form at zero verb-accuracy regression.

    This is the single source of truth for match-text synthesis;
    :func:`nexus.plans.session_cache._synthesize_match_text` is a
    thin dict-unpacking adapter around this function so the T1
    cosine embedding and the T2 FTS payload cannot drift
    (nexus-w98c).
    """
    desc = (description or "").strip()
    v = (verb or "").strip()
    n = (name or "").strip()
    s = (scope or "").strip()

    if not v or not n:
        return desc

    suffix = f"{v} {n}"
    if s:
        suffix += f" scope {s}"
    if desc:
        core = desc.rstrip(".").rstrip()
        return f"{core}. {suffix}"
    return suffix
