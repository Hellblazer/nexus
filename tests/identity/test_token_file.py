# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-process token file (RDR-253 "Handover to the MCP server"): mode 0600, one file per process start, one
file rewritten by a refresh. Tenant-free."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nexus.identity.token_file import (
    TokenFile, ancestor_token_path, delete_token_file, parse_token_name, read_token_file, token_files, token_path,
    write_token_file,
)
from tests.identity.fakes import FakeProcesses


def _record(**kw) -> TokenFile:
    base = dict(identity="i1", generation=1, token="tok-a", expires_at=2_000_000_000.0, session_id="sid", pid=4242,
                started_at=4_242_000, proc_start="2026-11-17T08:00:00Z", host="h")
    base.update(kw)
    return TokenFile(**base)


def test_the_file_is_written_mode_0600_and_reads_back(tmp_path: Path) -> None:
    path = token_path(tmp_path / "identity", 4242, 4_242_000)
    write_token_file(path, _record())
    if os.name == "posix":                      # Windows has no POSIX mode bits; _winsec guards the directory there
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_token_file(path) == TokenFile(**{**_record().__dict__, "written_at": read_token_file(path).written_at})
    assert path.name == "4242.4242000.token" and parse_token_name(path) == (4242, 4_242_000)


def test_two_processes_resuming_one_session_id_use_separate_files(tmp_path: Path) -> None:
    """Same session id, two process starts (a resume in a new process, or an in-place /restart): the pid or the
    startedAt differs, so the files differ and the superseded process never reads the new holder's token."""
    d = tmp_path / "identity"
    old = token_path(d, 4242, 4_242_000)
    restarted = token_path(d, 4242, 4_999_000)   # /restart: same pid, new startedAt
    other = token_path(d, 5151, 5_151_000)       # a resume in another process
    for path, tok in ((old, "old"), (restarted, "restarted"), (other, "other")):
        write_token_file(path, _record(token=tok, session_id="shared-sid", pid=parse_token_name(path)[0],
                                       started_at=parse_token_name(path)[1]))
    assert len({old, restarted, other}) == 3
    assert [read_token_file(p).token for p in (old, restarted, other)] == ["old", "restarted", "other"]
    assert token_files(d) == sorted([old, restarted, other])


def test_a_refresh_rewrites_the_one_file(tmp_path: Path) -> None:
    path = token_path(tmp_path / "identity", 4242, 4_242_000)
    write_token_file(path, _record(token="first", expires_at=100.0))
    write_token_file(path, _record(token="second", expires_at=200.0))
    assert [p.name for p in token_files(tmp_path / "identity")] == ["4242.4242000.token"]
    got = read_token_file(path)
    assert (got.token, got.expires_at) == ("second", 200.0)
    assert not [p for p in (tmp_path / "identity").iterdir() if p.name.endswith(".tmp")]


def test_a_missing_or_malformed_file_reads_as_none_and_delete_is_idempotent(tmp_path: Path) -> None:
    path = token_path(tmp_path, 1, 2)
    assert read_token_file(path) is None
    path.write_text("{not json")
    assert read_token_file(path) is None
    path.write_text('{"identity": "i1"}')  # no generation, pid or startedAt
    assert read_token_file(path) is None
    delete_token_file(path)
    delete_token_file(path)
    assert not path.exists()


def test_a_file_against_a_step_1_engine_has_no_token_and_still_names_the_identity(tmp_path: Path) -> None:
    path = token_path(tmp_path, 4242, 4_242_000)
    write_token_file(path, _record(token=None, expires_at=None))
    got = read_token_file(path)
    assert (got.identity, got.generation, got.token, got.expires_at) == ("i1", 1, None, None)


def test_the_path_of_a_claude_process_comes_from_its_registry_file(tmp_path: Path) -> None:
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4_242_000)
    assert ancestor_token_path(tmp_path / "identity", 4242, procs.sessions_dir) == tmp_path / "identity" / "4242.4242000.token"
    assert ancestor_token_path(tmp_path / "identity", 9999, procs.sessions_dir) is None


def test_the_token_is_not_in_the_repr_and_a_failed_write_leaves_no_temp_file(tmp_path: Path) -> None:
    assert "tok-a" not in repr(_record()) and "tok-a" not in str(_record())
    d = tmp_path / "identity"
    path = token_path(d, 4242, 4_242_000)
    path.mkdir(parents=True)                       # the final rename cannot replace a directory holding a file
    (path / "x").write_text("x")
    with pytest.raises(OSError):
        write_token_file(path, _record())
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_a_compare_and_swap_sees_the_other_writer_and_can_decline(tmp_path: Path) -> None:
    from nexus.identity.token_file import update_token_file

    path = token_path(tmp_path / "identity", 4242, 4_242_000)
    write_token_file(path, _record(identity="i1"))
    assert update_token_file(path, lambda cur: None).identity == "i1"                       # declined: untouched
    updated = update_token_file(path, lambda cur: TokenFile(**{**cur.__dict__, "identity": "i2"}))
    assert updated.identity == "i2" and read_token_file(path).identity == "i2"
    delete = __import__("nexus.identity.token_file", fromlist=["delete_token_file"]).delete_token_file
    delete(path)
    assert not path.exists() and not path.with_name(path.name + ".lock").exists()
