"""Loader for RDR-201 closed-vocabulary tables (state-machine / decision-table).

Ported from the enumcheck prototype (RDR-201 research question 3;
``tests/fixtures/tables/_prototype/checker.py``) onto the production schema
from ``docs/rdr/rdr-201-closed-vocabularies-as-checked-tables.md`` §
Technical Design::

    [table]
    id = "..."
    kind = "state-machine" | "decision-table"

    [dimensions.<name>]        # every dimension is a declared enum
    domain = [...]

    [[row]]
    id = "..."
    match = { ... }             # scopes the row's coverage group
    guard = { ... }              # optional; the dims coverage is proved over
    to = { ... } | emit = { ... } | refuse = "..."   # exactly one
    escape = true                 # optional, at most one per group

``match`` and ``guard`` atoms accept either a bare string (``eq``) or a list
of strings (``in``). A list-valued ``match`` key expands into one row per
member, suffixing the row id with ``#<member>`` (or ``#<m1>,<m2>`` when more
than one match key varies in the same row).

Every row must name the same set of match keys — a row naming fewer (or
different) keys than its siblings is refused at load
(:class:`MatchKeysMismatchError`), which is what keeps a broad "otherwise"
row from silently overlapping a narrower group.

stdlib only.
"""

from __future__ import annotations

import importlib.resources
import itertools
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

TableKind = Literal["state-machine", "decision-table"]
OutcomeKind = Literal["to", "emit", "refuse"]

_VALID_KINDS: tuple[TableKind, ...] = ("state-machine", "decision-table")
_OUTCOME_FIELDS: tuple[OutcomeKind, ...] = ("to", "emit", "refuse")


class TableLoadError(Exception):
    """A malformed table: refused at LOAD time, never at check time."""


class MatchKeysMismatchError(TableLoadError):
    """A row names a different set of match keys than its siblings."""


class DuplicateRowIdError(TableLoadError):
    """Two rows in one table share an id (post match-expansion)."""


class MultipleOutcomesError(TableLoadError):
    """A row does not carry exactly one of ``to`` / ``emit`` / ``refuse``."""


class MultipleEscapesInGroupError(TableLoadError):
    """More than one ``escape = true`` row shares a match group."""


class UnknownLiteralError(TableLoadError):
    """A match or guard literal falls outside its dimension's declared domain."""


@dataclass(frozen=True)
class Dimension:
    """A declared enum dimension: a name and its finite domain.

    ``kind`` defaults to ``"enum"``; any other value marks the dimension as
    non-enum, which the checker refuses to claim coverage over
    (``unprovable-coverage``, reason ``non-enum-dimension``) rather than
    pretending. No table shipped by this RDR uses a non-enum dimension
    today — the field exists so the checker's refusal path stays reachable
    and testable, per RDR-201's stated risk mitigation.
    """

    name: str
    domain: tuple[str, ...]
    kind: str = "enum"


@dataclass(frozen=True)
class Row:
    """One row of a table, fully resolved (list-valued match keys expanded)."""

    id: str
    match: dict[str, str]
    guard: dict[str, tuple[str, ...]]
    outcome_kind: OutcomeKind
    outcome: dict[str, str] | str
    escape: bool = False


@dataclass(frozen=True)
class Table:
    id: str
    kind: TableKind
    dimensions: dict[str, Dimension]
    match_keys: tuple[str, ...]
    rows: tuple[Row, ...]


def load_table(path: Path) -> Table:
    """Load and validate a table from an explicit filesystem path."""
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    return _build_table(doc, default_id=path.stem)


def load_packaged_table(resource: str, package: str = "nexus.tables") -> Table:
    """Load and validate a table shipped inside the ``package`` distribution.

    Uses :mod:`importlib.resources` so the table is reachable from an
    installed wheel, not just a checkout with a repo root to resolve
    (RDR-201 P1.3's TABLE LOCATION note — a docs-only table would 404 on
    every installed conexus).
    """
    data = importlib.resources.files(package).joinpath(resource).read_bytes()
    doc = tomllib.loads(data.decode("utf-8"))
    return _build_table(doc, default_id=resource)


def _build_table(doc: dict, *, default_id: str) -> Table:
    table_tbl = doc.get("table", {})
    table_id = table_tbl.get("id", default_id)
    kind = table_tbl.get("kind", "decision-table")
    if kind not in _VALID_KINDS:
        raise TableLoadError(f"table.kind must be one of {_VALID_KINDS}, got {kind!r}")

    dimensions: dict[str, Dimension] = {}
    for name, tbl in doc.get("dimensions", {}).items():
        domain = tuple(tbl.get("domain", ()))
        dim_kind = tbl.get("kind", "enum")
        if dim_kind == "enum" and len(domain) != len(set(domain)):
            raise TableLoadError(f"dimension {name!r}: domain has duplicate members")
        dimensions[name] = Dimension(name=name, domain=domain, kind=dim_kind)

    raw_rows = doc.get("row", [])
    match_keys = _reference_match_keys(raw_rows)

    rows: list[Row] = []
    for raw in raw_rows:
        rows.extend(_build_rows(raw, dimensions))

    seen_ids: set[str] = set()
    for row in rows:
        if row.id in seen_ids:
            raise DuplicateRowIdError(f"duplicate row id {row.id!r}")
        seen_ids.add(row.id)

    _check_escape_multiplicity(rows)

    return Table(id=table_id, kind=kind, dimensions=dimensions, match_keys=match_keys, rows=tuple(rows))


