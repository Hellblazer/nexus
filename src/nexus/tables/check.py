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

RDR-201's Technical Design lists ``unknown-literal`` as one of five typed
finding codes. Recorded decision (not to be re-litigated at each read): it
is raised at LOAD, never emitted by ``check_table`` here — see
:mod:`nexus.tables.load`'s module docstring and the ``UNKNOWN_LITERAL``
comment below.

A sixth code, ``unmatched-assignment`` (RDR-201 P1.2 addendum, not in the
original Technical Design's five), proves a SEPARATE totality claim: that
every combination of the table's match-key dimensions is named by SOME
row's ``match`` at all, before per-group coverage/overlap is even asked
about. Without it, ``check_table`` only ever proved coverage within
groups that already exist -- a value combination no row names has no
group, and so produced no finding, while
:func:`nexus.tables.resolve.resolve` still refuses ``no-match`` on it at
runtime. See :func:`_check_match_totality`.

stdlib only.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import structlog

from nexus.tables.load import Dimension, FrozenMapping, Row, Table

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------
# Typed finding codes

COVERAGE_GAP = "coverage-gap"
OVERLAP = "overlap"
CLOSED_BY_ESCAPE = "closed-by-escape"
UNPROVABLE_COVERAGE = "unprovable-coverage"
# A value combination of the table's MATCH-KEY dimensions (e.g. every
# (status, event) pair) that no row's `match` names at all -- there is no
# group for it, so nothing about it was ever checked. RDR-201 P1.2 critique
# (T2 nexus/critique-nexus-j9z30-2-2026-09-01 [24018]): coverage/overlap
# proof was previously scoped to EXISTING match groups only, so a status
# value no row ever names for a given event produced no finding, yet
# nexus.tables.resolve.resolve() refuses no-match on it at runtime -- a
# checker-clean table was not actually guaranteed total. See
# _check_match_totality below.
UNMATCHED_ASSIGNMENT = "unmatched-assignment"
#: Advisory: a declared dimension no row names (see ``_check_unused_dimensions``).
UNUSED_DIMENSION = "unused-dimension"
# unknown-literal is a LOAD-time refusal (nexus.tables.load.UnknownLiteralError),
# never a check()-time finding: by the time a Table exists, every match/guard
# literal has already been proven to lie within its declared domain. The
# constant is kept here for the shared finding-code vocabulary (RDR-201 §
# Technical Design lists all five together) and so a caller mapping codes to
# messages has one place to look.
UNKNOWN_LITERAL = "unknown-literal"

BLOCKING_CODES = frozenset({COVERAGE_GAP, OVERLAP, UNPROVABLE_COVERAGE, UNMATCHED_ASSIGNMENT})

# A cross-product larger than this is refused rather than enumerated,
# mirroring the prototype's published ProductBound refusal. No RDR-201 table
# gets remotely close; the bound exists so the checker fails loud instead of
# hanging on a pathological input.
PRODUCT_BOUND = 50_000


class ProductTooLargeError(Exception):
    """A group's guard-dimension cross-product exceeds ``PRODUCT_BOUND``."""


@dataclass(frozen=True)
class Finding:
    """``group`` is coerced to :class:`FrozenMapping` in ``__post_init__``
    (same discipline as :class:`nexus.tables.load.Row`), so ``group`` is
    always hashable. ``detail`` stays a plain, heterogeneous ``dict`` (it
    can hold nested lists, e.g. ``missing_sample``) and is deliberately
    excluded from ``__hash__`` — hash/eq consistency only requires the
    hash to be a function of a subset of the fields ``__eq__`` compares.
    """

    code: str
    group: FrozenMapping
    detail: dict

    def __post_init__(self) -> None:
        if not isinstance(self.group, FrozenMapping):
            object.__setattr__(self, "group", FrozenMapping(self.group))

    def __hash__(self) -> int:
        return hash((self.code, self.group))

    def to_json(self) -> dict:
        return {"code": self.code, "group": dict(self.group), **self.detail}


@dataclass(frozen=True)
class Group:
    match: FrozenMapping
    rows: tuple[Row, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.match, FrozenMapping):
            object.__setattr__(self, "match", FrozenMapping(self.match))


def groups_of(table: Table) -> list[Group]:
    by_match: dict[tuple[tuple[str, str], ...], list[Row]] = {}
    for row in table.rows:
        key = tuple(sorted(row.match.items()))
        by_match.setdefault(key, []).append(row)
    return [Group(match=dict(key), rows=tuple(rs)) for key, rs in sorted(by_match.items())]


def dimensions_of(group: Group) -> list[str]:
    dims: set[str] = set()
    for row in group.rows:
        dims |= set(row.guard.keys())
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


def _check_match_totality(table: Table) -> list[Finding]:
    """Every combination of the table's MATCH-KEY dimensions must be named
    by at least one row's ``match`` -- a combination no row names has no
    group at all, so ``_check_group`` below never even considers it (RDR-201
    P1.2 critique, T2 nexus/critique-nexus-j9z30-2-2026-09-01 [24018]).

    ``load.py``'s ``UndeclaredDimensionError`` (RDR-201 P1.2 code review, T2
    nexus/code-review-nexus-j9z30-2-2026-09-01) guarantees every match key
    on a table that actually loaded IS a declared dimension, so the
    ``unprovable`` branch below is unreachable via the normal loader path;
    it stays as defense-in-depth for a :class:`Table` built directly
    (bypassing :func:`nexus.tables.load.load_table`), matching
    ``_check_group``'s own guard-dimension pattern.
    """
    keys = list(table.match_keys)
    if not keys:
        return []

    reasons = {k: dimension_reason(table, k) for k in keys}
    unprovable = {k: r for k, r in reasons.items() if r is not None}
    if unprovable:
        return [
            Finding(
                code=UNPROVABLE_COVERAGE,
                group=FrozenMapping({}),
                detail={
                    "reason": reason,
                    "dimension": k,
                    "message": (
                        f"match-key dimension {k!r} is unprovable ({reason}); cannot prove "
                        "every match-key assignment is named by some row"
                    ),
                },
            )
            for k, reason in sorted(unprovable.items())
        ]

    named = {tuple(row.match[k] for k in keys) for row in table.rows}
    missing = sorted(full_product(keys, table.dimensions) - named)
    return [
        Finding(
            code=UNMATCHED_ASSIGNMENT,
            group=FrozenMapping(dict(zip(keys, combo))),
            detail={
                "message": (
                    f"no row names match assignment {dict(zip(keys, combo))!r}; "
                    "this combination has no group at all"
                ),
            },
        )
        for combo in missing
    ]


def _check_unused_dimensions(table: Table) -> list[Finding]:
    """Advisory: a dimension declared under ``[dimensions]`` that no row's
    match or guard ever names. Coverage is proved only over dimensions a
    group participates in, so a declared-but-never-guarded dimension gets
    no coverage statement at all; without this advisory that silence is
    indistinguishable from "proved" (RDR-201 Phase 1 critique, T2
    nexus/critique-rdr-201-phase-1-2026-09-01). Advisory, not blocking: a
    table may legitimately declare a dimension its evaluator callers bind
    but no row discriminates on yet."""
    used: set[str] = set()
    for row in table.rows:
        used |= set(row.match.keys()) | set(row.guard.keys())
    return [
        Finding(
            code=UNUSED_DIMENSION,
            group={},
            detail={
                "dimension": name,
                "message": (
                    f"dimension {name!r} is declared but no row's match or "
                    "guard names it; no coverage claim is made over it"
                ),
            },
        )
        for name in sorted(table.dimensions)
        if name not in used
    ]


def check_table(table: Table) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_check_unused_dimensions(table))
    findings.extend(_check_match_totality(table))
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
        # A zero-guard-dimension group still has exactly one assignment --
        # the empty tuple. Overlap must be checked over it regardless of
        # table kind: two non-escape (or non-bare-escape) rows sharing a
        # bare match both accept that single assignment, which is a real
        # overlap (CRITICAL 1 / nexus-akmum), not something the
        # no-participating-dimension advisory below can substitute for --
        # that advisory is a COVERAGE statement (decision tables only), it
        # says nothing about two rows conflicting on the one assignment
        # that exists.
        findings.extend(_check_overlap(group, dims, table.dimensions))
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
            return findings
        # state-machine: the group's single (empty-tuple) assignment is
        # trivially "accepted" by every row here (dims=[] means no guard
        # key is even being examined) -- so "only a bare escape row
        # accepts it" reduces to "the group has no ordinary (non-escape)
        # row at all". When that holds, the bare escape row is what
        # closes this group, exactly as _check_coverage flags below for
        # guarded groups (RDR-201 Sec Technical Design's no-bare-green
        # principle: a group closed only by its catch-all still earns the
        # advisory). This is the real packaged rdr-lifecycle.toml's own
        # shape: a list-valued match key's "-otherwise" escape row expands
        # into one bare-escape row per remaining status, each alone in its
        # own zero-dim group (code review, T2 nexus/code-review-
        # nexus-j9z30-6-2026-09-02 [24038]). A group proved by an ordinary
        # row (with or without a co-located escape row) stays silent, same
        # as before.
        ordinary = [r for r in group.rows if not r.escape]
        bare_escapes = sorted(r.id for r in group.rows if r.escape and not r.guard)
        if not ordinary and bare_escapes:
            findings.append(
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
            )
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


