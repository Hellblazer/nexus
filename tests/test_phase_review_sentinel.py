# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 P2: phase-review-gate sentinel writer + sweep.

Coupled with the phase_review_close_requires_gate routing hook (nexus-mzvwa.4).
The writer ships in this bead (mzvwa.2); the reader ships in mzvwa.4
on the same PR.
"""
from __future__ import annotations

import json
import os

import pytest

from nexus.phase_review_sentinel import (
    read_sentinel,
    sentinel_dir,
    sentinel_path,
    sweep_dead_sentinels,
    write_sentinel,
)


@pytest.fixture
def tmp_sentinel_dir(tmp_path, monkeypatch):
    """Redirect TMPDIR so sentinel writes land in tmp_path."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path / "nx-phase-gate-sentinel"


def test_sentinel_dir_honors_tmpdir(tmp_sentinel_dir):
    assert sentinel_dir() == tmp_sentinel_dir


def test_sentinel_path_format(tmp_sentinel_dir):
    path = sentinel_path(12345, "112", "1")
    assert path.name == "12345-112-1.json"
    assert path.parent == tmp_sentinel_dir


def test_write_sentinel_creates_file_with_correct_shape(tmp_sentinel_dir):
    path = write_sentinel("112", "1", claude_pid=99999)
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["outcome"] == "PASSED"
    assert payload["rdr_id"] == "112"
    assert payload["phase"] == "1"
    assert payload["claude_pid"] == 99999
    assert "timestamp" in payload


def test_write_sentinel_creates_directory(tmp_sentinel_dir):
    assert not tmp_sentinel_dir.exists()
    write_sentinel("121", "1", claude_pid=11111)
    assert tmp_sentinel_dir.is_dir()


def test_read_sentinel_round_trip(tmp_sentinel_dir):
    write_sentinel("112", "2", claude_pid=22222)
    data = read_sentinel(22222, "112", "2")
    assert data is not None
    assert data["outcome"] == "PASSED"
    assert data["rdr_id"] == "112"


def test_read_sentinel_returns_none_when_absent(tmp_sentinel_dir):
    assert read_sentinel(33333, "999", "9") is None


def test_read_sentinel_returns_none_on_malformed(tmp_sentinel_dir):
    tmp_sentinel_dir.mkdir(parents=True)
    bad = sentinel_path(44444, "999", "9")
    bad.write_text("not json{{{")
    assert read_sentinel(44444, "999", "9") is None


def test_sweep_removes_dead_pid_sentinels(tmp_sentinel_dir):
    """Sentinel keyed on a definitely-dead PID is reaped; alive is kept."""
    dead_pid = 999_999_999
    tmp_sentinel_dir.mkdir(parents=True)
    # Manually plant a dead-pid sentinel + an alive-pid sentinel, bypassing
    # write_sentinel so its internal sweep does not pre-empt this test.
    dead_path = sentinel_path(dead_pid, "112", "1")
    dead_path.write_text(json.dumps({"outcome": "PASSED", "claude_pid": dead_pid}))
    alive_path = sentinel_path(os.getpid(), "112", "2")
    alive_path.write_text(json.dumps({"outcome": "PASSED", "claude_pid": os.getpid()}))

    deleted = sweep_dead_sentinels()
    assert deleted == 1
    assert not dead_path.exists()
    assert alive_path.exists()


def test_sweep_with_no_dir_returns_zero(tmp_sentinel_dir):
    assert not tmp_sentinel_dir.exists()
    assert sweep_dead_sentinels() == 0


def test_sweep_runs_on_write(tmp_sentinel_dir):
    """write_sentinel sweeps as a side effect."""
    dead_pid = 999_999_999
    # Manually create a sentinel keyed on a dead PID without going through
    # write_sentinel (so we know the first call has stale state to sweep).
    tmp_sentinel_dir.mkdir(parents=True)
    stale = sentinel_path(dead_pid, "old", "1")
    stale.write_text(json.dumps({"outcome": "PASSED"}))

    write_sentinel("new", "1", claude_pid=os.getpid())

    assert not stale.exists(), "sweep should have removed dead-pid sentinel"


def test_sweep_ignores_non_json_files(tmp_sentinel_dir):
    """Files in sentinel dir that are not .json are left alone."""
    tmp_sentinel_dir.mkdir(parents=True)
    intruder = tmp_sentinel_dir / "README.txt"
    intruder.write_text("not a sentinel")
    sweep_dead_sentinels()
    assert intruder.exists()


def test_sweep_ignores_malformed_filenames(tmp_sentinel_dir):
    """File whose stem does not start with an integer pid is left alone."""
    tmp_sentinel_dir.mkdir(parents=True)
    weird = tmp_sentinel_dir / "notapid-foo-1.json"
    weird.write_text("{}")
    sweep_dead_sentinels()
    assert weird.exists()
