# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/shims_core.py`` must run with nexus absent.

nexus-utpuw.4. Same claim and same enforcement as the layout, census and gc
cores. It matters here because the shim writer is the thing that puts ``nx`` on
the operator's PATH in the first place: it runs from
``scripts/reinstall-tool.sh`` and from ``migrate_legacy.sh`` with NOTHING
installed, and an import error at that moment is an install that produces no
usable command at all.

It reaches ``layout_core`` by path through :func:`_sibling`, the accessor most
likely to be "simplified" later into ``from nexus import install_layout``,
which would work in every test and fail during an install.
"""
from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "shims_core.py"


def test_the_core_is_present() -> None:
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_no_import_of_nexus_anywhere_including_deferred() -> None:
    """Parsed, not grepped: the docstrings say "nexus" throughout, including in
    the sentences explaining why it must not import nexus, and the shim bodies
    it writes say it too."""
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
        "shims_core reaches nexus:\n  " + "\n  ".join(offenders) +
        "\nUse _sibling(); it loads the neighbour by path."
    )


def _generation(tools: Path, name: str, entry_points: list[str]) -> Path:
    gen = tools / f"gen-{name}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text("{}\n")
    names_file = gen / "declared-entry-points.txt"
    names_file.write_text("".join(f"{ep}\n" for ep in entry_points))
    python = gen / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncat "{names_file}"\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    for name_ in [*entry_points, "pip", "activate"]:
        target = gen / "bin" / name_
        if target.exists():
            continue
        target.write_text('#!/bin/sh\necho hi\n')
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return gen


def _run(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
sys.modules["nexus.install_layout"] = None
sys.argv = ["shims_core.py"] + {argv!r}
runpy.run_path({str(_CORE)!r}, run_name="__main__")
'''
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
    )


def test_it_writes_the_shims_with_nexus_unavailable(tmp_path: Path) -> None:
    """The whole point of the file: this is the moment nexus does not exist
    yet, and it is the moment the shims have to appear."""
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    gen = _generation(tools, "A", ["nx", "nx-mcp"])

    r = _run(["write", str(gen), str(bin_dir)], tmp_path)

    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    for name in ("nx", "nx-mcp"):
        written = bin_dir / name
        assert written.is_file(), f"{name} was not written: {sorted(bin_dir.iterdir())}"
        assert not written.is_symlink(), f"{name} is a symlink, not a rendered file"


def test_it_refuses_with_the_usage_status_and_writes_nothing(tmp_path: Path) -> None:
    from nexus._install.shims_core import SHIMS_USAGE_EXIT

    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    missing = tools / "gen-nope"

    for argv in (
        [],
        ["write"],
        ["nonsense", str(tools)],
        ["write", "relative/path", str(bin_dir)],
        ["write", str(missing), str(bin_dir)],
    ):
        r = _run(argv, tmp_path)
        assert r.returncode == SHIMS_USAGE_EXIT, f"{argv}: rc={r.returncode} {r.stderr}"
        assert r.stdout == "", f"{argv} printed on a refusal: {r.stdout!r}"
        assert "Traceback" not in r.stderr, f"{argv} raised rather than refused:\n{r.stderr}"
        assert not bin_dir.exists() or not any(bin_dir.iterdir()), (
            f"{argv} wrote something while refusing"
        )


def test_a_lookup_failure_writes_no_partial_set(tmp_path: Path) -> None:
    """A dist-name mismatch must not leave the dependency scripts written and
    the project's own console scripts missing: the operator would see mineru
    appear and nx quietly vanish (RG-A)."""
    from nexus._install.shims_core import SHIMS_USAGE_EXIT

    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    gen = tools / "gen-A"
    (gen / "bin").mkdir(parents=True)
    python = gen / "bin" / "python"
    python.write_text('#!/bin/sh\necho "NX_LOOKUP_FAILED no such distribution" >&2\nexit 3\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    for name in ("mineru", "mineru-api"):
        dep = gen / "bin" / name
        dep.write_text('#!/bin/sh\necho hi\n')
        dep.chmod(dep.stat().st_mode | stat.S_IXUSR)

    r = _run(["write", str(gen), str(bin_dir)], tmp_path)

    assert r.returncode == SHIMS_USAGE_EXIT, f"rc={r.returncode} {r.stderr}"
    assert not bin_dir.exists() or not any(bin_dir.iterdir()), (
        f"a partial shim set was written: {sorted(bin_dir.iterdir())}"
    )


def test_the_usage_status_matches_its_siblings() -> None:
    """Restated in each core so a refusal never depends on a sibling loading,
    which is exactly the duplication this arc removes, so it is pinned."""
    from nexus._install.census_core import CENSUS_USAGE_EXIT
    from nexus._install.gc_core import GC_USAGE_EXIT
    from nexus._install.layout_core import LAYOUT_USAGE_EXIT
    from nexus._install.shims_core import SHIMS_USAGE_EXIT

    assert SHIMS_USAGE_EXIT == LAYOUT_USAGE_EXIT == CENSUS_USAGE_EXIT == GC_USAGE_EXIT
