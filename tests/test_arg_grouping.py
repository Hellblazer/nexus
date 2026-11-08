# SPDX-License-Identifier: AGPL-3.0-or-later
"""Path arguments are regrouped so each directory's are contiguous (tests/_arg_grouping.py).

pytest 9.1.1 does not apply a directory's conftest.py to the tests of that directory when the
directory comes up again in the argument list after another one: ``tests/daemon/a.py tests/b.py
tests/daemon/c.py`` runs ``c.py`` without ``tests/daemon/conftest.py``'s autouse fixtures. A hand
run over a list of changed files hit it: ``test_rdr149_lifecycle_conformance.py`` lost its watchdog
and its re-embed-hint stub, so a background thread reached storage and reclaimed a test's lease.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._arg_grouping import group_args_by_directory

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("args", "expected"), [
    (["tests/daemon/a.py", "tests/b.py", "tests/daemon/c.py"],
     ["tests/b.py", "tests/daemon/a.py", "tests/daemon/c.py"]),
    (["tests/daemon/a.py::T::t", "tests/b.py", "tests/daemon/c.py::t[x]"],
     ["tests/b.py", "tests/daemon/a.py::T::t", "tests/daemon/c.py::t[x]"]),
    (["tests/x/y/a.py", "tests/c.py", "tests/x/b.py", "tests/x/y/d.py"],   # a subtree stays contiguous
     ["tests/c.py", "tests/x/b.py", "tests/x/y/a.py", "tests/x/y/d.py"]),
    (["tests/daemon", "tests/b.py", "tests/daemon/c.py"],                   # a directory argument
     ["tests/b.py", "tests/daemon", "tests/daemon/c.py"]),
    (["/r/tests/daemon/a.py", "tests/b.py", "tests/daemon/c.py"],            # one directory, two spellings
     ["tests/b.py", "/r/tests/daemon/a.py", "tests/daemon/c.py"]),
    (["tests/daemon/a.py", "tests/b.py", "tests/x/../daemon/c.py"],
     ["tests/b.py", "tests/daemon/a.py", "tests/x/../daemon/c.py"]),
    (["tests/a.py", "tests/b.py"], ["tests/a.py", "tests/b.py"]),           # already contiguous: unchanged
    (["tests/b.py", "tests/a.py"], ["tests/b.py", "tests/a.py"]),           # files in one directory keep their order
    ([], []),
])
def test_paths_are_grouped_by_directory(args: list[str], expected: list[str]) -> None:
    assert group_args_by_directory(args, base="/r") == expected


def test_an_existing_dotted_directory_is_its_own_key(tmp_path: Path) -> None:
    (tmp_path / "tests" / "data.v2").mkdir(parents=True)
    args = ["tests/data.v2", "tests/b.py", "tests/data.v2/c.py"]
    assert group_args_by_directory(args, base=str(tmp_path)) == ["tests/b.py", "tests/data.v2", "tests/data.v2/c.py"]


def _child(*paths: str, keep_order: bool = False) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "NX_SUITE_LEASE_HELD_BY"}
    env["NX_TEST_T2_SUBSTRATE"] = "none"
    env.pop("NX_TEST_KEEP_ARG_ORDER", None)  # an ambient opt-out would turn the positive case red
    if keep_order:
        env["NX_TEST_KEEP_ARG_ORDER"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", *paths],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )


_ARM_CHECK = "tests/daemon/test_rdr149_lifecycle_conformance.py::test_the_per_test_watchdog_is_armed_for_this_file"
# A whole file between the two tests/daemon arguments (a node id there does not trigger it).
_INTERLEAVED = ("tests/daemon/test_watchdog.py", "tests/test_init_service_binary.py", _ARM_CHECK)


@pytest.mark.skipif(sys.platform == "win32", reason="the arm check measures an interval timer, which Windows lacks")
def test_a_directory_revisited_in_the_argument_list_keeps_its_conftest() -> None:
    """The watchdog-arm check needs tests/daemon/conftest.py; here tests/daemon comes up twice."""
    result = _child(*_INTERLEAVED)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert re.search(r"\b\d+ passed\b", result.stdout), result.stdout[-2000:]   # non-vacuous


@pytest.mark.skipif(sys.platform == "win32", reason="the arm check measures an interval timer, which Windows lacks")
def test_the_opt_out_keeps_the_order_and_shows_the_pytest_behaviour() -> None:
    """Control: with the regrouping off, the same arguments lose the conftest, so the check above can fail.

    This pins pytest 9.1.1's behaviour. The day pytest applies a revisited directory's conftest, it goes
    red: then tests/_arg_grouping.py and its hook in tests/conftest.py can be removed."""
    result = _child(*_INTERLEAVED, keep_order=True)
    assert result.returncode != 0 and "did not arm for this module" in result.stdout, result.stdout[-3000:]
