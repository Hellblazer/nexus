# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P1 (nexus-z1d0l.1.5): the all-outcomes validation vocabulary is a checked table.

``src/nexus/tables/aspect-validation-outcomes.toml`` closes the ``status`` and
``reason_code`` vocabularies of ``aspect_validation.jsonl`` rows and records
which outcomes still write a row to the legacy
``aspect_validation_failures.jsonl``. The mapping below is the decision of
record; a change to it is a change to the RDR-189 policy, not a refactor.
"""
from __future__ import annotations

import dataclasses

import pytest

from nexus.tables.load import Row, Table

from nexus.tables import aspect_validation_outcomes as avo
from nexus.tables.check import BLOCKING_CODES, check_table

#: reason_code -> status, as the policy states it.
_EXPECTED_STATUS: dict[str, str] = {
    "verified": "verified",
    "verified_unwitnessed": "unverifiable",
    "verifier_rejected": "disagreement",
    "evidence_truncated": "unverifiable",
    "indexed_text_unavailable": "errored",
    "verify_exception": "errored",
    "malformed_verify_result": "errored",
}

#: The outcomes that write a legacy failures row today: every non-verified read
#: of the verify result, i.e. a false verdict against full evidence, a false
#: verdict against a truncated window, and a payload without a boolean
#: ``verified`` (``result.get("verified", False)`` reads it as false).
_EXPECTED_LEGACY_ROW: frozenset[str] = frozenset(
    {"verifier_rejected", "evidence_truncated", "malformed_verify_result"}
)


def test_table_lints_clean_under_the_house_checker() -> None:
    findings = [f for f in check_table(avo.load_table()) if f.code in BLOCKING_CODES]
    assert findings == []


def test_every_reason_code_has_exactly_one_row() -> None:
    declared = avo.load_table().dimensions["reason_code"].domain
    codes = [r.reason_code for r in avo.rows()]
    assert sorted(codes) == sorted(declared) == sorted(avo.REASON_CODES)
    assert len(codes) == len(set(codes))


def test_status_vocabulary_is_closed_and_every_status_is_reachable() -> None:
    reached = {r.status for r in avo.rows()}
    assert reached == set(avo.STATUSES), "a status no reason_code reaches is dead vocabulary"


def test_reason_code_to_status_mapping_is_the_policy() -> None:
    assert {r.reason_code: r.status for r in avo.rows()} == _EXPECTED_STATUS


def test_legacy_failures_rows_are_exactly_todays() -> None:
    assert {r.reason_code for r in avo.rows() if r.legacy_failures_row} == _EXPECTED_LEGACY_ROW


@pytest.mark.parametrize("code", sorted(_EXPECTED_STATUS))
def test_status_for_answers_each_code(code: str) -> None:
    assert avo.status_for(code) == _EXPECTED_STATUS[code]


def test_status_for_refuses_an_unknown_code() -> None:
    with pytest.raises(KeyError):
        avo.status_for("looks_fine")


def _table_with(rows: list[Row]) -> Table:
    real = avo.load_table()
    return dataclasses.replace(real, rows=tuple(rows))


def _row(guard: tuple[str, ...], emit: dict[str, str] | str, kind: str = "emit") -> Row:
    return Row(id="bad", match={}, guard={"reason_code": guard}, outcome_kind=kind, outcome=emit)


@pytest.mark.parametrize(
    "bad",
    [
        _row(("verified",), {"status": "bogus", "legacy_failures_row": "no"}),
        _row(("verified",), {"status": "verified", "legacy_failures_row": "maybe"}),
        _row(("not_a_code",), {"status": "verified", "legacy_failures_row": "no"}),
        _row(("verified", "verify_exception"), {"status": "errored", "legacy_failures_row": "no"}),
        _row(("verified",), "refuse", kind="to"),
    ],
    ids=["status", "legacy-flag", "reason-code", "multi-code-guard", "no-emit"],
)
def test_loader_refuses_rows_outside_the_vocabularies(
    bad: Row, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_table = _table_with([bad])
    monkeypatch.setattr(avo, "load_table", lambda: bad_table)
    avo.rows.cache_clear()
    try:
        with pytest.raises(ValueError):
            avo.rows()
    finally:
        avo.rows.cache_clear()
