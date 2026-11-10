# SPDX-License-Identifier: AGPL-3.0-or-later
"""``first_start`` and ``repo_main_transcripts`` (nexus-qo7ie.27): the first-record rule and the file listing
``nx memory curate`` shares with ``transcripts.sessions``. The agreement with ``sessions()`` on an adversarial
fixture is pinned in ``tests/test_memory_curate_reads.py``; these pin the functions' own contracts."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.lua_runtime import transcripts_read as tr
from nexus.lua_runtime import verbs_transcripts as vt
from nexus.lua_runtime.verb_types import VerbError, VerbTimeout

_FAR = 10**9


def _write(path: Path, *lines: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    return path


def _rec(**kw: object) -> bytes:
    return json.dumps(kw).encode() + b"\n"


def test_the_first_stamped_non_sidechain_record_is_the_start(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.jsonl", b"junk\n", _rec(type="user", isSidechain=True, timestamp="2026-11-01T00:00:00Z"),
               _rec(type="summary"), _rec(type="user", timestamp="2026-11-08T10:00:00"))
    assert vt.first_start(f, time.monotonic() + 30) == (datetime(2026, 11, 8, 10, 0, tzinfo=UTC), False)


def test_a_file_with_no_stamp_is_no_start_and_not_bounded(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.jsonl", _rec(type="summary"))
    assert vt.first_start(f, time.monotonic() + 30) == (None, False)
    assert vt.first_start(tmp_path / "missing.jsonl", time.monotonic() + 30) == (None, False)


def test_the_limit_stops_the_read_and_says_so(tmp_path: Path) -> None:
    pad = [_rec(type="summary", pad="x" * 400)] * 40  # 16 KB of records with no stamp
    f = _write(tmp_path / "a.jsonl", *pad, _rec(type="user", timestamp="2026-11-08T10:00:00Z"))
    assert vt.first_start(f, time.monotonic() + 30, 4096) == (None, True)
    assert vt.first_start(f, time.monotonic() + 30)[0] == datetime(2026, 11, 8, 10, 0, tzinfo=UTC)  # the default bound reaches it


def test_a_passed_deadline_is_a_verb_timeout(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.jsonl", *([_rec(type="summary")] * (vt.LINES_PER_DEADLINE_CHECK + 5)))
    with pytest.raises(VerbTimeout):
        vt.first_start(f, time.monotonic() - 1)


def test_repo_main_transcripts_lists_what_sessions_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "cfg"
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    proj = cfg / "projects" / "-r"
    user = _rec(type="user", cwd=str(repo), timestamp="2026-11-08T10:00:00Z")
    new = _write(proj / "new.jsonl", user)
    old = _write(proj / "old.jsonl", user)
    os.utime(old, (1_000_000, 1_000_000))
    _write(proj / "sid" / "subagents" / "agent-a.jsonl", user)  # a subagent: not a main transcript
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert vt.projects_root() == cfg / "projects"
    got = list(vt.repo_main_transcripts(repo, time.time() - 86400, time.monotonic() + 30))
    assert got == [new]  # the old one is before the floor


def test_repo_main_transcripts_refuses_a_missing_projects_directory_and_a_missing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(VerbError, match="no Claude Code projects directory"):
        list(vt.repo_main_transcripts(tmp_path, 0.0, time.monotonic() + 30))
    (tmp_path / "cfg" / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(VerbError, match="needs a repository"):
        list(vt.repo_main_transcripts(None, 0.0, time.monotonic() + 30))


def test_the_probe_state_is_what_a_session_read_for_start_holds() -> None:
    """``first_start`` stands a ``_Probe`` in for ``_Session(True, fields="start")`` when it asks ``_inert``."""
    real = vt._Session(True, vt._parse_fields("start", False))
    probe = tr._Probe()
    assert (probe.main, probe.inert_kinds, probe.session_id, probe.agent_id) == (
        real.main, real.inert_kinds, real.session_id, real.agent_id)
