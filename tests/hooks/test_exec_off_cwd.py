# SPDX-License-Identifier: AGPL-3.0-or-later
"""A hook script never spawns a binary the current directory can supply.

RDR-224 review finding A (nexus-f9bgu.36): on Windows ``CreateProcess`` searches
the current directory before ``PATH``, and ``shutil.which`` does the same.
Claude Code runs hooks with the project as cwd, so a ``nx-hook.exe`` or ``git.exe``
planted in a cloned repository would run on every hook and, for the shim, be
relayed as the hook's own verdict. The cure is a PATH-only lookup that hands the
spawner an absolute path (``conexus/hooks/scripts/_exec_path.py``).

Windows runs here on a POSIX host by injection: the helper takes the platform
explicitly, and the per-script cases patch ``sys.platform`` for the duration of
one call with the subprocess seam faked, so no Windows box is needed to see which
argv0 each script would spawn.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "conexus" / "hooks" / "scripts"


def _load(path: Path, name: str) -> types.ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def exec_path() -> types.ModuleType:
    return _load(_SCRIPTS / "_exec_path.py", "_exec_path_under_test")


def _plant(directory: Path, *names: str, mode: int = 0o755) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for n in names:
        p = directory / n
        p.write_text("planted")
        p.chmod(mode)
        out[n] = p
    return out


# -- the helper, Windows semantics injected ---------------------------------------


def test_windows_never_picks_a_binary_from_the_current_directory(
    exec_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cloned-repo"
    _plant(cwd, "uv.exe", "git.exe", "nx-hook.exe")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(cwd)
    for name in ("uv", "git", "nx-hook"):
        assert exec_path.which_off_cwd(name, platform="win32", path=str(empty), pathext=".EXE") is None


def test_windows_prefers_the_path_hit_over_a_planted_one_and_returns_it_absolute(
    exec_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cloned-repo"
    _plant(cwd, "uv.exe")
    real = _plant(tmp_path / "bin", "uv.exe")["uv.exe"]
    monkeypatch.chdir(cwd)
    got = exec_path.which_off_cwd("uv", platform="win32", path=str(tmp_path / "bin"), pathext=".exe;.cmd")
    assert got == str(real)
    assert os.path.isabs(got)


def test_windows_skips_relative_and_empty_path_entries(
    exec_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative PATH entry resolves against the cwd, which is the same hole."""
    cwd = tmp_path / "cloned-repo"
    _plant(cwd, "uv.exe")
    monkeypatch.chdir(cwd)
    path = ";".join([".", "", "bin-relative", str(tmp_path / "nowhere")])
    assert exec_path.which_off_cwd("uv", platform="win32", path=path, pathext=".EXE") is None


def test_windows_honours_pathext_for_a_bare_name(exec_path, tmp_path: Path) -> None:
    real = _plant(tmp_path / "bin", "uv.exe")["uv.exe"]
    assert exec_path.which_off_cwd("uv", platform="win32", path=str(tmp_path / "bin"), pathext=".com;.exe") == str(real)
    assert exec_path.which_off_cwd("nx-hook", platform="win32", path=str(tmp_path / "bin"), pathext=".EXE") is None


def test_windows_needs_no_exec_bit(exec_path, tmp_path: Path) -> None:
    """Windows has no exec bit; a lookup that required one would find nothing."""
    real = _plant(tmp_path / "bin", "git.exe", mode=0o644)["git.exe"]
    assert exec_path.which_off_cwd("git", platform="win32", path=str(tmp_path / "bin"), pathext=".exe") == str(real)


