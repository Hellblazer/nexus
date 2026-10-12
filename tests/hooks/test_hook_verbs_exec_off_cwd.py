# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx-hook`` verbs never spawn a binary the current directory can supply.

RDR-224 Phase 4 critique S1 (nexus-f9bgu.37). On Windows ``CreateProcess`` searches
the current directory before ``PATH``, and Claude Code runs hooks with the project
as cwd, so a bare ``git`` / ``bd`` argv[0] in a verb under ``src/nexus/hooks`` runs
a ``git.exe`` / ``bd.exe`` planted in a cloned repository. The scripts under
``conexus/hooks/`` were closed first (``tests/hooks/test_exec_off_cwd.py``); this is
the same class one layer down, in the verbs those scripts launch.

Windows runs here on any host by injection: ``nexus.util.nx_argv._platform`` reports
``win32``, ``os.getcwd`` / ``os.pathsep`` are faked inside that one module, and its
``shutil.which`` is a stand-in that answers the way CPython on Windows does (the
current directory's hit first, as a relative ``.\\git.exe``). Nothing process-wide
is patched (``tests/test_module_seam.py``). The spawner is ``run_bounded``, replaced
on ``nexus.bounded_subprocess`` (our module) with a recorder.

Each site is checked three ways: the planted copy is never the argv[0] that is
spawned, a copy on PATH is spawned as an absolute path, and a tool that exists
only as a planted copy spawns nothing and degrades exactly as "not found" did.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from nexus import bounded_subprocess
from nexus.hooks import _infer_repo, preflight_verb, rdr_verb, session_context, subagent_start
from nexus.util import nx_argv as nx_argv_mod
from tests._windows_box import CWD, TOOLS, install_windows


@pytest.fixture()
def windows_box(monkeypatch: pytest.MonkeyPatch):
    """Windows semantics for ``nx_argv``; returns ``configure(installed, planted)``."""
    monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "win32")

    def configure(installed: set[str], planted: set[str]) -> None:
        files = {rf"{TOOLS}\{n}.exe" for n in installed} | {rf"{CWD}\{n}.exe" for n in planted}
        # nx_argv's private view of the environment: the real PATH stays the host's,
        # so a verb's own lookups outside nx_argv still work.
        install_windows(monkeypatch, "nexus.util.nx_argv", files)

    configure({"git", "bd"}, {"git", "bd"})
    return configure


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every argv a verb hands ``run_bounded``; the call itself succeeds with some stdout."""
    seen: list[list[str]] = []

    def fake(argv, **_kw):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout=f"{CWD}\\clone\n", stderr="")

    monkeypatch.setattr(bounded_subprocess, "run_bounded", fake)
    return seen


def _heads(spawned: list[list[str]]) -> list[str]:
    return [a[0] for a in spawned]


GIT = rf"{TOOLS}\git.exe"
BD = rf"{TOOLS}\bd.exe"


# -- session_context ---------------------------------------------------------------


def test_session_context_spawns_git_and_bd_off_the_cwd(windows_box, spawned, monkeypatch, tmp_path: Path) -> None:

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd in {"nx", "bd"})
    session_context.run(None)
    assert _heads(spawned) == [GIT, BD], spawned


def test_session_context_with_only_a_planted_git_and_bd_spawns_nothing(
    windows_box, spawned, monkeypatch, tmp_path: Path,
) -> None:

    windows_box(set(), {"git", "bd"})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd in {"nx", "bd"})
    session_context.run(None)  # never raises into the hook
    assert spawned == []
    assert session_context.run_command(["git", "status"], 5) is None


# -- rdr_verb ----------------------------------------------------------------------


def test_rdr_verb_git_probes_spawn_off_the_cwd(windows_box, spawned, tmp_path: Path) -> None:

    assert rdr_verb._repo_root() is not None
    rdr_verb._repo_name(tmp_path)
    rdr = tmp_path / "rdr-001-x.md"
    rdr.write_text("---\nstatus: draft\n---\n")
    rdr_verb._unchecked_fix_edits(tmp_path, [rdr], {"001": "draft"}, {"1": "abcdef0"})
    assert _heads(spawned) == [GIT, GIT, GIT], spawned


def test_rdr_verb_with_only_a_planted_git_degrades_as_not_found(windows_box, spawned, tmp_path: Path) -> None:

    windows_box(set(), {"git"})
    assert rdr_verb._repo_root() is None
    assert rdr_verb._repo_name(tmp_path) == tmp_path.name
    rdr = tmp_path / "rdr-001-x.md"
    rdr.write_text("---\nstatus: draft\n---\n")
    assert rdr_verb._unchecked_fix_edits(tmp_path, [rdr], {"001": "draft"}, {"1": "abcdef0"}) == []
    assert spawned == []


# -- subagent_start ----------------------------------------------------------------


def test_subagent_start_resolves_git_off_the_cwd(windows_box, spawned) -> None:

    subagent_start._repo_root({})
    assert _heads(spawned) == [GIT], spawned


def test_subagent_start_with_only_a_planted_git_has_no_repo_root(windows_box, spawned) -> None:

    windows_box(set(), {"git"})
    assert subagent_start._repo_root({}) == ""
    assert spawned == []


# -- hooks/__init__ ------------------------------------------------------------------


def test_infer_repo_spawns_git_off_the_cwd(windows_box, spawned) -> None:

    _infer_repo()
    assert _heads(spawned) == [GIT], spawned


def test_infer_repo_with_only_a_planted_git_falls_back_to_the_cwd_name(
    windows_box, spawned, monkeypatch, tmp_path: Path,
) -> None:

    windows_box(set(), {"git"})
    monkeypatch.chdir(tmp_path)
    assert _infer_repo() == tmp_path.name
    assert spawned == []


# -- preflight_verb ------------------------------------------------------------------


def test_preflight_probes_spawn_off_the_cwd(windows_box, spawned, monkeypatch) -> None:

    windows_box({"git", "bd", "nx"}, {"bd", "nx"})
    monkeypatch.setattr(preflight_verb, "_platform", lambda: "win32")
    preflight_verb.run(None)
    assert _heads(spawned) == [rf"{TOOLS}\nx.exe", BD], spawned


def test_preflight_with_only_a_planted_bd_reports_it_missing_and_spawns_nothing(
    windows_box, spawned, monkeypatch,
) -> None:

    windows_box({"nx"}, {"bd"})
    monkeypatch.setattr(preflight_verb, "_platform", lambda: "win32")
    preflight_verb.run(None)
    assert BD not in _heads(spawned) and "bd" not in _heads(spawned)
    status = preflight_verb._probe("bd (beads, optional)", ["bd", "version"])
    assert (status.available, status.detail) == (False, "not on PATH")


# -- POSIX is unchanged ----------------------------------------------------------------


def test_posix_keeps_the_bare_name(spawned, monkeypatch) -> None:
    """execvp searches PATH only, so POSIX argv is exactly what the site wrote."""

    monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
    _infer_repo()
    assert spawned == [["git", "rev-parse", "--show-toplevel"]]


# -- the helper ----------------------------------------------------------------------


def test_argv_off_cwd_resolves_head_and_keeps_the_tail(windows_box) -> None:
    assert nx_argv_mod.argv_off_cwd(["git", "-C", "x", "log"]) == [GIT, "-C", "x", "log"]


def test_argv_off_cwd_leaves_an_absolute_head_alone(windows_box) -> None:
    exe = r"C:\Python312\python.exe"
    assert nx_argv_mod.argv_off_cwd([exe, "-m", "nexus.cli"]) == [exe, "-m", "nexus.cli"]


def test_argv_off_cwd_raises_file_not_found_instead_of_returning_a_planted_head(windows_box) -> None:
    windows_box(set(), {"git"})
    with pytest.raises(FileNotFoundError):
        nx_argv_mod.argv_off_cwd(["git", "status"])
