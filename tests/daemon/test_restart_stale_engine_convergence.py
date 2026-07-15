# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cfgo9: ``nx daemon restart-stale`` also converges the local engine
and heals ``nexus.diag_chash_conformance`` grant/ownership drift.

Wiring test only — the convergence/heal logic itself
(``nexus.upgrade_finish.converge_engine`` / ``detect_engine_convergence`` /
``heal_diag_view``) is unit-tested in ``tests/test_upgrade_finish.py``. This
confirms the CLI verb actually calls both and renders their action lines (or
the no-op lines).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands.daemon import daemon_group
from nexus.upgrade_finish import SkewReport


def _invoke(
    tmp_path: Path,
    *,
    engine_actions: list[str],
    heal_actions: list[str] | None = None,
    extra_args: list[str] | None = None,
):
    runner = CliRunner()
    with patch(
        "nexus.commands.daemon.nexus_config_dir", return_value=tmp_path,
    ), patch(
        "nexus.upgrade_finish.detect_stale_processes",
        return_value=SkewReport(installed_version="9.9.9"),
    ), patch(
        "nexus.upgrade_finish.install_source", return_value="PyPI, unpinned",
    ), patch(
        "nexus.upgrade_finish.converge_engine", return_value=engine_actions,
    ) as converge, patch(
        "nexus.upgrade_finish.heal_diag_view",
        return_value=heal_actions if heal_actions is not None else [],
    ) as heal:
        result = runner.invoke(
            daemon_group, ["restart-stale", *(extra_args or [])],
        )
    return result, converge, heal


class TestRestartStaleEngineConvergence:
    def test_no_action_needed_prints_noop_line(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(tmp_path, engine_actions=[])
        assert result.exit_code == 0, result.output
        assert "engine: no convergence action needed" in result.output
        converge.assert_called_once_with(tmp_path, dry_run=False)

    def test_convergence_actions_are_rendered(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(
            tmp_path,
            engine_actions=[
                "converged engine: installed engine-service-v0.1.43 (was 0.1.42)",
                "restarted the storage service to pick up the converged engine",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "converged engine: installed engine-service-v0.1.43" in result.output
        assert "restarted the storage service" in result.output
        converge.assert_called_once_with(tmp_path, dry_run=False)

    def test_dry_run_is_threaded_through(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(
            tmp_path, engine_actions=[], extra_args=["--dry-run"],
        )
        assert result.exit_code == 0, result.output
        converge.assert_called_once_with(tmp_path, dry_run=True)

    def test_needs_human_engine_action_is_rendered(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(
            tmp_path,
            engine_actions=[
                "NEEDS HUMAN: engine convergence blocked — the store looks "
                "chash-poisoned; installed engine stays at 0.1.42, required "
                "0.1.43. Remediate first, then re-run: UNBLOCK: ...",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "NEEDS HUMAN: engine convergence blocked" in result.output

    def test_process_skew_failure_does_not_block_convergence_or_heal(
        self, tmp_path: Path,
    ) -> None:
        """nexus-cfgo9 (found via the real --package-upgrade rehearsal on a
        `ps`-less minimal container): detect_stale_processes raising must
        not abort the whole command — convergence and the diag-view heal are
        independent legs and must still run."""
        runner = CliRunner()
        with patch(
            "nexus.commands.daemon.nexus_config_dir", return_value=tmp_path,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'ps'"),
        ), patch(
            "nexus.upgrade_finish.converge_engine",
            return_value=["converged engine: installed engine-service-v0.1.43 (was 0.1.42)"],
        ) as converge, patch(
            "nexus.upgrade_finish.heal_diag_view",
            return_value=["healed: nexus_diag lacked SELECT ..."],
        ) as heal:
            result = runner.invoke(daemon_group, ["restart-stale"])

        assert result.exit_code == 0, result.output
        assert "process-skew detection failed" in result.output
        assert "converged engine: installed engine-service-v0.1.43" in result.output
        assert "healed: nexus_diag lacked SELECT" in result.output
        converge.assert_called_once_with(tmp_path, dry_run=False)
        heal.assert_called_once_with(tmp_path)

    def test_converge_engine_unexpected_raise_does_not_block_heal(
        self, tmp_path: Path,
    ) -> None:
        """code-review HIGH, defense-in-depth: converge_engine documents a
        'never raises' contract, but that contract lives in one function.
        The CLI call site wraps it independently too (the same pattern the
        process-skew fix established) so a gap in that contract -- or a
        future regression of it -- degrades to a loud line and STILL lets
        the diag-view heal leg run, rather than aborting the whole command
        with an unhandled traceback and skipping heal entirely (the exact
        asymmetry the process-skew fix closed one leg earlier)."""
        runner = CliRunner()
        with patch(
            "nexus.commands.daemon.nexus_config_dir", return_value=tmp_path,
        ), patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="9.9.9"),
        ), patch(
            "nexus.upgrade_finish.install_source", return_value="PyPI, unpinned",
        ), patch(
            "nexus.upgrade_finish.converge_engine",
            side_effect=RuntimeError("unexpected gap in the never-raises contract"),
        ) as converge, patch(
            "nexus.upgrade_finish.heal_diag_view",
            return_value=["healed: nexus_diag lacked SELECT ..."],
        ) as heal:
            result = runner.invoke(daemon_group, ["restart-stale"])

        assert result.exit_code == 0, result.output
        assert "engine convergence failed" in result.output
        assert "unexpected gap in the never-raises contract" in result.output
        assert "healed: nexus_diag lacked SELECT" in result.output
        converge.assert_called_once_with(tmp_path, dry_run=False)
        heal.assert_called_once_with(tmp_path)


class TestRestartStaleDiagViewHeal:
    def test_no_action_needed_prints_noop_line(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(tmp_path, engine_actions=[], heal_actions=[])
        assert result.exit_code == 0, result.output
        assert "diag-view heal: no action needed" in result.output
        heal.assert_called_once_with(tmp_path)

    def test_heal_actions_are_rendered(self, tmp_path: Path) -> None:
        result, converge, heal = _invoke(
            tmp_path,
            engine_actions=[],
            heal_actions=[
                "healed: nexus.diag_chash_conformance was owned by "
                "non-RLS-exempt role 'nexus_admin' (ownership fragmentation, "
                "GH #1402) — reassigned to the superuser bootstrap role "
                "'hal.hildebrand'",
                "healed: nexus_diag lacked SELECT on "
                "nexus.diag_chash_conformance (missing-grant class, GH #1402) "
                "— granted",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ownership fragmentation" in result.output
        assert "missing-grant class" in result.output
        heal.assert_called_once_with(tmp_path)

    def test_dry_run_skips_heal_entirely(self, tmp_path: Path) -> None:
        """GRANT/ALTER OWNER have no dry-run preview mode — --dry-run must
        not execute them, and must say so rather than silently skipping."""
        result, converge, heal = _invoke(
            tmp_path, engine_actions=[], extra_args=["--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "diag-view heal: skipped (--dry-run" in result.output
        heal.assert_not_called()
