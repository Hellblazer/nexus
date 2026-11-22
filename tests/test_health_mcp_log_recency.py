# SPDX-License-Identifier: AGPL-3.0-or-later
"""mcp.claude_code_connection reads the newest nexus log Claude Code wrote, and says so when there is none recent.

2026-11-22: the row reported a 2026-10-01 failure ("ENOENT, executable not found: stdio") while the MCP server
worked. Claude Code keeps one log directory per project cwd, the doctor looked only at the directories of its own
cwd and the cwd's parents, and run from a worktree that reached only ``-`` (the log directory of ``/``), which
holds the stale October file. The row now falls back to every project directory under the cache root, and a log
older than ``_CLAUDE_CODE_LOG_MAX_AGE_SECONDS`` is not judged: it is informational ("no recent connection log").
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import nexus.health as h

_DIR = "mcp-logs-plugin-conexus-nexus"
_CONNECTED = "Successfully connected (transport: stdio) in 900ms"
_REGISTERED = "Channel notifications registered"
_FAILED = "Connection failed after 120ms: MCP error -32000: ENOENT, executable not found: \"stdio\""
_DAY = 24 * 3600


@pytest.fixture(autouse=True)
def _no_ambient_claude_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _log(root: Path, project: str, name: str, *lines: str, age_days: float = 0.0) -> Path:
    d = root / project / _DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("".join(json.dumps({"debug": line}) + "\n" for line in lines), encoding="utf-8")
    when = time.time() - age_days * _DAY
    os.utime(f, (when, when))
    return f


def _row(root: Path, cwd: str = "/w/p"):
    (r,) = h._check_claude_code_mcp_connection(cache_root=root, cwd=Path(cwd))
    return r


def test_a_stale_only_tree_is_informational_not_a_failure(tmp_path: Path) -> None:
    _log(tmp_path, "-", "2026-10-01T12-24-44-248Z.jsonl", _FAILED, age_days=52)
    r = _row(tmp_path)
    assert r.ok is True and r.warn is False and not r.fatal
    assert "no recent connection log" in r.detail and "2026-10-01T12-24-44-248Z.jsonl" in r.detail
    assert "ENOENT" not in r.detail, "a stale failure is not judged"


def test_a_recent_failure_still_fails(tmp_path: Path) -> None:
    _log(tmp_path, "-w-p", "2026-11-22T02-20-44-453Z.jsonl", _CONNECTED, _FAILED, age_days=0.1)
    r = _row(tmp_path)
    assert r.ok is False and r.warn is False and "Connection failed" in r.detail


def test_a_recent_success_passes(tmp_path: Path) -> None:
    _log(tmp_path, "-w-p", "2026-11-22T02-20-44-453Z.jsonl", _CONNECTED, _REGISTERED, age_days=0.1)
    r = _row(tmp_path)
    assert r.ok is True and "registered" in r.detail


def test_the_stale_log_of_a_parent_directory_does_not_hide_a_recent_one_elsewhere(tmp_path: Path) -> None:
    """The 2026-11-22 shape: the cwd's ancestors (here ``-``, the log directory of ``/``) hold an old failure,
    and the live session's log sits under another project directory the cwd derivation never reaches."""
    _log(tmp_path, "-", "2026-10-01T12-24-44-248Z.jsonl", _FAILED, age_days=52)
    _log(tmp_path, "-Users-x-git-nexus", "2026-11-22T02-20-44-453Z.jsonl", _CONNECTED, _REGISTERED, age_days=0.1)
    r = _row(tmp_path, cwd="/Users/x/git/nexus/.claude/worktrees/agent-a")
    assert r.ok is True and "registered" in r.detail and "2026-11-22" in r.detail


def test_a_recent_failure_elsewhere_is_found_when_the_cwd_has_only_stale_logs(tmp_path: Path) -> None:
    _log(tmp_path, "-", "2026-10-01T12-24-44-248Z.jsonl", _CONNECTED, _REGISTERED, age_days=52)
    _log(tmp_path, "-Users-x-other", "2026-11-22T02-20-44-453Z.jsonl", _CONNECTED, _FAILED, age_days=0.1)
    assert _row(tmp_path, cwd="/w/p").ok is False


def test_the_bound_is_seven_days_and_a_long_session_appending_to_an_old_file_counts(tmp_path: Path) -> None:
    """Recency is the file's mtime: a reconnect appends to the SAME file, whose name stays the session's start."""
    assert h._CLAUDE_CODE_LOG_MAX_AGE_SECONDS == 7 * _DAY
    _log(tmp_path, "-w-p", "2026-11-01T00-00-00-000Z.jsonl", _CONNECTED, _FAILED, age_days=6.5)
    assert _row(tmp_path).ok is False
    other = tmp_path / "other"
    _log(other, "-w-p", "2026-11-01T00-00-00-000Z.jsonl", _CONNECTED, _FAILED, age_days=7.5)
    r = _row(other)
    assert r.ok is True and "no recent connection log" in r.detail


def test_the_clock_is_a_seam(tmp_path: Path) -> None:
    f = _log(tmp_path, "-w-p", "2026-11-22T02-20-44-453Z.jsonl", _CONNECTED, _FAILED)
    later = f.stat().st_mtime + 8 * _DAY
    (r,) = h._check_claude_code_mcp_connection(cache_root=tmp_path, cwd=Path("/w/p"), now=later)
    assert r.ok is True and "no recent connection log" in r.detail
