"""Evaluator for RDR-201 closed-vocabulary tables.

:func:`resolve` takes a loaded :class:`~nexus.tables.load.Table` and a
concrete assignment of every declared dimension, and returns the single
:class:`Row` it hits, or a typed refusal drawn from the closed set
{no-match, ambiguous-match, unknown-value}.

The evaluator never breaks a tie. Ambiguity at resolve time is a defect
:func:`nexus.tables.check.check_table` should already have caught -- it is
reported here, naming every candidate row id, never silently resolved by
priority order. A dimension missing from the assignment, or a value
outside its declared domain, is ``unknown-value`` -- never a guess and
never a silent fallthrough.

A row whose own ``outcome_kind`` is ``"refuse"`` is a HIT: the table
author authored that refusal, and it is returned as ``Resolution.row``
like any other outcome, never conflated with an ``unknown-value`` /
``no-match`` / ``ambiguous-match`` refusal the EVALUATOR itself produces.

stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from nexus.tables.load import Row, Table

NO_MATCH = "no-match"
AMBIGUOUS_MATCH = "ambiguous-match"
UNKNOWN_VALUE = "unknown-value"
REFUSAL_CODES = frozenset({NO_MATCH, AMBIGUOUS_MATCH, UNKNOWN_VALUE})


@dataclass(frozen=True)
class Resolution:
    """Exactly one of ``row`` / ``refusal`` is set -- never both, never
    neither. A hit carries ``row`` (and ``escaped=True`` when it was
    reached only via the group's escape row); a refusal carries
    ``refusal`` (one of :data:`REFUSAL_CODES`) plus a ``detail`` payload
    naming what went wrong.
    """

    row: Row | None = None
    escaped: bool = False
    refusal: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.row is None) == (self.refusal is None):
            raise ValueError("Resolution must carry exactly one of `row` or `refusal`")


def resolve(table: Table, assignment: Mapping[str, str]) -> Resolution:
    invalid = _validate(table, assignment)
    if invalid is not None:
        return invalid

    candidates = [r for r in table.rows if not r.escape and _match_accepts(r, assignment) and _guard_accepts(r, assignment)]
    if len(candidates) == 1:
        return Resolution(row=candidates[0], escaped=False)
    if len(candidates) > 1:
        ids = sorted(r.id for r in candidates)
        return Resolution(refusal=AMBIGUOUS_MATCH, detail={"candidates": ids})

    # Zero ordinary candidates: fall back to the group's escape row, if any
    # exists AND its own guard (a non-bare escape narrows, it does not
    # widen) accepts the assignment. At most one escape row can ever share
    # an exact match assignment (load.py's _check_escape_multiplicity).
    escape_row = next((r for r in table.rows if r.escape and _match_accepts(r, assignment)), None)
    if escape_row is not None and _guard_accepts(escape_row, assignment):
        return Resolution(row=escape_row, escaped=True)
    return Resolution(refusal=NO_MATCH, detail={})


def _match_accepts(row: Row, assignment: Mapping[str, str]) -> bool:
    return all(assignment.get(key) == value for key, value in row.match.items())


def _guard_accepts(row: Row, assignment: Mapping[str, str]) -> bool:
    return all(assignment.get(key) in values for key, values in row.guard.items())


def _validate(table: Table, assignment: Mapping[str, str]) -> Resolution | None:
    """``None`` if ``assignment`` names every declared dimension with a
    value inside its domain; else the ``unknown-value`` refusal.

    Missing dimensions are checked first (a value can't be judged
    in-domain if it isn't there at all), then out-of-domain values --
    both scans walk dimension names in sorted order, so the refusal
    reported for a given bad assignment is deterministic. A dimension
    whose declared domain is empty (:mod:`nexus.tables.check`'s
    ``dimension-not-finite``) has nothing to validate against and is
    skipped, matching the loader's own domain-check discipline.
    """
    for name in sorted(table.dimensions):
        if name not in assignment:
            return Resolution(refusal=UNKNOWN_VALUE, detail={"dimension": name, "reason": "missing"})
    for name in sorted(table.dimensions):
        dim = table.dimensions[name]
        value = assignment[name]
        if dim.kind == "enum" and dim.domain and value not in dim.domain:
            return Resolution(
                refusal=UNKNOWN_VALUE,
                detail={"dimension": name, "value": value, "reason": "out-of-domain"},
            )
    return None
