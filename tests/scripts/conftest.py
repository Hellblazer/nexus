# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/scripts — isolate the release-gate LOGIC tests from live repo state.

``check_engine_release_floor.py`` and ``check_client_release_precondition.py``
both consult the checked-in wire-contract ledger
(``docs/wire-contract-pending.md``, via ``check_wire_contract_pairing
.DEFAULT_LEDGER_PATH``) on their real deploy paths. A non-empty
``## Unshipped`` section is a LEGITIMATE repo state (an engine-side wire
change whose client half is not yet in a published release) and makes those
gates return BLOCKED by design — but the logic tests here exercise the
paired-deploy / precondition branches with mocks and expect the ledger leg
to be quiet. Without isolation, the first real Unshipped entry
(2c95bae17, 2026-08-15: RDR-191 Phase 6 declared) turned 24 unrelated logic
tests red.

So every test in this directory runs against an EMPTY, tmp ledger unless it
opts out with ``@pytest.mark.real_ledger`` — the marker the
``TestWireContractLedgerNonVacuitySelfTest`` class carries precisely because
its job is to prove the production import reaches the real checked-in file
(an autouse fixture must not vacuate that contract). Tests that patch the
path themselves (``_write_ledger`` helpers) still win: their inner patch
shadows this one.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

import check_wire_contract_pairing as _wire_ledger
import release_choreography as _choreo
from nexus.tables.load import Table

_EMPTY_LEDGER = "## Unshipped\n\n(none)\n\n## Shipped\n"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_ledger: this test must see the REAL checked-in wire-contract "
        "ledger (opts out of tests/scripts/conftest.py's empty-ledger isolation)",
    )


@pytest.fixture(autouse=True)
def _isolate_wire_contract_ledger(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real_ledger") is not None:
        return
    ledger = tmp_path / "wire-contract-pending.md"
    ledger.write_text(_EMPTY_LEDGER, encoding="utf-8")
    monkeypatch.setattr(_wire_ledger, "DEFAULT_LEDGER_PATH", ledger)


@pytest.fixture(autouse=True)
def _scrub_gate_report_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-nx3l5: ``NX_GATE_REPORT_DIR`` is meant to be set once, globally, on
    the operator's box so the bare post-tag verify records the tracker. Inside
    the test suite that global must never turn a ``main(["--url", ...])`` call
    into a tracker write; every test that wants the env sets it itself."""
    monkeypatch.delenv("NX_GATE_REPORT_DIR", raising=False)


@pytest.fixture
def mutate_choreography_row() -> Callable[[str, int], Table]:
    """Factory: a copy of the REAL choreography table with ONE row's
    ``emit.exit_code`` replaced -- every other row untouched. ``Row`` and
    ``Table`` are frozen dataclasses, so this rebuilds both via
    ``dataclasses.replace``; the real cached table
    (``release_choreography.choreography_table()``) and the file on disk are
    never touched. Pair with ``patch.object(release_choreography,
    "choreography_table", return_value=<mutated>)`` to steer BOTH real gated
    scripts onto the corrupted copy (nexus-w2x5x): a script whose table path
    is genuinely live returns the mutated exit code; one whose switch is
    stale or whose emit reads some other cache returns the real one."""

    def _mutate(row_id: str, exit_code: int) -> Table:
        table = _choreo.choreography_table()
        rows = list(table.rows)
        for i, row in enumerate(rows):
            if row.id != row_id:
                continue
            assert isinstance(row.outcome, dict), (row_id, row.outcome)
            rows[i] = dataclasses.replace(row, outcome={**row.outcome, "exit_code": str(exit_code)})
            return dataclasses.replace(table, rows=tuple(rows))
        raise AssertionError(f"no row with id {row_id!r} in the real choreography table")

    return _mutate
