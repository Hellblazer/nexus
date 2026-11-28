# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 3 (nexus-2jomg.2): the mode-declarations census is a class-A check in report mode.

Sam's ruling (2026-11-27): the census becomes a warning that lists its offenders; the cause test (removing the
cloud-mode leak at its source) is a separate step and not decided here. The census keeps its own job, ``test-mode-census``,
because it needs the whole unsharded corpus and its own environment; the job now runs the census through
``scripts/class_a_report.py`` and pytest-gate no longer reads its result.

The behaviour test runs the census's REAL scan (``_scan_offenders``) and its real failure message (``_census_failure``)
over a planted item, a test whose source names ``voyage-context-3`` and does not use ``cloud_mode``, through the
reporter: the offender's node id is listed and the exit is 0. The wiring tests pin the job and the gate.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from tests.test_ci_release_ledger_gate import _pytest_gate_script, _run

REPO = Path(__file__).resolve().parents[2]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
OFFENDER = "tests/test_planted_voyage.py::test_names_the_model_without_cloud_mode"


def _reporter():
    spec = importlib.util.spec_from_file_location("class_a_report_census", REPO / "scripts" / "class_a_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["class_a_report_census"] = module
    spec.loader.exec_module(module)
    return module


def _scratch(tmp_path: Path, *, declares_cloud_mode: bool, offenders: int = 1) -> Path:
    scratch = tmp_path / "scratch" / "test_census_scratch.py"
    scratch.parent.mkdir()
    scratch.write_text(textwrap.dedent(f"""
        import pytest

        import tests.test_mode_declarations_are_explicit as census


        def names_the_model():
            assert "voyage-context-3"


        def test_the_real_census_over_a_planted_item():
            items = [
                census._FakeItem(names_the_model, nodeid={OFFENDER!r} if {offenders} == 1 else f"tests/test_planted_{{i}}.py::test_it", fixturenames={("cloud_mode",) if declares_cloud_mode else ()!r})
                for i in range({offenders})
            ]
            found = census._scan_offenders(items)
            if found:
                pytest.fail(census._census_failure(found))
    """))
    return scratch


def _report(tmp_path: Path, capsys, monkeypatch, scratch: Path) -> tuple[int, str, str]:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p))
    summary = tmp_path / "summary.md"
    code = _reporter().main([
        "--name", "mode-declarations census", "--min-tests", "1", "--summary", str(summary), "--",
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(scratch.parent), "-q", str(scratch),
    ])
    return code, capsys.readouterr().out, summary.read_text(encoding="utf-8") if summary.exists() else ""


def test_an_undeclared_voyage_test_is_listed_by_node_id_and_does_not_fail(tmp_path, capsys, monkeypatch):
    code, out, summary = _report(tmp_path, capsys, monkeypatch, _scratch(tmp_path, declares_cloud_mode=False))
    assert code == 0, out
    assert "0 passed, 1 failed, 0 skipped" in out
    assert "::warning title=class-A mode-declarations census::" in out
    assert "reference voyage-(context|code)-3 but do not opt in to the `cloud_mode` fixture" in out
    assert OFFENDER in out and OFFENDER in summary, "the offender's node id is listed, in the log and the summary"
    assert f"CLASS_A_ITEM test_census_scratch {OFFENDER}" in out, "and one fixed-format item line"


def test_a_test_that_declares_cloud_mode_is_not_listed(tmp_path, capsys, monkeypatch):
    code, out, _ = _report(tmp_path, capsys, monkeypatch, _scratch(tmp_path, declares_cloud_mode=True))
    assert code == 0, out
    assert "1 passed, 0 failed, 0 skipped" in out and OFFENDER not in out


def _doc() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def test_the_census_job_runs_through_the_reporter_with_the_census_env():
    job = _doc()["jobs"]["test-mode-census"]
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Run the mode-declarations census"))
    run = " ".join(step["run"].replace("\\\n", " ").split())
    assert "NX_CENSUS_ONLY_JOB=1 uv run python scripts/class_a_report.py" in run
    assert re.search(r"--min-tests \d+ .*-- uv run pytest tests/ -q$", run), run
    # the census still sees the whole unsharded corpus: no split, no -m, no -k
    assert "--splits" not in run and " -k " not in run and " -m " not in run.split("--", 2)[-1]
    assert job["env"]["NX_TEST_T2_SUBSTRATE"] == "none"
    assert job["if"].strip() == "success() && needs.changes.outputs.code == 'true'"


@pytest.mark.parametrize("code", ["true", "false"])
def test_pytest_gate_passes_with_no_census_result_at_all(code):
    """The real gate script, as bash, rendered with no ``needs.test-mode-census.result`` value: a script that read it would
    raise a KeyError in the renderer, so this proves the census cannot move the gate, on a code push and on a docs-only one."""
    docs_only = {"needs.test.result": "skipped", "needs.test-lint.result": "skipped", "needs.test-docs-readers.result": "success"}
    proc = _run(_pytest_gate_script(**{"needs.changes.outputs.code": code, **(docs_only if code == "false" else {})}))
    assert proc.returncode == 0, (code, proc.stdout, proc.stderr)
    assert "pytest-gate: PASS" in proc.stdout


def test_pytest_gate_does_not_wait_for_or_name_the_census_result():
    gate = _doc()["jobs"]["pytest-gate"]
    assert "test-mode-census" not in gate["needs"]
    script = next(s["run"] for s in gate["steps"] if "pytest-gate: PASS" in s.get("run", ""))
    assert "needs.test-mode-census" not in script and "test_mode_census_result" not in script
    assert "test-mode-census" in script, "the comment that says why it is not read is still there"


