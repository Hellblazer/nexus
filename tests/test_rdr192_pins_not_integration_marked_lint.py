# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Lint: the RDR-192 client pins must not carry ``pytest.mark.integration``.

The default ``addopts`` exclude ``integration``, and no routine gate selects
it (open bug nexus-rpaat), so an integration-marked pin runs in no PR CI at
all. nexus-wbfpw.38 dropped the marker from these files because each needs
only the self-provisioned engine substrate (``t2_service_env``). This lint
keeps the marker from silently coming back, at module level
(``pytestmark``), as a decorator, or as a ``pytest.param`` mark.

If a pin genuinely needs a live cloud, API keys, ``cloud_mode`` or
``lived_in``, remove it from ``RDR192_PIN_FILES`` in the same change and say
why in the commit; do not re-add the marker to a listed file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_TESTS = Path(__file__).resolve().parent

RDR192_PIN_FILES: tuple[str, ...] = (
    "test_wbfpw31_nxexp_owner.py",
    "test_wbfpw2_client_liveness_matrix.py",
    "test_bb6n2_supersede_reap.py",
    "test_livec_census_sql.py",
    "test_t3_census_manifest_less.py",
    "test_0ntxj_failed_index_run_leaves_no_hidden_chunks.py",
    "test_wbfpw10_backfill_reverse_reads_unowned.py",
    "test_o8dil5_expire_manifest_reap.py",
    "test_rdr191_gc_serverside_prune.py",
)


def _is_integration_mark(node: ast.AST) -> bool:
    """True for ``pytest.mark.integration`` or ``pytest.mark.integration(...)``."""
    if isinstance(node, ast.Call):
        node = node.func
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "integration"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    )


def integration_mark_lines(source: str) -> list[int]:
    """Line numbers of every ``pytest.mark.integration`` use in ``source``."""
    return sorted(
        {n.lineno for n in ast.walk(ast.parse(source)) if _is_integration_mark(n)}
    )


def test_rdr192_pin_files_carry_no_integration_marker() -> None:
    missing = [f for f in RDR192_PIN_FILES if not (_TESTS / f).is_file()]
    assert not missing, (
        f"RDR-192 pin files listed here no longer exist: {missing}. "
        "A renamed pin must be renamed here too, or this lint checks nothing."
    )
    offenders = {
        f: lines
        for f in RDR192_PIN_FILES
        if (lines := integration_mark_lines((_TESTS / f).read_text()))
    }
    assert not offenders, (
        "RDR-192 client pins must run in CI's default selection, but these "
        f"carry pytest.mark.integration (file: lines): {offenders}. "
        "Integration-marked tests run in no routine gate (nexus-rpaat); the "
        "marker was removed on purpose in nexus-wbfpw.38. Drop it, or remove "
        "the file from RDR192_PIN_FILES with a stated reason."
    )


def test_marker_detector_sees_module_decorator_and_call_forms() -> None:
    """Non-vacuity: the AST matcher finds each spelling of the marker."""
    assert integration_mark_lines(
        "import pytest\npytestmark = pytest.mark.integration\n"
    ) == [2]
    assert integration_mark_lines(
        "import pytest\npytestmark = [pytest.mark.integration]\n"
    ) == [2]
    assert integration_mark_lines(
        "import pytest\n@pytest.mark.integration\ndef test_x(): ...\n"
    ) == [2]
    assert integration_mark_lines(
        "import pytest\n@pytest.mark.integration()\ndef test_x(): ...\n"
    ) == [2]
    assert integration_mark_lines("import pytest\n@pytest.mark.lint\ndef t(): ...\n") == []
