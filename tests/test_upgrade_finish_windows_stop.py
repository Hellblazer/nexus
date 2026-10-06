# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P3.2e (nexus-f9bgu.20): what the upgrade finish pass says and does when
the Windows stop cannot reach the service, and how it hands the restart on.

The refusal arrives from ``terminate_pids`` as entries in ``refused_out`` (the
platform is faked there), so these run on every host.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus import upgrade_finish as uf
from nexus.daemon.replace_guard import ReplaceBlockedError
from nexus.daemon.service_registry import GracefulStopSend
from nexus.engine_version import REQUIRED_ENGINE_VERSION


def _refusal(pid: int, target: int = 3, own: int = 1) -> GracefulStopSend:
    return GracefulStopSend(
        pid=pid, sent=False, refused=True, error=5, target_session=target, own_session=own,
    )


def _stack(cfg: Path) -> list[tuple[int, str]]:
    return [
        (196, f"nx daemon service start --foreground --config-dir {cfg}"),
        (214, f"{cfg}/service/nexus-service -Xmx1g"),
    ]


def _refusing_terminate(refused_pid: int):
    def terminate(pids, *, refused_out=None, **_kw):
        survivors = []
        for pid in pids:
            if pid == refused_pid and refused_out is not None:
                refused_out.append(_refusal(pid))
            survivors.append(pid) if pid == refused_pid else None
        return survivors
    return terminate


def test_sweep_names_the_session_of_a_refused_survivor(tmp_path):
    cfg = tmp_path / "nexus"
    before = _stack(cfg)
    with patch.object(uf, "_pid_alive", return_value=True), \
         patch.object(uf, "process_state", return_value="S"), \
         patch.object(uf, "process_command", side_effect=lambda pid: dict(before)[pid]), \
         patch.object(uf, "terminate_pids", _refusing_terminate(196)):
        note, refused = uf._sweep_surviving_stack_detail(cfg, before)
    assert [r.pid for r in refused] == [196]  # non-vacuity: a refusal was seen
    assert "REFUSED" in note
    assert "Windows session 3" in note and "session 1" in note
    assert "Run this upgrade from session 3" in note
    assert "Nothing was signalled or killed" in note
    assert "SIGKILL escalation" not in note  # the escalation never ran for it


def test_sweep_wrapper_returns_the_same_note(tmp_path):
    cfg = tmp_path / "nexus"
    before = _stack(cfg)
    with patch.object(uf, "_pid_alive", return_value=True), \
         patch.object(uf, "process_state", return_value="S"), \
         patch.object(uf, "process_command", side_effect=lambda pid: dict(before)[pid]), \
         patch.object(uf, "terminate_pids", _refusing_terminate(214)):
        assert "REFUSED" in uf._sweep_surviving_stack(cfg, before)


def test_restart_stops_at_a_refusal_instead_of_starting_onto_the_old_engine(tmp_path):
    cfg = tmp_path / "nexus"
    before = _stack(cfg)
    stop = MagicMock(returncode=1, stdout="", stderr="REFUSED")
    with patch.object(uf, "service_stack_pids", return_value=before), \
         patch.object(uf, "_pid_alive", return_value=True), \
         patch.object(uf, "process_state", return_value="S"), \
         patch.object(uf, "process_command", side_effect=lambda pid: dict(before)[pid]), \
         patch.object(uf, "terminate_pids", _refusing_terminate(196)), \
         patch.object(uf, "run_bounded", side_effect=[stop]) as run, \
         patch.object(uf, "_running_engine") as running:
        actions = uf._restart_and_verify(cfg, [], "9.9.9")
    assert run.call_count == 1  # the stop only: no start
    running.assert_not_called()
    assert len(actions) == 1 and actions[0].startswith("NEEDS HUMAN: [stop-sweep] REFUSED")
    assert "Run this upgrade from session 3" in actions[0]


def _converge(tmp_path: Path, install: MagicMock) -> list[str]:
    status = uf.EngineConvergence(
        applicable=True, installed_version=(0, 0, 1),
        required_version=REQUIRED_ENGINE_VERSION, converged=False,
    )
    with patch.object(uf, "detect_engine_convergence", return_value=status), \
         patch.object(uf, "_poison_probe", return_value=uf.PoisonProbe()), \
         patch.object(uf, "_reassign_diag_view_before_restart", return_value=[]), \
         patch.object(uf, "_restart_and_verify", side_effect=lambda cd, acts, req: acts), \
         patch("nexus.daemon.binary_install.PINNED_SERVICE_TAG", "engine-service-v0.1.9"), \
         patch("nexus.daemon.binary_install.install_binary", install):
        return uf.converge_engine(tmp_path)


def test_converge_hands_the_restart_to_its_own_verified_cycle(tmp_path):
    install = MagicMock(return_value=(tmp_path / "x", {}))
    actions = _converge(tmp_path, install)
    install.assert_called_once()
    assert install.call_args.kwargs["restart_after"] is False
    assert any("converged engine" in a for a in actions)


def test_a_blocked_replacement_surfaces_its_remedy_in_the_needs_human_line(tmp_path):
    install = MagicMock(side_effect=ReplaceBlockedError(
        "cannot replace the engine: the storage service (pid 9) runs in Windows "
        "session 3; this shell is in session 1. Run this upgrade from session 3."))
    actions = _converge(tmp_path, install)
    assert len(actions) == 1
    assert actions[0].startswith("NEEDS HUMAN: engine convergence failed installing")
    assert "Run this upgrade from session 3" in actions[0]
