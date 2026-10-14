# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/hooks/scripts/nx_setup_notice.py: the session-start hook that tells the
model the truth when there is no CLI (RDR-228 Phase 1 Step 3, nexus-sv2wx.7).

In this phase it has two outcomes. The CLI resolves: it prints nothing. It does
not: it tells the model Nexus is not installed and names the manual commands. It
starts nothing, opens no socket and writes no file. The cases run the real script
as a subprocess. Every child environment sets ``NX_TOOLS_DIR`` and a ``PATH`` the
test owns, so the developer's own install is not found.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._platform import WINDOWS_SHEBANG, install_windows_exe_fake, scrubbed_env

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _ROOT / "conexus" / "hooks" / "scripts" / "nx_setup_notice.py"

_FIRST = "uv tool install conexus --python 3.12" + (" --torch-backend cpu" if sys.platform.startswith("linux") else "")
_MANUAL_COMMANDS = (_FIRST, "nx self install", "nx init")

#: Makes any socket or any child process a failure, for the whole child interpreter.
_NO_SIDE_EFFECTS = """
import socket, subprocess
def _refuse(*a, **k):
    raise RuntimeError("the setup hook must not open a socket or start a process")
socket.socket = _refuse
subprocess.Popen = _refuse
"""


def _dirs(tmp_path: Path) -> dict[str, Path]:
    tmp_path.mkdir(exist_ok=True)
    dirs = {name: tmp_path / name for name in ("home", "tools", "bin", "config", "site")}
    for d in dirs.values():
        d.mkdir()
    return dirs


def _install_nx_mcp(bindir: Path) -> None:
    if os.name == "nt":
        install_windows_exe_fake(bindir, "nx-mcp", f"{WINDOWS_SHEBANG}\nprint('real')\n")
    else:
        fake = bindir / "nx-mcp"
        fake.write_text("#!/bin/sh\necho real\n")
        fake.chmod(0o755)


def _run(
    dirs: dict[str, Path], *, guarded: bool = False, hook: Path = _HOOK, remote: str | None = None,
    **popen: object,
) -> subprocess.CompletedProcess[bytes]:
    env = scrubbed_env(
        PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]), USERPROFILE=str(dirs["home"]),
    )
    if remote is not None:
        env["CLAUDE_CODE_REMOTE"] = remote
    if guarded:
        (dirs["site"] / "sitecustomize.py").write_text(_NO_SIDE_EFFECTS)
        env["PYTHONPATH"] = str(dirs["site"])
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode(),
        capture_output=True, env=env, timeout=30, check=False, cwd=str(dirs["home"]), **popen,
    )


def _plugin_copy(tmp_path: Path) -> Path:
    """The two plugin directories the hook loads from, copied with no bytecode."""
    for sub in ("hooks/scripts", "launch"):
        shutil.copytree(
            _ROOT / "conexus" / sub, tmp_path / "plugin" / sub, ignore=shutil.ignore_patterns("__pycache__"),
        )
    return tmp_path / "plugin"


def _tree(dirs: dict[str, Path]) -> list[str]:
    return sorted(
        str(p.relative_to(base.parent)) for name, base in dirs.items() if name != "site" for p in base.rglob("*")
    )


def _context(r: subprocess.CompletedProcess[bytes]) -> str:
    """The text the hook hands the model. Standard output must be exactly one
    JSON object in the SessionStart shape, and nothing else."""
    out = json.loads(r.stdout.decode("utf-8"))
    assert set(out) == {"hookSpecificOutput"}, out
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    assert set(specific) == {"hookEventName", "additionalContext"}, specific
    return specific["additionalContext"]