def _overlap_participants(group: Group) -> list[Row]:
    """Rows that must be mutually non-overlapping (CRITICAL 2 / nexus-akmum).

    Every ordinary row participates. An escape row participates too UNLESS
    it is BARE (``escape = true`` with no guard atoms at all) -- a bare
    escape is the group's designated catch-all for whatever the ordinary
    rows don't cover, and it is EXPECTED to accept assignments other rows
    also accept (that's the point of a catch-all; ``closed-by-escape``
    already reports when one fires). A non-bare escape row is a NARROWED
    rescue -- it makes the same coverage/overlap claim an ordinary row
    would over the assignments its own guard names, so it must be held to
    the same non-overlap standard. At most one escape row exists per group
    (load.py's ``_check_escape_multiplicity``), so this is never an
    escape-vs-escape question.
    """
    return [r for r in group.rows if not (r.escape and not r.guard)]


def _check_overlap(group: Group, dims: list[str], dimensions: dict[str, Dimension]) -> list[Finding]:
    """Flag ANY non-empty intersection among participants' accepted sets.

    RDR-201 sec Background is explicit: there is no hit policy, so an
    overlap is a lint failure rather than something a priority order
    resolves. A shared assignment between two participating rows IS an
    overlap, full stop -- strict subsumption (a broader row whose accepted
    set is a proper superset of a narrower row's) is NOT exempted. An
    earlier revision of this function carved out strict subsumption as a
    "layered precedence, broad rule + narrower override" pattern; that
    carve-out has no basis in RDR-201's text (round-2 critique, T2
    nexus/critique-nexus-j9z30-1-round2-2026-09-01 [24008]) and hid a real
    three-way ambiguity in tests/fixtures/tables/release_decision_defect.toml
    (at-or-above-floor is a strict superset of both planted overlap rows).
    A layered "most specific wins" hit policy is a legitimate DESIGN for a
    table author to want, but it is not what RDR-201 specifies, and adding
    it is Sam's ruling / an RDR-201 amendment to make, not an implementer
    default.
    """
    findings: list[Finding] = []
    participants = _overlap_participants(group)
    accepted = {r.id: accepted_assignments(r, dims, dimensions) for r in participants}
    for a, b in itertools.combinations(sorted(accepted), 2):
        left, right = accepted[a], accepted[b]
        inter = left & right
        if not inter:
            continue
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
