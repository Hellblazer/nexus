# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rpaat: the affected-integration selector that ci.yml runs per push.

Default addopts deselect ``-m integration``, so before this selector the
integration-marked tests ran only in the nightly local-service gate and the
release battery. A test added in the same push as the change it proves went
unrun until the next nightly (the 7.67.0 evidence table found four).

These tests build their own tiny repo trees, so they pin the selection rule
itself rather than today's corpus.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from tests.test_ci_release_ledger_gate import _pytest_gate_script, _run

import select_affected_integration_tests as sel

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "select_affected_integration_tests.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture()
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    _write(tmp_path, "src/nexus/__init__.py", "")
    _write(tmp_path, "src/nexus/db/__init__.py", "")
    _write(tmp_path, "src/nexus/db/client.py", "X = 1\n")
    _write(tmp_path, "src/nexus/other.py", "Y = 1\n")
    _write(
        tmp_path,
        "tests/test_a_integration.py",
        "import pytest\nfrom nexus.db.client import X\n"
        "pytestmark = pytest.mark.integration\n",
    )
    _write(
        tmp_path,
        "tests/db/test_b_integration.py",
        "import pytest\nfrom nexus.db import client\n\n"
        "@pytest.mark.integration\ndef test_b():\n    pass\n",
    )
    _write(
        tmp_path,
        "tests/test_c_integration.py",
        "import pytest\n\n@pytest.mark.integration\ndef test_c():\n"
        "    import nexus.other\n",
    )
    _write(
        tmp_path,
        "tests/test_unit.py",
        "from nexus.db.client import X\n\ndef test_u():\n    pass\n",
    )
    return tmp_path


def test_unrelated_change_selects_nothing(tree: pathlib.Path) -> None:
    assert sel.select(tree, ["docs/x.md", "service/pom.xml"]) == []


def test_changed_integration_file_selects_itself(tree: pathlib.Path) -> None:
    assert sel.select(tree, ["tests/test_c_integration.py"]) == [
        "tests/test_c_integration.py"
    ]


def test_changed_unit_test_file_is_not_selected(tree: pathlib.Path) -> None:
    assert sel.select(tree, ["tests/test_unit.py"]) == []


def test_changed_src_module_selects_every_importer(tree: pathlib.Path) -> None:
    # `from nexus.db.client import X` and `from nexus.db import client` both
    # name the module; the unit-test importer is not integration-marked.
    assert sel.select(tree, ["src/nexus/db/client.py"]) == [
        "tests/db/test_b_integration.py",
        "tests/test_a_integration.py",
    ]


def test_function_local_import_counts(tree: pathlib.Path) -> None:
    assert sel.select(tree, ["src/nexus/other.py"]) == [
        "tests/test_c_integration.py"
    ]


def test_package_init_maps_to_the_package(tree: pathlib.Path) -> None:
    # `from nexus.db import client` imports the nexus.db package.
    assert sel.select(tree, ["src/nexus/db/__init__.py"]) == [
        "tests/db/test_b_integration.py"
    ]


def test_deleted_file_is_not_selected(tree: pathlib.Path) -> None:
    assert sel.select(tree, ["tests/test_gone_integration.py"]) == []


def test_select_all(tree: pathlib.Path) -> None:
    assert sel.select_all(tree) == [
        "tests/db/test_b_integration.py",
        "tests/test_a_integration.py",
        "tests/test_c_integration.py",
    ]


def test_covered_elsewhere_files_are_excluded(tree: pathlib.Path) -> None:
    rel = next(iter(sel.COVERED_ELSEWHERE))
    _write(tree, rel, "import pytest\npytestmark = pytest.mark.integration\n")
    assert rel not in sel.select(tree, [rel])
    assert rel not in sel.select_all(tree)


def test_covered_elsewhere_names_real_files_that_ci_runs() -> None:
    """Each exclusion must point at a real integration file that ci.yml runs.

    An exclusion whose dedicated job was deleted would hide that file from
    every per-push gate, which is the gap this selector closes.
    """
    ci = CI_YML.read_text()
    assert sel.COVERED_ELSEWHERE, "exclusion table is empty; delete this test with it"
    for rel, reason in sel.COVERED_ELSEWHERE.items():
        assert (REPO_ROOT / rel).is_file(), rel
        assert reason.strip(), rel
        assert rel in ci, f"{rel} is excluded but no ci.yml job names it"


def test_real_corpus_is_not_vacuous() -> None:
    """The live tree must yield a substantial integration-file population.

    A broken marker regex or walk would select nothing for every push and the
    CI job would report "no affected integration tests" forever.
    """
    files = sel.select_all(REPO_ROOT)
    assert len(files) >= 80, len(files)
    assert "tests/test_6pbwx_owner_from_documents.py" in files


def test_cli_reads_paths_from_stdin(tree: pathlib.Path) -> None:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tree)],
        input="src/nexus/other.py\n\ndocs/a.md\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines() == ["tests/test_c_integration.py"]


