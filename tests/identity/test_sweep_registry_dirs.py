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
    def __init__(self, views: list[dict[str, Any]], forbid: bool = False) -> None:
        self.views, self.forbid, self.gone_calls = views, forbid, []

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        return self.views

    def gone(self, identity: str, generation: int, holder: dict) -> dict:
        self.gone_calls.append(identity)
        if self.forbid:
            raise IdentityApiError(403, "forbidden", "a data credential may not call GONE", {})
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
