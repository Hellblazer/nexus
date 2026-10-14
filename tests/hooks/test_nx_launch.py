# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/launch/nx_launch.py: one script every server and hook entry goes through
(RDR-228 Phase 1 Step 1, nexus-sv2wx.5).

The launcher finds a CLI program and runs it, or handles its absence without an
error. These cases drive the real script as a subprocess, the way hooks.json and
.mcp.json will run it, against fake programs in a fake generation layout
(``NX_TOOLS_DIR``) and on a fake ``PATH``. Every child environment sets
``NX_TOOLS_DIR``: without it the lookup reads the developer's own install.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from tests._platform import IS_WINDOWS, install_windows_exe_fake, posix_only, prune_python, scrubbed_env

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCH = _ROOT / "conexus" / "launch" / "nx_launch.py"
_SCRIPTS = _ROOT / "conexus" / "hooks" / "scripts"
_LOOKUP = _SCRIPTS / "_cli_lookup.py"
_HOOKS_JSON = _ROOT / "conexus" / "hooks" / "hooks.json"

_HOOK_PROGRAMS = ("nx-hook", "nx-session-end-launcher")
_SERVER_PROGRAMS = ("nx-mcp", "nx-mcp-catalog")

#: A fake CLI program. It reports how it was started as one JSON line and exits
#: with ``FAKE_EXIT``. ``argv0`` is the path it was started by, as given.
_FAKE = """#!{python}
import json, os, sys
data = sys.stdin.buffer.read() if os.environ.get("FAKE_READ_STDIN") else b""
sys.stdout.write(json.dumps({{
    "marker": {marker!r},
    "argv0": sys.argv[0],
    "args": sys.argv[1:],
    "cwd": os.path.realpath(os.getcwd()),
    "stdin": data.decode("latin-1"),
    "setup_script": os.environ.get("NX_SETUP_SCRIPT"),
}}))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def _install_fake(bindir: Path, name: str, marker: str) -> Path:
    """Write a fake *name* into *bindir*; returns the file a lookup resolves.

    POSIX: a shebang script. Windows cannot execute one and the lookup refuses
    a batch file, so the fake is a real ``<name>.exe`` running
    ``<name>-script.py`` beside it (``install_windows_exe_fake``), which keeps
    the directory the launcher started it through visible in ``argv0``.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    source = _FAKE.format(python=sys.executable, marker=marker)
    if not IS_WINDOWS:
        fake = bindir / name
        fake.write_text(source)
        fake.chmod(0o755)
        return fake
    return install_windows_exe_fake(bindir, name, _FAKE.format(python=f'"{sys.executable}"', marker=marker))


def _point_current(tools: Path, generation: Path) -> None:
    """``<tools>/current`` -> *generation*: a symlink, or a junction on Windows."""
    link = tools / "current"
    if IS_WINDOWS:
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(generation), str(link))
    else:
        os.symlink(generation, link)


def _layout(tmp_path: Path, *names: str, marker: str = "layout") -> tuple[Path, Path]:
    """A tools root with one generation holding fakes for *names*. Returns
    ``(tools, generation)``."""
    tools = tmp_path / "tools"
    gen = tools / "gen-20260101T000000Z"
    gen.mkdir(parents=True)
    for name in names:
        _install_fake(gen / "bin", name, marker)
    _point_current(tools, gen)
    return tools, gen


