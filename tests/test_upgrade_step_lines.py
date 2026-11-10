# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx upgrade`` announces each step, times it and summarises (nexus-mjmjl).

Every step's work is replaced by a recorder (this module's own functions,
never a stdlib patch), so the test pins the order, the count and the lines,
not the work."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.commands.upgrade as up

STEP_FUNCS = (
    "_run_upgrade", "_converge_preconditions", "_run_ladder", "_emit_name_vs_embed_dim_advisory",
    "_migrate_repos_json_to_catalog", "_refresh_all_git_hooks", "_converge_plugins",
    "_refresh_generated_agents", "_install_beads_prime_best_effort", "_cycle_supervised_daemons_to_current",
)


@pytest.fixture()
def ran(monkeypatch) -> list[str]:
    calls: list[str] = []
    for name in STEP_FUNCS:
        monkeypatch.setattr(up, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr("nexus.config.backfill_install_mode_record", lambda: None)
    monkeypatch.setattr("nexus.lockstep_marker.settle_upgrade_retry", lambda *_a: None)
    return calls


def test_an_interactive_upgrade_announces_every_step_in_order_and_summarises(ran) -> None:
    result = CliRunner().invoke(up.upgrade, [])
    assert result.exit_code == 0, result.output
    out = result.stdout
    steps = [line for line in out.splitlines() if line.startswith("Step ")]
    assert steps[0] == "Step 1 of 10: Check for client-side migrations"
    assert steps[1].startswith("Step 2 of 10: Bring the engine, the PDF stack and the runtime to this release (")
    assert steps[2].startswith("Step 3 of 10: Run the upgrade ladder's data steps (")
    assert steps[-1].startswith("Step 10 of 10: Restart running services on the new version")
    assert len(steps) == 10
    assert out.count("  done in ") == 10
    assert "Summary: 10 step(s) in " in out
    assert ran == list(STEP_FUNCS)


def test_auto_is_quiet_on_stdout(ran) -> None:
    result = CliRunner().invoke(up.upgrade, ["--auto"])
    assert result.exit_code == 0
    assert "Step " not in result.stdout and "Summary:" not in result.stdout
    # P0 (nexus-f3yqd): --auto never restarts the storage service, so the final restart is not run.
    assert ran == ["_run_upgrade", "_converge_preconditions", "_run_ladder"]


def test_a_failing_step_says_so_and_the_summary_still_prints(monkeypatch, ran) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("ladder rung manifest-backfill failed: catalog unreachable")

    monkeypatch.setattr(up, "_run_ladder", boom)
    result = CliRunner().invoke(up.upgrade, [])
    assert result.exit_code != 0
    out = result.stdout
    assert "  failed after " in out and "ladder rung manifest-backfill failed: catalog unreachable" in out
    assert "Restart running services on the new version" in out  # the finally step still runs
    assert "(failed)" in out.split("Summary:")[1]


def test_dry_run_counts_only_the_steps_it_runs(ran) -> None:
    result = CliRunner().invoke(up.upgrade, ["--dry-run"])
    assert result.exit_code == 0, result.output
    steps = [line for line in result.stdout.splitlines() if line.startswith("Step ")]
    assert [s.split(":")[0] for s in steps] == [f"Step {i} of 6" for i in range(1, 7)]


def test_the_finish_pass_says_what_it_is_doing_before_it_does_it(capsys) -> None:
    """The root group's finish pass (a version change) restarts services and
    converges the engine before the command runs; it now says so first, on
    stderr (the user-seat rehearsal measured 6+ s of silence there)."""
    from nexus import upgrade_finish

    upgrade_finish._announce_finish_pass("8.0.0", "8.1.0")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("conexus changed from 8.0.0 to 8.1.0: finishing the switch first")


def test_the_finish_heartbeat_announces_then_closes_with_its_duration(capsys) -> None:
    from nexus import upgrade_finish

    beat = upgrade_finish._FinishHeartbeat()
    beat.start("7.99.0", "8.0.0")
    beat.stop()
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("conexus changed from 7.99.0 to 8.0.0: finishing the switch first")
    assert err[-1].startswith("  finished the switch in ")


def test_a_finish_pass_that_never_announces_prints_nothing(capsys) -> None:
    from nexus import upgrade_finish

    upgrade_finish._FinishHeartbeat().stop()
    assert capsys.readouterr().err == ""


def test_check_version_transition_wraps_the_pass_in_the_heartbeat(monkeypatch, capsys, tmp_path) -> None:
    from nexus import upgrade_finish

    def fake_pass(config_dir, *, preview=None, engine_report_only=False, _on_announce=None):
        _on_announce("7.99.0", "8.0.0")
        return "summary"

    monkeypatch.setattr(upgrade_finish, "_check_version_transition", fake_pass)
    assert upgrade_finish.check_version_transition(tmp_path) == "summary"
    err = capsys.readouterr().err
    assert "conexus changed from 7.99.0 to 8.0.0" in err and "finished the switch in" in err


def test_a_finish_pass_that_raises_does_not_claim_it_finished(monkeypatch, capsys, tmp_path) -> None:
    from nexus import upgrade_finish

    def fake_pass(config_dir, *, preview=None, engine_report_only=False, _on_announce=None):
        _on_announce("7.99.0", "8.0.0")
        raise RuntimeError("boom")

    monkeypatch.setattr(upgrade_finish, "_check_version_transition", fake_pass)
    with pytest.raises(RuntimeError):
        upgrade_finish.check_version_transition(tmp_path)
    err = capsys.readouterr().err
    assert "finished the switch" not in err and "the switch stopped after" in err


def test_the_finish_heartbeat_ticks_until_stopped(capsys) -> None:
    from nexus import upgrade_finish

    class FakeEvent:
        def __init__(self) -> None:
            self.answers = iter([False, False, True])

        def wait(self, _s):
            return next(self.answers, True)

        def set(self):
            pass

    beat = upgrade_finish._FinishHeartbeat()
    beat._stop = FakeEvent()
    beat._started = 0.0
    beat._run()
    assert capsys.readouterr().err.count("... still finishing the switch: ") == 2
