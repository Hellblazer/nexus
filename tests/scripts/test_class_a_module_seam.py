# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 4 (nexus-2jomg.2): ``module_seam`` is a class-A check in report mode.

Sam's ruling (2026-11-27): report only, offenders fixed at each review. The repo-state scans of
``tests/test_module_seam.py`` (the offender list and its non-vacuity sweep) carry ``class_a`` and run in the advisory
``test-class-a-report`` job; the self-tests of the scan (the parametrized bad and good samples) keep blocking, because
they pin the lint's own code and not the tree.

The behaviour test runs the REAL lint through ``scripts/class_a_report.py`` over a planted tests directory with one
process-wide ``subprocess.run`` fake, and checks that the report names ``path:line: target`` and exits 0.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "tests" / "test_module_seam.py"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"


def _reporter():
    spec = importlib.util.spec_from_file_location("class_a_report_seam", REPO / "scripts" / "class_a_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["class_a_report_seam"] = module
    spec.loader.exec_module(module)
    return module


def _planted(tmp_path: Path, planted_source: str) -> Path:
    """A scratch test file that points the real lint at a one-file tests directory, with the allowlist emptied so the
    only thing it can find is what was planted, then runs the real test function."""
    tests = tmp_path / "planted" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_planted.py").write_text(textwrap.dedent(planted_source))
    scratch = tmp_path / "scratch" / "test_seam_scratch.py"
    scratch.parent.mkdir()
    scratch.write_text(textwrap.dedent(f"""
        from pathlib import Path

        import tests.test_module_seam as seam


        def test_the_real_lint_over_the_planted_tree(monkeypatch):
            monkeypatch.setattr(seam, "_TESTS", Path({str(tests)!r}))
            monkeypatch.setattr(seam, "ALLOWED", {{}})
            seam.test_no_test_patches_a_stdlib_module_process_wide()
    """))
    return scratch


def _report(tmp_path: Path, capsys, scratch: Path, monkeypatch) -> tuple[int, str]:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p))
    code = _reporter().main([
        "--name", "module_seam", "--min-tests", "1", "--summary", str(tmp_path / "summary.md"), "--",
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(scratch.parent), "-q", str(scratch),
    ])
    return code, capsys.readouterr().out


def test_a_process_wide_stdlib_fake_is_listed_with_path_line_and_target_and_does_not_fail(tmp_path, capsys, monkeypatch):
    scratch = _planted(tmp_path, '''
        def test_x(monkeypatch):
            import subprocess
            monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    ''')
    code, out = _report(tmp_path, capsys, scratch, monkeypatch)
    assert code == 0, out
    assert "0 passed, 1 failed, 0 skipped" in out
    assert "::warning title=class-A module_seam::" in out
    assert "process-wide stdlib patches (use tests/_module_seam.py)" in out
    assert "tests/test_planted.py:4: subprocess.run" in out, "the item names the file, the line and the faked target"
    assert "tests/test_planted.py:4: subprocess.run" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_a_module_local_fake_is_clean(tmp_path, capsys, monkeypatch):
    scratch = _planted(tmp_path, '''
        def test_x(monkeypatch):
            from tests._module_seam import setattr_in
            setattr_in(monkeypatch, object(), "subprocess.run", lambda *a, **k: None)
    ''')
    code, out = _report(tmp_path, capsys, scratch, monkeypatch)
    assert code == 0, out
    assert "1 passed, 0 failed, 0 skipped" in out and "::warning" not in out


def test_only_the_two_repo_state_scans_carry_class_a():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    marked, unmarked = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(ast.unparse(d) == "pytest.mark.lint" for d in node.decorator_list):
            (marked if any(ast.unparse(d) == "pytest.mark.class_a" for d in node.decorator_list) else unmarked).add(node.name)
    assert marked == {"test_no_test_patches_a_stdlib_module_process_wide", "test_the_lint_has_sites_to_check"}
    assert {"test_the_lint_sees_each_global_form", "test_the_lint_passes_local_forms"} <= unmarked, "the lint's own self-tests keep blocking"


def test_the_advisory_job_runs_class_a_and_the_blocking_lint_job_leaves_it_out():
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert 'pytest -m "lint and not class_a"' in "\n".join(s.get("run", "") for s in doc["jobs"]["test-lint"]["steps"])
    advisory = "\n".join(s.get("run", "") for s in doc["jobs"]["test-class-a-report"]["steps"])
    assert "pytest -m class_a" in advisory
    assert "test-class-a-report" not in doc["jobs"]["pytest-gate"]["needs"]


def test_the_reporter_calls_carry_this_checks_own_floor_of_two():
    nightly = yaml.safe_load((REPO / ".github" / "workflows" / "local-service-gate-nightly.yml").read_text(encoding="utf-8"))["jobs"]["slow"]["steps"]
    ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["test-class-a-report"]["steps"]
    for steps in (ci, nightly):
        run = " ".join(next(s for s in steps if "-- uv run pytest -m class_a" in " ".join(s.get("run", "").split()))["run"].replace("\\\n", " ").split())
        assert "--require tests/test_module_seam=2 " in run + " "