def test_the_cli_resolves_and_the_hook_says_nothing(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    _install_nx_mcp(dirs["bin"])
    r = _run(dirs)
    assert r.returncode == 0
    assert r.stdout == b""
    assert r.stderr == b""


def test_no_cli_and_the_hook_names_the_manual_commands(tmp_path: Path) -> None:
    r = _run(_dirs(tmp_path))
    assert r.returncode == 0
    assert r.stderr == b""
    text = _context(r)
    assert "Nexus is not installed" in text
    for command in _MANUAL_COMMANDS:
        assert command in text, command
    assert "https://hellblazer.github.io/nexus/" in text
    assert "/conexus:setup" not in text
    assert "?" not in text, "Phase 1 asks no question; the question arrives with the setup tools"
    assert "ask the user" not in text.lower() and "tell the user" not in text.lower()
    assert "unless the user asks" in text, "the commands are the user's to run"


@pytest.mark.parametrize("value", ["true", "TRUE", "1"])
def test_in_a_remote_workspace_with_no_cli_the_hook_says_nothing(tmp_path: Path, value: str) -> None:
    """A Desktop conversation's loop runs in a cloud workspace that has ``uv``
    and no CLI, while the servers run on the user's machine and reach it through
    the application's bridge (measured 2026-10-13, T2 nexus_rdr/228-research-22).
    The hook cannot see those servers from there, and "this session has no Nexus
    tools" was false. When the machine really has no CLI the stand-in's tool
    descriptions say so through the bridge."""
    r = _run(_dirs(tmp_path), remote=value)
    assert r.returncode == 0
    assert r.stdout == b""
    assert r.stderr == b""


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_a_remote_marker_that_is_not_set_true_does_not_silence_the_hook(tmp_path: Path, value: str) -> None:
    r = _run(_dirs(tmp_path), remote=value)
    assert r.returncode == 0
    assert "Nexus is not installed" in _context(r)


def test_it_opens_no_socket_and_starts_no_process(tmp_path: Path) -> None:
    """Same output with sockets and child processes made to raise."""
    plain = _run(_dirs(tmp_path / "a"))
    guarded = _run(_dirs(tmp_path / "b"), guarded=True)
    assert guarded.returncode == 0, guarded.stderr
    assert guarded.stderr == b""
    assert _context(guarded) == _context(plain)

    with_cli = _dirs(tmp_path / "c")
    _install_nx_mcp(with_cli["bin"])
    r = _run(with_cli, guarded=True)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_the_guard_in_the_test_above_really_refuses(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["site"] / "sitecustomize.py").write_text(_NO_SIDE_EFFECTS)
    r = subprocess.run(
        [sys.executable, "-c", "import subprocess; subprocess.Popen(['x'])"],
        capture_output=True, env=scrubbed_env(PYTHONPATH=str(dirs["site"])), timeout=30, check=False,
    )
    assert r.returncode != 0 and b"must not open a socket or start a process" in r.stderr


def test_it_writes_nothing(tmp_path: Path) -> None:
    """Not under the home, the config or the tools directory, and not in the
    plugin's own directory either: importing its siblings must leave no bytecode
    cache in the installed plugin."""
    for with_cli in (False, True):
        base = tmp_path / str(with_cli)
        dirs = _dirs(base)
        plugin = _plugin_copy(base)
        dirs["plugin"] = plugin
        if with_cli:
            _install_nx_mcp(dirs["bin"])
        before = _tree(dirs)
        r = _run(dirs, hook=plugin / "hooks" / "scripts" / "nx_setup_notice.py")
        assert r.returncode == 0 and r.stderr == b""
        assert bool(r.stdout) is not with_cli
        assert _tree(dirs) == before


def test_a_cli_in_the_generation_layout_alone_is_a_cli(tmp_path: Path) -> None:
    """The lookup is the launcher's: the layout, then PATH. An application started
    before the install has the old PATH and must still be told nothing."""
    dirs = _dirs(tmp_path)
    gen = dirs["tools"] / "gen-20260101T000000Z"
    (gen / "bin").mkdir(parents=True)
    _install_nx_mcp(gen / "bin")
    if os.name == "nt":
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(gen), str(dirs["tools"] / "current"))
    else:
        os.symlink(gen, dirs["tools"] / "current")
    r = _run(dirs)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_it_exits_0_with_standard_output_closed_or_unread(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    env = scrubbed_env(PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]))
    closed = subprocess.run(
        [sys.executable, "-c", f"import os, runpy, sys; os.close(1); sys.argv=[{str(_HOOK)!r}]; runpy.run_path({str(_HOOK)!r}, run_name='__main__')"],
        capture_output=True, env=env, timeout=30, check=False,
    )
    assert closed.returncode == 0, closed.stderr
    proc = subprocess.Popen([sys.executable, str(_HOOK)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == b""


def test_it_exits_0_when_a_sibling_cannot_be_imported(tmp_path: Path) -> None:
    """A plugin laid out without ``launch/``: silence and exit 0, not a traceback."""
    plugin = _plugin_copy(tmp_path)
    shutil.rmtree(plugin / "launch")
    r = _run(_dirs(tmp_path / "d"), hook=plugin / "hooks" / "scripts" / "nx_setup_notice.py")
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_the_hook_speaks_exactly_when_the_launcher_serves_the_stand_in(tmp_path: Path) -> None:
    """One fact for both: whether ``nx-mcp`` resolves. A machine with ``nx`` and no
    ``nx-mcp`` (an incomplete install) gets the stand-in from the launcher and the
    notice from the hook, and the notice names the program that was not found."""
    dirs = _dirs(tmp_path)
    if os.name == "nt":
        install_windows_exe_fake(dirs["bin"], "nx", f"{WINDOWS_SHEBANG}\nprint('nx')\n")
    else:
        (dirs["bin"] / "nx").write_text("#!/bin/sh\necho nx\n")
        (dirs["bin"] / "nx").chmod(0o755)
    launch = _ROOT / "conexus" / "launch" / "nx_launch.py"
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
    env = scrubbed_env(PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]))

    def served_by_the_stand_in() -> bool:
        r = subprocess.run([sys.executable, str(launch), "nx-mcp"], input=ping, capture_output=True, env=env, timeout=30, check=False)
        return r.stdout.strip() == b'{"jsonrpc": "2.0", "id": 1, "result": {}}'

    assert served_by_the_stand_in()
    text = _context(_run(dirs))
    assert "`nx-mcp`) was not found" in text and "or its install is incomplete" in text

    _install_nx_mcp(dirs["bin"])
    assert not served_by_the_stand_in()
    assert _run(dirs).stdout == b""


def test_the_commands_and_the_address_are_the_stand_in_s_own(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("nx_standin_for_notice", _ROOT / "conexus" / "launch" / "nx_standin.py")
    assert spec is not None and spec.loader is not None
    standin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standin)
    text = _context(_run(_dirs(tmp_path)))
    for command in standin.manual_commands():
        assert f"`{command}`" in text
    assert text.endswith(standin.INSTALL_PAGE)
    assert "reconnects the `nexus` server or starts a new session" in text


def test_it_imports_only_the_standard_library_and_its_siblings() -> None:
    tree = ast.parse(_HOOK.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            found.add((node.module or "").split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__", "_cli_lookup", "nx_standin"}, found
    assert "subprocess" not in found and "socket" not in found
