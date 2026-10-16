# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/hooks/scripts/_cli_lookup.py ``cannot_start``: a CLI program that is
found and cannot run (RDR-228 Phase 2 Step 4, nexus-sv2wx.17; Sam's decision of
2026-10-14).

A generation whose interpreter ``uv`` pruned is found by the lookup and fails when
started. ``cannot_start`` says so from files alone (a stat and one small read, no
process), because the launcher and the setup hook must decide in a few
milliseconds. It claims "cannot start" only on positive evidence: a fake program
with no venv around it, or a shebang that names something that exists, is
runnable.

The Windows-shaped cases run on every host: ``windows=True`` selects the branch
that has no shebang to read, and the programs are ordinary files in a temporary
directory, so they pin the decision and not the operating system. The launcher
and hook tests drive the real thing on native Windows.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from tests._platform import long_dir, posix_only, prune_python, uv_polyglot_script, write_uv_polyglot

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"


@pytest.fixture(scope="module")
def lookup():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_cli_lookup_cannot_start", _SCRIPTS / "_cli_lookup.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script(path: Path, first_line: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(first_line + "\nprint('hi')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _venv(root: Path, *, home: Path | None, with_cfg: bool = True) -> Path:
    """A venv-shaped generation: ``pyvenv.cfg`` (naming *home*) and ``bin/``."""
    (root / "bin").mkdir(parents=True)
    if with_cfg and home is not None:
        (root / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = false\nversion = 3.12.1\n", encoding="utf-8")
    return root


# -- POSIX: the shebang --------------------------------------------------------------------------


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_a_missing_interpreter_cannot_start(lookup, tmp_path: Path) -> None:
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{tmp_path}/pruned/bin/python3")
    reason = lookup.cannot_start(str(exe), windows=False)
    assert reason is not None
    assert str(tmp_path / "pruned" / "bin" / "python3") in reason


@posix_only("a dangling symlink is how uv leaves a pruned interpreter")
def test_a_shebang_naming_a_dangling_symlink_cannot_start(lookup, tmp_path: Path) -> None:
    gone = tmp_path / "uv" / "cpython-3.12" / "bin" / "python3.12"
    link = tmp_path / "gen" / "bin" / "python"
    link.parent.mkdir(parents=True)
    os.symlink(gone, link)
    exe = _script(tmp_path / "gen" / "bin" / "nx-mcp", f"#!{link}")
    # "missing", not "not executable": a check that only follows the link through
    # os.access would still say cannot start, for the wrong reason.
    assert "is missing" in (lookup.cannot_start(str(exe), windows=False) or "")


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_an_interpreter_that_exists_can_start(lookup, tmp_path: Path) -> None:
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{sys.executable}")
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_a_file_that_is_not_executable_cannot_start(lookup, tmp_path: Path) -> None:
    interp = tmp_path / "python"
    interp.write_text("not a program", encoding="utf-8")
    interp.chmod(0o644)
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{interp}")
    assert "not executable" in (lookup.cannot_start(str(exe), windows=False) or "")


@posix_only("a shebang names the interpreter the kernel starts")
def test_an_env_shebang_needs_its_program_on_path(lookup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _script(tmp_path / "bin" / "nx-hook", "#!/usr/bin/env pythonx-not-here")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert "pythonx-not-here" in (lookup.cannot_start(str(exe), windows=False) or "")
    fake = _script(tmp_path / "onpath" / "pythonx-not-here", "#!/bin/sh")
    monkeypatch.setenv("PATH", str(fake.parent))
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("a shebang names the interpreter the kernel starts")
@pytest.mark.parametrize(
    "line", ["#!python3", "#!/usr/bin/env -S python3 -u", "#!/usr/bin/env FOO=1 python3", "#!", "#! "],
    ids=["relative", "env-with-option", "env-with-assignment", "empty", "blank"],
)
def test_a_shebang_it_cannot_judge_is_runnable(lookup, tmp_path: Path, line: str) -> None:
    """No guess: only a shebang whose meaning is certain can say "cannot start"."""
    exe = _script(tmp_path / "bin" / "nx-mcp", line)
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("reads a shebang")
def test_a_binary_and_an_empty_file_are_not_judged_by_a_shebang(lookup, tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "nx-mcp"
    binary.parent.mkdir()
    binary.write_bytes(b"\x7fELF\x02\x01\x01" + bytes(64))
    binary.chmod(0o755)
    empty = tmp_path / "bin" / "nx-hook"
    empty.write_bytes(b"")
    empty.chmod(0o755)
    assert lookup.cannot_start(str(binary), windows=False) is None
    assert lookup.cannot_start(str(empty), windows=False) is None


def test_a_program_that_vanished_is_not_judged(lookup, tmp_path: Path) -> None:
    assert lookup.cannot_start(str(tmp_path / "bin" / "gone"), windows=False) is None
    assert lookup.cannot_start(str(tmp_path / "bin" / "gone.exe"), windows=True) is None


# -- the venv around the program: POSIX and Windows-shaped ----------------------------------------


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_whose_base_python_was_pruned_cannot_start(lookup, tmp_path: Path, windows: bool) -> None:
    """``pyvenv.cfg`` names the interpreter the venv was built from; ``uv`` removes
    that directory when it prunes the Python. On Windows the program is a launcher
    copy with no shebang, and this is the whole check. On POSIX the interpreter links
    dangle too (``prune_python`` leaves both)."""
    if os.name == "nt" and not windows:
        pytest.skip("the POSIX reading needs links, which a Windows host's prune does not leave")
    gen = _venv(tmp_path / "gen-20260101T000000Z", home=None, with_cfg=False)
    prune_python(gen, tmp_path / "uv" / "cpython-3.12-gone")
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"MZ not a real launcher" if windows else b"\x7fELF" + bytes(8))
    exe.chmod(0o755)
    reason = lookup.cannot_start(str(exe), windows=windows)
    assert reason is not None
    assert "cpython-3.12-gone" in reason


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_whose_base_python_is_there_can_start(lookup, tmp_path: Path, windows: bool) -> None:
    home = tmp_path / "uv" / "cpython-3.12"
    home.mkdir(parents=True)
    gen = _venv(tmp_path / "gen-20260101T000000Z", home=home)
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"MZ" if windows else b"\x7fELF")
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=windows) is None


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_with_no_pyvenv_cfg_is_not_judged(lookup, tmp_path: Path, windows: bool) -> None:
    """A fake generation (the suite's) or a half-written one: no evidence either way."""
    gen = _venv(tmp_path / "gen", home=None, with_cfg=False)
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"x")
    assert lookup.cannot_start(str(exe), windows=windows) is None


def test_a_pyvenv_cfg_without_a_home_line_is_not_judged(lookup, tmp_path: Path) -> None:
    gen = _venv(tmp_path / "gen", home=None, with_cfg=False)
    (gen / "pyvenv.cfg").write_text("version = 3.12.1\n", encoding="utf-8")
    exe = gen / "bin" / "nx-mcp"
    exe.write_bytes(b"x")
    assert lookup.cannot_start(str(exe), windows=True) is None


@posix_only("a symlinked program")
def test_a_symlink_into_a_pruned_venv_cannot_start(lookup, tmp_path: Path) -> None:
    """A ``PATH`` entry that links to a program inside a venv is judged by that venv."""
    gen = _venv(tmp_path / "tool", home=None, with_cfg=False)
    prune_python(gen, tmp_path / "gone-python")
    target = gen / "bin" / "nx"
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    link = tmp_path / "local-bin" / "nx"
    link.parent.mkdir()
    os.symlink(target, link)
    assert lookup.cannot_start(str(link), windows=False) is not None


# -- a gone home is no evidence on POSIX by itself (Sam, 2026-10-14; review of 4cffeb898 I1) -------


def _real_venv(root: Path) -> Path:
    """A real venv (standard library, no pip, links into the running interpreter)
    holding a console script whose shebang names the venv's own python, as ``uv``
    writes them."""
    import venv

    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    script = root / "bin" / "nx-mcp"
    script.write_text(f"#!{root / 'bin' / 'python'}\nprint('real server')\n", encoding="utf-8")
    script.chmod(0o755)
    return root


def _set_home(venv_root: Path, home: Path) -> None:
    cfg = venv_root / "pyvenv.cfg"
    lines = [
        f"home = {home}" if line.partition("=")[0].strip() == "home" else line
        for line in cfg.read_text(encoding="utf-8").splitlines()
    ]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


@posix_only("a venv's python is a link that can resolve whatever pyvenv.cfg says")
def test_a_real_venv_whose_home_is_gone_but_whose_interpreter_still_resolves_is_not_called_broken(lookup, tmp_path: Path) -> None:
    """The review's false positive (4cffeb898 I1): ``home`` names a directory that is
    not there while ``bin/python`` still resolves. Sam's rule: ``home`` alone is no
    evidence on POSIX. Whether such a venv then starts depends on the interpreter
    build (measured 2026-10-14: a system CPython falls back to the link's real path
    and runs; a ``uv`` standalone CPython fails to find its library), so the test
    pins the rule and not the start."""
    root = _real_venv(tmp_path / "venv")
    _set_home(root, tmp_path / "uv" / "cpython-gone")
    assert not (tmp_path / "uv" / "cpython-gone").exists()
    assert (root / "bin" / "python").resolve().exists()
    assert lookup.cannot_start(str(root / "bin" / "nx-mcp"), windows=False) is None


@posix_only("a pruned venv's python links dangle")
def test_a_real_venv_whose_home_is_gone_and_whose_interpreter_dangles_cannot_start(lookup, tmp_path: Path) -> None:
    """What a real ``uv`` prune leaves, and the program really does not start."""
    import subprocess

    root = _real_venv(tmp_path / "venv")
    prune_python(root, tmp_path / "uv" / "cpython-gone")
    with pytest.raises(OSError):  # the kernel cannot find the shebang's interpreter
        subprocess.run([str(root / "bin" / "nx-mcp")], capture_output=True, timeout=60, check=False)
    reason = lookup.cannot_start(str(root / "bin" / "nx-mcp"), windows=False)
    assert reason is not None and "cpython-gone" in reason


@posix_only("links and a missing home")
def test_a_gone_home_with_no_dangling_link_is_runnable_for_a_program_with_no_shebang(lookup, tmp_path: Path) -> None:
    gen = _venv(tmp_path / "gen", home=tmp_path / "uv" / "cpython-gone")
    (gen / "bin" / "python").symlink_to(Path(sys.executable))
    exe = gen / "bin" / "nx-mcp"
    exe.write_bytes(b"\x7fELF")
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("links and a present home")
def test_a_dangling_link_with_the_home_present_is_not_enough_either(lookup, tmp_path: Path) -> None:
    """One fact is not enough: both the home and a link must say it is gone."""
    home = tmp_path / "uv" / "cpython-3.12"
    home.mkdir(parents=True)
    gen = _venv(tmp_path / "gen", home=home)
    (gen / "bin" / "python").symlink_to(tmp_path / "nowhere" / "python")
    exe = gen / "bin" / "nx-mcp"
    exe.write_bytes(b"\x7fELF")
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=False) is None


