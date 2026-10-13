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
import json
import os
import subprocess
import sys
from pathlib import Path

from tests._platform import scrubbed_env

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
        (bindir / "nx-mcp.cmd").write_text("@echo real\r\n")
    else:
        fake = bindir / "nx-mcp"
        fake.write_text("#!/bin/sh\necho real\n")
        fake.chmod(0o755)


def _run(dirs: dict[str, Path], *, guarded: bool = False) -> subprocess.CompletedProcess[bytes]:
    env = scrubbed_env(
        PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]), USERPROFILE=str(dirs["home"]),
    )
    if guarded:
        (dirs["site"] / "sitecustomize.py").write_text(_NO_SIDE_EFFECTS)
        env["PYTHONPATH"] = str(dirs["site"])
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode(),
        capture_output=True, env=env, timeout=30, check=False, cwd=str(dirs["home"]),
    )


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
    assert "unless the user asks" in text, "the commands are the user's to run"


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
    for with_cli in (False, True):
        dirs = _dirs(tmp_path / str(with_cli))
        if with_cli:
            _install_nx_mcp(dirs["bin"])
        before = _tree(dirs)
        assert _run(dirs).returncode == 0
        assert _tree(dirs) == before


def test_it_agrees_with_the_launcher_about_what_no_cli_means(tmp_path: Path) -> None:
    """The hook and the stand-in must not disagree: both speak exactly when
    ``nx-mcp`` does not resolve. A machine with ``nx`` and no ``nx-mcp`` (a CLI
    too old to have it does not exist; a broken install can) gets the notice."""
    dirs = _dirs(tmp_path)
    if os.name == "nt":
        (dirs["bin"] / "nx.cmd").write_text("@echo nx\r\n")
    else:
        (dirs["bin"] / "nx").write_text("#!/bin/sh\necho nx\n")
        (dirs["bin"] / "nx").chmod(0o755)
    assert "Nexus is not installed" in _context(_run(dirs))


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
