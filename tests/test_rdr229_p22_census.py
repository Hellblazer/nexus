# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P2.2 (nexus-p2m1x.7): the census after the parsers moved.

Item 1 of the Technical Design leaves ``legacy_names`` as the parser definitions and the
migration-source floor, and every other name read a classified census caller in one of five classes.
These guards pin what P2.2 changed:

- no module outside ``legacy_names`` defines a name-parsing regex;
- the derived helper set takes its roots from ``legacy_names`` (the parser
  definitions moved there, so deriving from ``corpus.py`` alone would find
  nothing and every caller would drop out of the census silently);
- calls inside ``legacy_names`` are the floor, not sites;
- no ledger entry is still waiting on P2.2, and the pin fell.
"""
from __future__ import annotations

import ast

import pytest

from tests import _collection_name_census_scan as _scan
from tests._collection_name_census_ledger import LEDGER
from tests.test_collection_name_parse_census import PARSE_SITE_PIN

pytestmark = pytest.mark.lint

LEGACY_REL = "src/nexus/catalog/rows/legacy_names.py"

#: Module-level regexes with a ``__`` in them that parse no collection name.
_NOT_COLLECTION_NAMES = {
    "src/nexus/memory_curate/cold.py": "splits an mcp__<server>__<tool> tool name",
    "src/nexus/commands/daemon.py": "matches the __NX_BIN__ / __CONFIG_DIR_ARGV__ placeholders of a launchd template",
}


def _pattern_text(call: ast.Call) -> str:
    return "".join(
        n.value for a in call.args[:1] for n in ast.walk(a)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _module_level_regexes_with_a_dunder(tree: ast.Module) -> list[int]:
    out: list[int] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        call = node.value
        if (
            isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "compile"
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "re"
            and "__" in _pattern_text(call)
        ):
            out.append(node.lineno)
    return out


def test_the_name_regexes_are_defined_only_in_legacy_names() -> None:
    found: dict[str, list[int]] = {}
    for path in _scan._py_files(_scan.SRC):
        rel = path.relative_to(_scan.REPO_ROOT).as_posix()
        tree = _scan._parse(path)
        assert tree is not None, rel
        hits = _module_level_regexes_with_a_dunder(tree)
        if hits:
            found[rel] = hits
    assert LEGACY_REL in found, "legacy_names defines the conformant and the split regexes"
    assert len(found[LEGACY_REL]) == 2
    outside = {rel for rel in found if rel != LEGACY_REL}
    assert outside == set(_NOT_COLLECTION_NAMES), (
        f"a collection-name regex is defined outside legacy_names (or an exemption went stale): {sorted(outside)}"
    )


def test_the_derived_helper_set_roots_in_legacy_names() -> None:
    helpers = _scan.live_parser_helpers()
    assert {
        "is_conformant_collection_name", "parse_conformant_collection_name",
        "embedding_model_for_collection_name", "model_version_for_collection_name",
        "split_candidate_collection_name", "candidate_knowledge_subject", "candidate_attributes",
    } <= helpers
    # corpus.py's own callers reach them through the import, so they stay derived.
    assert {"ensure_collection_registered", "t3_collection_name", "embedding_model_for_collection"} <= helpers
    legacy_tree = _scan._parse(_scan.REPO_ROOT / LEGACY_REL)
    assert legacy_tree is not None
    legacy_fns = set(_scan._module_functions(legacy_tree))
    assert legacy_fns & helpers >= {"candidate_attributes", "split_candidate_collection_name"}


def test_calls_inside_legacy_names_are_the_floor_not_sites() -> None:
    assert LEGACY_REL not in _scan.raw_sites()
    assert not [s for s in LEDGER if s.rel == LEGACY_REL]


def test_the_primitives_tuple_is_single_sourced_in_legacy_names() -> None:
    from nexus.catalog.rows.legacy_names import _CANDIDATE_STRING_PRIMITIVES

    assert set(_CANDIDATE_STRING_PRIMITIVES) <= _scan.live_parser_helpers()


def test_no_ledger_entry_still_waits_on_p22() -> None:
    """The 19 read-path model reads the P1.1 census held over for P2.2 are resolved: each reads the
    row through ``resolve_collection`` (its NoRow derivation is one counted site in ``rows/resolve.py``),
    or has its own class. Nothing is left with P2.2 as its owner."""
    waiting = [
        f"{s.rel} {s.anchor[-1]!r}" for s in LEDGER
        if s.cls == "holdover" and "P2.2" in s.note
    ]
    assert not waiting, waiting
    holdover_steps = {step for s in LEDGER if s.cls == "holdover" for step in _scan._STEP_RE.findall(s.note)}
    assert holdover_steps <= {"P2.4"}, holdover_steps


def test_the_pin_fell_from_the_p29_count() -> None:
    """P2.9's reland pinned 229. P2.2 moves 13 of the 19 holdover sites to the row (they leave the ledger) and reclassifies the other 6 (five
    ``T3Database`` reads to mint, ``doctor_search``'s drift probe to diagnostic), 229 -> 218 on P2.2's own tree. On
    the Phase 2 integration tree P2.4 (-3) and owner-is-source (+2: the two callers of ``legacy_names.owner_stem`` in
    ``nxexp_owner.py``; the parse itself sits in the floor and is not a site) leave 217, measured by the scan
    (``test_pins_are_the_measured_counts_by_total_class_and_shape`` checks it). RDR-229 P3.1 (nexus-p2m1x.17) leaves 215:
    its four register-before-write seams and HttpCatalogClient's three manifest registrations route through the one
    ``registration_retry.register_collection_for_write`` primitive, which the census counts once. P3.8 (nexus-p2m1x.28, -2: ``register_collection``
    derives nothing from the name) leaves 213."""
    assert PARSE_SITE_PIN == 213


def test_collection_attributes_is_one_type() -> None:
    """RDR item 1 names one ``CollectionAttributes``. ``collection_shape`` used to define a second, a row-or-name
    shape view for the audit (string version, a ``quarantine`` flag, a ``source``): it is ``CollectionShape`` now."""
    defined = []
    for path in _scan._py_files(_scan.SRC):
        tree = _scan._parse(path)
        assert tree is not None
        defined += [
            path.relative_to(_scan.REPO_ROOT).as_posix() for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "CollectionAttributes"
        ]
    assert defined == ["src/nexus/catalog/collection_name.py"], defined
