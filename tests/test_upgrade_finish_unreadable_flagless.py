# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qwn3p: an unreadable flagless ``daemon service start`` candidate is
carried out of ``upgrade_finish.service_stack_pids`` instead of being dropped.

``stop_storage_service`` already reports an unverified stop when a flagless
candidate's environment cannot be read (nexus-oacd3). ``service_stack_pids``
used to log ``stack_member_flagless_owner_unreadable`` and omit the pid, so
``restart-stale`` (``_restart_and_verify``), the surviving-stack sweep and the
changelog-lock liveness scan all treated the stack as fully listed. These tests
inject the environment reader, so no real process is read.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog

from nexus import upgrade_finish as uf

_FLAGLESS = "/v/bin/python3 /v/bin/nx daemon service start"


@pytest.fixture
def default_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The default config dir of a sandbox HOME: the one dir a bare flagless
    start is offered to by the stack matcher on every code version."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path / "home" / ".config" / "nexus"


def _reader(table: dict[int, dict[str, str] | None]):
    """An ``environment_of`` that answers from *table* (``None`` = unreadable)."""
    return lambda pid, _names: table.get(pid)


def _rows(cfg: Path) -> list[tuple[int, int, str]]:
    return [
        (196, 60, f"/v/bin/python3 /v/bin/nx daemon service start --foreground --config-dir {cfg}"),
        (214, 60, f"{cfg}/service/nexus-service -Xmx1g"),
        (310, 60, _FLAGLESS),  # serves this dir
        (311, 60, _FLAGLESS),  # serves another dir
        (312, 60, _FLAGLESS),  # environment cannot be read
    ]


class TestServiceStackPidsCarriesUnreadableOut:
    def test_an_unreadable_flagless_candidate_is_reported_not_dropped(
        self, tmp_path: Path, default_cfg: Path,
    ) -> None:
        cfg = default_cfg
        reader = _reader({
            310: {"NEXUS_CONFIG_DIR": str(cfg)},
            311: {"NEXUS_CONFIG_DIR": str(tmp_path / "elsewhere")},
            312: None,
        })
        unreadable: list[int] = []
        with patch.object(uf, "all_process_rows", return_value=_rows(cfg)):
            found = uf.service_stack_pids(
                cfg, unreadable_out=unreadable, environment_of=reader,
            )
        assert sorted(pid for pid, _ in found) == [196, 214, 310]
        assert unreadable == [312]

    def test_without_the_out_list_the_return_is_unchanged(self, default_cfg: Path) -> None:
        cfg = default_cfg
        reader = _reader({310: {"NEXUS_CONFIG_DIR": str(cfg)}, 311: None, 312: None})
        with patch.object(uf, "all_process_rows", return_value=_rows(cfg)):
            found = uf.service_stack_pids(cfg, environment_of=reader)
        assert sorted(pid for pid, _ in found) == [196, 214, 310]


class TestSweepNamesAnUnverifiedCandidate:
    def test_a_live_unreadable_pid_makes_the_note_say_unverified(self, tmp_path: Path) -> None:
        cfg = tmp_path / "nexus"
        with patch.object(uf, "_pid_alive", return_value=True), \
                patch.object(uf, "process_state", return_value="S"), \
                patch.object(uf, "process_command", return_value=_FLAGLESS):
            note, refused = uf._sweep_surviving_stack_detail(cfg, [], unreadable=[312])
        assert refused == []
        assert "unverified" in note.lower() and "312" in note, note

    def test_an_unreadable_pid_that_exited_is_not_narrated(self, tmp_path: Path) -> None:
        cfg = tmp_path / "nexus"
        with patch.object(uf, "_pid_alive", return_value=False):
            note, _ = uf._sweep_surviving_stack_detail(cfg, [], unreadable=[312])
        assert note == ""

    def test_a_recycled_unreadable_pid_is_not_narrated(self, tmp_path: Path) -> None:
        cfg = tmp_path / "nexus"
        with patch.object(uf, "_pid_alive", return_value=True), \
                patch.object(uf, "process_state", return_value="S"), \
                patch.object(uf, "process_command", return_value="vim /etc/hosts"):
            note, _ = uf._sweep_surviving_stack_detail(cfg, [], unreadable=[312])
        assert note == ""

    def test_the_note_rides_beside_a_survivor_note(self, tmp_path: Path) -> None:
        cfg = tmp_path / "nexus"
        sup = f"nx daemon service start --foreground --config-dir {cfg}"
        current = {196: sup, 312: _FLAGLESS}
        with patch.object(uf, "_pid_alive", return_value=True), \
                patch.object(uf, "process_state", return_value="S"), \
                patch.object(uf, "process_command", side_effect=lambda pid: current[pid]), \
                patch.object(uf, "terminate_pids", return_value=[]):
            note, _ = uf._sweep_surviving_stack_detail(cfg, [(196, sup)], unreadable=[312])
        assert "196" in note and "stop-sweep" in note
        assert "unverified" in note.lower() and "312" in note, note


class TestRestartAndVerifyReportsTheUnverifiedCandidate:
    def test_restart_stale_action_line_names_the_unreadable_pid(self, tmp_path: Path) -> None:
        """Wiring: ``_restart_and_verify`` hands ``service_stack_pids`` an out
        list and folds what comes back into the reported action."""

        def fake_stack(config_dir, *, unreadable_out=None, **_kw):
            if unreadable_out is not None:
                unreadable_out.append(312)
            return []

        ok = MagicMock(returncode=0, stdout="", stderr="")
        running = MagicMock(version=uf.REQUIRED_ENGINE_VERSION, pid=214)
        with patch.object(uf, "service_stack_pids", side_effect=fake_stack), \
                patch.object(uf, "_pid_alive", return_value=True), \
                patch.object(uf, "process_state", return_value="S"), \
                patch.object(uf, "process_command", return_value=_FLAGLESS), \
                patch.object(uf, "run_bounded", side_effect=[ok, ok]), \
                patch.object(uf, "_running_engine", return_value=running):
            actions: list[str] = []
            uf._restart_and_verify(tmp_path, actions, "x")
        joined = " ".join(actions)
        assert "unverified" in joined.lower() and "312" in joined, actions


class TestLivenessScanReportsTheUnverifiedCandidate:
    def test_default_engine_liveness_scan_logs_unreadable_pids(self, tmp_path: Path) -> None:
        from nexus.daemon.storage_service_daemon import _default_engine_liveness_scan  # noqa: PLC0415 — file pattern: deferred imports

        cfg = tmp_path / "nexus"
        binary = cfg / "service" / "nexus-service"

        def fake_stack(config_dir, *, unreadable_out=None, **_kw):
            if unreadable_out is not None:
                unreadable_out.append(312)
            return [(214, f"{binary} -Xmx1g"), (196, "nx daemon service start --foreground")]

        with structlog.testing.capture_logs() as logs, \
                patch("nexus.upgrade_finish.service_stack_pids", side_effect=fake_stack):
            alive = _default_engine_liveness_scan(cfg, binary)
        assert alive == [(214, f"{binary} -Xmx1g")]
        events = [e for e in logs if e.get("event") == "engine_liveness_scan_unverified"]
        assert events and events[0]["pids"] == [312], logs
