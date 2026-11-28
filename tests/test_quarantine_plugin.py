# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the `quarantine` marker is applied from the registry at collection.

Each case runs a real, separate pytest (``pytester.runpytest_subprocess``) with the plugin loaded by ``-p`` and a
registry the sandbox owns (``NX_QUARANTINE_REGISTRY``). The reach of the two checks is pinned here: a malformed
registry fails every run, serial or xdist; a registry entry no test has fails only a run that asks for it
(``NX_QUARANTINE_STRICT=1``, which the nightly sets), never an ordinary scoped or full run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _quarantine_registry as reg

_REPO = Path(__file__).resolve().parents[1]
SHA = "c" * 64


def _entry(nodeid: str, kind: str = "pytest", since: str = "2026-11-27", deadline: str = "2026-12-11") -> str:
    return (
        f'[[quarantine]]\nnodeid = "{nodeid}"\nkind = "{kind}"\nsince = {since}\ndeadline = {deadline}\n'
        f'bead = "nexus-x1"\nreason = "flaky"\nsource = "{SHA}"\n'
    )


@pytest.fixture
def sandbox(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
    """A sandbox project with two test files and a registry the case fills in."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(_REPO), os.environ.get("PYTHONPATH")])))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("NX_QUARANTINE_STRICT", raising=False)
    pytester.makeini("[pytest]\nmarkers =\n    quarantine: flake quarantine\n")
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
    registry.write_text("")
    monkeypatch.setenv("NX_QUARANTINE_REGISTRY", str(registry))

    def run(*args: str) -> pytest.RunResult:
        return pytester.runpytest_subprocess("-p", "tests._quarantine", "-p", "no:cacheprovider", "-v", *args)

    return pytester, registry, run


def _ran(result: pytest.RunResult) -> set[str]:
    out = set()
    for line in result.outlines:
        for status in (" PASSED", " FAILED"):
            if status in line and "::" in line:
                out.add(line.split(status)[0].split()[0])
    return out


def _said(result: pytest.RunResult) -> str:
    return result.stdout.str() + result.stderr.str()


ALL = {
    "test_alpha.py::test_one", "test_alpha.py::test_two", "test_alpha.py::TestK::test_m",
    "test_beta.py::test_three[1]", "test_beta.py::test_three[2]",
}


def test_empty_registry_marks_nothing_and_runs_everything(sandbox) -> None:
    _, _, run = sandbox
    assert _ran(run("-m", "not quarantine")) == ALL


def test_empty_registry_selects_nothing_under_the_quarantine_marker(sandbox) -> None:
    _, _, run = sandbox
    assert run("-m", "quarantine").ret == 5  # pytest's "no tests collected"


def test_a_listed_test_carries_the_marker_and_only_it(sandbox) -> None:
    _, registry, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one"))
    assert _ran(run("-m", "quarantine")) == {"test_alpha.py::test_one"}
    assert _ran(run("-m", "not quarantine")) == ALL - {"test_alpha.py::test_one"}


def test_the_marker_carries_since_deadline_and_bead(sandbox) -> None:
    pytester, registry, run = sandbox
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
    run("-s", "--collect-only", "-q").stdout.fnmatch_lines(
        ["*MARKER test_alpha.py::test_one {'since': '2026-11-27', 'deadline': '2026-12-11', 'bead': 'nexus-x1'}*"]
    )


def test_a_parametrised_and_a_class_member_nodeid_are_found(sandbox) -> None:
    _, registry, run = sandbox
    registry.write_text(_entry("test_beta.py::test_three[2]") + "\n" + _entry("test_alpha.py::TestK::test_m"))
    assert _ran(run("-m", "quarantine")) == {"test_beta.py::test_three[2]", "test_alpha.py::TestK::test_m"}


def test_a_quarantined_test_still_runs_in_a_plain_run(sandbox) -> None:
    """Quarantine changes nothing about the default suite: Sam decides the merge-gate change, not this marker."""
    _, registry, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_two"))
    result = run()
    assert _ran(result) == ALL
    assert result.ret == 1  # test_two fails, and its failure still counts


def test_a_java_entry_marks_no_pytest_test(sandbox) -> None:
    _, registry, run = sandbox
    registry.write_text(_entry("ChashVectorConcurrencyTest::concurrentChashAndVectorUpserts_noServerErrors", kind="java"))
    assert run("-m", "quarantine").ret == 5
    assert _ran(run()) == ALL


# ── an ordinary run never aborts on a stale entry ─────────────────────────────────────────────


@pytest.mark.parametrize("args", [[], ["test_beta.py"], ["test_alpha.py::test_one"], ["-k", "test_three"]])
def test_an_ordinary_run_does_not_abort_on_an_entry_no_test_has(sandbox, args: list[str]) -> None:
    pytester, registry, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_renamed_away") + "\n" + _entry("test_gone.py::test_x"))
    result = run(*args)
    assert _ran(result), "the run started its tests"
    assert "did not collect" not in _said(result)
    assert result.ret in (0, 1)  # 1 only where test_two ran and fails


# ── strict: the nightly's check that every pytest entry still names a collected test ────────────


def test_strict_fails_the_run_naming_the_missing_entry_and_runs_nothing(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _, registry, run = sandbox
    monkeypatch.setenv("NX_QUARANTINE_STRICT", "1")
    registry.write_text(_entry("test_alpha.py::test_renamed_away"))
    result = run()
    assert result.ret == 4
    assert "test_alpha.py::test_renamed_away" in _said(result) and "did not collect" in _said(result)
    assert _ran(result) == set()


def test_strict_fails_for_a_deleted_file_too(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _, registry, run = sandbox
    monkeypatch.setenv("NX_QUARANTINE_STRICT", "1")
    registry.write_text(_entry("test_gone.py::test_x"))
    result = run()
    assert result.ret == 4 and "test_gone.py::test_x" in _said(result) and not _ran(result)


def test_strict_passes_when_every_pytest_entry_collects_even_if_a_filter_deselects_it(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _, registry, run = sandbox
    monkeypatch.setenv("NX_QUARANTINE_STRICT", "1")
    registry.write_text(_entry("test_alpha.py::test_one"))
    result = run("-k", "test_three")
    assert result.ret == 0 and _ran(result) == {"test_beta.py::test_three[1]", "test_beta.py::test_three[2]"}


def test_strict_does_not_look_for_java_entries_among_pytest_tests(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _, registry, run = sandbox
    monkeypatch.setenv("NX_QUARANTINE_STRICT", "1")
    registry.write_text(_entry("Foo::bar", kind="java"))
    assert run("test_beta.py").ret == 0


# ── a malformed registry fails every run ───────────────────────────────────────────────────────


@pytest.mark.parametrize("args", [[], ["test_beta.py"], ["-m", "not quarantine"]])
def test_a_bad_deadline_fails_every_run_with_the_reason_and_runs_nothing(sandbox, args: list[str]) -> None:
    _, registry, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one", deadline="2027-01-01"))
    result = run(*args)
    assert result.ret == 4
    assert "deadline" in _said(result) and "quarantine registry refused" in _said(result)
    assert _ran(result) == set()


def test_a_bad_deadline_fails_an_xdist_run_with_the_reason_and_runs_nothing(sandbox) -> None:
    """One real `-n 2` run: the controller must stop before it starts workers, and say why."""
    _, registry, run = sandbox
    registry.write_text(_entry("test_alpha.py::test_one", deadline="2027-01-01"))
    result = run("-n", "2")
    assert result.ret != 0
    assert "deadline" in _said(result) and "quarantine registry refused" in _said(result)
    assert _ran(result) == set()


def test_a_missing_registry_file_is_an_empty_registry(sandbox) -> None:
    _, registry, run = sandbox
    registry.unlink()
    assert _ran(run("test_beta.py")) == {"test_beta.py::test_three[1]", "test_beta.py::test_three[2]"}


# ── the repo's own wiring ──────────────────────────────────────────────────────────────────────


def test_the_repo_conftest_loads_the_plugin(request: pytest.FixtureRequest) -> None:
    assert request.config.pluginmanager.has_plugin("tests._quarantine")


def test_the_committed_registry_is_valid() -> None:
    """Every entry the repo ships passes the quarantine rules, a 14-day deadline included."""
    entries = reg.load()
    assert all(e.deadline == reg.deadline_for(e.since) for e in entries)
