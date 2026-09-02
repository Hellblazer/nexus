"""enumcheck — a throwaway coverage/overlap prover for ENUM-ONLY transition
and decision tables. RDR-201 research question 3 prototype.

Scope, deliberately narrow (the "enum-only subset" per intrastate's
docs/model-authoring.md):

  - Every discriminating tag is a single-valued enum with a declared,
    non-empty ``domain``. No bool/int/set/scalar kinds, no int comparisons,
    no ``contains``/``exists``.
  - Two guard operators only: ``eq`` (a bare string value) and ``in`` (a
    list of values). Guard atoms live in ``guard_all`` (conjunction) or
    ``guard_unless`` (the whole block negated once, never per-atom).
  - A row's ``outcome`` field is its MATCH dimension: rows sharing an
    outcome form a coverage group, and the outcome itself contributes NO
    product dimension — exactly intrastate's "match atoms scope, they do
    not discriminate" rule. Only guard_all/guard_unless keys become
    dimensions.
  - One rescuable class only, spelled ``escape = true`` on a row. A bare
    escape row (no guard atoms) that closes a group's coverage earns the
    ``graph-coverage-closed-by-escape`` advisory, exactly as intrastate's
    ``bareEscapeFor`` does — closure by a non-bare escape row, or by the
    ordinary rows alone, is proved cleanly with no advisory.
  - ``[model] class`` is ``"state-machine"`` or ``"decision-table"``. A
    zero-guard-dimension group is legitimate (silently covered by the
    single empty assignment) for a state machine, and is a blocking
    ``graph-unprovable-coverage`` (reason ``no-participating-dimension``)
    for a decision table — this is intrastate's C5/BR6 distinction.

TOML input shape::

    [model]
    id = "example"
    class = "state-machine"        # or "decision-table"; default "decision-table"

    [tags.state]
    domain = ["draft", "accepted"]
    # kind defaults to "enum"; anything else is refused as a dimension
    # at coverage time (graph-unprovable-coverage, reason=non-enum-dimension)

    [[rows]]
    id = "r1"
    outcome = "create"              # or a list, expanded one row per member
    escape = false                  # optional, default false

    [rows.guard_all]
    state = "draft"                 # eq atom
    # other_tag = ["a", "b"]        # in atom

    [rows.guard_unless]
    state = "closed"

What this subset CANNOT express, and what intrastate's full grammar can:
int ranges (``min``/``max`` + ``lt``/``lte``/``gt``/``gte``), ``set`` tags
and ``contains``, ``exists`` over optional keys (and the row-can-refuse
withholding that comes with optionality), accessors (``[read.*]`` /
``[write.*]``) that actually drive a state machine's owned state, and any
notion of write-then-readback. See the accompanying report for the
recommendation this sizing exercise was run to support.
"""

from __future__ import annotations

import itertools
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Typed finding codes (the four this prototype is scoped to produce)

GRAPH_COVERAGE_GAP = "graph-coverage-gap"
GRAPH_OVERLAP = "graph-overlap"
GRAPH_COVERAGE_CLOSED_BY_ESCAPE = "graph-coverage-closed-by-escape"
GRAPH_UNPROVABLE_COVERAGE = "graph-unprovable-coverage"

BLOCKING_CODES = {GRAPH_COVERAGE_GAP, GRAPH_OVERLAP, GRAPH_UNPROVABLE_COVERAGE}

# A cross-product larger than this is refused rather than enumerated, mirroring
# intrastate's published ProductBound refusal (CodeProductTooLarge). No
# fixture in this prototype gets remotely close; the bound exists so the
# checker fails loud instead of hanging on a pathological input.
PRODUCT_BOUND = 50_000


class ModelError(Exception):
    """A malformed model: refused at LOAD time, never at coverage time."""


# --------------------------------------------------------------------------
# Model


@dataclass(frozen=True)
class Tag:
    kind: str
    domain: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    id: str
    outcome: str
    guard_all: dict[str, tuple[str, ...]]
    guard_unless: dict[str, tuple[str, ...]]
    escape: bool


@dataclass(frozen=True)
class Model:
    id: str
    cls: str  # "state-machine" | "decision-table"
    tags: dict[str, Tag]
    rows: tuple[Row, ...]


