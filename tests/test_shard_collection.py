# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cross-shard collection check (nexus-bo5lv): tests/_shard_collection.py, its plugin, and
its CI wiring."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _shard_collection as sc

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
IDS = [f"tests/test_x.py::test_{i}" for i in range(12)]


def _write(d: Path, group: int, full: list[str], kept: list[str], *, worker: str = "gw0", splits: int = 3,
           verified: bool = True) -> None:
    shard = d / f"ddl-report-shard-{group}"
    shard.mkdir(parents=True, exist_ok=True)
    rec = sc.report(full, kept, group, splits)
    rec["verified"] = verified
    (shard / f"nx-shard-collection-{worker}.json").write_text(json.dumps(rec))


def _partition(d: Path, full: list[str] = IDS) -> None:
    for g in (1, 2, 3):
        _write(d, g, full, full[g - 1::3])


def test_the_digest_is_order_free_and_xors_over_a_disjoint_union() -> None:
    assert sc.digest(IDS) == sc.digest(reversed(IDS))
    a, b = sc.digest(IDS[:5]), sc.digest(IDS[5:])
    assert int(a[1], 16) ^ int(b[1], 16) == int(sc.digest(IDS)[1], 16)
    assert sc.digest([]) == (0, "0" * 64)


def test_a_partition_of_one_collection_passes(tmp_path: Path) -> None:
    _partition(tmp_path)
    problems, summary = sc.check(tmp_path, 3)
    assert problems == [] and "each given the same 12 tests, groups holding 12" in summary


def test_two_workers_of_a_shard_are_one_group(tmp_path: Path) -> None:
    _partition(tmp_path)
    _write(tmp_path, 1, IDS, IDS[0::3], worker="gw1")
    assert sc.check(tmp_path, 3)[0] == []


def test_a_leg_that_collected_one_test_fewer_fails(tmp_path: Path) -> None:
    _partition(tmp_path)
    short = IDS[:-1]
    _write(tmp_path, 2, short, short[1::3])
    problems, _ = sc.check(tmp_path, 3)
    assert any("split different collections" in p for p in problems), problems


def test_a_test_in_two_groups_and_one_in_none_fails_though_the_counts_add_up(tmp_path: Path) -> None:
    _partition(tmp_path)
    g2 = IDS[1::3]
    swapped = [IDS[0]] + g2[1:]  # IDS[0] is also in group 1; g2[0] is now in no group
    _write(tmp_path, 2, IDS, swapped)
    problems, _ = sc.check(tmp_path, 3)
    assert problems == ["the groups are not a partition of the split input: some test is in two groups or none"]


def test_a_missing_group_and_disagreeing_workers_fail_and_an_unverified_report_is_a_note(tmp_path: Path) -> None:
    for g in (1, 2):
        _write(tmp_path, g, IDS, IDS[g - 1::3])
    assert any("no report from group(s) [3]" in p for p in sc.check(tmp_path, 3)[0])
    _write(tmp_path, 3, IDS, IDS[2::3], verified=False)
    problems, summary = sc.check(tmp_path, 3)
    assert problems == [] and "not reproduced from the recorded input (advice only)" in summary
    _write(tmp_path, 3, IDS, IDS[2::3])
    _write(tmp_path, 3, IDS, IDS[2::3][1:], worker="gw1")
    assert any("two of its workers kept different tests" in p for p in sc.check(tmp_path, 3)[0])


def test_no_reports_at_all_fail(tmp_path: Path) -> None:
    problems, _ = sc.check(tmp_path, 6)
    assert problems == ["no report from group(s) [1, 2, 3, 4, 5, 6]: a shard that wrote none cannot be checked"]


def test_the_cli_exit_code_follows_the_verdict(tmp_path: Path) -> None:
    _partition(tmp_path)
    assert sc.main([str(tmp_path), "--shards", "3"]) == 0
    assert sc.main([str(tmp_path), "--shards", "6"]) == 1


