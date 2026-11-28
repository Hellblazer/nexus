# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 (nexus-2jomg.2): ``scripts/class_a_report.py`` runs a pass/fail pytest check in report mode.

A class-A check with no value and no alarm line never fails develop: it lists every failing item and the run
stays green. The behaviour pinned here is the contract the advisory CI jobs rely on:

* a failing test is listed (its node id and its message, so a census names the offenders) and the exit is 0;
* a clean run exits 0 and says how many tests it examined and skipped;
* a run that examined nothing (a collection error, an empty selection, every test skipped, too few tests) exits 1,
  because a report over nothing is the vacuous pass the nexus-moht0 doctrine forbids;
* the annotations and the markdown summary carry the same items.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "class_a_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("class_a_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["class_a_report"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


report = _load()


def _suite(tmp_path: Path, body: str) -> Path:
    """A throwaway test file; its own directory is the rootdir, so nothing of this repo's conftest applies."""
    path = tmp_path / "suite" / "test_planted.py"
    path.parent.mkdir()
    path.write_text(textwrap.dedent(body))
    return path


def _run(tmp_path: Path, capsys, target: Path, *extra: str, min_tests: int = 1, flags: tuple[str, ...] = ()) -> tuple[int, str, str]:
    summary = tmp_path / "summary.md"
    code = report.main([
        "--name", "planted check", "--min-tests", str(min_tests), "--summary", str(summary), *flags, "--",
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(target.parent), str(target), *extra,
    ])
    out = capsys.readouterr().out
    return code, out, summary.read_text(encoding="utf-8") if summary.exists() else ""


def test_a_failing_test_is_listed_with_its_message_and_the_run_exits_zero(tmp_path, capsys):
    target = _suite(tmp_path, '''
        def test_clean():
            assert True

        def test_dangling():
            raise AssertionError("2 dangling citations:\\n  docs/rdr/rdr-1.md:7: abc1234\\n  docs/x.md:3: nexus-zzzzz")
    ''')
    code, out, summary = _run(tmp_path, capsys, target)
    assert code == 0, out
    assert "1 passed, 1 failed, 0 skipped" in out
    assert "::warning title=class-A planted check::" in out
    assert "test_planted.py::test_dangling" in out
    # the message is what names the offenders, so the report carries it, in the log, the annotation and the summary
    assert "docs/rdr/rdr-1.md:7: abc1234" in out and "docs/x.md:3: nexus-zzzzz" in out
    assert "docs/rdr/rdr-1.md:7: abc1234" in summary and "test_dangling" in summary
    assert "1 failed" in summary


def test_a_clean_run_reports_what_it_examined_and_skipped(tmp_path, capsys):
    target = _suite(tmp_path, '''
        import pytest

        def test_a():
            pass

        def test_b():
            pass

        @pytest.mark.skip(reason="not here")
        def test_c():
            pass
    ''')
    code, out, summary = _run(tmp_path, capsys, target, min_tests=2)
    assert code == 0, out
    assert "2 passed, 0 failed, 1 skipped" in out
    assert "::warning" not in out
    assert "2 passed, 0 failed, 1 skipped" in summary


def test_a_session_level_failure_with_no_failing_test_fails_the_report(tmp_path, capsys):
    target = _suite(tmp_path, "def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    (target.parent / "conftest.py").write_text("def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n")
    code, out, _ = _run(tmp_path, capsys, target)
    assert code == 1, out
    assert "2 passed, 0 failed, 0 skipped" in out
    assert "::error title=class-A planted check::pytest exited 1 although no test failed or errored" in out
    assert "a crash is not a report" in out


def test_a_clean_run_has_no_session_note(tmp_path, capsys):
    target = _suite(tmp_path, "def test_a():\n    pass\n")
    code, out, summary = _run(tmp_path, capsys, target)
    assert code == 0 and "although no test failed" not in out + summary


def test_an_error_at_setup_is_listed_and_is_not_examined(tmp_path, capsys):
    target = _suite(tmp_path, '''
        import pytest

        @pytest.fixture
        def boom():
            raise RuntimeError("fixture exploded")

        def test_ok():
            pass

        def test_uses_boom(boom):
            pass
    ''')
    code, out, _ = _run(tmp_path, capsys, target)
    assert code == 0, out
    assert "1 passed, 0 failed, 0 skipped, 1 errored" in out
    assert "test_uses_boom" in out and "fixture exploded" in out
    # a test that errored at setup examined nothing: it does not meet a floor
    code, out, _ = _run(tmp_path, capsys, target, flags=("--require", "test_planted::test_uses_boom=1"))
    assert code == 1 and "examined 0 test(s) under test_planted::test_uses_boom" in out


def test_a_run_whose_only_tests_error_at_setup_examined_nothing(tmp_path, capsys):
    target = _suite(tmp_path, '''
        import pytest

        @pytest.fixture
        def boom():
            raise RuntimeError("fixture exploded")

        def test_a(boom):
            pass
    ''')
    code, out, _ = _run(tmp_path, capsys, target)
    assert code == 1 and "examined 0 tests" in out and "1 errored" in out


def test_a_collection_error_is_a_failure_of_the_report_not_a_pass(tmp_path, capsys):
    target = _suite(tmp_path, "import module_that_is_not_there\n")
    code, out, _ = _run(tmp_path, capsys, target)
    assert code == 1
    assert "::error title=class-A planted check::" in out


def test_a_selection_that_matches_nothing_is_a_failure(tmp_path, capsys):
    target = _suite(tmp_path, "def test_a():\n    pass\n")
    code, out, _ = _run(tmp_path, capsys, target, "-k", "nothing_matches_this")
    assert code == 1
    assert "examined 0 tests" in out


def test_an_all_skipped_run_examined_nothing(tmp_path, capsys):
    target = _suite(tmp_path, '''
        import pytest

        @pytest.mark.skip(reason="x")
        def test_a():
            pass
    ''')
    code, out, _ = _run(tmp_path, capsys, target)
    assert code == 1
    assert "examined 0 tests" in out


def test_fewer_tests_than_the_floor_is_a_failure(tmp_path, capsys):
    target = _suite(tmp_path, "def test_a():\n    pass\n")
    code, out, _ = _run(tmp_path, capsys, target, min_tests=5)
    assert code == 1
    assert "floor of 5" in out


def test_a_command_that_cannot_start_is_a_failure(tmp_path, capsys):
    code = report.main(["--name", "x", "--min-tests", "1", "--", str(tmp_path / "no-such-binary")])
    assert code == 1
    assert "could not start" in capsys.readouterr().out


def test_the_summary_defaults_to_the_actions_step_summary(tmp_path, capsys, monkeypatch):
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    target = _suite(tmp_path, "def test_a():\n    raise AssertionError('listed')\n\ndef test_b():\n    pass\n")
    code = report.main(["--name", "planted check", "--min-tests", "1", "--", sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                        "-o", "addopts=", "--rootdir", str(target.parent), str(target)])
    capsys.readouterr()
    assert code == 0
    assert "listed" in step_summary.read_text(encoding="utf-8")


def test_a_long_message_is_cut_not_dropped(tmp_path, capsys):
    # built at run time, so the traceback's source line does not repeat the whole list
    target = _suite(tmp_path, "def test_a():\n    raise AssertionError(chr(10).join(f'offender-{i}' for i in range(400)))\n\ndef test_b():\n    pass\n")
    code, out, summary = _run(tmp_path, capsys, target)
    assert code == 0
    assert "offender-0" in out and "offender-399" not in out
    assert "more line" in out
    assert "offender-0" in summary


def test_the_junit_names_become_node_ids():
    assert report.node_id("tests.test_docs_reference_rot", "test_cited_commit_shas_resolve") == \
        "tests/test_docs_reference_rot.py::test_cited_commit_shas_resolve"
    assert report.node_id("tests.scripts.test_x.TestY", "test_z[a-1]") == "tests/scripts/test_x.py::TestY::test_z[a-1]"
    assert report.node_id("", "test_bare") == "test_bare"


@pytest.mark.parametrize("flag", ["--name", "--min-tests"])
def test_the_flags_are_required(flag, capsys):
    argv = ["--name", "x", "--min-tests", "1", "--", "true"]
    i = argv.index(flag)
    with pytest.raises(SystemExit):
        report.main(argv[:i] + argv[i + 2:])
    capsys.readouterr()


# -- per-check floors, the item lines and the items file ---------------------------------------------------------------


_TWO_CHECKS = '''
    def test_alpha_one():
        pass

    def test_alpha_two():
        pass

    def test_beta_only():
        pass
'''


def test_each_check_has_its_own_floor_so_one_cannot_hide_behind_another(tmp_path, capsys):
    target = _suite(tmp_path, _TWO_CHECKS)
    ok = _run(tmp_path, capsys, target, flags=("--require", "test_planted::test_alpha=2", "--require", "test_planted::test_beta=1"))
    assert ok[0] == 0, ok[1]
    short = _run(tmp_path, capsys, target, flags=("--require", "test_planted::test_alpha=3"))
    assert short[0] == 1
    assert "examined 2 test(s) under test_planted::test_alpha, under its floor of 3" in short[1]
    gone = _run(tmp_path, capsys, target, flags=("--require", "test_planted::test_gamma=1"))
    assert gone[0] == 1 and "examined 0 test(s) under test_planted::test_gamma" in gone[1]


def test_a_floor_counts_a_failing_test_as_examined(tmp_path, capsys):
    target = _suite(tmp_path, "def test_a():\n    raise AssertionError('x')\n")
    code, out, _ = _run(tmp_path, capsys, target, flags=("--require", "test_planted::test_a=1"))
    assert code == 0, out


@pytest.mark.parametrize("bad", ["x", "x=0", "=3", "x=two", "x=-1"])
def test_a_malformed_floor_is_refused(bad, capsys):
    with pytest.raises(SystemExit):
        report.main(["--name", "x", "--min-tests", "1", "--require", bad, "--", "true"])
    capsys.readouterr()


def test_failing_items_become_fixed_format_lines_and_json_rows(tmp_path, capsys):
    target = _suite(tmp_path, '''
        def test_rot():
            raise AssertionError("2 dangling:\\n  docs/a.md:3: abc1234\\n  docs/b.md:9: nexus-zzzzz")

        def test_census():
            raise AssertionError("offenders:\\n  CLASS_A_ITEM tests/x.py::test_one\\n  CLASS_A_ITEM tests/y.py::test_two[p q]\\textra words")

        def test_bare():
            raise AssertionError("no list at all")

        def test_clean():
            pass
    ''')
    out_json = tmp_path / "items.json"
    code, out, _ = _run(tmp_path, capsys, target, flags=("--items-out", str(out_json)))
    assert code == 0, out
    lines = [ln for ln in out.splitlines() if ln.startswith("CLASS_A_ITEM ")]
    assert lines == [
        "CLASS_A_ITEM test_planted docs/a.md:3\tabc1234",
        "CLASS_A_ITEM test_planted docs/b.md:9\tnexus-zzzzz",
        "CLASS_A_ITEM test_planted tests/x.py::test_one",
        "CLASS_A_ITEM test_planted tests/y.py::test_two[p q]\textra words",
        "CLASS_A_ITEM test_planted test_planted.py::test_bare",
    ], lines
    data = json.loads(out_json.read_text())
    assert (data["passed"], data["failed"], data["errored"], data["pytest_exit"]) == (1, 3, 0, 1)
    assert [(r["check"], r["id"], r["detail"]) for r in data["items"]] == [
        ("test_planted", "docs/a.md:3", "abc1234"), ("test_planted", "docs/b.md:9", "nexus-zzzzz"),
        ("test_planted", "tests/x.py::test_one", ""), ("test_planted", "tests/y.py::test_two[p q]", "extra words"),
        ("test_planted", "test_planted.py::test_bare", "")]
    assert {r["test"] for r in data["items"]} == {
        "test_planted.py::test_rot", "test_planted.py::test_census", "test_planted.py::test_bare"}


def test_every_offender_a_test_lists_reaches_the_items_not_just_the_first_sixty_lines(tmp_path, capsys):
    target = _suite(tmp_path, '''
        def test_census():
            raise AssertionError("\\n".join(f"CLASS_A_ITEM tests/t.py::test_{i}" for i in range(400)))
    ''')
    out_json = tmp_path / "items.json"
    code, out, _ = _run(tmp_path, capsys, target, flags=("--items-out", str(out_json)))
    assert code == 0, out
    assert sum(1 for ln in out.splitlines() if ln.startswith("CLASS_A_ITEM ")) == 400
    assert len(json.loads(out_json.read_text())["items"]) == 400
    assert "more line" in out, "the human message is still cut"


def test_the_items_file_carries_the_floors_it_met(tmp_path, capsys):
    target = _suite(tmp_path, _TWO_CHECKS)
    out_json = tmp_path / "items.json"
    _run(tmp_path, capsys, target, flags=("--items-out", str(out_json), "--require", "test_planted::test_alpha=2"))
    assert json.loads(out_json.read_text())["required"] == [{"prefix": "test_planted::test_alpha", "min": 2, "examined": 2}]