def test_cli_all(tree: pathlib.Path) -> None:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tree), "--all"],
        input="",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(out.splitlines()) == 3


def test_script_is_stdlib_only() -> None:
    """ci.yml runs it under the runner's bare python3, before any uv sync."""
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    tree = ast.parse(SCRIPT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for n in names:
            assert n.split(".")[0] in allowed, n


def test_ci_mark_expression_matches_the_nightly_gate() -> None:
    """The per-push job and the nightly gate must select the same family."""
    # local-service-gate.sh sources the expression from this shared lib.
    lib = (REPO_ROOT / "tests" / "e2e" / "lib" / "mandatory_pins.sh").read_text()
    m = re.search(r'^LSG_PYTEST_MARK_EXPR="([^"]+)"', lib, re.M)
    assert m, "LSG_PYTEST_MARK_EXPR not found in tests/e2e/lib/mandatory_pins.sh"
    ci = CI_YML.read_text()
    c = re.search(r'INTEGRATION_MARK_EXPR: "([^"]+)"', ci)
    assert c, "INTEGRATION_MARK_EXPR not found in ci.yml"
    assert c.group(1) == m.group(1)


# -- the ci.yml selection step, executed verbatim -----------------------------

_GH_EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def _selection_step_script() -> str:
    doc = yaml.safe_load(CI_YML.read_text())
    steps = doc["jobs"]["changes"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "integration"]
    return step["run"]


def _render(values: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        assert expr in values, f"step references {expr!r}; update this test"
        return values[expr]

    return _GH_EXPR.sub(repl, _selection_step_script())


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_all(repo: pathlib.Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tree: pathlib.Path) -> pathlib.Path:
    (tree / "scripts").mkdir()
    for name in ("select_affected_integration_tests.py", "assert_integration_affected_ran.py"):
        shutil.copy(REPO_ROOT / "scripts" / name, tree / "scripts" / name)
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "Test")
    _commit_all(tree, "seed")
    return tree


def _run_step(repo: pathlib.Path, event: str, base: str) -> dict[str, str]:
    script = _render(
        {
            "github.event_name": event,
            "github.event.pull_request.base.sha": base,
            "github.event.before": base,
        }
    )
    out = repo / ".gh_output"
    env = dict(os.environ, GITHUB_OUTPUT=str(out))
    subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True, capture_output=True)
    pairs = [line.split("=", 1) for line in out.read_text().splitlines() if "=" in line]
    out.unlink()
    return dict(pairs)


def test_step_push_selects_importers(repo: pathlib.Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "src/nexus/other.py").write_text("Y = 2\n")
    _commit_all(repo, "change")
    assert _run_step(repo, "push", before) == {
        "integration_files": "tests/test_c_integration.py",
        "integration_any": "true",
    }


def test_step_pr_uses_three_dot_base(repo: pathlib.Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tests/test_a_integration.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\n"
    )
    _commit_all(repo, "change")
    assert _run_step(repo, "pull_request", base)["integration_files"] == (
        "tests/test_a_integration.py"
    )


def test_step_doc_change_selects_nothing(repo: pathlib.Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "docs").mkdir()
    (repo / "docs/x.md").write_text("x\n")
    _commit_all(repo, "docs")
    assert _run_step(repo, "push", before) == {
        "integration_files": "",
        "integration_any": "false",
    }


@pytest.mark.parametrize("base", ["", "0" * 40, "deadbeef" * 5])
def test_step_unusable_base_selects_everything(repo: pathlib.Path, base: str) -> None:
    out = _run_step(repo, "push", base)
    assert out["integration_any"] == "true"
    assert out["integration_files"].split() == sel.select_all(repo)


def test_step_selector_change_selects_everything(repo: pathlib.Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    with (repo / "scripts/select_affected_integration_tests.py").open("a") as f:
        f.write("# touched\n")
    _commit_all(repo, "touch selector")
    assert _run_step(repo, "push", before)["integration_files"].split() == sel.select_all(repo)


# -- pytest-gate requires the job exactly when something was selected --------


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_fanin_fails_when_selected_job_did_not_succeed(result: str) -> None:
    proc = _run(_pytest_gate_script(**{
        "needs.changes.outputs.integration_any": "true",
        "needs.test-integration-affected.result": result,
    }))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "test-integration-affected" in proc.stdout + proc.stderr


def test_fanin_passes_when_selected_job_succeeded() -> None:
    proc = _run(_pytest_gate_script(**{
        "needs.changes.outputs.integration_any": "true",
        "needs.test-integration-affected.result": "success",
    }))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.parametrize("result", ["skipped", "success"])
def test_fanin_tolerates_skip_when_nothing_selected(result: str) -> None:
    proc = _run(_pytest_gate_script(**{
        "needs.test-integration-affected.result": result,
    }))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.parametrize("any_", ["", "maybe"])
def test_fanin_fails_on_unrecognised_selection_flag(any_: str) -> None:
    proc = _run(_pytest_gate_script(**{
        "needs.changes.outputs.integration_any": any_,
    }))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
