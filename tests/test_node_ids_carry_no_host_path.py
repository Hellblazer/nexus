# SPDX-License-Identifier: AGPL-3.0-or-later
"""No collected node id names a path of the machine that collected it.

pytest-gate checks that every CI shard split the same collection, by count and by an
XOR of the node ids' digests (``tests/_shard_collection.py``, nexus-bo5lv). A parametrize
id built from a value that holds the checkout path, the home directory or a lab runner's
work directory names the test differently on every shard. The count stays equal and the
digests differ, so every shard passes and pytest-gate goes red with nothing to point at.
That happened on develop at f02133858 (run 42505019981): a destructive_action_advice
case was named by a JSON payload carrying its ``cwd``.

This test collects the whole suite in a child pytest and fails on any node id containing
the rootdir, the real home directory, or ``/var/lib/lab/`` (the pool runners' work root).
Literal example paths in ids (``/home/u/...``, ``/Users/x/...``) are not this machine's
and pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parent.parent
LAB_WORK_ROOT = "/var/lib/lab/"


def host_paths(rootdir: Path, home: Path) -> list[str]:
    """The strings a node id must not contain: this machine's own paths."""
    found = [str(rootdir), LAB_WORK_ROOT]
    if str(home) not in ("", "/"):
        found.append(str(home))
    return found


def offending(node_ids: list[str], forbidden: list[str]) -> list[str]:
    return [nid for nid in node_ids if any(p in nid for p in forbidden)]


def test_the_check_catches_a_cwd_carrying_id_and_passes_example_paths(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    home = tmp_path / "home"
    forbidden = host_paths(root, home)
    ids = [
        f't.py::test_x[{{"cwd": "{root}"}}]',
        "t.py::test_y[/var/lib/lab/runs/514b9d3b/_work/nexus/nexus]",
        f"t.py::test_z[{home}/.config]",
        "t.py::test_ok[/home/u/.local/bin/nx]",
        "t.py::test_ok2[/Users/x/plain/a.py]",
    ]
    assert offending(ids, forbidden) == ids[:3]


def test_no_collected_node_id_names_a_host_path() -> None:
    env = {**os.environ, "NX_TEST_T2_SUBSTRATE": "none"}
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly", "-p", "no:cacheprovider",
         "--rootdir", str(ROOT), str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=600,
    )
    node_ids = [line for line in proc.stdout.splitlines() if "::" in line]
    # Non-vacuity: a collection that failed or found almost nothing proves nothing.
    assert len(node_ids) > 10_000, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    bad = offending(node_ids, host_paths(ROOT, Path.home()))
    assert not bad, (
        "node ids name a path of this machine, so each CI shard would collect a different id "
        "(give the parametrize explicit ids):\n  " + "\n  ".join(bad[:20])
    )
