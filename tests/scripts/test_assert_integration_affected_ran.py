# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rpaat: non-vacuity check for ci.yml's affected-integration job."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import assert_integration_affected_ran as chk
import select_affected_integration_tests as sel

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "assert_integration_affected_ran.py"


def _junit(tmp: pathlib.Path, cases: list[tuple[str, str, bool | str]]) -> pathlib.Path:
    """cases: (classname, name, skipped). skipped may be a pytest skip type."""

    def tag(s: bool | str) -> str:
        if s is False:
            return ""
        kind = "pytest.skip" if s is True else s
        return f'<skipped type="{kind}" message="x"/>'

    body = "".join(
        f'<testcase classname="{c}" name="{n}">{tag(s)}</testcase>' for c, n, s in cases
    )
    p = tmp / "j.xml"
    p.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>')
    return p


def test_every_file_ran_passes(tmp_path: pathlib.Path) -> None:
    j = _junit(
        tmp_path,
        [
            ("tests.db.test_a", "t1", False),
            ("tests.db.test_a.TestX", "t2", True),
            ("tests.test_b.TestY", "t3", False),
        ],
    )
    problems = chk.check(j, ["tests/db/test_a.py", "tests/test_b.py"], collected=3)
    assert problems == []


def test_all_skipped_file_fails(tmp_path: pathlib.Path) -> None:
    j = _junit(tmp_path, [("tests.test_a", "t1", False), ("tests.test_b", "t2", True)])
    problems = chk.check(j, ["tests/test_a.py", "tests/test_b.py"], collected=2)
    assert len(problems) == 1 and "tests/test_b.py" in problems[0]


def test_allowlisted_all_skip_file_passes(tmp_path: pathlib.Path) -> None:
    rel = next(iter(chk.ALL_SKIP_ALLOWED))
    mod = rel[: -len(".py")].replace("/", ".")
    j = _junit(tmp_path, [(mod, "t1", True), ("tests.test_a", "t2", False)])
    assert chk.check(j, [rel, "tests/test_a.py"], collected=2) == []


def test_count_mismatch_fails(tmp_path: pathlib.Path) -> None:
    j = _junit(tmp_path, [("tests.test_a", "t1", False)])
    problems = chk.check(j, ["tests/test_a.py"], collected=5)
    assert problems and "5" in problems[0]


def test_zero_collected_is_not_a_failure(tmp_path: pathlib.Path) -> None:
    # The selected files carry only carved-out marks (lived_in, cloud_mode):
    # the expression collects nothing, and the job says so instead of failing.
    j = _junit(tmp_path, [])
    assert chk.check(j, ["tests/test_a.py"], collected=0) == []


def test_zero_testcases_with_collected_tests_fails(tmp_path: pathlib.Path) -> None:
    j = _junit(tmp_path, [])
    assert chk.check(j, ["tests/test_a.py"], collected=4)


def test_prefix_is_not_a_match(tmp_path: pathlib.Path) -> None:
    # tests.test_a must not claim tests.test_ab's cases.
    j = _junit(tmp_path, [("tests.test_ab", "t1", False), ("tests.test_a", "t2", True)])
    problems = chk.check(j, ["tests/test_a.py", "tests/test_ab.py"], collected=2)
    assert len(problems) == 1 and "tests/test_a.py" in problems[0]


def test_allowlist_names_real_integration_files() -> None:
    files = set(sel.select_all(REPO_ROOT))
    assert chk.ALL_SKIP_ALLOWED
    for rel, reason in chk.ALL_SKIP_ALLOWED.items():
        assert rel in files, rel
        assert reason.strip(), rel


def test_cli_exit_codes(tmp_path: pathlib.Path) -> None:
    j = _junit(tmp_path, [("tests.test_a", "t1", True)])
    sel_file = tmp_path / "sel.txt"
    sel_file.write_text("tests/test_a.py\n")
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--junit", str(j), "--selected", str(sel_file), "--collected", "1"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr
    j2 = _junit(tmp_path, [("tests.test_a", "t1", False)])
    good = subprocess.run(
        [sys.executable, str(SCRIPT), "--junit", str(j2), "--selected", str(sel_file), "--collected", "1"],
        capture_output=True,
        text=True,
    )
    assert good.returncode == 0, good.stdout + good.stderr


def test_xfail_counts_as_executed(tmp_path: pathlib.Path) -> None:
    # pytest writes an xfail as <skipped type="pytest.xfail">; a file of
    # strict xfail pins (tests/db/test_i711w_gap_xfails.py) ran its tests.
    j = _junit(tmp_path, [("tests.db.test_x", "t1", "pytest.xfail")])
    assert chk.check(j, ["tests/db/test_x.py"], collected=1) == []


def _with_module_skip(tmp: pathlib.Path, mod: str, cases: list[tuple[str, str, bool | str]]) -> pathlib.Path:
    j = _junit(tmp, cases)
    text = j.read_text().replace(
        "</testsuite>",
        f'<testcase classname="" name="{mod}"><skipped type="pytest.skip" message="absent"/></testcase></testsuite>',
    )
    j.write_text(text)
    return j


def test_module_level_skip_is_named_not_miscounted(tmp_path: pathlib.Path, capsys) -> None:
    j = _with_module_skip(tmp_path, "tests.db.test_m", [("tests.test_a", "t1", False)])
    problems = chk.check(j, ["tests/test_a.py", "tests/db/test_m.py"], collected=1)
    assert len(problems) == 1 and "tests/db/test_m.py" in problems[0]
    assert "skipped at module level" in capsys.readouterr().out


def test_module_level_skip_of_allowlisted_file_passes(tmp_path: pathlib.Path) -> None:
    rel = next(iter(chk.ALL_SKIP_ALLOWED))
    j = _with_module_skip(tmp_path, rel[:-3].replace("/", "."), [])
    assert chk.check(j, [rel], collected=0) == []


def test_testcase_outside_the_selection_fails(tmp_path: pathlib.Path) -> None:
    # A classname the per-file loop cannot attribute would otherwise pass
    # silently as "nothing collected" for its real file.
    j = _junit(tmp_path, [("tests.test_a", "t1", False), ("weird.module", "t2", False)])
    problems = chk.check(j, ["tests/test_a.py"], collected=2)
    assert any("belong to no selected file" in p for p in problems)
