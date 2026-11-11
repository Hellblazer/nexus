# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/split_integration_files.py: the affected-integration job's shard split (nexus-jh1f2 cut 4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "split_integration_files.py"

FILES = [f"tests/integration/test_{c}.py" for c in "abcdefg"]


def _collect(counts: dict[str, int]) -> str:
    lines = [f"{f}::test_{i}" for f, n in counts.items() for i in range(n)]
    return "\n".join([*lines, "", f"{sum(counts.values())} tests collected in 0.10s"]) + "\n"


def _split(files: list[str], shard: int, of: int, counts: dict[str, int] | None, tmp_path: Path) -> list[str]:
    args = [sys.executable, "-I", str(SCRIPT), "--shard", str(shard), "--of", str(of)]
    if counts is not None:
        path = tmp_path / "collect.txt"
        path.write_text(_collect(counts), encoding="utf-8")
        args += ["--collected", str(path)]
    out = subprocess.run(args, input="\n".join(files) + "\n", capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


@pytest.mark.parametrize("of", [1, 2, 3, 5])
def test_the_shards_cover_every_file_once(tmp_path: Path, of: int) -> None:
    counts = {f: i + 1 for i, f in enumerate(FILES)}
    shards = [_split(FILES, k, of, counts, tmp_path) for k in range(1, of + 1)]
    flat = [f for s in shards for f in s]
    assert sorted(flat) == sorted(FILES)
    assert len(flat) == len(set(flat))


def test_one_shard_is_the_whole_selection(tmp_path: Path) -> None:
    assert _split(FILES, 1, 1, None, tmp_path) == sorted(FILES)


def test_the_split_balances_test_count(tmp_path: Path) -> None:
    counts = {"tests/a.py": 100, "tests/b.py": 60, "tests/c.py": 50, "tests/d.py": 10}
    files = list(counts)
    loads = sorted(sum(counts[f] for f in _split(files, k, 2, counts, tmp_path)) for k in (1, 2))
    assert loads == [110, 110], loads


def test_a_file_that_collected_nothing_still_lands_in_a_shard(tmp_path: Path) -> None:
    files = ["tests/new.py", "tests/old.py"]
    shards = [_split(files, k, 2, {"tests/old.py": 5}, tmp_path) for k in (1, 2)]
    assert sorted(f for s in shards for f in s) == sorted(files)


def test_more_shards_than_files_leaves_some_empty(tmp_path: Path) -> None:
    shards = [_split(FILES[:2], k, 3, None, tmp_path) for k in (1, 2, 3)]
    assert sorted(len(s) for s in shards) == [0, 1, 1]


def test_the_split_does_not_depend_on_input_order(tmp_path: Path) -> None:
    counts = dict.fromkeys(FILES, 1)  # all ties
    assert _split(FILES, 2, 3, counts, tmp_path) == _split(list(reversed(FILES)), 2, 3, counts, tmp_path)


def test_indented_and_summary_lines_are_not_counted(tmp_path: Path) -> None:
    path = tmp_path / "collect.txt"
    path.write_text("tests/a.py::t1\n  tests/b.py::nested\n\n1 test collected\n", encoding="utf-8")
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import split_integration_files as s  # noqa: PLC0415 - the script is not a package module
    finally:
        sys.path.pop(0)
    assert s.test_counts(path.read_text(encoding="utf-8")) == {"tests/a.py": 1}


def test_recorded_seconds_outweigh_test_count(tmp_path: Path) -> None:
    """One slow file (tests/test_indexer_e2e.py took 1321 of 3281 s) gets a shard to itself."""
    files = ["tests/slow.py", "tests/a.py", "tests/b.py", "tests/c.py"]
    counts = {"tests/slow.py": 30, "tests/a.py": 100, "tests/b.py": 100, "tests/c.py": 100}
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"tests/slow.py": 1300.0, "tests/a.py": 300.0, "tests/b.py": 300.0, "tests/c.py": 300.0}))
    collect = tmp_path / "collect.txt"
    collect.write_text(_collect(counts), encoding="utf-8")
    out = [subprocess.run([sys.executable, "-I", str(SCRIPT), "--shard", str(k), "--of", "2", "--collected", str(collect),
                           "--durations", str(durations)], input="\n".join(files), capture_output=True, text=True,
                          check=True).stdout.split() for k in (1, 2)]
    assert sorted(out, key=len) == [["tests/slow.py"], ["tests/a.py", "tests/b.py", "tests/c.py"]]


def test_an_unrecorded_file_weighs_its_count_times_the_recorded_rate(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import split_integration_files as s  # noqa: PLC0415 - the script is not a package module
    finally:
        sys.path.pop(0)
    w = s.weights(["tests/a.py", "tests/new.py"], {"tests/a.py": 10, "tests/new.py": 4}, {"tests/a.py": 20.0})
    assert w == {"tests/a.py": 20.0, "tests/new.py": 8.0}


def test_record_writes_per_file_seconds_from_junit(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.integration.test_x.TestY" name="a" time="1.5"/>'
        '<testcase classname="tests.integration.test_x" name="b" time="2.0"/>'
        '<testcase classname="tests.test_z" name="c" time="4.0"/>'
        '<testcase classname="" name="tests/test_q.py" time="0.0"/>'
        '</testsuite></testsuites>')
    out = tmp_path / "durations.json"
    subprocess.run([sys.executable, "-I", str(SCRIPT), "--record", str(out), str(junit)],
                   input="tests/integration/test_x.py\ntests/test_z.py\n", capture_output=True, text=True, check=True)
    assert json.loads(out.read_text()) == {"tests/integration/test_x.py": 3.5, "tests/test_z.py": 4.0}


def test_the_committed_time_table_names_only_real_integration_files() -> None:
    table = json.loads((ROOT / "tests" / "integration_durations.json").read_text(encoding="utf-8"))
    assert table, "the time table is empty"
    missing = [f for f in table if not (ROOT / f).is_file()]
    assert not missing, f"time table names files that no longer exist (re-record it): {missing}"


@pytest.mark.parametrize("bad", [["--shard", "0", "--of", "3"], ["--shard", "4", "--of", "3"], ["--shard", "1", "--of", "0"]])
def test_a_bad_shard_number_is_refused(bad: list[str]) -> None:
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), *bad], input="", capture_output=True, text=True, check=False)
    assert out.returncode == 2
