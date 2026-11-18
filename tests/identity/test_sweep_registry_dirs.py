# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sweep judges a holder gone only against the registry directory its own token file names, and ends quietly on a
credential that may not report gone (RDR-253 review round). Tenant-free: a recording stand-in for the engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from tests.identity.fakes import HOST, FakeProcesses

T0 = "2026-11-17T08:00:00Z"


class _Engine:
    def __init__(self, views: list[dict[str, Any]], forbid: bool = False, fail: tuple[str, ...] = ()) -> None:
        self.views, self.forbid, self.fail, self.gone_calls = views, forbid, fail, []

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        return self.views

    def gone(self, identity: str, generation: int, holder: dict) -> dict:
        self.gone_calls.append(identity)
        if self.forbid:
            raise IdentityApiError(403, "forbidden", "a data credential may not call GONE", {})
        if identity in self.fail:
            raise IdentityApiError(500, "internal", "the engine broke on this row", {})
        return {"recorded": True}


def _view(identity: str, pid: int) -> dict:
    return {"id": identity, "kind": "seat", "state": "live", "generation": 1,
            "holder": {"host": HOST, "pid": pid, "started_at": pid * 1000, "proc_start": T0}}


def _token(directory: Path, pid: int, registry_dir: str | None) -> None:
    write_token_file(token_path(directory, pid, pid * 1000), TokenFile(
        identity=f"i{pid}", generation=1, token=None, expires_at=None, session_id="s", pid=pid, started_at=pid * 1000,
        proc_start=T0, host=HOST, registry_dir=registry_dir))


def test_a_holder_of_another_claude_config_dir_is_not_reported_gone_and_its_file_stays(tmp_path: Path) -> None:
    """The hook of a session under CLAUDE_CONFIG_DIR=A cannot see the registry files of a session under B: every live
    binding of B would look gone to it. Its own (A) crashed holder is still reported."""
    procs = FakeProcesses(tmp_path / "A-sessions")
    directory = tmp_path / "identity"
    _token(directory, 100, str(procs.sessions_dir))                        # bound under A, crashed
    _token(directory, 200, str(tmp_path / "B-sessions"))                   # bound under B, alive there
    _token(directory, 300, None)                                           # bound before the registry dir was recorded
    procs.procs[200] = T0                                                  # running, but its file is in B
    procs.procs[300] = T0
    engine = _Engine([_view("i100", 100), _view("i200", 200), _view("i300", 300)])
    report = sweep_host(engine, procs, host=HOST, own=(999, 999_000), identity_dir=directory)
    assert engine.gone_calls == ["i100"] and report.recorded_gone == ["i100"] and report.unjudged_other_registry == 2
    assert not token_path(directory, 100, 100_000).exists()
    assert token_path(directory, 200, 200_000).exists() and token_path(directory, 300, 300_000).exists()


def test_a_credential_that_may_not_report_gone_ends_the_sweep_quietly(tmp_path: Path) -> None:
    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    _token(directory, 100, str(procs.sessions_dir))
    _token(directory, 101, str(procs.sessions_dir))
    engine = _Engine([_view("i100", 100), _view("i101", 101)], forbid=True)
    report = sweep_host(engine, procs, host=HOST, own=None, identity_dir=directory)
    assert report.forbidden and engine.gone_calls == ["i100"] and report.recorded_gone == []
    assert token_path(directory, 100, 100_000).exists(), "nothing was recorded, so nothing is deleted"


def test_a_token_file_is_deleted_only_after_this_runs_sweep_dealt_with_its_holder(tmp_path: Path) -> None:
    """clean_local_files deletes a gone process's token file only when the sweep recorded the gone with the engine, or the
    engine no longer lists that process as the holder of the file's identity. No sweep, a sweep the credential could not
    finish, or a holder the engine still lists and the sweep did not record: the file stays."""
    from nexus.identity.sweep import SweepReport, clean_local_files

    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    for pid in (100, 101, 102):
        _token(directory, pid, str(procs.sessions_dir))                    # three crashed holders (no process, no file)
    assert clean_local_files(directory, procs, host=HOST).token_files_removed == []          # no sweep ran
    forbidden = SweepReport(swept=True, forbidden=True)
    assert clean_local_files(directory, procs, host=HOST, report=forbidden).token_files_removed == []
    # i100: the sweep recorded it; i101: still listed, not recorded (a repeat verdict); i102: not listed (taken over)
    report = SweepReport(swept=True, recorded_gone=["i100"], holders={"i100": (100, 100_000), "i101": (101, 101_000)})
    removed = clean_local_files(directory, procs, host=HOST, report=report).token_files_removed
    assert sorted(removed) == ["100.100000.token", "102.102000.token"]
    assert token_path(directory, 101, 101_000).exists()


def test_one_row_the_engine_refuses_does_not_stop_the_rest_and_keeps_its_token_file(tmp_path: Path) -> None:
    """RDR-253 Test Plan: one failing row does not stop the sweep. Three crashed holders; the engine fails the first."""
    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    for pid in (100, 200, 300):
        _token(directory, pid, str(procs.sessions_dir))                    # all three crashed under this config dir
    engine = _Engine([_view("i100", 100), _view("i200", 200), _view("i300", 300)], fail=("i100",))
    report = sweep_host(engine, procs, host=HOST, own=None, identity_dir=directory)
    assert engine.gone_calls == ["i100", "i200", "i300"], "the sweep went on past the failing row"
    assert report.failed == ["i100"] and report.recorded_gone == ["i200", "i300"]
    assert token_path(directory, 100, 100_000).exists(), "the failed row's holder is not recorded gone, so its file stays"
    assert not token_path(directory, 200, 200_000).exists() and not token_path(directory, 300, 300_000).exists()
