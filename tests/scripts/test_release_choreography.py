# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/release_choreography.py -- the one decision path both release
gates share (RDR-201 P2.4-P2.6). The cell-by-cell behaviour is pinned by
test_release_table_parity.py; these are the module's own contracts: a
table-level defect refuses to run with exit 2 (RDR-201 § Failure Modes),
and an ``emit`` table cannot carry a key nothing reads."""
from __future__ import annotations

import dataclasses

import pytest

import release_choreography as _choreo


def test_run_gate_turns_a_table_defect_into_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    def _main() -> int:
        raise _choreo.TableDefect("row 'x' is wrong")

    assert _choreo.run_gate(_main) == 2
    err = capsys.readouterr().err
    assert err.startswith("TABLE DEFECT (exit 2): row 'x' is wrong")


def test_run_gate_passes_an_ordinary_verdict_through() -> None:
    assert _choreo.run_gate(lambda: 3) == 3


def test_run_gate_turns_any_other_exception_into_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    """[26114] #2: run_gate used to catch ONLY TableDefect, so a catalog
    miss (KeyError from release_messages.get) or any other non-TableDefect
    escape exited 1 -- the BLOCKED code -- instead of the exit 2 the module
    docstring promises for a defect that can never be misread as a
    legitimate refusal. A catch-all maps every other exception to exit 2,
    naming its type and message."""

    def _main() -> int:
        raise ValueError("not a table defect")

    assert _choreo.run_gate(_main) == 2
    err = capsys.readouterr().err
    assert err.startswith("TABLE DEFECT (exit 2):")
    assert "ValueError" in err
    assert "not a table defect" in err


def test_run_gate_catches_a_catalog_miss_as_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    """The concrete failure scenario [26114] #2 names: a row added to the
    table with no matching release_messages entry raises KeyError inside
    emit_choreography, escaping run_gate's old TableDefect-only catch."""
    import release_messages as _release_messages  # noqa: PLC0415 — test-local

    key = "check_client_lag_ledger::ledger_blocked"
    saved = _release_messages.RELEASE_MESSAGES.pop(key)
    try:
        def _main() -> int:
            return _choreo.emit_choreography("check_client_lag_ledger", {"ledger": "blocking"})

        assert _choreo.run_gate(_main) == 2
    finally:
        _release_messages.RELEASE_MESSAGES[key] = saved
    err = capsys.readouterr().err
    assert err.startswith("TABLE DEFECT (exit 2):")
    assert "KeyError" in err


def test_resolve_choreography_row_refuses_out_of_domain_value_as_table_defect() -> None:
    with pytest.raises(_choreo.TableDefect):
        _choreo.resolve_choreography_row("check_pin_currency", {"newest": "not-a-real-value"})


def test_resolve_choreography_row_refuses_a_misspelt_guard_key() -> None:
    """[26114] #1: a misspelt guard key (``ledgr`` for ``ledger``) used to
    be silently dropped -- the real ``ledger`` dimension then defaulted to
    its domain's first member and resolved to whatever row that hit
    (``check_client_lag_ledger::ledger_clean``, an exit-0 "ledger clean"
    for a call that never actually looked at the ledger). An undeclared
    guard key is refused by name, never silently ignored."""
    with pytest.raises(_choreo.TableDefect, match="ledgr"):
        _choreo.resolve_choreography_row("check_client_lag_ledger", {"ledgr": "blocking"})


def test_resolve_choreography_row_refuses_an_omitted_guard_key() -> None:
    """Same failure, the other cause: the caller supplies NO guard at all,
    so ``check_client_lag_ledger.ledger`` defaults to its domain's first
    member (``empty``) and resolves to ``ledger_clean`` regardless of the
    real ledger state. The resolved row's own guard names a
    ``check_client_lag_ledger.*`` dimension the caller never supplied --
    refused, never defaulted."""
    with pytest.raises(_choreo.TableDefect, match="check_client_lag_ledger.ledger"):
        _choreo.resolve_choreography_row("check_client_lag_ledger", {})


def test_resolve_choreography_row_refuses_a_guard_chain_supplied_only_up_to_its_first_step() -> None:
    """The multi-dimension GuardChain case: ``check_source_ancestry`` has
    two own dimensions (``tag_exists`` then ``diff_result``); a call that
    supplies only the first and omits the second, once ``tag_exists`` is
    "true", resolves a row that guards on ``diff_result`` too -- refused,
    rather than silently defaulting ``diff_result`` to its domain's first
    member."""
    with pytest.raises(_choreo.TableDefect, match="check_source_ancestry.diff_result"):
        _choreo.resolve_choreography_row("check_source_ancestry", {"tag_exists": "true"})


