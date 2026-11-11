# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release battery's lsg leg is smoke-only, and stops only after the smoke proof (nexus-jh1f2 cut 4)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = (ROOT / "tests" / "e2e" / "local-service-gate.sh").read_text(encoding="utf-8").splitlines()
BATTERY = (ROOT / "tests" / "e2e" / "release-battery.sh").read_text(encoding="utf-8")


def _line(pred) -> int:
    hits = [i for i, line in enumerate(GATE) if pred(line)]
    assert len(hits) == 1, hits
    return hits[0]


def test_the_smoke_only_exit_comes_after_the_smoke_and_engine_log_proofs_and_before_the_family() -> None:
    smoke_only = _line(lambda s: s.startswith('if [ "${NX_GATE_SMOKE_ONLY:-0}" = "1" ]'))
    smoke_count = _line(lambda s: s.startswith('smoke_verify_count "$SMOKE_PASSED" "$SMOKE_EXPECTED"'))
    engine_log = _line(lambda s: s.startswith('engine_log_expect "event=pci_sweep valid="'))
    family = _line(lambda s: s.startswith("LIVED_IN_COUNT="))
    assert smoke_count < smoke_only and engine_log < smoke_only < family


def test_the_battery_runs_lsg_smoke_only() -> None:
    assert ('define_leg lsg        group  "LOCAL-SERVICE GATE (PASSED|FAILED)"       '
            "env NX_GATE_SMOKE_ONLY=1 tests/e2e/local-service-gate.sh") in BATTERY
