"""Checker for RDR-201 closed-vocabulary tables.

Proves coverage and overlap over each match group's declared guard
dimensions, and refuses to claim coverage over any undeclared or non-enum
dimension rather than pretending. Ported from the enumcheck prototype
(``tests/fixtures/tables/_prototype/checker.py``); the algorithm is
unchanged — group rows by match assignment, take the cross-product of the
group's guard dimensions' declared domains, coverage is the union of the
rows' accepted assignments equalling that product, overlap is two rows
accepting one assignment in common — only the schema it operates on has
moved from the prototype's ``outcome``/``guard_all`` to the production
``match``/``guard``.

stdlib only.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import structlog

from nexus.tables.load import Dimension, Row, Table

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------
# Typed finding codes

COVERAGE_GAP = "coverage-gap"
OVERLAP = "overlap"
CLOSED_BY_ESCAPE = "closed-by-escape"
UNPROVABLE_COVERAGE = "unprovable-coverage"
# unknown-literal is a LOAD-time refusal (nexus.tables.load.UnknownLiteralError),
# never a check()-time finding: by the time a Table exists, every match/guard
# literal has already been proven to lie within its declared domain. The
# constant is kept here for the shared finding-code vocabulary (RDR-201 §
# Technical Design lists all five together) and so a caller mapping codes to
# messages has one place to look.
UNKNOWN_LITERAL = "unknown-literal"

BLOCKING_CODES = frozenset({COVERAGE_GAP, OVERLAP, UNPROVABLE_COVERAGE})

# A cross-product larger than this is refused rather than enumerated,
# mirroring the prototype's published ProductBound refusal. No RDR-201 table
# gets remotely close; the bound exists so the checker fails loud instead of
# hanging on a pathological input.
PRODUCT_BOUND = 50_000


class ProductTooLargeError(Exception):
    """A group's guard-dimension cross-product exceeds ``PRODUCT_BOUND``."""


@dataclass(frozen=True)
class Finding:
    code: str
    group: dict[str, str]
    detail: dict

    def to_json(self) -> dict:
        return {"code": self.code, "group": self.group, **self.detail}


@dataclass(frozen=True)
class Group:
    match: dict[str, str]
    rows: tuple[Row, ...]


def groups_of(table: Table) -> list[Group]:
    by_match: dict[tuple[tuple[str, str], ...], list[Row]] = {}
    for row in table.rows:
        key = tuple(sorted(row.match.items()))
        by_match.setdefault(key, []).append(row)
    return [Group(match=dict(key), rows=tuple(rs)) for key, rs in sorted(by_match.items())]


def dimensions_of(group: Group) -> list[str]:
    dims: set[str] = set()
    for row in group.rows:
        dims |= row.guard.keys()
    return sorted(dims)


def dimension_reason(table: Table, key: str) -> str | None:
    """``None`` if ``key`` is a provable enum dimension; else the refusal reason."""
    dim = table.dimensions.get(key)
    if dim is None:
        return "undeclared-dimension"
    if dim.kind != "enum":
        return "non-enum-dimension"
    if not dim.domain:
        return "dimension-not-finite"
    return None


# --------------------------------------------------------------------------
# Assignment-set algebra (tuples of values, aligned to a sorted dims list)

Assignment = tuple[str, ...]


def full_product(dims: list[str], dimensions: dict[str, Dimension]) -> set[Assignment]:
    ranges = [dimensions[d].domain for d in dims]
    size = 1
    for r in ranges:
        size *= len(r)
    if size > PRODUCT_BOUND:
        raise ProductTooLargeError(
            f"scoped product over {dims} is {size} assignments, above the published bound of {PRODUCT_BOUND}"
        )
    return set(itertools.product(*ranges))


def accepted_assignments(row: Row, dims: list[str], dimensions: dict[str, Dimension]) -> set[Assignment]:
    """The assignments (over ``dims``, all provable enums) ``row`` accepts."""
    ranges = []
    for d in dims:
        allowed = row.guard.get(d)
        ranges.append(allowed if allowed is not None else dimensions[d].domain)
    return set(itertools.product(*ranges))


# --------------------------------------------------------------------------
# Findings


def check_table(table: Table) -> list[Finding]:
    findings: list[Finding] = []
    for group in groups_of(table):
        findings.extend(_check_group(table, group))
    return findings