def test_emit_rejects_an_unknown_emit_key(mutate_choreography_row, capsys: pytest.CaptureFixture[str]) -> None:
    """``nexus.tables.load`` validates only that ``emit`` is a table. A
    misspelt ``strem = "stderr"`` must not be ignored into the exit-code
    default stream: it is a TableDefect."""
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    table = mutate_choreography_row("check_pin_currency::pin_currency_zero_tags", 2)
    rows = list(table.rows)
    for i, row in enumerate(rows):
        if row.id == "check_pin_currency::pin_currency_zero_tags":
            rows[i] = dataclasses.replace(row, outcome={**row.outcome, "strem": "stderr"})
    table = dataclasses.replace(table, rows=tuple(rows))
    with patch.object(_choreo, "choreography_table", return_value=table), \
         pytest.raises(_choreo.TableDefect, match="unknown emit key"):
        _choreo.emit_choreography("check_pin_currency", {"newest": "none"})
    assert capsys.readouterr().err == ""


def test_emit_rejects_a_bad_stream_value(mutate_choreography_row) -> None:
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    table = mutate_choreography_row("check_pin_currency::pin_currency_zero_tags", 2)
    rows = list(table.rows)
    for i, row in enumerate(rows):
        if row.id == "check_pin_currency::pin_currency_zero_tags":
            rows[i] = dataclasses.replace(row, outcome={**row.outcome, "stream": "syslog"})
    table = dataclasses.replace(table, rows=tuple(rows))
    with patch.object(_choreo, "choreography_table", return_value=table), \
         pytest.raises(_choreo.TableDefect, match="emit.stream"):
        _choreo.emit_choreography("check_pin_currency", {"newest": "none"})


def _with_emit(mutate_choreography_row, row_id: str, exit_code: int, **extra):
    table = mutate_choreography_row(row_id, exit_code)
    rows = list(table.rows)
    for i, row in enumerate(rows):
        if row.id == row_id:
            rows[i] = dataclasses.replace(row, outcome={**row.outcome, **extra})
    return dataclasses.replace(table, rows=tuple(rows))


def test_emit_advisory_needs_a_reason_naming_the_default(mutate_choreography_row) -> None:
    """nexus-1c7oq: the advisory line is a self-contained grep target, so
    a row marked passed-by-default must say what default carried it
    (review [24384] Major 1)."""
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    table = _with_emit(
        mutate_choreography_row, "check_pin_currency::pin_currency_current_at_floor", 0,
        advisory="passed-by-default",
    )
    with patch.object(_choreo, "choreography_table", return_value=table), \
         pytest.raises(_choreo.TableDefect, match="advisory_reason"):
        _choreo.emit_choreography("check_pin_currency", {"newest": "at_floor"})


def test_emit_advisory_on_a_refusal_is_a_contradiction(mutate_choreography_row) -> None:
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    table = _with_emit(
        mutate_choreography_row, "check_pin_currency::pin_currency_zero_tags", 2,
        advisory="passed-by-default", advisory_reason="x",
    )
    with patch.object(_choreo, "choreography_table", return_value=table), \
         pytest.raises(_choreo.TableDefect, match="non-zero exit"):
        _choreo.emit_choreography("check_pin_currency", {"newest": "none"})


def test_emit_advisory_prints_the_line_with_the_rows_reason(
    mutate_choreography_row, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import patch  # noqa: PLC0415 — test-local

    from nexus.gate_advisory import count_passed_by_default  # noqa: PLC0415 — test-local

    table = _with_emit(
        mutate_choreography_row, "check_pin_currency::pin_currency_current_at_floor", 0,
        advisory="passed-by-default", advisory_reason="the floor was read from a cached probe",
    )
    with patch.object(_choreo, "choreography_table", return_value=table):
        rc = _choreo.emit_choreography("check_pin_currency", {"newest": "at_floor"})
    out = capsys.readouterr().out
    assert rc == 0 and count_passed_by_default(out) == 1
    assert "GATE PASSED-BY-DEFAULT: check_pin_currency the floor was read from a cached probe" in out
