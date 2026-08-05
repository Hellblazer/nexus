"""Lint: the `scenario` journey layer cannot silently vanish (round-2 critique,
T2 nexus/test-suite-compression-P2-reduced-round2-critique).

The runtime non-vacuity guard in tests/conftest.py is deliberately inert when
zero `scenario` tests are selected — a shard that receives none must not fail.
That leaves two whole-suite removal paths silent: excluding the marker in
addopts, and deleting/emptying the journeys file. Both defeat the guard AND
tests/test_marker_selection_coverage.py (its `scenario` consumer lives in
conftest code, independent of any scenario test existing). This lint pins both
paths; it is `lint`-marked, so it runs in the per-PR test-lint CI job.
"""

from __future__ import annotations

import ast
import pathlib
import tomllib

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_JOURNEYS_FILE = _REPO_ROOT / "tests" / "test_scenario_journeys.py"
_MIN_SCENARIO_TESTS = 4


def test_addopts_never_excludes_scenario() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "not scenario" not in addopts, (
        "addopts excludes the `scenario` marker — the journey layer would "
        "silently leave the default loop, and the conftest non-vacuity guard "
        "is inert at zero-selected by design. Scenarios run in the default "
        "loop; remove the exclusion."
    )


def test_journeys_file_carries_scenario_tests() -> None:
    assert _JOURNEYS_FILE.is_file(), (
        f"{_JOURNEYS_FILE.name} is missing — the promoted journey layer is "
        "gone. Deleting it is a coverage decision that must retire this lint "
        "explicitly, never a silent side effect."
    )
    tree = ast.parse(_JOURNEYS_FILE.read_text())
    module_marked = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        )
        and "scenario" in ast.dump(node.value)
        for node in tree.body
    )
    test_fns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]

    def _fn_marked(fn: ast.FunctionDef) -> bool:
        return any("scenario" in ast.dump(d) for d in fn.decorator_list)

    marked = [fn for fn in test_fns if module_marked or _fn_marked(fn)]
    assert len(marked) >= _MIN_SCENARIO_TESTS, (
        f"{_JOURNEYS_FILE.name} has {len(marked)} scenario-marked test "
        f"functions, expected >= {_MIN_SCENARIO_TESTS}. If a journey was "
        "deliberately retired, lower _MIN_SCENARIO_TESTS in the same commit "
        "with the rationale."
    )
