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
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

__all__ = [
    "BEHAVIOURS", "BEHAVIOUR_PHRASES", "CENSUS_ROW_COUNT", "ENDPOINT_CLASSES", "ENDPOINT_DESTINATIONS", "PHASES",
    "RELEASE_ENFORCED_PHASE", "SurfaceRow", "Table", "all_targets", "enforced_through", "listed_rows",
    "load_table", "not_yet_enforced_line", "render_policy_table", "render_sensitive_help", "row_for", "rows",
    "rows_of",
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

#: The phase whose rows the release being built enforces (RDR-243 Technical Design item 7). The generated
#: privacy-policy table and ``--sensitive`` help list the rows at or below it and nothing above it. Phases 1 and 2
#: ship as one release, so it is 2; a client release bead moves it and regenerates ``docs/privacy-policy.md``
#: (``scripts/gen_privacy_policy_table.py --write``).
RELEASE_ENFORCED_PHASE: Final = 2

#: The RDR-243 census has 25 rows; the table has one row for each.
CENSUS_ROW_COUNT: Final = 25

_REQUIRED_EMIT_FIELDS: Final[tuple[str, ...]] = (
    "targets", "endpoints", "behaviour", "phase", "census_row", "label", "sends",
)
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
    label: str
    sends: str

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


def _text_field(row_id: object, field: str, value: object) -> str:
    """A label or sends text: non-empty, one line, no ``|`` (it is printed into a markdown table cell)."""
    if not isinstance(value, str) or not value.strip() or "|" in value or "\n" in value:
        raise ValueError(
            f"{TABLE_RESOURCE}: row {row_id!r} {field} must be a non-empty one-line string without '|', got {value!r}"
        )
    return value


def rows_of(table: Table) -> tuple[SurfaceRow, ...]:
    """Every row of *table*, in file order; a row outside the closed vocabularies is refused."""
    out: list[SurfaceRow] = []
    seen_census: dict[int, str] = {}
    seen_labels: dict[str, str] = {}
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
        label, sends = _text_field(row.id, "label", emit["label"]), _text_field(row.id, "sends", emit["sends"])
        if label in seen_labels:
            raise ValueError(f"{TABLE_RESOURCE}: rows {seen_labels[label]!r} and {row.id!r} both carry label {label!r}")
        seen_labels[label] = str(row.id)
        out.append(SurfaceRow(
            name=name, census_row=census_row, targets=targets, endpoints=endpoints,
            behaviour=behaviour, phase=phase, label=label, sends=sends,
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


# --------------------------------------------------------------------------- #
# Generated text: the privacy-policy section 2 table and the ``--sensitive`` help.
# Both list the rows enforced in the release (``RELEASE_ENFORCED_PHASE``) and carry one
# line about the rows that are not (RDR-243 accept residual S-E, nexus-rfvyy.5).
# --------------------------------------------------------------------------- #

#: Where each endpoint class sends text, as the policy words it. ``none`` is absent on purpose: a row that
#: reaches nothing is not listed.
ENDPOINT_DESTINATIONS: Final[dict[str, str]] = {
    "anthropic": "Anthropic (`claude -p` or `api.anthropic.com`)",
    "openai-compat": "the endpoint in `llm.openai_compat.base_url`",
    "voyage": "Voyage AI (`api.voyageai.com`)",
    "s2": "Semantic Scholar",
    "openalex": "OpenAlex",
    "crossref-via-dt": "CrossRef, through DEVONthink",
    "mineru-url": "the MinerU server at `pdf.mineru_server_url`",
    "cloud-tenant": "the managed nexus-service (managed-cloud mode)",
    "session-model": "the model of the Claude Code session",
    "none": "nowhere",
}

#: What a row does to a sensitive document, as the policy table's last column words it.
BEHAVIOUR_PHRASES: Final[dict[str, str]] = {
    "refuse": "refused",
    "skip": "skipped, loudly",
    "exclude": "excluded",
    "withhold": "withheld from the session model",
    "disclose": "disclosed; needs an acknowledgement",
    "n/a-not-a-document": "not covered: a note is not a document",
    "n/a-no-content": "not applicable: no content is sent",
}

#: The order the help groups behaviours in, with the lead-in each group takes.
_HELP_GROUPS: Final[tuple[tuple[str, str], ...]] = (
    ("refuse", "Refused by"),
    ("skip", "Skipped by"),
    ("exclude", "Excluded from"),
    ("withhold", "Withheld from the session model by"),
    ("disclose", "Disclosed, with an acknowledgement required, by"),
    ("n/a-not-a-document", "Not covered"),
)


def _rows_or_default(table_rows: Sequence[SurfaceRow] | None) -> Sequence[SurfaceRow]:
    return rows() if table_rows is None else table_rows


def listed_rows(phase: int = RELEASE_ENFORCED_PHASE, table_rows: Sequence[SurfaceRow] | None = None) -> tuple[SurfaceRow, ...]:
    """The rows the generated text lists: enforced at or below *phase*, and reaching somewhere."""
    return tuple(r for r in _rows_or_default(table_rows) if r.phase <= phase and r.endpoints != ("none",))


def not_yet_enforced_line(
    phase: int = RELEASE_ENFORCED_PHASE, table_rows: Sequence[SurfaceRow] | None = None,
) -> str:
    """One line naming what the release does not yet cover, or ``""`` when every such row is enforced.

    Derived from the rows above *phase*, never written by hand: session-model rows that withhold (the output
    posture, census rows 23 and 24) and rows that send to Voyage (the rerank, census row 20). Omitting a
    protection is not disclosing its absence, so this line is what the honesty rule of RDR-243 leans on.
    """
    pending = [r for r in _rows_or_default(table_rows) if r.phase > phase]
    shown = [r for r in pending if "session-model" in r.endpoints and r.behaviour == "withhold"]
    voyage = [r for r in pending if "voyage" in r.endpoints]
    clauses: list[str] = []
    if shown:
        labels = "; ".join(r.label for r in shown)
        clauses.append(f"search results ({labels}) are still shown to the session model until the output posture ships")
    clauses.extend(f"{r.label} still sends {r.sends} to Voyage AI until it excludes sensitive documents" for r in voyage)
    return f"Not yet enforced: {'; '.join(clauses)}." if clauses else ""


def render_policy_table(
    phase: int = RELEASE_ENFORCED_PHASE, table_rows: Sequence[SurfaceRow] | None = None,
) -> str:
    """The privacy-policy section 2 table for the release enforcing *phase*, byte for byte (no trailing newline)."""
    listed = listed_rows(phase, table_rows)
    out = [
        "Each row is a feature that sends document-derived text off the machine or to a model. A destination "
        "applies only where your mode and configuration reach it. The last column says what `--sensitive` does "
        "to a document in this release.",
        "",
        "| Surface | What it sends | Where | With `--sensitive` |",
        "| --- | --- | --- | --- |",
    ]
    for r in listed:
        where = "; ".join(ENDPOINT_DESTINATIONS[e] for e in r.endpoints if e != "none")
        out.append(f"| {r.label} | {r.sends} | {where} | {BEHAVIOUR_PHRASES[r.behaviour]} |")
    line = not_yet_enforced_line(phase, table_rows)
    if line:
        out += ["", line]
    return "\n".join(out)


def render_sensitive_help(
    phase: int = RELEASE_ENFORCED_PHASE, table_rows: Sequence[SurfaceRow] | None = None,
) -> str:
    """The ``--sensitive`` option help for the release enforcing *phase*.

    The flag marks the document, and every surface reads the mark, so the text is the same for every verb
    that takes the flag (``nx index pdf``, ``nx index md``, ``nx dt index``, ``nx dt capture``,
    ``nx store put``). Paragraphs are separated by a blank line, which click keeps.
    """
    listed = listed_rows(phase, table_rows)
    parts = ["Mark the document sensitive."]
    for behaviour, lead in _HELP_GROUPS:
        labels = [r.label for r in listed if r.behaviour == behaviour]
        if labels:
            parts.append(f"{lead}: {'; '.join(labels)}.")
    text = " ".join(parts)
    line = not_yet_enforced_line(phase, table_rows)
    return f"{text}\n\n{line}" if line else text