def load_model(path: Path) -> Model:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)

    model_tbl = doc.get("model", {})
    model_id = model_tbl.get("id", path.stem)
    cls = model_tbl.get("class", "decision-table")
    if cls not in ("state-machine", "decision-table"):
        raise ModelError(f"model.class must be 'state-machine' or 'decision-table', got {cls!r}")

    tags: dict[str, Tag] = {}
    for name, tbl in doc.get("tags", {}).items():
        kind = tbl.get("kind", "enum")
        domain = tuple(tbl.get("domain", ()))
        if kind == "enum" and len(domain) != len(set(domain)):
            raise ModelError(f"tag {name!r}: domain has duplicate members")
        tags[name] = Tag(kind=kind, domain=domain)

    rows: list[Row] = []
    for raw in doc.get("rows", []):
        row_id = raw["id"]
        outcomes = raw["outcome"]
        if isinstance(outcomes, str):
            outcomes = [outcomes]
        guard_all = _normalize_guard_block(raw.get("guard_all", {}), tags, row_id, "guard_all")
        guard_unless = _normalize_guard_block(raw.get("guard_unless", {}), tags, row_id, "guard_unless")
        escape = bool(raw.get("escape", False))
        for i, outcome in enumerate(outcomes):
            # An `in`-on-outcome expansion mints one row per member, exactly
            # as intrastate's match-block `in` does. Suffix disambiguates ids.
            rid = row_id if len(outcomes) == 1 else f"{row_id}[{i}]"
            rows.append(
                Row(
                    id=rid,
                    outcome=outcome,
                    guard_all=guard_all,
                    guard_unless=guard_unless,
                    escape=escape,
                )
            )

    seen_ids = set()
    for r in rows:
        if r.id in seen_ids:
            raise ModelError(f"duplicate row id {r.id!r}")
        seen_ids.add(r.id)

    return Model(id=model_id, cls=cls, tags=tags, rows=tuple(rows))


def _normalize_guard_block(
    raw: dict, tags: dict[str, Tag], row_id: str, block_name: str
) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            values = (value,)
        elif isinstance(value, list):
            if not value:
                raise ModelError(f"row {row_id!r}: {block_name}.{key} is an empty `in` literal")
            values = tuple(value)
        else:
            raise ModelError(f"row {row_id!r}: {block_name}.{key} must be a string (eq) or list (in)")
        tag = tags.get(key)
        if tag is not None and tag.kind == "enum" and tag.domain:
            bad = set(values) - set(tag.domain)
            if bad:
                raise ModelError(
                    f"row {row_id!r}: {block_name}.{key} literal(s) {sorted(bad)} "
                    f"not in declared domain {list(tag.domain)}"
                )
        out[key] = values
    return out


# --------------------------------------------------------------------------
# Grouping (match dimension) and dimensions (guard dimensions)


@dataclass(frozen=True)
class Group:
    outcome: str
    rows: tuple[Row, ...]


def groups_of(model: Model) -> list[Group]:
    by_outcome: dict[str, list[Row]] = {}
    for row in model.rows:
        by_outcome.setdefault(row.outcome, []).append(row)
    return [Group(outcome=o, rows=tuple(rs)) for o, rs in sorted(by_outcome.items())]


def dimensions_of(group: Group) -> list[str]:
    dims: set[str] = set()
    for row in group.rows:
        dims |= row.guard_all.keys()
        dims |= row.guard_unless.keys()
    return sorted(dims)


def dimension_reason(model: Model, key: str) -> str | None:
    """None if `key` is a provable enum dimension; else the refusal reason."""
    tag = model.tags.get(key)
    if tag is None:
        return "undeclared-dimension"
    if tag.kind != "enum":
        return "non-enum-dimension"
    if not tag.domain:
        return "dimension-not-finite"
    return None


# --------------------------------------------------------------------------
# Assignment-set algebra (tuples of values, aligned to a sorted dims list)

Assignment = tuple[str, ...]


def full_product(dims: list[str], tags: dict[str, Tag]) -> set[Assignment]:
    ranges = [tags[d].domain for d in dims]
    size = 1
    for r in ranges:
        size *= len(r)
    if size > PRODUCT_BOUND:
        raise ModelError(
            f"scoped product over {dims} is {size} assignments, "
            f"above the published bound of {PRODUCT_BOUND}"
        )
    return set(itertools.product(*ranges))


def accepted_assignments(row: Row, dims: list[str], tags: dict[str, Tag]) -> set[Assignment]:
    """The assignments (over `dims`, all provable enums) `row` accepts."""
    ranges = []
    for d in dims:
        allowed = row.guard_all.get(d)
        ranges.append(allowed if allowed is not None else tags[d].domain)
    accepted = set(itertools.product(*ranges))

    if row.guard_unless:
        unless_dims = [d for d in dims if d in row.guard_unless]
        if unless_dims:
            unless_idx = {d: dims.index(d) for d in unless_dims}

            def unless_holds(a: Assignment) -> bool:
                return all(a[unless_idx[d]] in row.guard_unless[d] for d in unless_dims)

            accepted = {a for a in accepted if not unless_holds(a)}
    return accepted


# --------------------------------------------------------------------------
# Findings


@dataclass
class Finding:
    code: str
    group: str
    detail: dict

    def to_json(self) -> dict:
        return {"code": self.code, "group": self.group, **self.detail}


def check_model(model: Model) -> list[Finding]:
    findings: list[Finding] = []
    for group in groups_of(model):
        findings.extend(_check_group(model, group))
    return findings


