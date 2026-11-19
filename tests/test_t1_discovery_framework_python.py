# SPDX-License-Identifier: AGPL-3.0-or-later
"""The /clear handoff on a macOS framework build of Python (nexus-hczdo.1.11, RDR-253 MVV rehearsal check 4).

``find_mcp_sibling_pids`` must see an nx-mcp whose interpreter is a framework Python, whose executable is
``.../Python.app/Contents/MacOS/Python``, and the SessionStart hook must say so when it finds no server rather than
return silently. Its own module because tests/test_t1_discovery.py is at the test file-size cap.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.session import _list_processes, find_mcp_sibling_pids

#: The plugin's uv launch wrapper, as tests/test_t1_discovery.py's TestFindMcpSiblingPids writes it.
_UV = "uv tool run --directory /p --no-config --quiet --python >=3.12 python /p/launch/nx_launch.py"

# nexus-hczdo.1.11 (RDR-253 MVV rehearsal, check 4): a conexus installed on a
# macOS framework build of Python (Homebrew's python@3.x, python.org's
# installer) runs its console scripts through the framework's app stub, so ps
# reports the server as ``.../Python.app/Contents/MacOS/Python .../bin/nx-mcp``
# and ``comm`` as ``Python``, capital P. Neither matches the lowercase
# interpreter rule, the server is not found, the SessionStart hook writes no
# T1 handoff marker on /clear, the MCP server never re-leases onto the new
# session id, and the first post-clear write fails with
# t1_explicit_session_no_usable_lease. Measured live 2026-11-19 in a sandbox
# whose nx was installed with `uv tool install --python
# /opt/homebrew/bin/python3.14`.

_FRAMEWORK_PYTHON = (
    "/opt/homebrew/Cellar/python@3.14/3.14.4_1/Frameworks/Python.framework/Versions/3.14/"
    "Resources/Python.app/Contents/MacOS/Python"
)

def test_finds_a_server_run_by_a_macos_framework_python() -> None:
    """The rehearsal's process tree, row for row: claude, the plugin's uv
    wrappers, and under each a framework Python running the installed
    console script. ``comm`` for those rows is ``Python``."""

    sandbox_bin = "/tmp/mvv-home/.local/bin"
    table = [
        (1200, 1, "/Users/x/.local/bin/claude --plugin-dir /w/conexus --model haiku"),
        (1250, 1200, f"{_UV} nx-mcp"),
        (1300, 1250, f"{_FRAMEWORK_PYTHON} {sandbox_bin}/nx-mcp"),
        (1251, 1200, f"{_UV} nx-mcp-catalog"),
        (1301, 1251, f"{_FRAMEWORK_PYTHON} {sandbox_bin}/nx-mcp-catalog"),
        (1252, 1200, "uv tool run --directory /w/conexus python /w/conexus/mcp/sequential_thinking.py"),
        (1302, 1252, f"{_FRAMEWORK_PYTHON} /w/conexus/mcp/sequential_thinking.py"),
    ]
    images = {1250: "uv", 1251: "uv", 1252: "uv", 1300: "Python", 1301: "Python", 1302: "Python"}
    with patch("nexus.session._list_processes", return_value=table), \
            patch("nexus.session._command_name_of", side_effect=lambda pid, **_k: images.get(pid, "")):
        assert sorted(find_mcp_sibling_pids(1200)) == [1300, 1301]

def test_a_framework_python_server_against_the_REAL_process_table(tmp_path: Path) -> None:
    """The same, from real processes: a script named ``nx-mcp`` run by a
    framework build of Python, found through a real ``ps``. Needs such a
    Python on the box (macOS with Homebrew or python.org Python)."""


    if sys.platform != "darwin":
        pytest.skip("framework builds of Python exist only on macOS")
    candidates = sorted(
        glob.glob("/opt/homebrew/opt/python@3.*/bin/python3.*[0-9]")
        + glob.glob("/Library/Frameworks/Python.framework/Versions/3.*/bin/python3.*[0-9]")
    )
    if not candidates:
        pytest.skip("no framework build of Python on this Mac (Homebrew or python.org)")
    me = os.getpid()
    if not any(pid == me for pid, _ppid, _cmd in _list_processes()):
        pytest.skip(f"process enumeration cannot see this test's own pid ({me})")
    script = tmp_path / "bin" / "nx-mcp"
    script.parent.mkdir()
    script.write_text("import time; time.sleep(30)\n")
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
        [candidates[-1], str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        found: list[int] = []
        for _ in range(100):
            found = find_mcp_sibling_pids(me)
            if proc.pid in found:
                break
            time.sleep(0.1)
        rows = [row for row in _list_processes() if row[0] == proc.pid]
        assert proc.pid in found, f"framework-Python nx-mcp {proc.pid} not found; ps said {rows}"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_clear_whose_claude_has_no_recognised_mcp_server_says_so() -> None:
    """The /clear handoff no longer fails silently (nexus-hczdo.1.11): when the matcher finds no MCP server under the
    claude process, the hook logs a warning naming the claude pid and the new session id, and writes no marker."""
    from unittest.mock import patch  # noqa: PLC0415

    from structlog.testing import capture_logs  # noqa: PLC0415

    from nexus.hooks import _write_t1_handoff_markers  # noqa: PLC0415

    with (
        patch("nexus.session.find_immediate_claude_pid", return_value=4242),
        patch("nexus.session.find_mcp_sibling_pids", return_value=[]),
        patch("nexus.daemon.t1_handoff.write_handoff_marker") as write,
        capture_logs() as logs,
    ):
        _write_t1_handoff_markers("new-sess-id")
    assert not write.called
    assert [(e["event"], e["log_level"], e["claude_pid"], e["new_session_id"]) for e in logs] == [
        ("t1_handoff_no_mcp_server_found", "warning", 4242, "new-sess-id"),
    ]
