# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the `quarantine` marker is applied from the registry at collection.

Each case runs a real, separate pytest (``pytester.runpytest_subprocess``) with the plugin loaded by ``-p``,
a registry that the sandbox owns (``NX_QUARANTINE_REGISTRY``) and a results path (``NX_QUARANTINE_RESULTS``).
The repo's own registry ships empty and the committed-file case at the bottom checks that.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import _quarantine_registry as reg

_REPO = Path(__file__).resolve().parents[1]
SHA = "c" * 64


def _entry(nodeid: str, since: str = "2026-11-27", deadline: str = "2026-12-11") -> str:
    return (
        f'[[quarantine]]\nnodeid = "{nodeid}"\nsince = {since}\ndeadline = {deadline}\n'
        f'bead = "nexus-x1"\nreason = "flaky"\nsource = "{SHA}"\n'
    )


@pytest.fixture
def sandbox(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
    """A sandbox project with two test files and a registry the case fills in."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(_REPO), os.environ.get("PYTHONPATH")])))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    pytester.makeini("[pytest]\nmarkers =\n    quarantine: flake quarantine\n    other: another marker\n")
    pytester.makepyfile(
        test_alpha="""
            def test_one(): pass
            def test_two(): assert False, "alpha two fails"
            class TestK:
                def test_m(self): pass
        """,
        test_beta="""
            import pytest
            @pytest.mark.parametrize("p", [1, 2])
            def test_three(p): pass
        """,
    )
    registry = pytester.path / "registry.toml"
    results = pytester.path / "results.json"
    registry.write_text("")
    monkeypatch.setenv("NX_QUARANTINE_REGISTRY", str(registry))
    monkeypatch.setenv("NX_QUARANTINE_RESULTS", str(results))

    def run(*args: str) -> pytest.RunResult:
        return pytester.runpytest_subprocess("-p", "tests._quarantine", "-p", "no:cacheprovider", "-v", *args)

    return pytester, registry, results, run


def _ran(result: pytest.RunResult) -> set[str]:
    out = set()
    for line in result.outlines:
        for status in (" PASSED", " FAILED"):
            if status in line and "::" in line:
                out.add(line.split(status)[0].split()[0])
    return out


def test_empty_registry_marks_nothing_and_runs_everything(sandbox) -> None:
    _, _, _, run = sandbox
    result = run("-m", "not quarantine")
    assert _ran(result) == {
        "test_alpha.py::test_one", "test_alpha.py::test_two", "test_alpha.py::TestK::test_m",
        "test_beta.py::test_three[1]", "test_beta.py::test_three[2]",
    }


def test_empty_registry_selects_nothing_under_the_quarantine_marker(sandbox) -> None:
    _, _, _, run = sandbox
    result = run("-m", "quarantine")
    assert result.ret == 5  # pytest's "no tests collected": the nightly handles it by not running at all


def test_a_listed_test_carries_the_marker_and_only_it(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one"))
    assert _ran(run("-m", "quarantine")) == {"test_alpha.py::test_one"}
    assert "test_alpha.py::test_one" not in _ran(run("-m", "not quarantine"))


def test_the_marker_carries_since_deadline_and_bead(sandbox) -> None:
    pytester, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one"))
    pytester.makeconftest(
        """
        def pytest_collection_modifyitems(items):
            for item in items:
                m = item.get_closest_marker("quarantine")
                if m:
                    print("MARKER", item.nodeid, dict(m.kwargs))
        """
    )
    result = run("-s", "--collect-only", "-q")
    result.stdout.fnmatch_lines(
        ["*MARKER test_alpha.py::test_one {'since': '2026-11-27', 'deadline': '2026-12-11', 'bead': 'nexus-x1'}*"]
    )


def test_a_parametrised_and_a_class_member_nodeid_are_found(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_beta.py::test_three[2]") + "\n" + _entry("test_alpha.py::TestK::test_m"))
    assert _ran(run("-m", "quarantine")) == {"test_beta.py::test_three[2]", "test_alpha.py::TestK::test_m"}


def test_a_quarantined_test_still_runs_in_a_plain_run(sandbox) -> None:
    """Quarantine changes nothing about the default suite: Sam decides the merge-gate change, not this marker."""
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_two"))
    result = run()
    assert "test_alpha.py::test_two" in _ran(result)
    assert result.ret == 1  # test_two fails, and its failure still counts


def test_a_nodeid_no_test_has_fails_the_run_loudly(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_renamed_away"))
    result = run()
    assert result.ret != 0
    assert "test_alpha.py::test_renamed_away" in result.stdout.str() + result.stderr.str()
    assert _ran(result) == set()


def test_a_deleted_file_fails_the_run_loudly(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_gone.py::test_x"))
    assert run().ret != 0


def test_a_scoped_run_that_never_collected_the_file_does_not_fail(sandbox) -> None:
    """`pytest tests/test_beta.py` cannot see an entry in test_alpha.py: a scoped run must stay usable."""
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_renamed_away"))
    assert run("test_beta.py").ret == 0


def test_a_run_naming_another_test_of_the_same_file_does_not_fail(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_renamed_away"))
    assert run("test_alpha.py::test_one").ret == 0


def test_a_run_naming_the_missing_test_itself_fails(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_renamed_away"))
    assert run("test_alpha.py::test_renamed_away").ret != 0


def test_a_deselected_listed_test_is_still_found(sandbox) -> None:
    """-k and -m deselect after the check, so a listed test that exists is never reported missing."""
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one"))
    assert run("-k", "test_three").ret == 0


def test_a_bad_deadline_fails_the_run_loudly(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one", deadline="2027-01-01"))
    result = run()
    assert result.ret != 0
    assert "deadline" in result.stdout.str() + result.stderr.str()


def test_a_missing_registry_file_is_an_empty_registry(sandbox) -> None:
    _, registry, _, run = sandbox
    registry.unlink()
    assert run("-m", "not quarantine", "test_beta.py").ret == 0


def test_the_results_file_names_the_outcome_of_each_listed_test_that_ran(sandbox) -> None:
    _, registry, results, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one") + "\n" + _entry("test_alpha.py::test_two"))
    registry_text = registry.read_text()
    run("-m", "quarantine")
    assert json.loads(results.read_text()) == {
        "results": {"test_alpha.py::test_one": "passed", "test_alpha.py::test_two": "failed"}
    }
    assert registry.read_text() == registry_text  # a passing test is never released by the run


def test_a_listed_test_that_was_not_selected_has_no_result(sandbox) -> None:
    _, registry, results, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one") + "\n" + _entry("test_alpha.py::test_two"))
    run("-k", "test_one")
    assert json.loads(results.read_text()) == {"results": {"test_alpha.py::test_one": "passed"}}


def test_no_results_path_writes_nothing(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _, registry, results, run = sandbox
    monkeypatch.delenv("NX_QUARANTINE_RESULTS")
    registry.write_text(_entry("test_alpha.py::test_one"))
    run("-m", "quarantine")
    assert not results.exists()


@pytest.mark.parametrize(
    ("phase", "expected"),
    [("call_pass", "passed"), ("call_fail", "failed"), ("setup_fail", "error"), ("skip", "skipped")],
)
def test_outcome_merge(phase: str, expected: str) -> None:
    from tests import _quarantine as q

    reports = {
        "call_pass": [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
        "call_fail": [("setup", "passed"), ("call", "failed"), ("teardown", "passed")],
        "setup_fail": [("setup", "failed"), ("teardown", "passed")],
        "skip": [("setup", "skipped")],
    }[phase]
    outcome: str | None = None
    for when, result in reports:
        outcome = q.merge_outcome(outcome, when, result)
    assert outcome == expected


def test_a_teardown_failure_after_a_pass_is_an_error() -> None:
    from tests import _quarantine as q

    o = q.merge_outcome(None, "setup", "passed")
    o = q.merge_outcome(o, "call", "passed")
    assert q.merge_outcome(o, "teardown", "failed") == "error"


def test_the_committed_registry_ships_empty_and_parses() -> None:
    """No test is quarantined by the change that builds the mechanism; the source list is not final yet."""
    assert reg.load() == []