def _check_group(model: Model, group: Group) -> list[Finding]:
    findings: list[Finding] = []
    dims = dimensions_of(group)

    if not dims:
        if model.cls == "decision-table":
            findings.append(
                Finding(
                    code=GRAPH_UNPROVABLE_COVERAGE,
                    group=group.outcome,
                    detail={
                        "reason": "no-participating-dimension",
                        "message": (
                            f"group {group.outcome!r} ranges over no participating "
                            "guard dimension; a decision table's discriminators must be "
                            "authored as guard_all/guard_unless, not left as the bare "
                            "outcome match"
                        ),
                    },
                )
            )
        # state-machine: zero-dim group is legitimate and silent.
        return findings

    reasons = {d: dimension_reason(model, d) for d in dims}
    unprovable = {d: r for d, r in reasons.items() if r is not None}
    if unprovable:
        for d, reason in sorted(unprovable.items()):
            findings.append(
                Finding(
                    code=GRAPH_UNPROVABLE_COVERAGE,
                    group=group.outcome,
                    detail={
                        "reason": reason,
                        "dimension": d,
                        "message": f"dimension {d!r} in group {group.outcome!r} is unprovable ({reason})",
                    },
                )
            )
        # Coverage cannot be proved with an unprovable dimension in play, but
        # overlap is still decidable on the dims that ARE provable — mirrors
        # intrastate's "withholding one claim must not suppress the other".
        decidable = [d for d in dims if d not in unprovable]
        if decidable:
            findings.extend(_check_overlap(group, decidable, model.tags))
        return findings

    findings.extend(_check_overlap(group, dims, model.tags))
    findings.extend(_check_coverage(group, dims, model.tags))
    return findings


def _check_overlap(group: Group, dims: list[str], tags: dict[str, Tag]) -> list[Finding]:
    findings: list[Finding] = []
    ordinary = [r for r in group.rows if not r.escape]
    accepted = {r.id: accepted_assignments(r, dims, tags) for r in ordinary}
    for a, b in itertools.combinations(sorted(accepted, key=lambda x: x), 2):
        left, right = accepted[a], accepted[b]
        inter = left & right
        if not inter:
            continue
        if left <= right or right <= left:
            continue  # subsumption is a different (non-overlap) advisory; out of scope here
        findings.append(
            Finding(
                code=GRAPH_OVERLAP,
                group=group.outcome,
                detail={
                    "row_a": a,
                    "row_b": b,
                    "intersection_count": len(inter),
                    "message": f"rows {a!r} and {b!r} are both enabled by {len(inter)} assignment(s) in group {group.outcome!r}",
                },
            )
        )
    return findings


def _check_coverage(group: Group, dims: list[str], tags: dict[str, Tag]) -> list[Finding]:
    product = full_product(dims, tags)

    ordinary = [r for r in group.rows if not r.escape]
    escapes = [r for r in group.rows if r.escape]

    union_ordinary: set[Assignment] = set()
    for r in ordinary:
        union_ordinary |= accepted_assignments(r, dims, tags)

    if union_ordinary == product:
        return []  # proved by the ordinary rows alone; escape rows (if any) closed nothing

    union_all = set(union_ordinary)
    for r in escapes:
        union_all |= accepted_assignments(r, dims, tags)

    if union_all != product:
        missing = sorted(product - union_all)
        sample = [dict(zip(dims, a)) for a in missing[:10]]
        return [
            Finding(
                code=GRAPH_COVERAGE_GAP,
                group=group.outcome,
                detail={
                    "dimensions": dims,
                    "product_size": len(product),
                    "covered_size": len(union_all),
                    "missing_count": len(missing),
                    "missing_sample": sample,
                    "message": (
                        f"group {group.outcome!r} leaves {len(missing)} of {len(product)} "
                        f"assignments uncovered over dimensions {dims}"
                    ),
                },
            )
        ]

    bare_escapes = sorted(r.id for r in escapes if not r.guard_all and not r.guard_unless)
    if bare_escapes:
        return [
            Finding(
                code=GRAPH_COVERAGE_CLOSED_BY_ESCAPE,
                group=group.outcome,
                detail={
                    "escape_row": bare_escapes[0],
                    "message": (
                        f"coverage of group {group.outcome!r} is closed by the bare "
                        f"escape row {bare_escapes[0]!r} rather than proved over its "
                        "declared domains"
                    ),
                },
            )
        ]
    return []  # closed by ordinary rows + a non-bare escape row: proved, no advisory


# --------------------------------------------------------------------------
# CLI


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: checker.py <model.toml>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        model = load_model(path)
        findings = check_model(model)
    except ModelError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2

    print(json.dumps([f.to_json() for f in findings], indent=2))
    return 1 if any(f.code in BLOCKING_CODES for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
