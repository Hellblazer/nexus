# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nexus.util.nx_argv.which_off_cwd`` / ``argv_off_cwd`` on Windows, and POSIX unchanged.

RDR-224 review of the off-cwd sweep (nexus-f9bgu.37). The Windows arm walks ``PATH``
itself (``shutil.which`` searches the working directory first even with ``path=``, so
it cannot be asked for "PATH only"), against the simulated file system of
``tests/_windows_box.py``. It follows what ``CreateProcess`` does for a program name:

* a bare name is looked up on ``PATH`` entries that are absolute and are not the
  working directory; the working directory is never searched;
* a name with a directory part or a drive is an explicit path the caller chose, and
  ``CreateProcess`` does not search for it, so it is left as it is;
* a ``.cmd`` or ``.bat`` hit is not returned unless the caller opts in with
  ``allow_batch=True``: a bare ``git`` or ``bd`` spawn only ever found an ``.exe``,
  and a batch file runs through ``cmd.exe``, whose quoting CPython does not escape.
"""
from __future__ import annotations

import shutil
import stat

import pytest

from nexus.util import nx_argv as nx
from tests._windows_box import CWD, TOOLS, install_windows


@pytest.fixture()
def win(monkeypatch: pytest.MonkeyPatch):
    """Windows semantics for ``nx_argv``; ``win(files, **kw)`` sets the file system."""
    monkeypatch.setattr(nx, "_platform", lambda: "win32")

    def configure(files, **kw):  # noqa: ANN001, ANN003, ANN202
        install_windows(monkeypatch, "nexus.util.nx_argv", files, **kw)

    return configure


# -- a batch file is not what a bare name ever found ----------------------------------


@pytest.mark.parametrize("ext", [".cmd", ".bat", ".CMD"])
def test_a_program_found_only_as_a_batch_file_is_not_found(win, ext) -> None:
    win({rf"{TOOLS}\bd{ext}"})
    assert nx.which_off_cwd("bd") is None
    with pytest.raises(FileNotFoundError):
        nx.argv_off_cwd(["bd", "list"])


def test_a_batch_hit_does_not_hide_an_exe_further_down_the_path(win) -> None:
    win({rf"{TOOLS}\bd.cmd", r"C:\more\bd.exe"}, path=rf"{CWD};{TOOLS};C:\more")
    assert nx.argv_off_cwd(["bd", "list"]) == [r"C:\more\bd.exe", "list"]


def test_an_exe_beats_a_batch_file_in_the_same_directory_when_batch_is_allowed(win) -> None:
    win({rf"{TOOLS}\claude.cmd", rf"{TOOLS}\claude.exe"})
    assert nx.which_off_cwd("claude", allow_batch=True) == rf"{TOOLS}\claude.exe"


def test_a_batch_file_named_outright_is_still_refused_by_default(win) -> None:
    win({rf"{TOOLS}\tool.cmd"})
    assert nx.which_off_cwd("tool.cmd") is None
    assert nx.which_off_cwd("tool.cmd", allow_batch=True) == rf"{TOOLS}\tool.cmd"


def test_allow_batch_returns_a_batch_hit_from_both_helpers(win) -> None:
    win({rf"{TOOLS}\claude.cmd"})
    assert nx.which_off_cwd("claude", allow_batch=True) == rf"{TOOLS}\claude.cmd"
    assert nx.argv_off_cwd(["claude", "-p"], allow_batch=True) == [rf"{TOOLS}\claude.cmd", "-p"]


# -- a head with a directory part or a drive is the caller's explicit path ------------

_EXPLICIT_HEADS = [
    pytest.param(r".\tools\x.exe", id="dot-backslash"),
    pytest.param("./tools/x", id="dot-slash"),
    pytest.param(r"tools\x", id="relative-subdir"),
    pytest.param("tools/x", id="relative-subdir-slash"),
    pytest.param(r"..\x", id="parent"),
    pytest.param(r"C:\x\y.exe", id="absolute"),
    pytest.param("C:x", id="drive-relative"),
    pytest.param(r"\\srv\share\x.exe", id="unc"),
    pytest.param(r"\rooted\x.exe", id="rooted-no-drive"),
]


@pytest.mark.parametrize("head", _EXPLICIT_HEADS)
def test_a_head_with_a_directory_part_or_a_drive_is_left_untouched(win, head) -> None:
    win(set())  # nothing exists: it is not looked up, so nothing is missing
    assert nx.argv_off_cwd([head, "a", "b"]) == [head, "a", "b"]


def test_which_returns_an_existing_explicit_path_and_none_for_a_missing_one(win) -> None:
    win({r"C:\x\y.exe"})
    assert nx.which_off_cwd(r"C:\x\y.exe") == r"C:\x\y.exe"
    assert nx.which_off_cwd(r"C:\x\y") == r"C:\x\y.exe"
    assert nx.which_off_cwd(r"C:\x\z.exe") is None


def test_a_head_with_a_directory_part_is_not_searched_on_path(win) -> None:
    win({rf"{TOOLS}\x.exe"})
    assert nx.argv_off_cwd([r"tools\x", "q"]) == [r"tools\x", "q"]


def test_a_bare_name_found_only_in_the_working_directory_is_still_not_found(win) -> None:
    win({rf"{CWD}\git.exe"})
    assert nx.which_off_cwd("git") is None
    with pytest.raises(FileNotFoundError):
        nx.argv_off_cwd(["git", "status"])


def test_a_planted_copy_does_not_hide_the_installed_one(win) -> None:
    win({rf"{CWD}\git.exe", rf"{TOOLS}\git.exe"})
    assert nx.argv_off_cwd(["git", "status"]) == [rf"{TOOLS}\git.exe", "status"]


def test_a_working_directory_entry_is_skipped_however_it_is_spelled(win) -> None:
    win({rf"{CWD}\git.exe"}, path=CWD.upper() + "\\;" + TOOLS)
    assert nx.which_off_cwd("git") is None


def test_a_relative_path_entry_is_skipped(win) -> None:
    # Relative entries resolve against the working directory: the same hole.
    win({r"tools\git.exe"}, path=rf"tools;.;{TOOLS}")
    assert nx.which_off_cwd("git") is None
    win({r"tools\git.exe", rf"{TOOLS}\git.exe"}, path=rf"tools;{TOOLS}")
    assert nx.which_off_cwd("git") == rf"{TOOLS}\git.exe"


@pytest.mark.parametrize("pathext", [None, ""])
def test_an_unset_or_empty_pathext_means_the_default_list(win, pathext) -> None:
    win({rf"{TOOLS}\git.exe"}, pathext=pathext)
    found = nx.which_off_cwd("git")
    assert found is not None and found.lower() == rf"{TOOLS}\git.exe".lower()


def test_a_name_with_a_listed_extension_is_tried_as_written(win) -> None:
    win({rf"{TOOLS}\git.exe"})
    assert nx.which_off_cwd("git.exe") == rf"{TOOLS}\git.exe"


def test_an_empty_argv_comes_back_empty(win) -> None:
    win(set())
    assert nx.argv_off_cwd([]) == []


# -- POSIX is unchanged ----------------------------------------------------------------


@pytest.fixture()
def posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nx, "_platform", lambda: "linux")


def test_posix_argv_is_equal_in_value_to_its_input(posix) -> None:
    for argv in (["git", "status"], ("git", "status"), ["/usr/bin/git"], ["bd.cmd", "x"], ["./x", "y"], []):
        out = nx.argv_off_cwd(argv)
        assert out == list(argv)
        assert isinstance(out, list)
    assert nx.argv_off_cwd(["bd"], allow_batch=True) == ["bd"]


def test_posix_which_is_shutil_which(posix, monkeypatch, tmp_path) -> None:
    tool = tmp_path / "sometool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("sometool", "missing-tool", "sometool.cmd"):
        assert nx.which_off_cwd(name) == shutil.which(name)
        assert nx.which_off_cwd(name, allow_batch=True) == shutil.which(name)
    assert nx.which_off_cwd("sometool") == str(tool)
