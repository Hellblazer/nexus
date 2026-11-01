# SPDX-License-Identifier: AGPL-3.0-or-later
"""The validation-outcome vocabulary, read from ``aspect-validation-outcomes.toml`` (RDR-189 P1, nexus-z1d0l.1.5).

Closes the ``status`` and ``reason_code`` vocabularies of
``aspect_validation.jsonl`` rows and says which outcomes still append a row to
the legacy ``aspect_validation_failures.jsonl``. The validator classifies each
sampled record to one reason_code and takes the status from here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "aspect-validation-outcomes.toml"

STATUSES: Final[tuple[str, ...]] = ("verified", "disagreement", "unverifiable", "errored")
REASON_CODES: Final[tuple[str, ...]] = (
    "verified",
    "verified_unwitnessed",
    "verifier_rejected",
    "evidence_truncated",
    "indexed_text_unavailable",
    "verify_exception",
    "malformed_verify_result",
)
_YES_NO: Final[tuple[str, ...]] = ("yes", "no")


@dataclass(frozen=True)
class OutcomeRow:
    reason_code: str
    status: str
    legacy_failures_row: bool


@lru_cache(maxsize=1)
def load_table() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


@lru_cache(maxsize=1)
def rows() -> tuple[OutcomeRow, ...]:
    """Every outcome row, in file order; a row outside the closed vocabularies is refused."""
    out: list[OutcomeRow] = []
    for row in load_table().rows:
        emit = row.outcome
        if not isinstance(emit, dict):
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has no emit table")
        guarded = row.guard.get("reason_code", ())
        if len(guarded) != 1:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} must guard exactly one reason_code")
        code = str(guarded[0])
        status = emit.get("status", "")
        legacy = emit.get("legacy_failures_row", "")
        if code not in REASON_CODES or status not in STATUSES or legacy not in _YES_NO:
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} is outside the closed vocabularies")
        out.append(OutcomeRow(code, status, legacy == "yes"))
    return tuple(out)


def status_for(reason_code: str) -> str:
    """The row status for *reason_code*; an unknown code raises ``KeyError``."""
    for r in rows():
        if r.reason_code == reason_code:
            return r.status
    raise KeyError(reason_code)
