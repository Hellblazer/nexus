# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/select_docs_reader_tests.py`` and its ci.yml wiring (nexus-q99w4)."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from tests.test_ci_release_ledger_gate import _pytest_gate_script, _run

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "select_docs_reader_tests", REPO_ROOT / "scripts" / "select_docs_reader_tests.py",
)
assert _SPEC and _SPEC.loader
sel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sel)


def test_real_tree_selects_the_incident_test_and_clears_the_floor() -> None:
    files = sel.select(REPO_ROOT)
    # The red that motivated the lane (15f4c7a94) must be in it.
    assert "tests/catalog/test_rdr_dependency_edges.py" in files
    assert "tests/test_docs_reference_rot.py" in files
    assert len(files) >= sel.MIN_SELECTED, files


@pytest.mark.parametrize(
    "body",
    [
        'REPO_ROOT = Path(__file__).parents[1]\nX = REPO_ROOT / "docs" / "rdr"\n',
        'x = REPO_ROOT / "docs/rdr/README.md"\n',
        'p = Path(__file__).resolve().parents[2] / "docs"\n',
        'p = Path.cwd() / "docs"\n',
    ],
)
def test_repo_anchored_docs_selects(tmp_path: pathlib.Path, body: str) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(body)
    assert sel.select(tmp_path) == ["tests/test_x.py"]


@pytest.mark.parametrize(
    "body",
    [
        'coll = "docs"\n',
        'd = tmp_path / "docs"\n',
        '# REPO_ROOT / "docs" in a comment\n',
    ],
)
def test_unanchored_docs_does_not_select(tmp_path: pathlib.Path, body: str) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(body)
    assert sel.select(tmp_path) == []


def test_main_refuses_below_the_floor(tmp_path: pathlib.Path, capsys) -> None:
    (tmp_path / "tests").mkdir()
    assert sel.main(["--repo", str(tmp_path)]) == 1
    assert "fewer than the floor" in capsys.readouterr().err


def test_ci_runs_the_selector_on_docs_only_pushes() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/select_docs_reader_tests.py" in ci
    assert "test-docs-readers:" in ci
    # The fan-in must require it, or a red here would not block anything.
    assert "needs.test-docs-readers.result" in ci


# -- pytest-gate: the docs-reader leg must RUN on a doc-only diff -------------


_DOC_ONLY = {
    "needs.changes.outputs.code": "false",
    "needs.test.result": "skipped",
    "needs.test-lint.result": "skipped",
    "needs.test-mode-census.result": "skipped",
}


def test_fanin_passes_doc_only_when_docs_readers_succeeded() -> None:
    proc = _run(_pytest_gate_script(**_DOC_ONLY, **{"needs.test-docs-readers.result": "success"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_fanin_fails_doc_only_when_docs_readers_did_not_succeed(result: str) -> None:
    proc = _run(_pytest_gate_script(**_DOC_ONLY, **{"needs.test-docs-readers.result": result}))
    assert proc.returncode != 0
    assert "test-docs-readers" in proc.stdout + proc.stderr


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_fanin_fails_code_push_on_a_broken_docs_readers_leg(result: str) -> None:
    proc = _run(_pytest_gate_script(**{"needs.test-docs-readers.result": result}))
    assert proc.returncode != 0