def test_posix_ignores_the_current_directory_and_matches_shutil_which(
    exec_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cloned-repo"
    _plant(cwd, "git")
    real = _plant(tmp_path / "bin", "git")["git"]
    monkeypatch.chdir(cwd)
    path = f"{tmp_path / 'bin'}:{tmp_path / 'other'}"
    got = exec_path.which_off_cwd("git", platform="linux", path=path)
    assert got == str(real) == shutil.which("git", path=path)
    # Nothing on PATH: the planted cwd copy is still not chosen.
    assert exec_path.which_off_cwd("git", platform="linux", path=str(tmp_path / "other")) is None
    assert shutil.which("git", path=str(tmp_path / "other")) is None


def test_posix_requires_the_exec_bit(exec_path, tmp_path: Path) -> None:
    _plant(tmp_path / "bin", "git", mode=0o644)
    assert exec_path.which_off_cwd("git", platform="linux", path=str(tmp_path / "bin")) is None


def test_the_helper_imports_only_the_standard_library() -> None:
    import ast

    tree = ast.parse((_SCRIPTS / "_exec_path.py").read_text())
    found: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module.split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__"}, found


# -- each script, spawning through the helper -------------------------------------


@pytest.fixture()
def windows_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A Windows-flavoured box on a POSIX host: PATH holds the real tools, the cwd
    (a 'cloned repository') holds a planted copy of each."""
    names = ("uv.exe", "nx.exe", "nx-hook.exe", "git.exe", "python3.13.exe")
    real = _plant(tmp_path / "bin", *names)
    _plant(tmp_path / "cloned-repo", *names)
    monkeypatch.chdir(tmp_path / "cloned-repo")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    # lower case so the returned path matches the planted file name on a
    # case-insensitive host filesystem
    monkeypatch.setenv("PATHEXT", ".exe")
    monkeypatch.setattr(sys, "platform", "win32")
    return real


class _Done:
    returncode = 0
    stdout = "nx, version 1.2.3\n"
    stderr = ""


def _record(mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(argv, *a, **k):
        seen.append(list(argv))
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", fake)
    return seen


def _argv0_is(seen: list[list[str]], expected: Path) -> None:
    assert seen, "the script spawned nothing; the case examined nothing"
    for argv in seen:
        assert argv[0] == str(expected), f"spawned {argv[0]!r}, not the PATH binary {str(expected)!r}"


def test_the_shim_spawns_the_path_nx_hook_not_the_planted_one(
    windows_box, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = _load(_SCRIPTS / "nx_hook_shim.py", "nx_hook_shim_offcwd")
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0

        def __init__(self, argv, **k):
            seen.append(list(argv))

        def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            return b"verdict", b""

    monkeypatch.setattr(shim.subprocess, "Popen", _Proc)
    monkeypatch.setattr(shim.signal, "signal", lambda *a: None)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=io.BytesIO()))
    assert shim.main(["auto-approve"]) == 0
    _argv0_is(seen, windows_box["nx-hook.exe"])
    assert seen[0][1:] == ["auto-approve"]


def test_the_shim_skips_with_a_note_when_only_a_planted_nx_hook_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """No nx-hook on PATH, one in the cwd: the shim must treat the CLI as absent
    (exit 0 with the note) rather than run the planted binary."""
    _plant(tmp_path / "cloned-repo", "nx-hook.exe")
    (tmp_path / "bin").mkdir()
    monkeypatch.chdir(tmp_path / "cloned-repo")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(sys, "platform", "win32")
    shim = _load(_SCRIPTS / "nx_hook_shim.py", "nx_hook_shim_offcwd_absent")
    spawned: list[object] = []
    monkeypatch.setattr(shim.subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("spawned"))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}")))
    assert shim.main(["auto-approve"]) == 0
    assert not spawned
    assert "`nx-hook` is not installed" in capsys.readouterr().err


def test_the_lockstep_action_spawns_path_uv_and_nx(
    windows_box, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    mod = _load(_SCRIPTS / "version_lockstep_action.py", "version_lockstep_action_offcwd")
    seen = _record(mod, monkeypatch)
    monkeypatch.setenv("NX_LOCKSTEP_LOG", str(tmp_path / "lockstep.log"))
    mod.uv_receipt_present()
    assert mod.installed_nx_version() == "1.2.3"
    assert mod.run_cmd(["nx", "upgrade"]) is True
    assert mod.run_cmd(["uv", "tool", "upgrade", "conexus"]) is True
    mod._run_nx_upgrade_for_ref_drift(5)
    by_name = {Path(a[0]).stem: a[0] for a in seen}
    assert by_name["uv"] == str(windows_box["uv.exe"])
    assert by_name["nx"] == str(windows_box["nx.exe"])
    for argv in seen:
        assert os.path.isabs(argv[0]), argv
        assert "cloned-repo" not in argv[0], argv


def test_the_lockstep_action_treats_a_planted_only_cli_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plant(tmp_path / "cloned-repo", "uv.exe", "nx.exe")
    (tmp_path / "bin").mkdir()
    monkeypatch.chdir(tmp_path / "cloned-repo")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("NX_LOCKSTEP_LOG", str(tmp_path / "lockstep.log"))
    monkeypatch.setattr(sys, "platform", "win32")
    mod = _load(_SCRIPTS / "version_lockstep_action.py", "version_lockstep_action_offcwd_absent")
    seen = _record(mod, monkeypatch)
    assert mod.uv_receipt_present() is False
    assert mod.installed_nx_version() is None
    assert mod.run_cmd(["nx", "upgrade"]) is False
    mod._run_nx_upgrade_for_ref_drift(5)
    assert seen == [], "a binary planted in the cwd was spawned"


def test_the_lockstep_hook_resolves_the_ref_with_path_git(
    windows_box, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    mod = _load(_SCRIPTS / "version_lockstep_hook.py", "version_lockstep_hook_offcwd")
    seen = _record(mod, monkeypatch)
    mod._resolve_ref_sha(tmp_path, "v1.0.0", 5.0)
    _argv0_is(seen, windows_box["git.exe"])


def test_the_git_write_gate_asks_path_git_about_the_worktree(
    windows_box, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    mod = _load(
        _SCRIPTS / "routing" / "subagent_git_write_requires_orchestrator.py",
        "subagent_git_write_offcwd",
    )
    seen = _record(mod, monkeypatch)
    mod._in_linked_worktree(str(tmp_path))
    assert len(seen) == 2
    _argv0_is(seen, windows_box["git.exe"])


def test_the_interpreter_shim_execs_the_path_python_not_a_planted_one(
    windows_box, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load(_SCRIPTS / "_interpreter.py", "_interpreter_offcwd")
    monkeypatch.delenv("NX_HOOK_PYTHON", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("NX_TOOLS_DIR", str(windows_box["uv.exe"].parent / "no-tools"))
    got = mod.resolve()
    assert got == str(windows_box["python3.13.exe"])
