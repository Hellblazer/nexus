# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 (nexus-2jomg.2): the advisory-job layout every class-A pass/fail pytest check shares.

A report-mode check carries ``class_a`` beside ``lint``. ``test-lint`` selects ``lint and not class_a``, so the check no
longer blocks; the advisory ``test-class-a-report`` job selects ``class_a`` through ``scripts/class_a_report.py``, which
lists the failing items and exits 0. The job is outside pytest-gate's ``needs``. Each check's own demotion (its marker, its
other blocking paths) is pinned in its own ``test_class_a_<check>.py``; this file holds only what they share, so a demotion
can be reverted alone and the layout stays.
"""
from __future__ import annotations

import ast
import re
import shlex
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
NIGHTLY = REPO / ".github" / "workflows" / "local-service-gate-nightly.yml"


def _doc() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def test_the_class_a_marker_is_registered():
    markers = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("class_a:") for m in markers)


def test_the_blocking_lint_job_leaves_class_a_to_the_advisory_job():
    runs = "\n".join(s.get("run", "") for s in _doc()["jobs"]["test-lint"]["steps"])
    assert 'pytest -m "lint and not class_a"' in runs


def test_the_advisory_job_selects_class_a_through_the_reporter_and_runs_on_every_push():
    doc = _doc()
    job = doc["jobs"]["test-class-a-report"]
    assert "needs" not in job and "if" not in job, "a docs-reading check must run on a docs-only push too"
    step = next(s for s in job["steps"] if "class_a_report.py" in s.get("run", ""))
    run = " ".join(step["run"].replace("\\\n", " ").split())
    assert "scripts/class_a_report.py" in run and "--min-tests" in run and "-- uv run pytest -m class_a" in run
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0, "a check that resolves cited shas needs history"
    assert "test-class-a-report" not in doc["jobs"]["pytest-gate"]["needs"], "advisory: pytest-gate never waits on it"
    gate = "\n".join(s.get("run", "") for s in doc["jobs"]["pytest-gate"]["steps"])
    assert "test-class-a-report" not in gate


def test_the_advisory_job_floor_is_one_so_a_demotion_can_be_reverted_alone():
    step = next(s for s in _doc()["jobs"]["test-class-a-report"]["steps"] if "class_a_report.py" in s.get("run", ""))
    assert "--min-tests 1 " in " ".join(step["run"].split()) + " "


# -- every class_a test is also a lint test (the marker is only ever added beside lint) ------------------------------------


def _marks(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute) and sub.value.attr == "mark":
            names.add(sub.attr)
    return names


def class_a_without_lint(source: str) -> list[str]:
    """Names of the tests (and test classes) of *source* that carry ``class_a`` and do not carry ``lint``, counting the
    module's ``pytestmark`` and the enclosing classes' decorators and ``pytestmark``."""
    tree = ast.parse(source)
    module = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            module |= _marks(node.value)
    bad: list[str] = []

    def visit(body: list[ast.stmt], inherited: set[str], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                own = inherited | {m for d in node.decorator_list for m in _marks(d)}
                for sub in node.body:
                    if isinstance(sub, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in sub.targets):
                        own |= _marks(sub.value)
                visit(node.body, own, f"{prefix}{node.name}::")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own = inherited | {m for d in node.decorator_list for m in _marks(d)}
                if "class_a" in own and "lint" not in own:
                    bad.append(f"{prefix}{node.name}")

    visit(tree.body, module, "")
    return bad


def test_the_pin_sees_class_a_without_lint_and_passes_class_a_beside_lint():
    assert class_a_without_lint("import pytest\n@pytest.mark.class_a\ndef test_x():\n    pass\n") == ["test_x"]
    assert class_a_without_lint("import pytest\n@pytest.mark.lint\n@pytest.mark.class_a\ndef test_x():\n    pass\n") == []
    assert class_a_without_lint("import pytest\npytestmark = [pytest.mark.lint]\n@pytest.mark.class_a\ndef test_x():\n    pass\n") == []
    assert class_a_without_lint("import pytest\npytestmark = pytest.mark.class_a\ndef test_x():\n    pass\n") == ["test_x"]
    src = "import pytest\n@pytest.mark.lint\nclass TestA:\n    @pytest.mark.class_a\n    def test_x(self):\n        pass\n"
    assert class_a_without_lint(src) == []
    assert class_a_without_lint(src.replace("@pytest.mark.lint\n", "")) == ["TestA::test_x"]


def test_every_class_a_test_in_the_tree_also_carries_lint():
    offenders = []
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "class_a" in text:
            offenders += [f"{path.relative_to(REPO).as_posix()}::{n}" for n in class_a_without_lint(text)]
    assert offenders == [], f"class_a without lint: the marker is only ever added beside lint, so `pytest -m lint` still runs it: {offenders}"


# -- the reporter calls: floors, items file, artifact ----------------------------------------------------------------------


def _reporter_call(run: str) -> list[str]:
    return shlex.split(" ".join(run.replace("\\\n", " ").split()).split("scripts/class_a_report.py", 1)[1].split(" -- ", 1)[0])


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def _nightly_step() -> dict:
    steps = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]["slow"]["steps"]
    return next(s for s in steps if "-- uv run pytest -m class_a" in " ".join(s.get("run", "").split()))


def test_the_nightly_job_runs_the_same_reporter_with_the_same_floors_and_reds_only_on_a_structural_failure():
    step = _nightly_step()
    assert step["if"] == "always()" and step["env"]["NX_TEST_T2_SUBSTRATE"] == "none"
    assert "continue-on-error" not in step, "a floor missed or a crash must red the nightly: the reporter exits 0 on findings"
    nightly = _reporter_call(step["run"])
    ci_step = next(s for s in _doc()["jobs"]["test-class-a-report"]["steps"] if "class_a_report.py" in s.get("run", ""))
    ci = _reporter_call(ci_step["run"])
    assert sorted(_flag_values(nightly, "--require")) == sorted(_flag_values(ci, "--require")), "nightly and CI carry the same per-check floors"
    assert "pytest -m class_a" in step["run"]
    for argv in (nightly, ci):
        assert _flag_values(argv, "--min-tests") == ["1"] and len(_flag_values(argv, "--items-out")) == 1


def test_each_reporter_call_uploads_its_items_file_for_thirty_days():
    for job, steps in (("ci", _doc()["jobs"]["test-class-a-report"]["steps"]),
                       ("nightly", yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]["slow"]["steps"])):
        run_step = next(s for s in steps if "-- uv run pytest -m class_a" in " ".join(s.get("run", "").split()))
        out = _flag_values(_reporter_call(run_step["run"]), "--items-out")[0]
        upload = steps[steps.index(run_step) + 1]
        assert upload["uses"].startswith("actions/upload-artifact@"), job
        assert upload["with"]["path"] == out and upload["with"]["retention-days"] == 30, job
        assert upload["if"] == "always()", job


def test_every_floor_names_a_file_that_exists():
    """A floor's prefix is a node id prefix written without the `.py` (so the lab-entry sweep, which reads every `tests/x.py` in a CI
    step as a script CI runs, does not take it for one)."""
    for run in (next(s for s in _doc()["jobs"]["test-class-a-report"]["steps"] if "class_a_report.py" in s.get("run", ""))["run"],
                _nightly_step()["run"]):
        for prefix_n in _flag_values(_reporter_call(run), "--require"):
            prefix, _, n = prefix_n.rpartition("=")
            assert (REPO / (prefix.split("::", 1)[0] + ".py")).is_file() and n.isdigit() and int(n) >= 1, prefix_n


def test_the_pre_push_meta_test_sweep_leaves_class_a_to_the_advisory_job():
    from scripts import meta_tests  # noqa: PLC0415 -- scripts/ is not on the path at collection

    assert "not class_a" in meta_tests.META_RUN
    assert re.search(r"\(lint or census\) and not class_a", meta_tests.META_RUN)


def test_preflight_leaves_class_a_to_the_advisory_job_in_the_lint_bucket_step_and_the_changed_tests_step():
    """The push train's whole-lint-bucket step and its changed-tests step both select lint-marked tests, so both exclude
    class_a (as CI's test-lint does): a class-A check in report mode must not refuse a push locally where CI no longer refuses."""
    lua = (REPO / ".nexus" / "procedures" / "preflight.lua").read_text(encoding="utf-8")
    assert '-m \\"lint and not class_a\\" -n auto' in lua
    assert "-m 'not integration and not slow and not class_a'" in lua
    assert "-m lint -n auto" not in lua
    skill = (REPO / ".claude" / "skills" / "pre-push-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "Run the whole lint bucket, not a subset** (the class-A checks in it, marked `class_a`, report and do not refuse)" in skill
