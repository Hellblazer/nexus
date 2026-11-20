# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-229 item 9 non-stranding guards are all still there (P2.1, nexus-p2m1x.6).

Item 9: Phase 2 may repoint a reader off the name only after every migration source it serves has a test that
runs a row-less legacy name through it. Those guards stay in the suite until P4.4 retires the fallback they
guard, and a guard that is deleted, renamed or emptied would let a reader repoint strand a restore or an upgrade
without a red test. This lint lists them by name, reads the test files from disk (never ``session.items``, so a
``-m lint`` or ``-k`` run sees the same thing), and fails when

* a listed Python guard or the Java class or one of its tests is missing,
* a guard's docstring (Java: its javadoc) lost the phrase ``non-stranding guard (RDR-229 item 9; keep until P4.4)``,
* fewer guards are present than the floor below (a rewrite that empties both lists cannot pass).

THE CHANGE THAT RETIRES THE FALLBACK (P4.4) DELETES THE GUARDS AND THIS LIST TOGETHER. Until then, adding a guard
adds its name here; removing one is a decision, not a cleanup.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parent.parent
_PHRASE = "non-stranding guard (rdr-229 item 9; keep until p4.4)"

_PY_FILE = _REPO / "tests" / "test_rdr229_non_stranding_guards.py"
#: The client migration sources, in order: recovery bundle (3), migrate-fallback (5), the indexer's legacy-collection
#: migration (1, run for two shapes, two catalogs and both modes), the ``.nxexp`` import (2). The engine's ETL import
#: is the Java class below.
_PY_GUARDS = (
    "test_a_bundle_note_recorded_under_a_name_with_no_row_restores_to_the_targets_collection",
    "test_a_two_segment_and_a_four_segment_bundle_name_restore_with_the_same_attributes",
    "test_a_bundle_recorded_in_the_other_mode_restores_to_a_cloud_targets_model",
    "test_migrate_fallback_gives_a_row_less_target_the_model_its_legacy_source_name_dispatches_to",
    "test_migrate_fallback_a_source_row_naming_another_model_than_its_name_dispatches_to",
    "test_migrate_fallback_an_explicit_target_model_wins_over_the_name_dispatch",
    "test_migrate_fallback_a_source_with_no_row_is_refused_not_stranded_silently",
    "test_migrate_fallback_a_conformant_named_source_with_a_row_is_refused_and_nothing_is_written",
    "test_the_indexer_migrates_a_legacy_collection_with_no_row_to_the_write_models_name",
    "test_an_nxexp_import_into_a_target_with_no_row_names_the_model_the_target_registers_with",
    "test_an_nxexp_import_into_a_target_that_has_a_row_names_the_rows_model",
)
_MIN_PY_GUARDS = 11

_JAVA_FILE = (
    _REPO / "service" / "src" / "test" / "java" / "dev" / "nexus" / "service" / "CatalogEtlImportNonStrandingTest.java"
)
_JAVA_GUARDS = (
    "batchImport_rowlessLegacyNames_deriveLifecycleFromTheNameWhenTheRowCarriesNone",
    "singleImport_rowlessLegacyNames_deriveLifecycleFromTheNameWhenTheRowCarriesNone",
    "batchImport_aRowCarryingALifecycleStateThatAgreesWithItsName_landsInThatState",
)
_MIN_JAVA_GUARDS = 3


def _python_guard_nodes() -> dict[str, ast.FunctionDef]:
    """Every top-level test function of the guards module."""
    tree = ast.parse(_PY_FILE.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def _python_guards() -> dict[str, str]:
    """Every top-level test function of the guards module, with its docstring."""
    return {name: (ast.get_docstring(node) or "") for name, node in _python_guard_nodes().items()}


def _asserts_something(node: ast.FunctionDef) -> bool:
    """A guard body that still checks: an ``assert`` statement or a ``pytest.raises`` block inside it."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "raises":
            return True
    return False


def test_every_python_non_stranding_guard_is_present_and_says_why_it_stays():
    """The Python half of the inventory (RDR-229 item 9; guards stay until P4.4, which also deletes this list)."""
    found = _python_guards()

    missing = [name for name in _PY_GUARDS if name not in found]
    assert not missing, f"non-stranding guards removed or renamed (P4.4 is the only change that may): {missing}"
    unlisted = sorted(set(found) - set(_PY_GUARDS))
    assert not unlisted, f"a guard exists that this inventory does not list: add it: {unlisted}"
    assert len(found) >= _MIN_PY_GUARDS, f"only {len(found)} guards present, the floor is {_MIN_PY_GUARDS}"
    unmarked = [name for name in _PY_GUARDS if _PHRASE not in " ".join(found[name].lower().split())]
    assert not unmarked, f"guard docstrings without the phrase {_PHRASE!r}: {unmarked}"
    nodes = _python_guard_nodes()
    emptied = [name for name in _PY_GUARDS if not _asserts_something(nodes[name])]
    assert not emptied, f"guards whose body no longer asserts anything (emptied or gutted): {emptied}"


def test_the_java_etl_import_guard_class_and_its_tests_are_present_and_say_why_they_stay():
    """The engine half of the inventory: the class and each test, by name, with the phrase in the class javadoc
    and above every test (RDR-229 item 9; guards stay until P4.4, which also deletes this list)."""
    assert _JAVA_FILE.is_file(), f"{_JAVA_FILE} is gone: the ETL-import non-stranding guard (P4.4 only)"
    text = _JAVA_FILE.read_text(encoding="utf-8")

    tests = re.findall(r"@Test\s+void\s+(\w+)\s*\(", text)
    missing = [name for name in _JAVA_GUARDS if name not in tests]
    assert not missing, f"Java non-stranding guards removed or renamed (P4.4 is the only change that may): {missing}"
    assert len(tests) >= _MIN_JAVA_GUARDS, f"only {len(tests)} Java guards present, the floor is {_MIN_JAVA_GUARDS}"
    assert sorted(set(tests) - set(_JAVA_GUARDS)) == [], "a Java guard exists that this inventory does not list"
    squashed = " ".join(text.lower().replace("*", " ").replace("//", " ").split())
    assert squashed.count(_PHRASE) >= len(tests) + 1, (
        f"the phrase {_PHRASE!r} must appear in the class javadoc and above each of the {len(tests)} tests"
    )
