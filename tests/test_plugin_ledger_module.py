# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/plugin_ledger.py owns the ledger and Phase-2 helpers (nexus-rbmja.2).

The drift-ledger test outlives the anchored-tag cut script, so the helpers
it imports must live in a module that does not depend on that script.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

LEDGER_TEST_SYMBOLS = (
    "deferred_paths",
    "phase2_deferral_check",
    "phase2_in_play",
    "phase2_guarded_paths",
    "PHASE2_GUARDED",
    "PHASE2_SAFE_ALONE",
)


@pytest.mark.parametrize("name", LEDGER_TEST_SYMBOLS)
def test_symbol_imports_from_plugin_ledger(name: str) -> None:
    import plugin_ledger

    assert hasattr(plugin_ledger, name), f"scripts/plugin_ledger.py lacks {name}"


def test_plugin_ledger_does_not_import_the_cut_script() -> None:
    tree = ast.parse((SCRIPTS / "plugin_ledger.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "cut_plugin_release" not in imported


def test_cut_script_reuses_the_single_copy() -> None:
    """The cut script imports back; two copies of a parser drift."""
    import cut_plugin_release as cut
    import plugin_ledger as ledger

    for name in (*LEDGER_TEST_SYMBOLS, "_deferred_entries", "_ledger_blocks"):
        assert getattr(cut, name) is getattr(ledger, name), name
    assert cut.CutRefused is ledger.LedgerRefused


def test_drift_ledger_test_no_longer_names_the_cut_script() -> None:
    text = (REPO / "tests" / "test_plugin_release_drift_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "cut_plugin_release" not in text
