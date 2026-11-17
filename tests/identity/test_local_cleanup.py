# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hook's local cleanup (RDR-253 "Retention"): token files whose process the holder check finds gone, and pending
files whose process is not running or whose start time differs. Tenant-free."""
from __future__ import annotations

from pathlib import Path

from nexus.identity.holder import epoch_seconds
from nexus.identity.pending import Pending, pending_path, write_pending
from nexus.identity.sweep import SweepReport, clean_local_files
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from tests.identity.fakes import HOST, FakeProcesses

T0 = "2026-11-17T08:00:00Z"
T1 = "2026-11-17T09:00:00Z"


def _token(directory: Path, pid: int, started_at: int, identity: str, registry_dir: str | None = None) -> None:
    write_token_file(token_path(directory, pid, started_at),
                     TokenFile(identity=identity, generation=1, token=None, expires_at=None, session_id="s", pid=pid,
                               started_at=started_at, proc_start=T0, host=HOST, registry_dir=registry_dir))


def test_local_cleanup_removes_token_files_of_gone_processes_and_stale_pending_files(tmp_path: Path) -> None:
    """A restarted pid and a dead pid lose their token files; a running process keeps its own; a pending file whose
    process is not running goes, and the running process's stays."""
    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    procs.start(100, T0, started_at=100_000)
    procs.start(200, T1, started_at=200_000)
    _token(directory, 100, 100_000, "i1", str(procs.sessions_dir))               # alive: stays
    _token(directory, 200, 111_000, "i2", str(procs.sessions_dir))               # pid 200 restarted in place (startedAt differs): gone
    _token(directory, 300, 300_000, "i3", str(procs.sessions_dir))               # no process, no registry file: gone
    live = write_pending(directory, Pending(session_id="s", source="startup", seat=None, os_start=T0, claude_pid=100),
                         epoch_seconds(T0))
    stale = write_pending(directory, Pending(session_id="s", source="startup", seat=None, os_start=T1, claude_pid=300),
                          epoch_seconds(T1))
    report = clean_local_files(directory, procs, host=HOST, report=SweepReport(swept=True))
    assert sorted(report.token_files_removed) == ["200.111000.token", "300.300000.token"]
    assert report.pending_files_removed == [stale.name]
    assert token_path(directory, 100, 100_000).exists() and live.exists() and not stale.exists()
    assert pending_path(directory, 100, epoch_seconds(T0)) == live


def test_a_token_file_the_hook_just_wrote_is_never_removed(tmp_path: Path) -> None:
    """The caller names its own fresh file; even if the check could not prove its process yet, it stays."""
    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    _token(directory, 400, 400_000, "i9")                # no registry file for 400 yet
    own = token_path(directory, 400, 400_000)
    clean_local_files(directory, procs, host=HOST, own_token=own)
    assert own.exists()


def test_cleanup_leaves_the_files_of_a_claude_under_another_config_dir_alone(tmp_path: Path) -> None:
    """The token file names the registry directory its process used; one that is not this reader's is not judged, however
    absent its registry file looks from here. Stale temp files a crashed writer left are removed, a fresh one is not."""
    import os
    import time

    procs = FakeProcesses(tmp_path / "sessions")
    directory = tmp_path / "identity"
    _token(directory, 600, 600_000, "i6", str(tmp_path / "other" / "sessions"))      # another profile, no file here
    _token(directory, 601, 601_000, "i7")                                              # no registry_dir recorded at all
    procs.procs[600] = T0                                                              # both running
    procs.procs[601] = T0
    old = directory / "700.1.token.123.abc.tmp"
    new = directory / "700.1.token.123.def.tmp"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (time.time() - 3_600, time.time() - 3_600))
    report = clean_local_files(directory, procs, host=HOST, report=SweepReport(swept=True))
    assert report.token_files_removed == [] and token_path(directory, 600, 600_000).exists() and token_path(directory, 601, 601_000).exists()
    assert report.tmp_files_removed == [old.name] and new.exists() and not old.exists()
