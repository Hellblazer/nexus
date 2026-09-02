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


def test_run_gate_does_not_swallow_other_exceptions() -> None:
    def _main() -> int:
        raise ValueError("not a table defect")

    with pytest.raises(ValueError):
        _choreo.run_gate(_main)


def test_resolve_choreography_row_refuses_out_of_domain_value_as_table_defect() -> None:
    with pytest.raises(_choreo.TableDefect):
        _choreo.resolve_choreography_row("check_pin_currency", {"newest": "not-a-real-value"})


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
