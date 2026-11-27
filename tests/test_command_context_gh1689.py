# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx command-context`` preamble blocks for a ``bd`` with no database (GH #1689).

Split out of ``tests/test_command_context_command.py``, which is at its file-size ceiling. The runner-faked tests replace
only ``run_bounded``; the child-process ``bd`` tests use real I/O. ``nx doctor``'s tests are ``tests/test_preflight_doctor.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# GH #1689: a bd with no database
# ---------------------------------------------------------------------------


def _fake_bounded(monkeypatch, cc, *, returncode: int = 0, stdout: str = "", stderr: str = "", raises: Exception | None = None):
    """Replace the preamble's ``run_bounded``; returns the list of ``(argv, kwargs)`` it was called with."""
    import types

    calls: list[tuple[list[str], dict]] = []

    def fake(argv, **kw):  # noqa: ANN001, ANN003
        calls.append((list(argv), kw))
        if raises is not None:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(cc, "run_bounded", fake)
    return calls


def test_the_other_preamble_calls_keep_the_short_bound(monkeypatch, tmp_path: Path) -> None:
    from nexus.commands import command_context as cc

    calls = _fake_bounded(monkeypatch, cc, stdout="x\n")
    cc.beads_block(tmp_path, args=[], heading="### Beads")
    assert [kw["timeout"] for _, kw in calls] == [cc._PREAMBLE_TIMEOUT]


_NO_DB_STDERR = "Error: no beads database found\nHint: run 'bd init' to create a database in the current directory\n"


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=["--status=in_progress"], heading="### Beads"),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_bd_with_no_database_is_reported_as_such_not_as_unavailable(monkeypatch, tmp_path: Path, call) -> None:
    """bd is installed and says so on stderr (exit 1); 'unavailable or no results' was false (GH #1689)."""
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=1, stderr=_NO_DB_STDERR)
    text = "\n".join(call(cc, tmp_path))
    assert "no beads database" in text
    assert "unavailable" not in text and "no results" not in text


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=[], heading=None),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_bd_that_fails_for_another_reason_says_what_it_said(monkeypatch, tmp_path: Path, call) -> None:
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=2, stderr="Error: dolt server unreachable\n")
    text = "\n".join(call(cc, tmp_path))
    assert "exit 2" in text and "dolt server unreachable" in text
    assert "no beads database" not in text


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=[], heading=None),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_missing_or_slow_bd_is_named_for_what_it_is(monkeypatch, tmp_path: Path, call) -> None:
    import subprocess

    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, raises=FileNotFoundError("bd"))
    assert "not installed" in "\n".join(call(cc, tmp_path))
    _fake_bounded(monkeypatch, cc, raises=subprocess.TimeoutExpired(["bd"], cc._PREAMBLE_TIMEOUT))
    assert "timed out" in "\n".join(call(cc, tmp_path))


def test_bd_with_a_database_and_no_rows_still_says_none(monkeypatch, tmp_path: Path) -> None:
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=0, stdout="\n")
    assert cc.beads_block(tmp_path, args=[], heading=None) == ["- (no beads found)"]
    assert cc._ready_beads_block(tmp_path) == ["### Ready beads (top 10)", "- (none)"]


# The measured bd v1.0.4 behaviour with no database (reporter's Windows run, GH #1689): exit 1, EMPTY stdout, text on
# stderr only. The block builders used to drop stderr and the exit code, so a detector fed from them never matched; these
# tests run a real child process instead of faking the runner, so a regression that drops stderr again goes red.
_BD_NO_DB_STDERR = (
    "Error: no beads database found\n"
    "Hint: run 'bd where' to inspect the resolved workspace, or 'bd init' to create a new database\n"
    "      or set BEADS_DIR to point to your .beads directory\n"
)


@pytest.fixture
def bd_without_database(tmp_path: Path, monkeypatch) -> Path:
    """A real ``bd`` on PATH that behaves as bd v1.0.4 does where there is no beads database."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bd = bindir / "bd"
    bd.write_text("#!/bin/sh\nprintf '%s' \"$BD_FAKE_STDERR\" >&2\nexit 1\n")
    bd.chmod(0o755)
    monkeypatch.setenv("BD_FAKE_STDERR", _BD_NO_DB_STDERR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    work = tmp_path / "repo"
    work.mkdir()
    return work


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="the fake bd is a shell script; Windows refuses a .cmd shim for bd by design")


@_POSIX_ONLY
def test_real_bd_with_no_database_through_the_preamble_blocks(bd_without_database: Path) -> None:
    from nexus.commands import command_context as cc

    for lines in (cc.beads_block(bd_without_database, args=[], heading=None), cc._ready_beads_block(bd_without_database)):
        text = "\n".join(lines)
        assert "no beads database" in text, text
        assert "unavailable" not in text and "no results" not in text, text


@_POSIX_ONLY
def test_real_bd_with_no_database_through_the_brief_and_the_hook_render(bd_without_database: Path) -> None:
    from nexus import brief
    from nexus.hooks import session_context

    rendered = "\n".join(session_context._render_beads(bd_without_database))
    assert "no beads database" in rendered, rendered
    text = brief.render("beads", cwd=str(bd_without_database)).text
    assert "no beads database" in text and "printed nothing" not in text, text
