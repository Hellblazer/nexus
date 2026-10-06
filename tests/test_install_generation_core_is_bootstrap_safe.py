# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/generation_core.py`` must run with nexus absent.

RDR-224, nexus-f9bgu.47. Same claim and same enforcement as the layout, census,
gc and shims cores. The Windows generation installer is what builds the
replacement tree while the running nexus keeps executing from its own, and it
reaches three siblings (``layout_core``, ``gc_core``, ``winproc_core``) through
``_sibling``. That accessor is the thing most likely to be "simplified" later
into a package import, which would pass every test and fail during an install.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from nexus._install import gc_core, generation_core
from nexus._install import layout_core as package_layout

_INSTALL = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install"
_CORE = _INSTALL / "generation_core.py"


def test_the_core_is_present() -> None:
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_no_import_of_nexus_anywhere_including_deferred() -> None:
    """Parsed, not grepped: the docstrings say "nexus" throughout."""
    tree = ast.parse(_CORE.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "nexus":
            offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            offenders += [
                f"line {node.lineno}: import {a.name}"
                for a in node.names if a.name.split(".")[0] == "nexus"
            ]
        elif isinstance(node, ast.ImportFrom) and node.level:
            offenders.append(f"line {node.lineno}: relative import")
    assert not offenders, (
        "generation_core reaches nexus:\n  " + "\n  ".join(offenders) +
        "\nUse _sibling(); it loads the neighbour by path."
    )


def test_it_flips_registers_and_reads_extras_with_nexus_unavailable(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    for name in ("gen-A", "gen-B"):
        (tools / name / "Scripts").mkdir(parents=True)
    legacy = tmp_path / "uv" / "conexus"
    legacy.mkdir(parents=True)
    (legacy / "uv-receipt.toml").write_text('extras = ["local", "mineru"]\n')
    program = f'''
import os, runpy, sys
from pathlib import Path
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
sys.modules["nexus.install_layout"] = None
ns = runpy.run_path({str(_CORE)!r}, run_name="gen_probe")

class Ops(ns["LinkOps"]):
    def create(self, target, link):
        os.symlink(target, link, target_is_directory=True)

tools = Path({str(tools)!r})
ops = Ops("linux")
ns["flip_current"](tools / "gen-A", tools, ops=ops, platform="linux")
ns["flip_current"](tools / "gen-B", tools, ops=ops, platform="linux")
assert os.readlink(tools / "current") == str(tools / "gen-B"), "flip"
assert os.readlink(tools / "previous") == str(tools / "gen-A"), "previous"
ns["register_legacy"](Path({str(legacy)!r}), tools, ops=ops, platform="linux")
assert os.readlink(tools / "gen-legacy-uv-tool") == {str(legacy)!r}, "register"
assert ns["legacy_extras"](Path({str(legacy)!r})) == ["local"], "extras"
store = ns["FileUserPath"](tools / "p.txt")
result = ns["ensure_user_path"]("C:/nx/current/bin", uv_bin="C:/uv", store=store, environ=dict())
assert result.changed and store.read()[0] == "C:/nx/current/bin", "user path"
# One module object per process: the three siblings, loaded by path.
assert ns["_layout"]() is ns["_gc"]()._layout()
print("OK")
'''
    r = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_the_package_world_shares_one_module_per_sibling() -> None:
    assert generation_core._layout() is package_layout
    assert generation_core._gc() is gc_core
