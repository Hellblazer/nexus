# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/lease_core.py`` and ``_nx_lease.py`` must run with nexus absent.

The lease's gc half runs from ``gc_core`` at bootstrap with nothing installed, and
its hook half runs inside every interpreter a generation starts, where an import
of anything beyond ``os``/``sys``/``time``/``fcntl`` is startup cost and a way to
fail. Same enforcement as the other cores: AST, not grep (the docstrings say
"nexus" throughout), and one run with the package made unimportable.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from nexus._install.layout_core import LAYOUT_USAGE_EXIT
from nexus._install.lease_core import LEASE_USAGE_EXIT

_INSTALL = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install"
_CORE = _INSTALL / "lease_core.py"
_HOOK = _INSTALL / "_nx_lease.py"


def _imports(path: Path) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, node.module or "", node.level))
    return out


def test_the_files_are_present() -> None:
    assert _CORE.is_file() and _HOOK.is_file()


def test_lease_core_imports_nothing_from_nexus_anywhere_including_deferred() -> None:
    offenders = [
        f"line {n}: {m or '.'}" for n, m, level in _imports(_CORE)
        if m.split(".")[0] == "nexus" or level
    ]
    assert not offenders, "lease_core reaches nexus:\n  " + "\n  ".join(offenders)


def test_the_hook_imports_only_what_every_interpreter_start_can_afford() -> None:
    names = {m.split(".")[0] for _n, m, _l in _imports(_HOOK)}
    assert names <= {"os", "sys", "time", "fcntl"}, names
    assert not any(level for _n, _m, level in _imports(_HOOK))


def test_probe_and_install_run_with_nexus_unavailable(tmp_path: Path) -> None:
    program = f'''
import runpy, sys, venv
sys.modules["nexus"] = None
sys.modules["nexus._install"] = None
mod = runpy.run_path({str(_CORE)!r}, run_name="lease_probe")
gen = {str(tmp_path / "gen")!r}
venv.EnvBuilder(with_pip=False).create(gen)
from pathlib import Path
assert mod["probe"](Path(gen)) == "absent"
mod["install_lease"](Path(gen))
assert mod["probe"](Path(gen)) == "free", mod["probe"](Path(gen))
print("OK")
'''
    r = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_the_cli_refuses_with_the_usage_status(tmp_path: Path) -> None:
    assert LEASE_USAGE_EXIT == LAYOUT_USAGE_EXIT == 64
    r = subprocess.run(
        [sys.executable, str(_CORE), "frobnicate"], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == LEASE_USAGE_EXIT and r.stdout == "", (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