def test_the_check_needs_nothing_outside_the_standard_library() -> None:
    path = REPO_ROOT / "tests" / "_shard_collection.py"
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported and imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    probe = (
        "import sys; sys.modules['pytest'] = None; sys.path.insert(0, sys.argv[1]); "
        "import tests._shard_collection as m; assert callable(m.check)"
    )
    done = subprocess.run([sys.executable, "-I", "-c", probe, str(REPO_ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(("algorithm", "workers"), [("least_duration", "2"), ("duration_based_chunks", "0")])
def test_the_plugin_records_a_real_split_that_the_check_accepts(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, algorithm: str, workers: str,
) -> None:
    # Ten tests, one deselected by -m before the split: the split's input is the other nine, so taking the
    # mark deselection for the split's own would break the partition. Under xdist each worker reports.
    body = "\n".join(f"def test_{i}():\n    pass\n" for i in range(9))
    pytester.makepyfile(test_many="import pytest\n\n" + body + "\n@pytest.mark.slow\ndef test_slow():\n    pass\n")
    pytester.makeconftest(
        "pytest_plugins = ['tests._shard_collection_plugin']\n\n"
        "def pytest_configure(config):\n    config.addinivalue_line('markers', 'slow: slow')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    # These children stand in for real CI shards, not for a pytest run from inside a test.
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    for g in (1, 2, 3):
        monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / f"shard-{g}"))
        res = pytester.runpytest_subprocess("-m", "not slow", "-n", workers, "--splits", "3", "--group", str(g),
                                            "--splitting-algorithm", algorithm)
        res.assert_outcomes(passed=3)
    names = sorted(p.name for p in tmp_path.rglob(sc.REPORT_GLOB))
    assert names == (["nx-shard-collection-main.json"] * 3 if workers == "0"
                     else sorted(["nx-shard-collection-gw0.json", "nx-shard-collection-gw1.json"] * 3)), names
    recs = [json.loads(p.read_text()) for p in tmp_path.rglob(sc.REPORT_GLOB)]
    assert all(r["verified"] is True for r in recs), recs
    problems, summary = sc.check(tmp_path, 3)
    assert problems == [] and "given the same 9 tests, groups holding 9" in summary, (problems, summary)
    # One durations file per shard, from the controller only, holding exactly the tests that shard ran.
    files = sorted(tmp_path.rglob(sc.DURATIONS_NAME))
    assert [f.parent.name for f in files] == ["shard-1", "shard-2", "shard-3"], files
    ran = [set(json.loads(f.read_text())) for f in files]
    assert all(len(r) == 3 for r in ran) and set().union(*ran) == {f"test_many.py::test_{i}" for i in range(9)}, ran
    target = tmp_path / "durations.json"
    target.write_text(json.dumps({"tests/test_gone.py::test_kept": 7.0, "test_many.py::test_0": 99.0}))
    assert sc.main([str(tmp_path), "--shards", "3", "--write-durations", str(target)]) == 0
    written = json.loads(target.read_text())
    assert set(written) == set().union(*ran) | {"tests/test_gone.py::test_kept"}, written
    assert written["tests/test_gone.py::test_kept"] == 7.0 and written["test_many.py::test_0"] < 99.0
    assert all(isinstance(v, float) and v >= 0 for v in written.values())


def test_the_plugin_writes_nothing_without_a_split_or_a_report_dir(pytester: pytest.Pytester, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    pytester.makepyfile(test_few="def test_a():\n    pass\n")
    pytester.makeconftest("pytest_plugins = ['tests._shard_collection_plugin']\n")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.delenv("PYTEST_CURRENT_TEST")  # so only the missing split or dir keeps it quiet
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / "unsplit"))
    pytester.runpytest_subprocess().assert_outcomes(passed=1)
    monkeypatch.delenv("NX_DDL_REPORT_DIR")
    pytester.runpytest_subprocess("--splits", "2", "--group", "1").assert_outcomes(passed=1)
    assert not list(tmp_path.rglob(sc.REPORT_GLOB)) and not list(tmp_path.rglob(sc.DURATIONS_NAME))


def test_ci_uploads_the_reports_and_pytest_gate_checks_them() -> None:
    text = CI.read_text()
    assert "${{ runner.temp }}/ddl-report/nx-shard-collection-*.json" in text
    assert "${{ runner.temp }}/ddl-report/nx-shard-durations.json" in text
    assert "            tests/_shard_collection.py\n" in text, "pytest-gate's sparse checkout must carry the check"
    shards = text.count("python3 -m tests._shard_collection ddl-reports --shards 6")
    assert shards == 1, shards
    step = text.split("- name: Every shard split the same collection", 1)[1].split("\n      - ", 1)[0]
    assert "continue-on-error" not in step, "the check is enforced"
    assert "tests._shard_collection_plugin" in (REPO_ROOT / "tests" / "conftest.py").read_text()


def test_a_pytest_started_inside_a_test_never_writes_over_the_shards_report(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split-shard probe (tests/test_mode_declarations_are_explicit.py) runs --splits 4 children inside
    a CI shard; they inherit NX_DDL_REPORT_DIR and PYTEST_XDIST_WORKER (review of 624a0a007)."""
    pytester.makepyfile(test_few="def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    pytester.makeconftest("pytest_plugins = ['tests._shard_collection_plugin']\n")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    shard = tmp_path / "shard"
    shard.mkdir()
    (shard / "nx-shard-collection-gw0.json").write_text("the parent shard's report\n")
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(shard))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert "PYTEST_CURRENT_TEST" in __import__("os").environ, "this test runs inside a test, like the probe"
    pytester.runpytest_subprocess("--splits", "2", "--group", "1").assert_outcomes(passed=1)
    assert [p.name for p in shard.iterdir()] == ["nx-shard-collection-gw0.json"]
    assert (shard / "nx-shard-collection-gw0.json").read_text() == "the parent shard's report\n"


def _durations(d: Path, group: int, seconds: dict[str, float]) -> None:
    (d / f"ddl-report-shard-{group}" / sc.DURATIONS_NAME).write_text(json.dumps(seconds))


def test_durations_are_written_only_from_a_whole_checked_run(tmp_path: Path) -> None:
    """nexus-zkr7q: the refresh takes one run's six files, or leaves the durations file alone."""
    reports = tmp_path / "reports"
    _partition(reports)
    for g in (1, 2):
        _durations(reports, g, {i: 1.5 for i in IDS[g - 1::3]})
    target = tmp_path / "durations.json"
    target.write_text('{"x": 1.0}')
    assert sc.main([str(reports), "--shards", "3", "--write-durations", str(target)]) == 1
    assert target.read_text() == '{"x": 1.0}', "a missing group leaves the file unchanged"
    assert sc.merge_durations(reports, 3)[0] == ["no durations from group(s) [3]"]

    _durations(reports, 3, {i: 2.5 for i in IDS[2::3]})
    _write(reports, 2, IDS[:-1], IDS[:-1][1::3])  # the check now fails: group 2 split another collection
    assert sc.main([str(reports), "--shards", "3", "--write-durations", str(target)]) == 1
    assert target.read_text() == '{"x": 1.0}', "a failed check leaves the file unchanged"

    _write(reports, 2, IDS, IDS[1::3])
    _durations(reports, 3, {})
    assert sc.merge_durations(reports, 3)[0][0].startswith("group 3: 0 durations for 4 tests kept")
    _durations(reports, 3, {i: 2.5 for i in IDS[2::3]})
    twin = reports / "ddl-report-shard-3-again"
    twin.mkdir()
    (twin / "nx-shard-collection-gw0.json").write_text((reports / "ddl-report-shard-3" / "nx-shard-collection-gw0.json").read_text())
    (twin / sc.DURATIONS_NAME).write_text(json.dumps({i: 2.5 for i in IDS[2::3]}))
    assert any("more than one durations file" in p for p in sc.merge_durations(reports, 3)[0])
    for f in twin.iterdir():
        f.unlink()
    twin.rmdir()
    (reports / "stray").mkdir()
    (reports / "stray" / sc.DURATIONS_NAME).write_text("{}")
    assert any("no collection report beside it" in p for p in sc.merge_durations(reports, 3)[0])
    (reports / "stray" / sc.DURATIONS_NAME).unlink()
    assert sc.main([str(reports), "--shards", "3", "--write-durations", str(target)]) == 0
    assert json.loads(target.read_text()) == {"x": 1.0, **{i: 1.5 for i in IDS[0::3] + IDS[1::3]},
                                              **{i: 2.5 for i in IDS[2::3]}}


def test_a_tests_duration_is_its_setup_call_and_teardown(pytester: pytest.Pytester, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded seconds sum the three phases, as pytest-split's --store-durations does (nexus-zkr7q)."""
    pytester.makepyfile(test_slow_fixture=(
        "import time, pytest\n\n"
        "@pytest.fixture\ndef slow():\n    time.sleep(0.3)\n    yield\n    time.sleep(0.3)\n\n"
        "def test_a(slow):\n    time.sleep(0.3)\n"
    ))
    pytester.makeconftest("pytest_plugins = ['tests._shard_collection_plugin']\n")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path))
    pytester.runpytest_subprocess("--splits", "1", "--group", "1").assert_outcomes(passed=1)
    seconds = json.loads((tmp_path / sc.DURATIONS_NAME).read_text())
    assert list(seconds) == ["test_slow_fixture.py::test_a"] and seconds["test_slow_fixture.py::test_a"] >= 0.9, seconds