def test_on_windows_a_gone_home_alone_is_the_whole_evidence(lookup, tmp_path: Path) -> None:
    """There are no links there: a launcher copy with no shebang, and ``home``."""
    gen = _venv(tmp_path / "gen", home=tmp_path / "uv" / "cpython-gone")
    exe = gen / "bin" / "nx-mcp.exe"
    exe.write_bytes(b"MZ")
    assert lookup.cannot_start(str(exe), windows=True) is not None


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_longer_than_256_bytes_is_read_whole(lookup, tmp_path: Path) -> None:
    """The line used to be cut at 256 bytes, which named a path that is not there and
    so called a working program broken (and cut a missing path short in the reason)."""
    deep = tmp_path
    for i in range(3):
        deep = deep / (f"d{i}-" + "x" * 120)
    deep.mkdir(parents=True)
    alive = deep / "python"
    alive.symlink_to(Path(sys.executable))
    long_line = f"#!{alive}"
    assert len(long_line) > 256, len(long_line)
    exe = _script(tmp_path / "bin" / "nx-mcp", long_line)
    assert lookup.cannot_start(str(exe), windows=False) is None
    dead = deep / "pruned" / "python"
    exe2 = _script(tmp_path / "bin" / "nx-hook", f"#!{dead}")
    reason = lookup.cannot_start(str(exe2), windows=False)
    assert reason is not None and str(dead) in reason, reason


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_first_line_that_never_ends_is_not_judged(lookup, tmp_path: Path) -> None:
    """Bounded read: a file that starts ``#!`` and has no line end within the bound
    is not a shebang anyone can resolve."""
    exe = tmp_path / "bin" / "nx-mcp"
    exe.parent.mkdir()
    exe.write_bytes(b"#!/nowhere/" + b"a" * 20000)
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=False) is None


