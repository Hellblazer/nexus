# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx init``'s local first-run path runs on the shared step reporter
(nexus-mjmjl): each step announced, timed and summarised. The work is
replaced by recorders on this module's own functions, never a stdlib patch."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.commands.init as init_mod


@pytest.fixture()
def ran(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(init_mod._config, "detect_stranded_install_default", lambda: None)
    monkeypatch.setattr(init_mod._config, "is_local_mode", lambda: True)
    monkeypatch.setattr(init_mod, "_install_beads_prime_best_effort", lambda **_k: None)
    monkeypatch.setattr(init_mod, "_resolve_init_mode", lambda: "local")
    monkeypatch.setattr(init_mod, "_decide_autostart", lambda *_a: False)
    monkeypatch.setattr(init_mod, "provision_and_start_service", lambda *_a: calls.append("provision") or object())
    monkeypatch.setattr(init_mod, "_converge_ladder_best_effort", lambda: calls.append("ladder"))
    monkeypatch.setattr(init_mod, "_seed_builtin_plans_best_effort", lambda: calls.append("seed"))
    return calls


def test_the_local_first_run_announces_each_step_and_summarises(ran) -> None:
    result = CliRunner().invoke(init_mod.init_cmd, [])
    assert result.exit_code == 0, result.output
    steps = [line for line in result.stdout.splitlines() if line.startswith("Step ")]
    assert steps[0].startswith("Step 1 of 3: Provision and start the local service (on a first run this downloads")
    assert steps[1].startswith("Step 2 of 3: Run the upgrade ladder")
    assert steps[2] == "Step 3 of 3: Seed the builtin plan library"
    assert result.stdout.count("  done in ") == 3
    assert "Summary: 3 step(s) in " in result.stdout
    assert ran == ["provision", "ladder", "seed"]


def test_a_service_that_cannot_start_says_so_and_exits_non_zero(monkeypatch, ran) -> None:
    from nexus.daemon.storage_service_daemon import StorageServiceStartError

    def boom(*_a):
        raise StorageServiceStartError("no native binary")

    monkeypatch.setattr(init_mod, "provision_and_start_service", boom)
    result = CliRunner().invoke(init_mod.init_cmd, [])
    assert result.exit_code == 1
    assert "  failed after " in result.stdout and "no native binary" in result.stdout
    assert "Service NOT started" in result.stderr
    assert "Step 2 of 3" not in result.stdout