def exit_code(findings: list[Finding]) -> int:
    """1 if any finding is in ``BLOCKING_CODES``, else 0 (advisories only)."""
    return 1 if any(f.code in BLOCKING_CODES for f in findings) else 0


def _check_group(table: Table, group: Group) -> list[Finding]:
    findings: list[Finding] = []
    dims = dimensions_of(group)

    if not dims:
        if table.kind == "decision-table":
            findings.append(
                Finding(
                    code=UNPROVABLE_COVERAGE,
                    group=group.match,
                    detail={
                        "reason": "no-participating-dimension",
                        "message": (
                            f"group {group.match!r} ranges over no participating guard "
                            "dimension; a decision table's discriminators must be "
                            "authored in `guard`, not left as the bare match"
                        ),
                    },
                )
            )
        # state-machine: zero-dim group is legitimate and silent.
        return findings

    reasons = {d: dimension_reason(table, d) for d in dims}
    unprovable = {d: r for d, r in reasons.items() if r is not None}
    if unprovable:
        for d, reason in sorted(unprovable.items()):
            findings.append(
                Finding(
                    code=UNPROVABLE_COVERAGE,
                    group=group.match,
                    detail={
                        "reason": reason,
                        "dimension": d,
                        "message": f"dimension {d!r} in group {group.match!r} is unprovable ({reason})",
                    },
                )
            )
        # Coverage cannot be proved with an unprovable dimension in play, but
        # overlap is still decidable on the dims that ARE provable.
        decidable = [d for d in dims if d not in unprovable]
        if decidable:
            findings.extend(_check_overlap(group, decidable, table.dimensions))
        return findings

    findings.extend(_check_overlap(group, dims, table.dimensions))
    findings.extend(_check_coverage(group, dims, table.dimensions))
    return findings


def _check_overlap(group: Group, dims: list[str], dimensions: dict[str, Dimension]) -> list[Finding]:
    findings: list[Finding] = []
    ordinary = [r for r in group.rows if not r.escape]
    accepted = {r.id: accepted_assignments(r, dims, dimensions) for r in ordinary}
    for a, b in itertools.combinations(sorted(accepted), 2):
        left, right = accepted[a], accepted[b]
        inter = left & right
        if not inter:
            continue
        if left <= right or right <= left:
            continue  # subsumption is a different (non-overlap) advisory; out of scope here
        findings.append(
            Finding(
                code=OVERLAP,
                group=group.match,
                detail={
                    "row_a": a,
                    "row_b": b,
                    "intersection_count": len(inter),
                    "message": (
                        f"rows {a!r} and {b!r} are both enabled by {len(inter)} "
                        f"assignment(s) in group {group.match!r}"
                    ),
                },
            )
        )
    return findings


def _check_coverage(group: Group, dims: list[str], dimensions: dict[str, Dimension]) -> list[Finding]:
    product = full_product(dims, dimensions)

    ordinary = [r for r in group.rows if not r.escape]
    escapes = [r for r in group.rows if r.escape]

    union_ordinary: set[Assignment] = set()
    for r in ordinary:
        union_ordinary |= accepted_assignments(r, dims, dimensions)

    if union_ordinary == product:
        return []  # proved by the ordinary rows alone; escape rows (if any) closed nothing

    union_all = set(union_ordinary)
    for r in escapes:
        union_all |= accepted_assignments(r, dims, dimensions)

    if union_all != product:
        missing = sorted(product - union_all)
        sample = [dict(zip(dims, a)) for a in missing[:10]]
        return [
            Finding(
                code=COVERAGE_GAP,
                group=group.match,
                detail={
                    "dimensions": dims,
                    "product_size": len(product),
                    "covered_size": len(union_all),
                    "missing_count": len(missing),
                    "missing_sample": sample,
                    "message": (
                        f"group {group.match!r} leaves {len(missing)} of {len(product)} "
                        f"assignments uncovered over dimensions {dims}"
                    ),
                },
            )
        ]

    bare_escapes = sorted(r.id for r in escapes if not r.guard)
    if bare_escapes:
        return [
            Finding(
                code=CLOSED_BY_ESCAPE,
                group=group.match,
                detail={
                    "escape_row": bare_escapes[0],
                    "message": (
                        f"coverage of group {group.match!r} is closed by the bare "
                        f"escape row {bare_escapes[0]!r} rather than proved over its "
                        "declared domains"
                    ),
                },
            )
        ]
    return []  # closed by ordinary rows + a non-bare escape row: proved, no advisory
