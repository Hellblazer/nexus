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

from pathlib import Path

import pytest

import check_wire_contract_pairing as _wire_ledger
import release_choreography as _choreo

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


@pytest.fixture(autouse=True)
def _reset_decision_path() -> None:
    """RDR-201 P2.4 (nexus-j9z30.14): ``release_choreography.DECISION_PATH``
    -- the ONE switch both gated scripts consult (nexus-w2x5x) -- is a
    module-level flag a test can flip (directly, or via
    ``test_release_table_parity.py``'s ``_new_path``, which already
    restores it in a ``finally``). This is defense-in-depth against a test
    that flips it and errors before restoring -- every test in this
    directory starts AND ends on the default "old" path, never leaking
    "table" into an unrelated test."""
    _choreo.DECISION_PATH = "old"
    yield
    _choreo.DECISION_PATH = "old"


@pytest.fixture
def table_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the gated scripts through the choreography table for one test.
    ``monkeypatch.setattr`` (raising on a missing attribute) rather than a
    bare assignment: a flip written onto a module that no longer owns the
    switch must fail loudly, not silently drive the old path and pass."""
    monkeypatch.setattr(_choreo, "DECISION_PATH", "table")
