# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/census_core.py`` must run with nexus absent.

nexus-utpuw.10. Same claim, same enforcement, as
``tests/test_install_layout_core_is_bootstrap_safe.py``: census.sh is sourced
by GC and the installer, which run with NOTHING installed, so the module they
dispatch into cannot import nexus. Every ordinary test session HAS nexus
importable, so no other test in the suite can see that rule break.

This module has one wrinkle the layout core does not. It needs the layout, and
it reaches it through :func:`_sibling_layout`, which loads ``layout_core.py``
by path from its own directory. That accessor is the thing most likely to be
"simplified" later into ``from nexus import install_layout``, which would work
in every test and fail only during an install.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "census_core.py"


def test_the_core_is_present() -> None:
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_no_module_scope_import_of_nexus() -> None:
    """Parsed, not grepped: this module's docstrings say "nexus" repeatedly,
    including in the sentences explaining why it must not import nexus."""
    tree = ast.parse(_CORE.read_text())
    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "nexus"]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "nexus":
                offenders.append(node.module or "")
            elif node.level:
                offenders.append("." * node.level + (node.module or ""))
    assert not offenders, (
        f"census_core imports {offenders} at module scope; census.sh runs it as "
        f"a script with nothing installed"
    )


def test_no_import_of_nexus_anywhere_including_deferred() -> None:
    """Stricter than the layout core's equivalent, and deliberately.

    The layout core defers two imports of nexus on purpose, and they are
    guarded by try/except ImportError so the bootstrap path survives them.
    This module defers none: its only cross-module need is the layout, and
    :func:`_sibling_layout` satisfies it by PATH. A ``from nexus import
    install_layout`` anywhere in here, at any nesting depth, is the exact
    regression that would pass every test and fail during an install.
    """
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
    assert not offenders, (
        "census_core reaches nexus:\n  " + "\n  ".join(offenders) +
        "\nUse _sibling_layout() for the layout; it loads layout_core.py by path."
    )


def _run(argv: list[str], home: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
sys.modules["nexus.install_layout"] = None
sys.argv = ["census_core.py"] + {argv!r}
runpy.run_path({str(_CORE)!r}, run_name="__main__")
'''
    return subprocess.run(
        [sys.executable, "-c", program], input=stdin,
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
    )


def test_the_cli_answers_with_nexus_unavailable(tmp_path: Path) -> None:
    """Including the sibling-layout hop, which `report` needs in order to
    resolve the tools root and the generation prefix. A ``from nexus import``
    left in _sibling_layout fails exactly here and nowhere else."""
    tools = tmp_path / "tools"
    (tools / "gen-A" / "bin").mkdir(parents=True)
    (tools / "gen-B" / "bin").mkdir(parents=True)

    r = _run(["report", str(tools), "-"], tmp_path, stdin="")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("holders=") == 2, r.stdout

    r = _run(["holder_pids", str(tools / "gen-A"), "-"],
             tmp_path, stdin=f"999 {tools}/gen-A/bin/nx serve\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["999"], r.stdout


def test_the_cli_refuses_with_the_usage_status_and_an_empty_stdout(tmp_path: Path) -> None:
    """A refusal that also prints is a refusal a caller reads as an answer, and
    here the answer would be a holder list a reap acts on."""
    from nexus._install.census_core import CENSUS_USAGE_EXIT

    for argv in (
        ["holder_pids", "relative/path"],
        ["holder_pids", ""],
        ["holder_pids", "/"],
        ["no_such_verb"],
        [],
    ):
        r = _run(argv, tmp_path, stdin="")
        assert r.returncode == CENSUS_USAGE_EXIT, f"{argv}: rc={r.returncode} {r.stderr}"
        assert r.stdout == "", f"{argv} printed on a refusal: {r.stdout!r}"
        assert "Traceback" not in r.stderr, f"{argv} raised rather than refused:\n{r.stderr}"


def test_the_usage_status_matches_the_layout_cores(tmp_path: Path) -> None:
    """census_core restates EX_USAGE rather than importing it from the sibling,
    so that a refusal path does not depend on the sibling loading. Restating it
    is exactly the two-copies shape this whole arc removes, so it is pinned."""
    from nexus._install.census_core import CENSUS_USAGE_EXIT
    from nexus._install.layout_core import LAYOUT_USAGE_EXIT

    assert CENSUS_USAGE_EXIT == LAYOUT_USAGE_EXIT
