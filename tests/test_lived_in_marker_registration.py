# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-edwlp Task 4: ``lived_in`` marker registration + application.

The local-service functional gate (tests/e2e/local-service-gate.sh) runs
``-m "integration and not lived_in"``. This test pins the two halves of
that contract:

1. ``lived_in`` is registered in ``[tool.pytest.ini_options] markers`` in
   pyproject.toml, so pytest's ``--strict-markers`` (if ever enabled) and
   ``pytest --markers`` both recognise it rather than treating an
   unregistered marker as a silent typo.
2. Each of the five class-D (real ``claude -p`` / seeded-corpora) test
   files carries ``pytest.mark.lived_in`` in its module-level
   ``pytestmark``, so ``-m "integration and not lived_in"`` deselects them
   entirely while ``-m integration`` (no exclusion) still collects them.

The per-file check is a source-substring smoke test, not a collection
assertion — the behavioral bound lives in the gate script itself, which
asserts the ``-m "integration and lived_in"`` collect-only count equals
``LIVED_IN_EXPECTED`` exactly before every run (nexus-no210: marker drift
moves tests into pytest's ``deselected`` bucket, invisible to the
passed/skipped guard).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_LIVED_IN_FILES = (
    "tests/test_hybrid_plan_factual_qa.py",
    "tests/test_abstract_themes_plan_integration.py",
    "tests/integration/test_rdr_088_operator_pipelines.py",
    "tests/integration/test_rdr_093_groupby_aggregate_pipelines.py",
    "tests/integration/test_nx_answer_equivalence.py",
)


def test_lived_in_marker_registered_in_pyproject() -> None:
    """``lived_in`` appears in [tool.pytest.ini_options] markers."""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("lived_in:") or m == "lived_in" for m in markers), (
        f"'lived_in' marker not registered in pyproject.toml markers: {markers}"
    )


@pytest.mark.parametrize("rel_path", _LIVED_IN_FILES)
def test_file_carries_lived_in_marker(rel_path: str) -> None:
    """Each class-D file's pytestmark includes pytest.mark.lived_in."""
    source = (_REPO_ROOT / rel_path).read_text()
    assert "pytest.mark.lived_in" in source, (
        f"{rel_path} must carry pytest.mark.lived_in in its pytestmark so "
        "'-m \"integration and not lived_in\"' deselects it"
    )
    assert "pytest.mark.integration" in source, (
        f"{rel_path} must still carry pytest.mark.integration (lived_in is "
        "additive, not a replacement)"
    )