def _empty(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


def _run(
    argv: list[str], *, tools: Path, path: Path, cwd: Path | None = None,
    payload: bytes = b"", **extra: str,
) -> subprocess.CompletedProcess[bytes]:
    env = scrubbed_env(PATH=str(path), NX_TOOLS_DIR=str(tools), **extra)
    return subprocess.run(
        [sys.executable, str(_LAUNCH), *argv],
        input=payload, capture_output=True, env=env, timeout=30, check=False,
        cwd=str(cwd) if cwd else None,
    )


def _report(r: subprocess.CompletedProcess[bytes]) -> dict:
    assert r.stdout, f"the program printed nothing: rc={r.returncode} stderr={r.stderr!r}"
    return json.loads(r.stdout)


def _same_file(a: str, b: Path) -> bool:
    """Path equality that sees through Windows case and the ``<name>.exe`` /
    ``<name>-script.py`` pair of a Windows fake."""
    def stem(path: str) -> str:
        # The executable fake reports its script by an extended-length path.
        path = path.removeprefix("\\\\?\\")
        return os.path.normcase(os.path.splitext(path)[0]).removesuffix("-script")

    return stem(a) == stem(str(b))


# -- standard library only, no nexus ----------------------------------------------


@pytest.mark.parametrize("script", [_LAUNCH, _LOOKUP], ids=lambda p: p.name)
def test_it_imports_only_the_standard_library_and_its_siblings(script: Path) -> None:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):  # every scope: a deferred nexus import is refused too
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{script.name} has a relative import"
            found.add((node.module or "").split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__", "_exec_path", "_cli_lookup", "nx_standin"}, found


def test_it_runs_with_nexus_unimportable(tmp_path: Path) -> None:
    """The launcher has to run on a machine with no CLI at all. ``nexus`` is
    poisoned in ``sys.modules`` so any import of it raises, then the script is
    run as ``__main__``."""
    program = (
        "import runpy, sys\n"
        "sys.modules['nexus'] = None\n"
        f"sys.argv = [{str(_LAUNCH)!r}, 'nx-hook', 'preflight']\n"
        f"runpy.run_path({str(_LAUNCH)!r}, run_name='__main__')\n"
    )
    env = scrubbed_env(PATH=str(_empty(tmp_path, "bin")), NX_TOOLS_DIR=str(_empty(tmp_path, "tools")))
    r = subprocess.run(
        [sys.executable, "-c", program], input=b"{}", capture_output=True, env=env, timeout=30, check=False,
    )
    assert r.returncode == 0, r.stderr
    assert b"`nx-hook` is not installed" in r.stderr


# -- lookup order ------------------------------------------------------------------


def test_the_layout_is_searched_before_path(tmp_path: Path) -> None:
    tools, gen = _layout(tmp_path, "nx-hook", marker="layout")
    _install_fake(tmp_path / "bin", "nx-hook", "path")
    r = _run(["nx-hook", "preflight"], tools=tools, path=tmp_path / "bin", payload=b"{}")
    assert _report(r)["marker"] == "layout"


def test_path_is_used_when_the_layout_has_no_such_program(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-mcp")
    _install_fake(tmp_path / "bin", "nx-hook", "path")
    r = _run(["nx-hook", "preflight"], tools=tools, path=tmp_path / "bin", payload=b"{}")
    assert _report(r)["marker"] == "path"


def test_path_is_used_when_there_is_no_layout_at_all(tmp_path: Path) -> None:
    _install_fake(tmp_path / "bin", "nx-hook", "path")
    r = _run(["nx-hook", "preflight"], tools=tmp_path / "no-tools", path=tmp_path / "bin", payload=b"{}")
    assert _report(r)["marker"] == "path"


def test_the_current_directory_is_never_searched(tmp_path: Path) -> None:
    """A program planted in the cwd only is not a CLI. On Windows a bare-name
    spawn and ``shutil.which`` both look there first."""
    planted = tmp_path / "cloned-repo"
    _install_fake(planted, "nx-hook", "planted")
    r = _run(
        ["nx-hook", "preflight"], tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"),
        cwd=planted, payload=b"{}",
    )
    assert r.returncode == 0
    assert r.stdout == b""
    assert b"`nx-hook` is not installed" in r.stderr


@posix_only("shutil.which returns a relative hit for a relative PATH entry; the Windows walk already skips such an entry")
def test_a_relative_path_entry_is_never_used(tmp_path: Path) -> None:
    """The launcher changes directory to the project before it starts the program.
    A relative hit found from the plugin root would then name the project's file."""
    plugin_root, project = _empty(tmp_path, "plugin"), _empty(tmp_path, "project")
    _install_fake(plugin_root / "rel", "nx-hook", "plugin-root")
    _install_fake(project / "rel", "nx-hook", "planted")
    env = scrubbed_env(PATH="rel", NX_TOOLS_DIR=str(_empty(tmp_path, "tools")), CLAUDE_PROJECT_DIR=str(project))
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-hook", "preflight"],
        input=b"{}", capture_output=True, env=env, timeout=30, check=False, cwd=str(plugin_root),
    )
    assert r.returncode == 0
    assert r.stdout == b"", "a program reached through a relative PATH entry was run"
    assert b"`nx-hook` is not installed" in r.stderr


def test_a_name_with_a_directory_part_is_refused(tmp_path: Path) -> None:
    planted = _install_fake(tmp_path / "elsewhere", "nx-hook", "planted")
    r = _run([str(planted), "preflight"], tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"))
    assert r.returncode == 1
    assert r.stdout == b""


# -- the hand-off ------------------------------------------------------------------


def test_a_found_program_gets_its_arguments_and_its_exit_code_comes_back(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-mcp")
    r = _run(["nx-mcp", "--flag", "two words", ""], tools=tools, path=_empty(tmp_path, "bin"), FAKE_EXIT="7")
    assert r.returncode == 7
    assert _report(r)["args"] == ["--flag", "two words", ""]


def test_the_program_is_started_by_its_generation_path_not_through_current(tmp_path: Path) -> None:
    """``layout_core`` (WHY THE SHIM READS THE POINTER BEFORE IT EXECS): a start
    through ``<tools>/current`` can carry the pointer into the running process,
    and the next flip then retargets it. The launcher resolves the pointer first."""
    tools, gen = _layout(tmp_path, "nx-mcp")
    argv0 = _report(_run(["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin")))["argv0"]
    assert "current" not in Path(argv0).parts, argv0
    assert _same_file(argv0, gen / "bin" / "nx-mcp"), argv0


@posix_only("a venv interpreter reached through a symlinked directory; Windows launchers embed their generation's python path")
def test_the_measured_mechanism_a_console_script_carries_no_current_in_sys_prefix(tmp_path: Path) -> None:
    """The measured fact behind the test above, with its control. An interpreter
    started THROUGH ``current`` takes ``current`` into ``sys.prefix``; a console
    script does not, because its shebang names the generation's interpreter.

    This records the mechanism. It does NOT pin the launcher: it passes with the
    launcher starting programs through ``current``. The test above is the pin."""
    tools = tmp_path / "tools"
    gen = tools / "gen-20260101T000000Z"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(gen)], check=True, timeout=120)
    _point_current(tools, gen)
    script = gen / "bin" / "nx-mcp"
    script.write_text(f"#!{gen}/bin/python\nimport json, sys\nprint(json.dumps([sys.prefix, *sys.path]))\n")
    script.chmod(0o755)

    control = subprocess.run(
        [str(tools / "current" / "bin" / "python"), "-c", "import sys; print(sys.prefix)"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert "current" in Path(control.stdout.strip()).parts, (
        "the control did not show the leak, so this test can no longer tell a leak from none"
    )

    r = _run(["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"))
    seen = json.loads(r.stdout)
    assert seen, r.stderr
    assert not [p for p in seen if "current" in Path(p).parts], seen


# -- the project directory ---------------------------------------------------------
#
# The entries start the launcher with `uv tool run --directory ${CLAUDE_PLUGIN_ROOT}`,
# so its cwd is the plugin root. The programs it starts read the project from their
# own cwd, so the launcher hands them the project, as nx_hook_shim.py does.


def test_a_hook_program_runs_in_the_payload_cwd_and_gets_the_payload(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-hook")
    plugin_root, project, other = (_empty(tmp_path, n) for n in ("plugin", "project", "other"))
    payload = json.dumps({"cwd": str(project), "prompt": "café"}).encode()
    r = _run(
        ["nx-hook", "session-start"], tools=tools, path=_empty(tmp_path, "bin"), cwd=plugin_root,
        payload=payload, CLAUDE_PROJECT_DIR=str(other), FAKE_READ_STDIN="1",
    )
    seen = _report(r)
    assert seen["cwd"] == os.path.realpath(project)
    assert seen["stdin"].encode("latin-1") == payload


def test_a_hook_program_falls_back_to_claude_project_dir(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-hook")
    plugin_root, project = _empty(tmp_path, "plugin"), _empty(tmp_path, "project")
    for payload in (b"{}", b"not json", json.dumps({"cwd": str(tmp_path / "gone")}).encode()):
        r = _run(
            ["nx-hook", "rdr"], tools=tools, path=_empty(tmp_path, "bin"), cwd=plugin_root,
            payload=payload, CLAUDE_PROJECT_DIR=str(project),
        )
        assert _report(r)["cwd"] == os.path.realpath(project), payload


def test_a_hook_program_inherits_the_cwd_when_no_project_is_known(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-session-end-launcher")
    plugin_root = _empty(tmp_path, "plugin")
    r = _run(["nx-session-end-launcher"], tools=tools, path=_empty(tmp_path, "bin"), cwd=plugin_root, payload=b"{}")
    assert _report(r)["cwd"] == os.path.realpath(plugin_root)


def test_a_server_runs_in_claude_project_dir_and_its_stdin_is_untouched(tmp_path: Path) -> None:
    """A server's standard input is the protocol stream. The launcher must not
    read it: whatever the application writes reaches the server."""
    tools, _gen = _layout(tmp_path, "nx-mcp")
    plugin_root, project = _empty(tmp_path, "plugin"), _empty(tmp_path, "project")
    stream = json.dumps({"cwd": str(tmp_path / "other"), "jsonrpc": "2.0"}).encode()
    r = _run(
        ["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), cwd=plugin_root,
        payload=stream, CLAUDE_PROJECT_DIR=str(project), FAKE_READ_STDIN="1",
    )
    seen = _report(r)
    assert seen["cwd"] == os.path.realpath(project)
    assert seen["stdin"].encode("latin-1") == stream


# -- no CLI ------------------------------------------------------------------------


def _hook_invocations() -> list[tuple[str, ...]]:
    """Every (program, *args) hooks.json runs from the CLI: an entry that names a
    hook program directly, or one that reaches it through the launcher."""
    out: set[tuple[str, ...]] = set()
    data = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    for groups in data["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook.get("type") != "command":
                    continue
                args = list(hook.get("args", []))
                if hook["command"] in _HOOK_PROGRAMS:
                    out.add((hook["command"], *args))
                for i, arg in enumerate(args):
                    if arg.endswith("launch/nx_launch.py"):
                        out.add(tuple(args[i + 1:]))
    return sorted(out)


def test_the_hook_census_found_every_hook_program_entry() -> None:
    found = _hook_invocations()
    assert len(found) >= 7, found
    assert {inv[0] for inv in found} == set(_HOOK_PROGRAMS), found


@pytest.mark.parametrize("invocation", _hook_invocations(), ids=" ".join)
def test_no_cli_and_a_hook_program_is_exit_0_with_one_line(tmp_path: Path, invocation: tuple[str, ...]) -> None:
    r = _run(list(invocation), tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"), payload=b"{}")
    assert r.returncode == 0
    assert r.stdout == b""
    lines = r.stderr.decode().splitlines()
    assert len(lines) == 1, lines
    assert f"`{invocation[0]}` is not installed" in lines[0]
    assert "nx self install" not in lines[0]


@pytest.mark.parametrize("name", _SERVER_PROGRAMS)
def test_no_cli_and_a_server_program_goes_to_the_stand_in(tmp_path: Path, name: str) -> None:
    """The stand-in serves until its input closes, then exits 0, and writes
    nothing to standard error. tests/hooks/test_nx_standin.py drives it with a
    real client."""
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
    r = _run([name], tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"), payload=request)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert r.stderr == b""


def test_no_cli_and_any_other_program_is_exit_127(tmp_path: Path) -> None:
    r = _run(["nx", "doctor"], tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"))
    assert r.returncode == 127
    assert len(r.stderr.decode().splitlines()) == 1, r.stderr


def test_no_argument_is_a_usage_line_and_exit_1(tmp_path: Path) -> None:
    r = _run([], tools=_empty(tmp_path, "tools"), path=_empty(tmp_path, "bin"))
    assert r.returncode == 1
    assert r.stdout == b""
    assert len(r.stderr.decode().splitlines()) == 1, r.stderr


@posix_only("a file with the exec bit and no program in it; Windows decides by extension")
def test_a_program_the_system_will_not_execute_skips_a_hook(tmp_path: Path) -> None:
    tools, gen = _layout(tmp_path)
    broken = gen / "bin" / "nx-hook"
    broken.parent.mkdir(exist_ok=True)
    broken.write_bytes(b"")
    broken.chmod(0o755)
    r = _run(["nx-hook", "preflight"], tools=tools, path=_empty(tmp_path, "bin"), payload=b"{}")
    assert r.returncode == 0
    assert r.stdout == b""
    lines = r.stderr.decode().splitlines()
    assert len(lines) == 1 and "could not start" in lines[0], lines


@posix_only("a file with the exec bit and no program in it; Windows decides by extension")
def test_a_server_the_system_will_not_execute_is_served_the_installed_but_cannot_start_stand_in(
    tmp_path: Path,
) -> None:
    """Sam, 2026-10-14: before, exit 1 and no stand-in, so the application showed
    a failed server and said nothing. Now the stand-in tells the model and the user."""
    tools, gen = _layout(tmp_path)
    broken = gen / "bin" / "nx-mcp"
    broken.parent.mkdir(exist_ok=True)
    broken.write_bytes(b"")
    broken.chmod(0o755)
    r = _run(["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), payload=_INITIALIZE)
    assert r.returncode == 0
    assert r.stderr == b""
    assert _BROKEN_PHRASE in json.loads(r.stdout.splitlines()[0])["result"]["instructions"]


# -- the CLI is found and cannot start (Sam, 2026-10-14) -------------------------------------

_BROKEN_PHRASE = "Nexus is installed but cannot start; setup repairs it"
_INITIALIZE = (
    json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }).encode() + b"\n"
    + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode() + b"\n"
)


def _broken_layout(tmp_path: Path, *names: str) -> tuple[Path, Path]:
    """A generation holding fakes for *names*, whose ``pyvenv.cfg`` names a Python
    that is gone: what ``uv`` leaves when it prunes the interpreter. The same on
    POSIX and Windows, where the fake is a launcher ``.exe`` with no shebang."""
    tools, gen = _layout(tmp_path, *names)
    prune_python(gen, tmp_path / "uv" / "cpython-3.12-pruned")
    return tools, gen


@pytest.mark.parametrize("invocation", _hook_invocations(), ids=" ".join)
def test_a_hook_whose_cli_cannot_start_is_exit_0_with_one_line(tmp_path: Path, invocation: tuple[str, ...]) -> None:
    tools, _gen = _broken_layout(tmp_path, *_HOOK_PROGRAMS)
    r = _run(list(invocation), tools=tools, path=_empty(tmp_path, "bin"), payload=b"{}")
    assert r.returncode == 0
    assert r.stdout == b"", "the broken program must not have run"
    lines = r.stderr.decode().splitlines()
    assert len(lines) == 1, lines
    assert "could not start" in lines[0] and "cpython-3.12-pruned" in lines[0], lines
    assert "is not installed" not in lines[0], "it is installed"


def test_nx_mcp_whose_cli_cannot_start_is_served_the_stand_in_that_says_so(tmp_path: Path) -> None:
    tools, _gen = _broken_layout(tmp_path, *_SERVER_PROGRAMS)
    call = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "setup_status", "arguments": {}}})
    r = _run(
        ["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), payload=_INITIALIZE + call.encode() + b"\n",
        NEXUS_CONFIG_DIR=str(_empty(tmp_path, "config")), HOME=str(_empty(tmp_path, "home")),
        USERPROFILE=str(tmp_path / "home"),
    )
    assert r.returncode == 0
    assert r.stderr == b"", "a stdio server's standard error is recorded as an error by the application"
    init, listed, status = (json.loads(line) for line in r.stdout.splitlines())
    assert _BROKEN_PHRASE in init["result"]["instructions"]
    assert "cpython-3.12-pruned" in init["result"]["instructions"]
    assert "Nexus is not installed" not in init["result"]["instructions"]
    tools_listed = {t["name"]: t for t in listed["result"]["tools"]}
    assert sorted(tools_listed) == ["setup", "setup_status"]
    for tool in tools_listed.values():
        assert tool["description"].startswith(_BROKEN_PHRASE), "Desktop shows the model the descriptions only"
    text = status["result"]["content"][0]["text"]
    assert text.startswith(_BROKEN_PHRASE) and "Nexus is not installed" not in text
    assert "ask the user first" in text.lower()


def test_nx_mcp_catalog_whose_cli_cannot_start_is_the_stand_in_with_no_tools(tmp_path: Path) -> None:
    tools, _gen = _broken_layout(tmp_path, *_SERVER_PROGRAMS)
    r = _run(["nx-mcp-catalog"], tools=tools, path=_empty(tmp_path, "bin"), payload=_INITIALIZE)
    assert r.returncode == 0 and r.stderr == b""
    init, listed = (json.loads(line) for line in r.stdout.splitlines())
    assert "instructions" not in init["result"] and listed["result"]["tools"] == []


def test_any_other_program_whose_cli_cannot_start_fails_with_one_line(tmp_path: Path) -> None:
    tools, _gen = _broken_layout(tmp_path, "nx")
    r = _run(["nx", "doctor"], tools=tools, path=_empty(tmp_path, "bin"))
    assert r.returncode == 1 and r.stdout == b""
    assert len(r.stderr.decode().splitlines()) == 1, r.stderr


def test_a_working_cli_beside_a_broken_one_on_path_is_not_chosen(tmp_path: Path) -> None:
    """The layout is searched first, and a layout hit that cannot start is the
    answer: serving the stand-in and not a `PATH` program is the same fact the
    setup hook states, and `setup` repairs the layout."""
    tools, _gen = _broken_layout(tmp_path, "nx-hook")
    _install_fake(tmp_path / "bin", "nx-hook", "path")
    r = _run(["nx-hook", "preflight"], tools=tools, path=tmp_path / "bin", payload=b"{}")
    assert r.stdout == b"" and b"could not start" in r.stderr


@posix_only("a shebang names the interpreter the kernel starts")
@pytest.mark.parametrize("name", ["nx-hook", "nx-mcp"])
def test_a_script_whose_shebang_interpreter_is_gone_cannot_start(tmp_path: Path, name: str) -> None:
    """The POSIX shape of the pruned interpreter: a console script whose shebang
    names the generation's python, a link `uv` left dangling."""
    tools, gen = _layout(tmp_path)
    gone = tmp_path / "uv" / "cpython-3.12-pruned" / "bin" / "python3.12"
    link = gen / "bin" / "python"
    link.parent.mkdir(exist_ok=True)
    os.symlink(gone, link)
    script = gen / "bin" / name
    script.write_text(f"#!{link}\nprint('never run')\n")
    script.chmod(0o755)
    r = _run([name], tools=tools, path=_empty(tmp_path, "bin"), payload=_INITIALIZE)
    assert r.returncode == 0
    assert b"never run" not in r.stdout
    if name == "nx-hook":
        assert r.stdout == b"" and b"could not start" in r.stderr and str(link).encode() in r.stderr
    else:
        assert _BROKEN_PHRASE in json.loads(r.stdout.splitlines()[0])["result"]["instructions"]


def test_a_dangling_current_falls_through_to_path(tmp_path: Path) -> None:
    """The pointer names a generation that was reaped: the ordinary state between
    a GC pass and the next flip."""
    tools = _empty(tmp_path, "tools")
    gone = tools / "gen-20250101T000000Z"
    gone.mkdir()
    _point_current(tools, gone)
    gone.rmdir()
    _install_fake(tmp_path / "bin", "nx-hook", "path")
    r = _run(["nx-hook", "preflight"], tools=tools, path=tmp_path / "bin", payload=b"{}")
    assert _report(r)["marker"] == "path"


# -- the tools root: one rule, two statements --------------------------------------


def _lookup() -> types.ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_cli_lookup_under_test", _LOOKUP)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw", [None, "", "   ", "~", "~/gens", "ABS", "relative/tools", "~other/tools"])
def test_the_tools_root_agrees_with_layout_core(
    raw: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_cli_lookup`` restates ``layout_core.tools_dir`` because it cannot import
    it. Where the layout refuses an override the lookup has no layout to search."""
    from nexus import install_layout  # noqa: PLC0415 -- the reference statement

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if raw is None:
        monkeypatch.delenv("NX_TOOLS_DIR", raising=False)
    else:
        monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "abs") if raw == "ABS" else raw)
    try:
        expected: str | None = str(install_layout.tools_dir())
    except install_layout.InstallLayoutError:
        expected = None
    assert _lookup().tools_dir() == expected


# -- the entries as hooks.json writes them (RDR-228 Phase 1 Step 4, nexus-sv2wx.8) --


@pytest.mark.parametrize("invocation", _hook_invocations(), ids=" ".join)
def test_with_a_cli_every_hook_entry_reaches_its_program_and_verb(tmp_path: Path, invocation: tuple[str, ...]) -> None:
    tools, _gen = _layout(tmp_path, invocation[0])
    r = _run(list(invocation), tools=tools, path=_empty(tmp_path, "bin"), payload=b"{}")
    assert r.returncode == 0, r.stderr
    seen = _report(r)
    assert seen["marker"] == "layout"
    assert seen["args"] == list(invocation[1:])


def _entries(event: str) -> list[dict]:
    data = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    return [hook for group in data["hooks"][event] for hook in group["hooks"]]


@posix_only("builds a PATH that holds uv alone by symlink; the Windows first session is in the Phase 1 gate")
def test_a_session_with_no_cli_says_one_thing_and_writes_nothing(tmp_path: Path) -> None:
    """Every SessionStart entry and then the SessionEnd entry, run in order exactly
    as hooks.json writes them, through the real ``uv``, on a machine with no CLI:
    no error, at most one line on standard error each, one message for the model,
    and nothing written under the Nexus config directory or the tools directory.
    (uv keeps its own cache; that is outside this pin.)"""
    import shutil

    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; it starts every entry, so this is a failure, not a skip"
    entries = _entries("SessionStart")
    assert len(entries) == 9, [e["args"][-2:] for e in entries]
    session_end = _entries("SessionEnd")
    assert len(session_end) == 1 and session_end[0]["args"][-1] == "nx-session-end-launcher", session_end
    entries = entries + session_end
    assert entries[0]["args"][-1].endswith("/hooks/scripts/nx_setup_notice.py"), "the setup notice is the first entry"

    def uv_says(*args: str) -> str:
        return subprocess.run([uv, *args], capture_output=True, text=True, check=True, timeout=60).stdout.strip()

    home, config, tools, project = (_empty(tmp_path, n) for n in ("home", "config", "tools", "project"))
    only_uv = _empty(tmp_path, "only-uv")
    os.symlink(uv, only_uv / "uv")
    plugin = tmp_path / "plugin"
    for sub in ("hooks", "launch", ".claude-plugin"):
        shutil.copytree(_ROOT / "conexus" / sub, plugin / sub, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        "PATH": os.pathsep.join([str(only_uv), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "NEXUS_CONFIG_DIR": str(config),
        "NX_TOOLS_DIR": str(tools),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        # uv's own interpreters and cache stay where this machine keeps them.
        "UV_PYTHON_INSTALL_DIR": uv_says("python", "dir"),
        "UV_CACHE_DIR": uv_says("cache", "dir"),
    }
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(project), "session_id": "no-cli"})
    spoke: list[str] = []
    for entry in entries:
        argv = [entry["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for a in entry["args"])]
        r = subprocess.run(argv, input=payload, capture_output=True, text=True, env=env, timeout=120, check=False, cwd=str(project))
        what = " ".join(entry["args"][-2:])
        assert r.returncode == 0, (what, r.stderr)
        assert "Traceback" not in r.stderr and "not found in" not in r.stderr, (what, r.stderr)
        assert len(r.stderr.splitlines()) <= 1, (what, r.stderr)
        if any(a.endswith("/launch/nx_launch.py") for a in entry["args"]):
            assert "is not installed" in r.stderr and not r.stdout, (what, r.stdout, r.stderr)
        if r.stdout.strip():
            spoke.append(what)
            assert "Nexus is not installed" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(spoke) == 1 and spoke[0].endswith("nx_setup_notice.py"), spoke
    written = sorted(str(p.relative_to(tmp_path)) for base in (config, tools) for p in base.rglob("*"))
    assert written == [], written


# -- NX_SETUP_SCRIPT: the real nx-mcp reaches the setup process through the launcher ----------
#
# RDR-228 Phase 2 Step 3 (nexus-sv2wx.16). The real server's ``setup`` tool starts the
# file this variable names, so the setup process has one copy, in the plugin. The
# launcher sets it when it hands off to ``nx-mcp`` and reads none of the setup directory.

_NX_SETUP = _ROOT / "conexus" / "launch" / "nx_setup.py"


def _setup_dir_states(tmp_path: Path) -> dict[str, Path]:
    """One config directory per state the setup directory can be in. Returns
    ``{state: config dir}``."""
    states: dict[str, Path] = {}
    states["no directory"] = _empty(tmp_path, "cfg-none")
    marker = _empty(tmp_path, "cfg-marker")
    (marker / "setup").mkdir()
    (marker / "setup" / "not-now").write_text("2026-01-01T00:00:00Z", encoding="utf-8")
    states["a not-now marker"] = marker
    failed = _empty(tmp_path, "cfg-failed")
    (failed / "setup").mkdir()
    (failed / "setup" / "status.json").write_text(json.dumps({"state": "failed", "stage": "build"}), encoding="utf-8")
    states["a failed status"] = failed
    stale = _empty(tmp_path, "cfg-stale")
    (stale / "setup" / "lock").mkdir(parents=True)
    (stale / "setup" / "lock" / "owner").write_text("a-dead-token", encoding="utf-8")
    heartbeat = stale / "setup" / "lock" / "heartbeat"
    heartbeat.write_text("x", encoding="utf-8")
    os.utime(heartbeat, (1_000_000_000, 1_000_000_000))
    (stale / "setup" / "status.json").write_text(json.dumps({"state": "running", "stage": "init"}), encoding="utf-8")
    states["a stale lock"] = stale
    return states


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    out: dict[str, tuple[int, bytes | None]] = {}
    for p in sorted(root.rglob("*")):
        out[str(p.relative_to(root))] = (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
    return out


@pytest.mark.parametrize("state", ["no directory", "a not-now marker", "a failed status", "a stale lock"])
def test_the_real_nx_mcp_is_handed_the_setup_script_in_every_state_of_the_setup_directory(
    tmp_path: Path, state: str,
) -> None:
    config = _setup_dir_states(tmp_path)[state]
    tools, _gen = _layout(tmp_path, "nx-mcp")
    before = _tree(config)
    r = _run(["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), NEXUS_CONFIG_DIR=str(config))
    report = _report(r)
    assert report["marker"] == "layout", "the real program must run, not the stand-in"
    assert report["setup_script"] is not None
    assert os.path.samefile(report["setup_script"], _NX_SETUP)
    assert os.path.isabs(report["setup_script"])
    assert _tree(config) == before, "the launcher read or wrote the setup directory"


def test_a_value_inherited_from_the_environment_is_replaced(tmp_path: Path) -> None:
    tools, _gen = _layout(tmp_path, "nx-mcp")
    r = _run(
        ["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), NEXUS_CONFIG_DIR=str(_empty(tmp_path, "cfg")),
        NX_SETUP_SCRIPT=str(tmp_path / "elsewhere.py"),
    )
    assert os.path.samefile(_report(r)["setup_script"], _NX_SETUP)


def test_the_script_is_found_from_a_relative_launch_and_a_project_directory(tmp_path: Path) -> None:
    """The launcher moves to the project before it starts the program; the path it
    hands on is absolute and does not depend on that move."""
    tools, _gen = _layout(tmp_path, "nx-mcp")
    project = _empty(tmp_path, "project")
    r = _run(
        ["nx-mcp"], tools=tools, path=_empty(tmp_path, "bin"), cwd=_LAUNCH.parent,
        NEXUS_CONFIG_DIR=str(_empty(tmp_path, "cfg")), CLAUDE_PROJECT_DIR=str(project),
    )
    report = _report(r)
    assert os.path.isabs(report["setup_script"]) and os.path.samefile(report["setup_script"], _NX_SETUP)
    assert os.path.samefile(report["cwd"], project)


@pytest.mark.parametrize("program", ["nx-mcp-catalog", "nx-hook", "nx-session-end-launcher"])
def test_no_other_program_is_handed_the_setup_script(tmp_path: Path, program: str) -> None:
    tools, _gen = _layout(tmp_path, program)
    r = _run([program], tools=tools, path=_empty(tmp_path, "bin"), NEXUS_CONFIG_DIR=str(_empty(tmp_path, "cfg")))
    assert _report(r)["setup_script"] is None


def test_a_plugin_copy_without_the_script_hands_on_no_path(tmp_path: Path) -> None:
    """The variable names a file this plugin ships. If the file is not there the
    real server must find the variable unset, and answer with the terminal commands."""
    plugin = tmp_path / "plugin"
    for sub in ("hooks/scripts", "launch"):
        shutil.copytree(_ROOT / "conexus" / sub, plugin / sub, ignore=shutil.ignore_patterns("__pycache__"))
    (plugin / "launch" / "nx_setup.py").unlink()
    tools, _gen = _layout(tmp_path, "nx-mcp")
    env = scrubbed_env(
        PATH=str(_empty(tmp_path, "bin")), NX_TOOLS_DIR=str(tools), NX_SETUP_SCRIPT=str(tmp_path / "elsewhere.py"),
    )
    r = subprocess.run(
        [sys.executable, str(plugin / "launch" / "nx_launch.py"), "nx-mcp"],
        input=b"", capture_output=True, env=env, timeout=30, check=False,
    )
    assert _report(r)["setup_script"] is None