def _reference_match_keys(raw_rows: list[dict]) -> tuple[str, ...]:
    """Every row must name the same set of match keys; refuse the first mismatch.

    A row naming fewer (or different) match keys than the table's first row
    is refused rather than silently treated as a broader "otherwise" over
    the missing key — the shape forbids the question of whether two
    differently-keyed match blocks denote "the same group".
    """
    if not raw_rows:
        return ()
    reference = frozenset(raw_rows[0].get("match", {}).keys())
    for raw in raw_rows:
        keys = frozenset(raw.get("match", {}).keys())
        if keys != reference:
            raise MatchKeysMismatchError(
                f"row {raw.get('id')!r} names match keys {sorted(keys)}, "
                f"expected {sorted(reference)} (every row must name the same match keys)"
            )
    return tuple(sorted(reference))


def _build_rows(raw: dict, dimensions: dict[str, Dimension]) -> list[Row]:
    row_id = raw["id"]
    match_raw = raw.get("match", {})
    guard_raw = raw.get("guard", {})
    escape = bool(raw.get("escape", False))

    present = [f for f in _OUTCOME_FIELDS if f in raw]
    if len(present) != 1:
        raise MultipleOutcomesError(
            f"row {row_id!r} must carry exactly one of 'to'/'emit'/'refuse', found {present or 'none'}"
        )
    outcome_kind = present[0]
    outcome_value = raw[outcome_kind]
    if outcome_kind in ("to", "emit"):
        if not isinstance(outcome_value, dict):
            raise TableLoadError(f"row {row_id!r}: {outcome_kind!r} must be a table")
    elif not isinstance(outcome_value, str):
        raise TableLoadError(f"row {row_id!r}: 'refuse' must be a string code")

    guard = _normalize_literal_block(guard_raw, dimensions, row_id, "guard")

    expand_keys = sorted(k for k, v in match_raw.items() if isinstance(v, list))
    if not expand_keys:
        match_resolved = _normalize_match_scalars(match_raw, dimensions, row_id)
        return [
            Row(
                id=row_id,
                match=match_resolved,
                guard=guard,
                outcome_kind=outcome_kind,
                outcome=outcome_value,
                escape=escape,
            )
        ]

    value_lists = [tuple(match_raw[k]) for k in expand_keys]
    for k, values in zip(expand_keys, value_lists):
        if not values:
            raise TableLoadError(f"row {row_id!r}: match.{k} is an empty list literal")
    fixed = {k: v for k, v in match_raw.items() if k not in expand_keys}

    rows: list[Row] = []
    for combo in itertools.product(*value_lists):
        member_match = dict(fixed)
        member_match.update(dict(zip(expand_keys, combo)))
        member_match = _normalize_match_scalars(member_match, dimensions, row_id)
        suffix = ",".join(combo)
        rows.append(
            Row(
                id=f"{row_id}#{suffix}",
                match=member_match,
                guard=guard,
                outcome_kind=outcome_kind,
                outcome=outcome_value,
                escape=escape,
            )
        )
    return rows


def _normalize_literal_block(
    raw: dict, dimensions: dict[str, Dimension], row_id: str, block_name: str
) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            values: tuple[str, ...] = (value,)
        elif isinstance(value, list):
            if not value:
                raise TableLoadError(f"row {row_id!r}: {block_name}.{key} is an empty list literal")
            values = tuple(value)
        else:
            raise TableLoadError(f"row {row_id!r}: {block_name}.{key} must be a string or a list of strings")
        _validate_domain(key, values, dimensions, row_id, block_name)
        out[key] = values
    return out


def _normalize_match_scalars(
    match: dict[str, str], dimensions: dict[str, Dimension], row_id: str
) -> dict[str, str]:
    for key, value in match.items():
        _validate_domain(key, (value,), dimensions, row_id, "match")
    return dict(match)


def _validate_domain(
    key: str,
    values: tuple[str, ...],
    dimensions: dict[str, Dimension],
    row_id: str,
    block_name: str,
) -> None:
    """Refuse a literal outside its dimension's declared domain.

    Only checked when the key IS declared as an enum dimension with a
    non-empty domain — an undeclared or non-enum dimension is not a load
    error, it is a check-time ``unprovable-coverage`` finding (the checker
    refuses to claim coverage there, it does not refuse to load).
    """
    dim = dimensions.get(key)
    if dim is not None and dim.kind == "enum" and dim.domain:
        bad = set(values) - set(dim.domain)
        if bad:
            raise UnknownLiteralError(
                f"row {row_id!r}: {block_name}.{key} literal(s) {sorted(bad)} "
                f"not in declared domain {list(dim.domain)}"
            )


def _check_escape_multiplicity(rows: list[Row]) -> None:
    seen: dict[tuple[tuple[str, str], ...], str] = {}
    for row in rows:
        if not row.escape:
            continue
        key = tuple(sorted(row.match.items()))
        if key in seen:
            raise MultipleEscapesInGroupError(
                f"rows {seen[key]!r} and {row.id!r} both carry escape=true for match {dict(key)}"
            )
        seen[key] = row.id
