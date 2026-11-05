# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LLM-surface registry, read from ``llm-surfaces.toml`` (RDR-243 Technical Design item 7, nexus-rfvyy.6.16).

One row per census row of RDR-243 (Key Discoveries): every surface that sends
document-derived text off the machine or to a model. A row names the
``module:function`` targets that carry the surface, the endpoint classes the
text can reach, the behaviour the RDR proposes for a sensitive document, and
the phase that enforces it. The TOML carries the field meanings; this module
loads it through the house loader, closes the vocabularies, and answers the
questions the canary, the registry lint and the generated policy text ask.

Importing this module imports only ``nexus.tables``; the targets are names,
never imported here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

__all__ = [
    "BEHAVIOURS", "CENSUS_ROW_COUNT", "ENDPOINT_CLASSES", "PHASES", "SurfaceRow", "Table", "all_targets",
    "enforced_through", "load_table", "row_for", "rows", "rows_of",
]

TABLE_RESOURCE: Final = "llm-surfaces.toml"

ENDPOINT_CLASSES: Final[tuple[str, ...]] = (
    "anthropic", "openai-compat", "voyage", "s2", "openalex", "crossref-via-dt",
    "mineru-url", "cloud-tenant", "session-model", "none",
)
BEHAVIOURS: Final[tuple[str, ...]] = (
    "refuse", "skip", "exclude", "withhold", "disclose", "n/a-not-a-document", "n/a-no-content",
)
PHASES: Final[tuple[int, ...]] = (1, 2, 3)

#: The RDR-243 census has 25 rows; the table has one row for each.
CENSUS_ROW_COUNT: Final = 25

_REQUIRED_EMIT_FIELDS: Final[tuple[str, ...]] = ("targets", "endpoints", "behaviour", "phase", "census_row")
_TARGET_RE: Final = re.compile(
    r"^nexus(\.[a-z_][a-z0-9_]*)*:(<module>|[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)


@dataclass(frozen=True)
class SurfaceRow:
    """One census row. ``targets[0]`` is the primary entry point and ``endpoints[0]`` the primary class."""

    name: str
    census_row: int
    targets: tuple[str, ...]
    endpoints: tuple[str, ...]
    behaviour: str
    phase: int

    @property
    def target(self) -> str:
        return self.targets[0]

    @property
    def endpoint(self) -> str:
        return self.endpoints[0]


def _str_list(row_id: object, field: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{TABLE_RESOURCE}: row {row_id!r} {field} must be a non-empty list of strings, got {value!r}")
    return tuple(value)


def rows_of(table: Table) -> tuple[SurfaceRow, ...]:
    """Every row of *table*, in file order; a row outside the closed vocabularies is refused."""
    out: list[SurfaceRow] = []
    seen_census: dict[int, str] = {}
    for row in table.rows:
        emit = row.outcome
        if not isinstance(emit, dict):
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has no emit table")
        missing = [f for f in _REQUIRED_EMIT_FIELDS if f not in emit]
        if missing:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} lacks emit field(s) {missing}")
        name = str(row.match["surface"])
        targets = _str_list(row.id, "targets", emit["targets"])
        for t in targets:
            if not _TARGET_RE.match(t):
                raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} target {t!r} is not nexus.module:function")
        endpoints = _str_list(row.id, "endpoints", emit["endpoints"])
        for e in endpoints:
            if e not in ENDPOINT_CLASSES:
                raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has endpoint {e!r}, not one of {ENDPOINT_CLASSES}")
        behaviour = emit["behaviour"]
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has behaviour {behaviour!r}, not one of {BEHAVIOURS}")
        phase, census_row = emit["phase"], emit["census_row"]
        if isinstance(phase, bool) or not isinstance(phase, int) or phase not in PHASES:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has phase {phase!r}, not one of {PHASES}")
        if isinstance(census_row, bool) or not isinstance(census_row, int) or census_row < 1:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has census_row {census_row!r}, not a census row number")
        if census_row in seen_census:
            raise ValueError(
                f"{TABLE_RESOURCE}: rows {seen_census[census_row]!r} and {row.id!r} both carry census_row {census_row}"
            )
        seen_census[census_row] = str(row.id)
        out.append(SurfaceRow(
            name=name, census_row=census_row, targets=targets, endpoints=endpoints,
            behaviour=behaviour, phase=phase,
        ))
    return tuple(out)


@lru_cache(maxsize=1)
def load_table() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


@lru_cache(maxsize=1)
def rows() -> tuple[SurfaceRow, ...]:
    return rows_of(load_table())


def row_for(name: str) -> SurfaceRow:
    for r in rows():
        if r.name == name:
            return r
    raise KeyError(name)


def enforced_through(phase: int) -> tuple[SurfaceRow, ...]:
    """The rows enforced in phase *phase* or earlier: what the release of that phase lists and the canary judges."""
    if phase not in PHASES:
        raise ValueError(f"phase {phase!r} is not one of {PHASES}")
    return tuple(r for r in rows() if r.phase <= phase)


def all_targets() -> tuple[str, ...]:
    """Every ``module:function`` any row names, once, in table order: what the registry lint treats as registered."""
    seen: dict[str, None] = {}
    for r in rows():
        for t in r.targets:
            seen.setdefault(t)
    return tuple(seen)
