# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx_argv.which_off_cwd`` and ``argv_off_cwd`` on a real Windows file system
(RDR-224 test review S3, nexus-f9bgu.35).

The Windows arm of the resolver is tested everywhere else on a SIMULATED Windows:
``tests/test_off_cwd_resolver_parity.py`` and ``tests/hooks/test_hook_verbs_exec_off_cwd.py``
replace ``os.path.isfile``, ``os.environ`` and ``os.getcwd`` (``tests/_windows_box.py``),
and ``tests/hooks/test_exec_off_cwd.py`` runs the hook helper's copy on a POSIX tmp
dir with ``platform="win32"`` injected. None of them runs ``nx_argv`` with Windows'
own case-insensitive ``isfile``, its real ``PATHEXT``, its real working directory
and its real executables. This file does, and states the premise the whole class
rests on as two controls: with a file planted in the working directory,
``CreateProcess`` (a bare ``subprocess`` argv[0]) and ``shutil.which`` both answer
with the planted file, and the resolver does not.

The planted files are real executables (copies of ``cmd.exe``, which is
self-contained, as ``tests/test_install_generation_real_windows.py`` does) plus real
``.cmd`` and ``.bat`` files. The tool name is not ``uv`` or ``git``: the runner has
those on its real ``PATH``, and the test sets its own ``PATH``.

Windows only, and in the rehearsal's Windows test set. The class is a
``--require-passed`` pattern in ``windows-pg-bundle-rehearsal.yml``, so a skip fails
that job instead of passing it having proved nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.util import nx_argv

ON_WINDOWS = sys.platform == "win32"

_NAME = "nxplant"

#: PATH shapes that put something that resolves to the working directory AHEAD of the
#: honest entries: id -> builder from (clone, honest tail) to the PATH entries. ``clone``
#: is ``os.getcwd()`` as the process sees it, so each spelling is of the real cwd.
_SHAPES: dict[str, Callable[[str, list[str]], list[str]]] = {
    "plain": lambda clone, tail: [*tail],
    "dot-first": lambda clone, tail: [".", *tail],
    "empty-first": lambda clone, tail: ["", *tail],
    "empty-between-honest-entries": lambda clone, tail: ["", ".", "", *tail],
    "relative-entry-first": lambda clone, tail: ["relbin", *tail],
    "cwd-absolute-first": lambda clone, tail: [clone, *tail],
    "cwd-upper-case": lambda clone, tail: [clone.upper(), *tail],
    "cwd-trailing-backslash": lambda clone, tail: [clone + "\\", *tail],
    "cwd-forward-slashes": lambda clone, tail: [clone.replace("\\", "/"), *tail],
    "everything-at-once": lambda clone, tail: [".", "", clone, "relbin", *tail],
}


def _same_file(found: str | None, expected: Path) -> bool:
    """True when *found* is the very file *expected* (by file identity, so a short-name
    or differently-cased spelling of the same path still matches, and a copy never does)."""
    try:
        return found is not None and os.path.samefile(found, expected)
    except OSError:
        return False


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A working directory holding planted copies of the tool, a ``PATH`` directory
    holding the real one, and a ``PATH`` directory holding only a ``.cmd`` shim."""
    clone, tools, shims, nowhere = (tmp_path / n for n in ("clone", "tools", "shims", "nowhere"))
    for d in (clone, clone / "relbin", tools, shims):
        d.mkdir()
    cmd = Path(os.environ["COMSPEC"])
    for d in (clone, clone / "relbin", tools):
        shutil.copyfile(cmd, d / f"{_NAME}.exe")
    for suffix in (".cmd", ".bat"):
        (clone / f"{_NAME}{suffix}").write_text("@echo planted\r\n")
    (shims / f"{_NAME}.cmd").write_text("@echo shim\r\n")
    # Only the clone has this one, so a bare-name spawn that finds it found it in the cwd.
    shutil.copyfile(cmd, clone / "nxonly.exe")

    monkeypatch.chdir(clone)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    return SimpleNamespace(
        clone=os.getcwd(), tools=tools, shims=shims, nowhere=nowhere,
        planted=clone / f"{_NAME}.exe", real=tools / f"{_NAME}.exe", shim=shims / f"{_NAME}.cmd",
    )


@pytest.mark.skipif(not ON_WINDOWS, reason="needs Windows' own file system, PATHEXT and CreateProcess")
class TestRealWindows:
    # -- the premise: the working directory really does win without the resolver ----

    def test_control_a_bare_name_spawn_runs_the_file_planted_in_the_working_directory(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", str(world.tools))
        done = subprocess.run(
            ["nxonly", "/c", "echo", "ran-from-cwd"], capture_output=True, text=True, timeout=30,
        )
        assert done.returncode == 0 and done.stdout.strip() == "ran-from-cwd", (done.returncode, done.stdout, done.stderr)

    def test_control_shutil_which_answers_with_the_planted_file_ahead_of_the_path_hit(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", str(world.tools))
        found = shutil.which(_NAME)
        assert _same_file(found, world.planted), (found, str(world.planted))
        assert not _same_file(found, world.real)

    # -- the resolver --------------------------------------------------------------

    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_the_path_hit_wins_and_the_planted_file_is_never_returned(
        self, shape: str, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entries = _SHAPES[shape](world.clone, [str(world.tools)])
        monkeypatch.setenv("PATH", os.pathsep.join(entries))
        found = nx_argv.which_off_cwd(_NAME)
        assert _same_file(found, world.real), (shape, entries, found)
        assert os.path.isabs(found)  # type: ignore[arg-type]
        # The same answer from the argv form a hook verb or daemon spawn uses, with the rest intact.
        argv = nx_argv.argv_off_cwd([_NAME, "/c", "echo", "x"])
        assert _same_file(argv[0], world.real) and argv[1:] == ["/c", "echo", "x"], (shape, argv)

    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_with_no_path_hit_the_answer_is_none_never_the_planted_file(
        self, shape: str, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entries = _SHAPES[shape](world.clone, [str(world.nowhere)])
        monkeypatch.setenv("PATH", os.pathsep.join(entries))
        assert nx_argv.which_off_cwd(_NAME) is None, (shape, entries)
        with pytest.raises(FileNotFoundError):
            nx_argv.argv_off_cwd([_NAME, "/c", "echo", "x"])

    def test_a_batch_only_path_hit_is_refused_unless_allowed_and_a_planted_batch_file_never_wins(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The clone holds a real .exe, .cmd and .bat of this name; the only PATH hit is a .cmd.
        monkeypatch.setenv("PATH", os.pathsep.join([".", world.clone, str(world.shims)]))
        assert nx_argv.which_off_cwd(_NAME) is None
        with pytest.raises(FileNotFoundError):
            nx_argv.argv_off_cwd([_NAME])
        allowed = nx_argv.which_off_cwd(_NAME, allow_batch=True)
        assert _same_file(allowed, world.shim), allowed

    def test_the_resolved_path_is_a_real_spawn_target(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join([".", world.clone, str(world.tools)]))
        argv = nx_argv.argv_off_cwd([_NAME, "/c", "echo", "via-path"])
        assert _same_file(argv[0], world.real)
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0 and done.stdout.strip() == "via-path", (done.returncode, done.stdout, done.stderr)

    def test_a_name_with_a_directory_part_is_returned_as_written_and_never_searched(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", str(world.tools))
        explicit = str(world.real)
        assert nx_argv.which_off_cwd(explicit) == explicit
        assert nx_argv.argv_off_cwd([explicit, "/c", "echo", "x"])[0] == explicit
        assert nx_argv.which_off_cwd(str(world.tools / "absent")) is None
