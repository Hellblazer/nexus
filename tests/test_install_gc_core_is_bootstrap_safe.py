# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/gc_core.py`` must run with nexus absent.

nexus-utpuw.6. Same claim and same enforcement as the layout and census cores.
It matters most here: gc is the only code in the arc that DELETES, it runs from
``scripts/reinstall-tool.sh`` with nothing installed, and an import error at
that moment is an install that cannot clean up after itself.

It reaches two siblings rather than one, layout_core and census_core, both by
path through :func:`_sibling`. That accessor is the thing most likely to be
"simplified" later into a package import, which would work in every test and
fail only during an install.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "gc_core.py"


def test_the_core_is_present() -> None:
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_no_import_of_nexus_anywhere_including_deferred() -> None:
    """Parsed, not grepped: the docstrings say "nexus" throughout, including in
    the sentences explaining why it must not import nexus."""
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
        "gc_core reaches nexus:\n  " + "\n  ".join(offenders) +
        "\nUse _sibling(); it loads the neighbour by path."
    )


def _run(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
sys.modules["nexus.install_layout"] = None
sys.modules["nexus.install_census"] = None
sys.argv = ["gc_core.py"] + {argv!r}
runpy.run_path({str(_CORE)!r}, run_name="__main__")
'''
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
    )


def test_the_sweep_plans_with_nexus_unavailable(tmp_path: Path) -> None:
    """Dry run, so the bootstrap check cannot itself delete anything. Exercises
    both sibling hops: layout_core for the prefix and receipt name, census_core
    for the holder snapshot."""
    tools = tmp_path / "tools"
    for name in ("gen-A", "gen-B", "gen-C", "gen-D"):
        (tools / name / "bin").mkdir(parents=True)
        (tools / name / "nexus-install.json").write_text("{}\n")

    r = _run(["--keep", "2", "--dry-run", str(tools)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("would reap") == 2, r.stdout
    for name in ("gen-A", "gen-B", "gen-C", "gen-D"):
        assert (tools / name).is_dir(), "a dry run deleted something"


def test_it_refuses_with_the_usage_status_and_deletes_nothing(tmp_path: Path) -> None:
    from nexus._install.gc_core import GC_USAGE_EXIT

    tools = tmp_path / "tools"
    for name in ("gen-A", "gen-B", "gen-C"):
        (tools / name / "bin").mkdir(parents=True)
        (tools / name / "nexus-install.json").write_text("{}\n")

    for argv in (["--keep", "0", str(tools)], ["--keep", "x", str(tools)], ["--keep"]):
        r = _run(argv, tmp_path)
        assert r.returncode == GC_USAGE_EXIT, f"{argv}: rc={r.returncode} {r.stderr}"
        assert r.stdout == "", f"{argv} printed on a refusal: {r.stdout!r}"
        assert "Traceback" not in r.stderr, f"{argv} raised rather than refused:\n{r.stderr}"
        for name in ("gen-A", "gen-B", "gen-C"):
            assert (tools / name).is_dir(), f"{argv} reaped {name} while refusing"


def test_the_usage_status_matches_its_siblings() -> None:
    """Restated in each core so a refusal never depends on a sibling loading,
    which is exactly the duplication this arc removes, so it is pinned."""
    from nexus._install.census_core import CENSUS_USAGE_EXIT
    from nexus._install.gc_core import GC_USAGE_EXIT
    from nexus._install.layout_core import LAYOUT_USAGE_EXIT

    assert GC_USAGE_EXIT == LAYOUT_USAGE_EXIT == CENSUS_USAGE_EXIT
