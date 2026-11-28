# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 2 (nexus-2jomg.2): ``docs_reference_rot`` is a class-A check in report mode.

Sam's ruling (2026-11-27): it never fails develop; the open citations are listed and fixed in each 2-week review's
batch. These tests pin the behaviour and the layout that carries it:

* the real test module, run through ``scripts/class_a_report.py`` over a scratch repository with one planted
  dangling citation, lists that citation (``path:line: value``) and exits 0; without it, the report is clean;
* the module carries ``class_a`` beside ``lint`` (the advisory job and the marker are in ``test_class_a_layout.py``);
* ``test-docs-readers`` and the push gate's docs-readers proof take their files from the selector, which no longer
  names it.
"""
from __future__ import annotations

import ast
import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
MODULE = REPO / "tests" / "test_docs_reference_rot.py"


def _load_reporter():
    spec = importlib.util.spec_from_file_location("class_a_report_rot", REPO / "scripts" / "class_a_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["class_a_report_rot"] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _scratch_repo(tmp_path: Path, *, dangling: bool) -> Path:
    """A repository holding the REAL test module, a published commit (a tag) cited 120 times, and, when asked, one
    commit-shaped citation that names nothing."""
    root = tmp_path / "repo"
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "docs" / "rdr").mkdir(parents=True)
    shutil.copy(MODULE, root / "tests" / "test_docs_reference_rot.py")
    (root / "tests" / "fixtures" / "bead_ids.txt").write_text("nexus-aaaaa\n")
    _git(root, "init", "-q")
    for attempt in range(40):  # a short sha with a letter and a digit is what the scan reads as a commit
        (root / "seed.txt").write_text(f"seed {attempt}\n")
        _git(root, "add", "seed.txt")
        _git(root, "commit", "-q", "-m", f"seed {attempt}")
        sha = _git(root, "rev-parse", "--short=9", "HEAD")
        if any(c.isalpha() for c in sha) and any(c.isdigit() for c in sha):
            break
        _git(root, "reset", "-q", "--hard", "HEAD~1")
    else:  # pragma: no cover
        raise AssertionError("no commit with a mixed sha in 40 tries")
    _git(root, "tag", "v0")
    lines = [f"Landed in commit {sha} (line {i})." for i in range(120)]
    if dangling:
        lines.append("A branch commit that was squashed away: 0badc0de1.")
    (root / "docs" / "rdr" / "note.md").write_text("\n".join(lines) + "\n")
    return root


def _report(root: Path, capsys, summary: Path) -> tuple[int, str]:
    reporter = _load_reporter()
    code = reporter.main([
        "--name", "docs_reference_rot", "--min-tests", "1", "--summary", str(summary), "--",
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(root), "-q",
        "-m", "class_a", "-k", "test_cited_commit_shas_resolve", str(root / "tests" / "test_docs_reference_rot.py"),
    ])
    return code, capsys.readouterr().out


def test_a_dangling_citation_is_listed_with_its_path_and_line_and_does_not_fail(tmp_path, capsys):
    root = _scratch_repo(tmp_path, dangling=True)
    summary = tmp_path / "summary.md"
    code, out = _report(root, capsys, summary)
    assert code == 0, out
    assert "0 passed, 1 failed, 0 skipped" in out
    assert "::warning title=class-A docs_reference_rot::" in out
    assert "tests/test_docs_reference_rot.py::test_cited_commit_shas_resolve" in out
    assert "docs/rdr/note.md:121: 0badc0de1" in out, "the item names the file, the line and the cited value"
    assert "CLASS_A_ITEM test_docs_reference_rot docs/rdr/note.md:121\t0badc0de1" in out, "and is a fixed-format line"
    assert "docs/rdr/note.md:121: 0badc0de1" in summary.read_text(encoding="utf-8")


def test_without_a_dangling_citation_the_report_is_clean(tmp_path, capsys):
    root = _scratch_repo(tmp_path, dangling=False)
    code, out = _report(root, capsys, tmp_path / "summary.md")
    assert code == 0, out
    assert "1 passed, 0 failed, 0 skipped" in out and "::warning" not in out


#: The tests that read the repo's own docs. The planted and blob tests pin the module's scanner and keep blocking.
REPO_STATE_TESTS = {
    "test_cited_commit_shas_resolve", "test_sha_allowlist_carries_no_dead_rows", "test_cited_rdr_ids_resolve",
    "test_cited_jdr_ids_resolve", "test_cited_bead_ids_resolve", "test_bead_id_manifest_is_fresh_where_bd_is",
}


def test_only_the_six_repo_state_tests_carry_class_a_and_every_test_keeps_lint():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    module_marks = next(ast.unparse(n.value) for n in tree.body
                        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in n.targets))
    assert module_marks == "pytest.mark.lint", "the module-level mark stays lint alone: class_a is per function"
    marked, unmarked = set(), set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            (marked if any(ast.unparse(d) == "pytest.mark.class_a" for d in node.decorator_list) else unmarked).add(node.name)
    assert marked == REPO_STATE_TESTS
    assert {"test_planted_bogus_sha_is_detected", "test_planted_bogus_rdr_is_detected", "test_planted_bogus_bead_is_detected",
            "test_planted_bogus_jdr_is_detected", "test_a_cited_blob_sha_resolves", "test_a_real_published_sha_resolves",
            "test_a_planted_bogus_sha_still_fails_next_to_a_blob", "test_a_blob_present_only_locally_stays_unresolved"} <= unmarked


def _doc() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _reporter_floors(run: str) -> list[str]:
    argv = shlex.split(" ".join(run.replace("\\\n", " ").split()).split("scripts/class_a_report.py", 1)[1].split(" -- ", 1)[0])
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--require"]


def test_the_advisory_job_and_the_nightly_job_each_carry_this_checks_own_floor():
    floor = f"tests/test_docs_reference_rot={len(REPO_STATE_TESTS) - 1}"   # the bd freshness test skips on a runner with no bd
    ci = next(s for s in _doc()["jobs"]["test-class-a-report"]["steps"] if "class_a_report.py" in s.get("run", ""))["run"]
    nightly = next(s for s in yaml.safe_load((REPO / ".github" / "workflows" / "local-service-gate-nightly.yml").read_text(encoding="utf-8"))
                   ["jobs"]["slow"]["steps"] if "-- uv run pytest -m class_a" in " ".join(s.get("run", "").split()))["run"]
    assert floor in _reporter_floors(ci) and floor in _reporter_floors(nightly)


def test_the_docs_readers_selector_and_so_the_docs_readers_job_and_proof_leave_it_out():
    spec = importlib.util.spec_from_file_location("select_docs_reader_tests_rot", REPO / "scripts" / "select_docs_reader_tests.py")
    selector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selector)
    assert "tests/test_docs_reference_rot.py" not in selector.select(REPO)
    # and the job takes its files from the selector, so it cannot run the module another way
    run = next(s["run"] for s in _doc()["jobs"]["test-docs-readers"]["steps"] if "select_docs_reader_tests.py" in s.get("run", ""))
    assert "test_docs_reference_rot" not in run


@pytest.mark.parametrize("job", ["test", "test-docs-readers"])
def test_no_other_blocking_job_names_the_module(job):
    runs = "\n".join(s.get("run", "") for s in _doc()["jobs"][job]["steps"])
    assert "test_docs_reference_rot" not in runs