def test_the_gate_still_fails_on_a_red_leg_it_does_read():
    """The control: the same script, still red on test-lint, so the passes above are not a gate that passes everything."""
    proc = _run(_pytest_gate_script(**{"needs.test-lint.result": "failure"}))
    assert proc.returncode == 1 and "test-lint" in proc.stdout + proc.stderr


def test_every_offender_is_an_item_though_the_human_message_lists_twenty(tmp_path, capsys, monkeypatch):
    scratch = _scratch(tmp_path, declares_cloud_mode=False, offenders=45)
    out_json = tmp_path / "items.json"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p))
    code = _reporter().main([
        "--name", "mode-declarations census", "--min-tests", "1", "--items-out", str(out_json), "--",
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(scratch.parent), "-q", str(scratch),
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "... (+25 more)" in out, "the human sample is still capped at twenty"
    ids = sorted(f"tests/test_planted_{i}.py::test_it" for i in range(45))
    assert sorted(r["id"] for r in json.loads(out_json.read_text())["items"]) == ids
    assert sum(1 for ln in out.splitlines() if ln.startswith("CLASS_A_ITEM test_census_scratch ")) == 45


def test_report_mode_turns_the_census_into_a_warning_and_the_default_fails(monkeypatch):
    from tests import test_mode_declarations_are_explicit as census  # noqa: PLC0415

    monkeypatch.delenv(census.CLASS_A_MODE_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception):
        census._fail_or_report("offenders")
    monkeypatch.setenv(census.CLASS_A_MODE_ENV, "report")
    with pytest.warns(UserWarning, match="offenders"):
        census._fail_or_report("offenders")


def _lint_tests_without_class_a_calling(names: set[str]) -> list[str]:
    """The tests that carry ``lint`` and not ``class_a`` and call one of *names* (by name, attribute or reference)."""
    out = []
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(n in text for n in names):
            continue
        tree = ast.parse(text)
        module_marks = {m for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in n.targets)
                        for m in _mark_names(n.value)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                marks = module_marks | {m for d in node.decorator_list for m in _mark_names(d)}
                called = {x.id for x in ast.walk(node) if isinstance(x, ast.Name)} | {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute)}
                if "lint" in marks and "class_a" not in marks and called & names:
                    out.append(f"{path.relative_to(REPO).as_posix()}::{node.name}")
    return out


def _mark_names(node: ast.AST) -> set[str]:
    return {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Attribute) and x.value.attr == "mark"}


def test_no_lint_test_without_class_a_runs_the_census_scan():
    """The census is class A in report mode. A lint test that runs its scan or its failure message and is not class_a would
    still fail the blocking test-lint job on an offender (the lint twin did, until it was marked)."""
    assert _lint_tests_without_class_a_calling({"_scan_offenders", "_census_failure"}) == []


def test_the_pin_sees_a_lint_test_that_runs_the_scan(tmp_path):
    src = tmp_path / "tests" / "test_x.py"
    src.parent.mkdir()
    src.write_text("import pytest\n@pytest.mark.lint\ndef test_twin():\n    _scan_offenders([])\n")
    original = globals()["REPO"]
    globals()["REPO"] = tmp_path
    try:
        assert _lint_tests_without_class_a_calling({"_scan_offenders"}) == ["tests/test_x.py::test_twin"]
        src.write_text("import pytest\n@pytest.mark.lint\n@pytest.mark.class_a\ndef test_twin():\n    _scan_offenders([])\n")
        assert _lint_tests_without_class_a_calling({"_scan_offenders"}) == []
    finally:
        globals()["REPO"] = original


def test_the_lint_twin_and_only_the_twin_carries_class_a_in_the_census_file():
    tree = ast.parse((REPO / "tests" / "test_mode_declarations_are_explicit.py").read_text(encoding="utf-8"))
    marked = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and any("class_a" in _mark_names(d) for d in n.decorator_list)}
    assert marked == {"test_the_mode_census_runs_in_a_lint_run_over_the_full_collection"}


def test_the_local_sweeps_set_report_mode_so_the_census_refuses_nowhere_it_no_longer_refuses_in_ci():
    from scripts import meta_tests  # noqa: PLC0415

    assert meta_tests.GUARD_ENV["NX_CLASS_A_MODE"] == "report"
    preflight = (REPO / ".nexus" / "procedures" / "preflight.lua").read_text(encoding="utf-8")
    assert "env NX_CLASS_A_MODE=report" in preflight


def test_the_census_job_and_the_nightly_job_carry_the_census_floor_and_upload_the_items():
    floor = "tests/test_mode_declarations_are_explicit::test_mode_declarations_are_explicit=1"
    ci = _doc()["jobs"]["test-mode-census"]["steps"]
    nightly = yaml.safe_load((REPO / ".github" / "workflows" / "local-service-gate-nightly.yml").read_text(encoding="utf-8"))["jobs"]["slow"]["steps"]
    for steps in (ci, nightly):
        step = next(s for s in steps if "NX_CENSUS_ONLY_JOB=1 uv run python scripts/class_a_report.py" in s.get("run", ""))
        run = " ".join(step["run"].replace("\\\n", " ").split())
        assert f'--require "{floor}"' in run and "--items-out" in run
        upload = steps[steps.index(step) + 1]
        assert upload["uses"].startswith("actions/upload-artifact@") and upload["with"]["retention-days"] == 30 and upload["if"] == "always()"
    twin = "tests/test_mode_declarations_are_explicit::test_the_mode_census_runs_in_a_lint_run_over_the_full_collection=1"
    for steps in (_doc()["jobs"]["test-class-a-report"]["steps"],):
        run = " ".join(next(s for s in steps if "class_a_report.py" in s.get("run", ""))["run"].replace("\\\n", " ").split())
        assert f'--require "{twin}"' in run