def test_a_home_that_is_a_file_cannot_start(lookup, tmp_path: Path) -> None:
    """``home`` must be a directory; a file at that path is no interpreter directory."""
    home = tmp_path / "home-file"
    home.write_text("x", encoding="utf-8")
    gen = _venv(tmp_path / "gen", home=home)
    exe = gen / "bin" / "nx-mcp.exe"
    exe.write_bytes(b"MZ")
    assert lookup.cannot_start(str(exe), windows=True) is not None


def test_the_check_starts_no_process_and_opens_no_socket() -> None:
    """Standard library only, and not ``subprocess``: a stat and one small read."""
    import ast

    tree = ast.parse((_SCRIPTS / "_cli_lookup.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"subprocess", "socket", "urllib", "http"}, imported


# -- the plugin and the CLI judge a generation by one rule -------------------------------------------


def _generation_fixture(root: Path, *, home: str, link: str, shebang: str) -> Path:
    """A generation with ``bin/nx`` and ``bin/nx-mcp`` (the same file twice), built
    from three independent facts: what ``pyvenv.cfg`` says, what ``bin/python`` is, and
    what the scripts' first line says."""
    gen = root / "gen"
    (gen / "bin").mkdir(parents=True)
    gone = root / "uv" / "cpython-gone"
    cfg = {
        "ok": f"home = {root}\n", "gone": f"home = {gone}\n", "nocfg": None, "nohomeline": "version = 3.12.1\n",
    }[home]
    if cfg is not None:
        (gen / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    if os.name != "nt":
        if link == "resolves":
            (gen / "bin" / "python").symlink_to(Path(sys.executable))
        elif link == "dangles":
            (gen / "bin" / "python").symlink_to(gone / "bin" / "python")
    first = {
        "none": None, "ok": f"#!{sys.executable}", "missing": f"#!{root / 'pruned' / 'python'}",
        "env-missing": "#!/usr/bin/env pythonx-not-here",
        "polyglot-ok": None, "polyglot-missing": None,
    }[shebang]
    polyglot = {"polyglot-ok": sys.executable, "polyglot-missing": str(root / "pruned" / "python")}.get(shebang)
    for name in ("nx", "nx-mcp"):
        exe = gen / "bin" / name
        if polyglot is not None:
            exe.write_text(uv_polyglot_script(polyglot), encoding="utf-8")
        else:
            exe.write_bytes((first + "\nprint('x')\n").encode() if first else b"\x7fELF")
        exe.chmod(0o755)
    return gen


# On Windows only the Windows arm is generated: the POSIX reading needs links and a shebang,
# and 72 cells generated only to be skipped would count against the Windows job's skip ceiling.
@pytest.mark.parametrize("windows", [True] if os.name == "nt" else [False, True], ids=lambda w: "windows" if w else "posix")
@pytest.mark.parametrize("shebang", ["none", "ok", "missing", "env-missing", "polyglot-ok", "polyglot-missing"])
@pytest.mark.parametrize("link", ["absent", "resolves", "dangles"])
@pytest.mark.parametrize("home", ["ok", "gone", "nocfg", "nohomeline"])
def test_the_plugin_and_the_cli_give_the_same_verdict_on_a_generation(
    lookup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str, link: str, shebang: str, windows: bool,
) -> None:
    """``_cli_lookup.cannot_start`` decides for the launcher and the hook;
    ``self_cmd._generation_interpreter_gone`` decides whether ``--bootstrap`` refuses
    or repairs. A disagreement is the loop the review found: the plugin says
    "setup repairs it" and setup's build is refused as "already installed"."""
    from nexus.commands import self_cmd

    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    gen = _generation_fixture(tmp_path, home=home, link=link, shebang=shebang)
    plugin = lookup.cannot_start(str(gen / "bin" / "nx-mcp"), windows=windows) is not None
    cli = self_cmd._generation_interpreter_gone(gen, windows=windows)
    assert plugin == cli, (home, link, shebang, windows, plugin, cli)
    # Not only agreement: each cell has its expected answer, so two wrong verdicts cannot agree.
    expected = (
        home == "gone"
    ) if windows else ((home == "gone" and link == "dangles") or shebang in ("missing", "env-missing", "polyglot-missing"))
    assert plugin is expected



@posix_only("a shebang names the interpreter the kernel starts")
def test_the_one_documented_difference_the_plugin_judges_the_program_it_is_given_the_cli_judges_either(
    lookup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matrix above writes one shebang into both console scripts. The plugin judges the
    program the launcher is about to run (``nx-mcp`` for the server, ``nx-hook`` for a hook);
    ``--bootstrap`` judges the generation by ``bin/nx-mcp`` OR ``bin/nx``, the two it always
    has. Where only ``nx`` is bad, the server would start and ``--bootstrap`` repairs the
    generation anyway: the difference is safe (the plugin never says "cannot start" of a
    program that starts, and the CLI replaces a generation with a script that does not), and
    it is pinned here so that a change on either side is seen. No launcher path builds the
    case, since both scripts share one interpreter."""
    from nexus.commands import self_cmd

    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    gen = _generation_fixture(tmp_path, home="ok", link="resolves", shebang="ok")
    bad = _script(gen / "bin" / "nx", f"#!{tmp_path / 'pruned' / 'python'}")
    good = gen / "bin" / "nx-mcp"
    assert lookup.cannot_start(str(good), windows=False) is None, "the program given starts"
    assert lookup.cannot_start(str(bad), windows=False) is not None, "the program given is judged by its own line"
    assert self_cmd._generation_interpreter_gone(gen, windows=False) is True, "the CLI judges the generation by either script"
    # And the other way round: the server script is the bad one.
    _script(gen / "bin" / "nx", f"#!{sys.executable}")
    _script(good, f"#!{tmp_path / 'pruned' / 'python'}")
    assert lookup.cannot_start(str(good), windows=False) is not None
    assert self_cmd._generation_interpreter_gone(gen, windows=False) is True


# -- uv's sh polyglot launcher (nexus-sv2wx.20) -----------------------------------------------------
#
# ``uv`` writes a console script as ``#!/bin/sh`` plus a ``'''exec' '<python>' "$0" "$@"`` line when
# the interpreter's path will not fit a shebang (over 127 characters, or a space). The kernel then
# starts the shell, so the start succeeds and the shell fails at its own ``exec`` with 126 or 127:
# not an OSError, and not a failure a caller can tell from the program's own. Found by the setup
# journey on macOS, where a short TMPDIR hid it.


def _wheel(dest: Path) -> Path:
    """A wheel of one package with one console script, built by hand: ``uv pip install
    --offline`` needs nothing from the network for it."""
    dist = "tinypkg-0.1.0.dist-info"
    files = {
        "tinypkg/__init__.py": "def main():\n    return 0\n",
        f"{dist}/METADATA": "Metadata-Version: 2.1\nName: tinypkg\nVersion: 0.1.0\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": "[console_scripts]\ntiny-hook = tinypkg:main\n",
    }
    files[f"{dist}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{dist}/RECORD"])
    whl = dest / "tinypkg-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return whl


@pytest.fixture
def uv_installed_venv(tmp_path: Path) -> Path:
    """A venv at a path over 127 characters with ``tiny-hook`` installed by the real ``uv``."""
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; it writes the script under test, so this is a failure, not a skip"
    root = long_dir(tmp_path / "venvs")
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_NO_CONFIG": "1"}
    wheel = _wheel(tmp_path)
    for argv in (
        [uv, "venv", "--allow-existing", "--python", sys.executable, str(root)],
        [uv, "pip", "install", "--offline", "--python", str(root / "bin" / "python"), str(wheel)],
    ):
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120, check=False)
        assert done.returncode == 0, (argv[1:3], done.stderr[-400:])
    return root


@posix_only("uv writes the sh polyglot for POSIX shebangs; Windows has launcher executables")
def test_the_polyglot_the_tests_build_is_the_one_uv_writes(uv_installed_venv: Path) -> None:
    """Pins the fixture to the tool: the first three lines (the part that names the
    interpreter) are the same text, so the other tests build what uv builds."""
    real = (uv_installed_venv / "bin" / "tiny-hook").read_text(encoding="utf-8").splitlines()
    mine = uv_polyglot_script(uv_installed_venv / "bin" / "python").splitlines()
    assert real[:3] == mine[:3], real[:3]
    assert real[0] == "#!/bin/sh" and real[1].startswith("'''exec' '"), real[:2]


@posix_only("uv writes the sh polyglot for POSIX shebangs; Windows has launcher executables")
def test_a_pruned_venv_with_a_script_uv_wrote_for_a_long_path_cannot_start_and_does_not_start_either(
    lookup, uv_installed_venv: Path, tmp_path: Path,
) -> None:
    """Real uv, a venv path over 127 characters, its Python pruned (``prune_python``).
    The script runs the shell, which exits 126 or 127; ``cannot_start`` says so first."""
    script = uv_installed_venv / "bin" / "tiny-hook"
    assert lookup.cannot_start(str(script), windows=False) is None, "a working venv starts"
    assert subprocess.run([str(script)], capture_output=True, timeout=60, check=False).returncode == 0
    gone = prune_python(uv_installed_venv, tmp_path / "uv" / "cpython-gone")
    started = subprocess.run([str(script)], capture_output=True, timeout=60, check=False)
    assert started.returncode in (126, 127), started.returncode  # not an OSError: the shell ran and failed
    reason = lookup.cannot_start(str(script), windows=False)
    assert reason is not None and str(gone) in reason, reason


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_naming_a_missing_interpreter_cannot_start(lookup, tmp_path: Path) -> None:
    """The script sits in a directory with no ``pyvenv.cfg`` around it (a ``uv tool``
    bin directory), so the script's own second line is the whole of the evidence."""
    gone = long_dir(tmp_path) / "bin" / "python"
    exe = write_uv_polyglot(tmp_path / "tool-bin" / "nx-hook", gone)
    reason = lookup.cannot_start(str(exe), windows=False)
    assert reason is not None and str(gone) in reason, reason


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_naming_a_dangling_link_cannot_start_even_while_pyvenv_cfg_home_is_there(
    lookup, tmp_path: Path,
) -> None:
    """The state the journey found: the venv's ``home`` exists, so ``pyvenv.cfg`` says
    nothing, and ``bin/python`` dangles. Only the polyglot's line can say it."""
    home = tmp_path / "uv" / "cpython-3.12"
    home.mkdir(parents=True)
    gen = _venv(long_dir(tmp_path / "gens"), home=home)
    link = gen / "bin" / "python"
    os.symlink(tmp_path / "nowhere" / "python3.12", link)
    exe = write_uv_polyglot(gen / "bin" / "nx-hook", link)
    reason = lookup.cannot_start(str(exe), windows=False)
    assert reason is not None and str(link) in reason and "is missing" in reason, reason


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_naming_an_interpreter_that_exists_can_start(lookup, tmp_path: Path) -> None:
    exe = write_uv_polyglot(long_dir(tmp_path) / "bin" / "nx-hook", sys.executable)
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_naming_a_file_that_is_not_executable_cannot_start(lookup, tmp_path: Path) -> None:
    interp = tmp_path / "python"
    interp.write_text("not a program", encoding="utf-8")
    interp.chmod(0o644)
    exe = write_uv_polyglot(tmp_path / "bin" / "nx-hook", interp)
    assert "not executable" in (lookup.cannot_start(str(exe), windows=False) or "")


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_path_with_a_space_and_a_quote_is_read_back_whole(lookup, tmp_path: Path) -> None:
    """uv writes an embedded quote as ``'"'"'``; a path with a space is the other reason
    uv writes a polyglot at all."""
    alive_dir = tmp_path / "it's a dir"
    alive_dir.mkdir()
    alive = alive_dir / "python"
    alive.symlink_to(Path(sys.executable))
    assert "'\"'\"'" in uv_polyglot_script(alive), "the fixture quotes the way uv does"
    assert lookup.cannot_start(str(write_uv_polyglot(tmp_path / "bin" / "a", alive)), windows=False) is None
    dead = alive_dir / "pruned" / "python"
    reason = lookup.cannot_start(str(write_uv_polyglot(tmp_path / "bin" / "b", dead)), windows=False)
    assert reason is not None and str(dead) in reason, reason


@posix_only("a polyglot's second line names the interpreter the shell execs")
def test_a_polyglot_written_with_double_quotes_the_way_pip_does_is_read_too(lookup, tmp_path: Path) -> None:
    dead = tmp_path / "pruned" / "python"
    exe = tmp_path / "bin" / "nx-hook"
    exe.parent.mkdir()
    exe.write_text(f"#!/bin/sh\n'''exec' \"{dead}\" \"$0\" \"$@\"\n' '''\nimport sys\n", encoding="utf-8")
    exe.chmod(0o755)
    assert str(dead) in (lookup.cannot_start(str(exe), windows=False) or "")


@posix_only("a polyglot's second line names the interpreter the shell execs")
@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\necho hi\n",                                    # a plain sh script
        "#!/bin/sh\n",                                              # no second line
        "#!/bin/sh\n'''exec' '/nowhere/python\n' '''\n",            # an unterminated quote
        "#!/bin/sh\n'''exec' python3 \"$0\" \"$@\"\n' '''\n",         # relative: not judged
        "#!/bin/sh\n'''exec' \n' '''\n",                            # no interpreter word
        "#!/bin/sh\n'''exec' '/nowhere/python' \"$0\"",             # the line never ends
        "#!/bin/sh -e\n'''exec' '/nowhere/python' \"$0\" \"$@\"\n",  # an sh with an option is not uv's launcher
    ],
    ids=["plain-sh", "one-line", "unterminated", "relative", "no-word", "no-newline", "sh-with-option"],
)
def test_an_sh_script_that_is_not_the_polyglot_is_not_judged(lookup, tmp_path: Path, text: str) -> None:
    exe = tmp_path / "bin" / "nx-hook"
    exe.parent.mkdir()
    exe.write_text(text, encoding="utf-8")
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("links and a polyglot")
def test_a_polyglot_in_a_pruned_generation_at_a_long_path_cannot_start(lookup, tmp_path: Path) -> None:
    """The whole journey shape: a generation at a path over 127 characters whose scripts
    are polyglots, its Python pruned the way ``prune_python`` prunes it."""
    gen = _venv(long_dir(tmp_path / "tools") / "gen-20260101T000000Z", home=None, with_cfg=False)
    gone = prune_python(gen, tmp_path / "uv" / "cpython-3.12-gone")
    exe = write_uv_polyglot(gen / "bin" / "nx-hook", gen / "bin" / "python")
    reason = lookup.cannot_start(str(exe), windows=False)
    assert reason is not None and str(gone) in reason, reason
