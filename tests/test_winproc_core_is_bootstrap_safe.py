# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_install/winproc_core.py`` must run with nexus absent (nexus-f9bgu.21).

``census_core.py`` runs as a script with nothing installed and reaches this
module through its ``_sibling`` accessor, so this module cannot import nexus.
Every ordinary test session HAS nexus importable, so no other test can see
that rule break; this one runs census_core's Windows snapshot in a subprocess
with ``nexus`` blocked.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_INSTALL = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install"
_CORE = _INSTALL / "winproc_core.py"
_CENSUS = _INSTALL / "census_core.py"


def test_the_core_is_present() -> None:
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_imports_nothing_from_nexus_at_any_depth() -> None:
    tree = ast.parse(_CORE.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] == "nexus":
                offenders.append(f"line {node.lineno}: from {'.' * node.level}{node.module}")
        elif isinstance(node, ast.Import):
            offenders += [
                f"line {node.lineno}: import {a.name}"
                for a in node.names if a.name.split(".")[0] == "nexus"
            ]
    assert not offenders, offenders


def test_census_windows_snapshot_runs_with_nexus_unavailable() -> None:
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.modules["nexus._install"] = None
census = runpy.run_path({str(_CENSUS)!r}, run_name="census_probe")

class Api:
    def open_process(self, pid): return (pid, 0) if pid in (7, 8) else (None, 87)
    def close(self, h): pass
    def creation_filetime(self, h): return 133_000_000_000_000_000
    def image_path(self, h): return "C:\\\\g\\\\gen-A\\\\bin\\\\nx.exe"
    def command_line(self, h):
        return '"C:\\\\g\\\\gen-A\\\\bin\\\\nx.exe" serve' if h == 7 else None
    def parent_pid(self, h): return 1
    def snapshot(self): return [(7, 1), (8, 1), (9, 1)]

text = census["ps_snapshot"](platform="win32", win_info_api=Api())
print(repr(text))
'''
    r = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    # pid 7 has a command line, pid 8 falls back to the image path, pid 9 is
    # unopenable and absent. Non-vacuity: two rows, in ps's `pid command` shape,
    # backslashes written as slashes.
    assert eval(r.stdout.strip()) == (  # noqa: S307 - our own repr of a str
        "7 C:/g/gen-A/bin/nx.exe serve\n8 C:/g/gen-A/bin/nx.exe\n"
    )
