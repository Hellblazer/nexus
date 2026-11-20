# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P1.1 (nexus-p2m1x.1) fixture tests for the widened client census:
one new raw parse site of each new shape, plus the negatives. Test Plan:
"a new raw parse site of each shape is added to a fixture file. Verify: the
client census fails, naming the shape." Split from
``tests/test_collection_name_parse_census.py`` to keep that file under the
1000-line test cap.
"""
from __future__ import annotations

import pathlib

import pytest

from tests import _collection_name_census_scan as _scan

pytestmark = pytest.mark.lint

#: shape -> (fixture source, 1-based line the hit must sit on). Every fixture
#: is a NEW raw parse site of that shape; the unwidened scanner matches none.
_NEW_SHAPE_FIXTURES: dict[str, tuple[str, int]] = {
    "fstring_prefix": (
        'def f(name, x):\n    return name.startswith(f"{x}__")\n', 2),
    "fstring_prefix_via_local": (
        'def f(name, x):\n    p = f"{x}__"\n    return name.endswith(p)\n', 3),
    "const_prefix": (
        '_PFX = ("code__", "docs__")\n\ndef f(name):\n'
        '    return name.startswith(_PFX)\n', 4),
    "const_prefix_attribute_form": (
        '_PFX = ("code__", "docs__")\n\ndef f(name, mod):\n'
        '    return name.endswith(mod._PFX)\n', 4),
    "loop_prefix_over_dict_items": (
        '_MAP = {"code__": 1, "docs__": 2}\n\ndef f(name):\n'
        '    for pfx, v in _MAP.items():\n'
        '        if name.startswith(pfx):\n            return v\n', 5),
    "loop_prefix_over_tuple_of_pairs": (
        '_PAIRS = (("code__", "a"), ("docs__", "b"))\n\ndef f(name):\n'
        '    return [v for p, v in _PAIRS if name.startswith(p)]\n', 4),
    "loop_prefix_over_literal_tuple": (
        'def f(name):\n    for p in ("rdr__", "knowledge__"):\n'
        '        if name.startswith(p):\n            return p\n', 3),
    "substring": (
        'def f(name, o):\n    return "__repo__" in name\n', 2),
    "substring_fstring": (
        'def f(name, o):\n    return f"__{o}__" in name\n', 2),
    "substring_via_local": (
        'def f(name, o):\n    seg = f"__{o}__"\n    return seg in name\n', 3),
    "taxonomy_literal_tuple": (
        'def f(name):\n    return name.startswith(("taxonomy__", "tmp__"))\n', 2),
    "glob_default": ('EXCLUDE = ["code__*"]\n', 1),
    "sql_prefix_starts_with": (
        'Q = "SELECT 1 FROM t WHERE starts_with(coll, \'rdr__\')"\n', 1),
    "sql_prefix_like": (
        'Q = "SELECT 1 FROM t WHERE coll LIKE \'docs__%\'"\n', 1),
    "sql_prefix_split_part": (
        'Q = "SELECT split_part(coll, \'__\', 1) FROM t"\n', 1),
    "fnmatch_attribute": (
        'import fnmatch\n\ndef f(name, pat):\n'
        '    return fnmatch.fnmatch(name, pat)\n', 4),
    "fnmatchcase_bare": (
        'from fnmatch import fnmatchcase\n\ndef f(name, pat):\n'
        '    return fnmatchcase(name, pat)\n', 4),
    "derived_helper_not_a_typed_primitive": (
        'def f(client, name):\n    ensure_collection_registered(client, name)\n', 2),
    "derived_helper_is_conformant": (
        'def f(name):\n    return is_conformant_collection_name(name)\n', 2),
}

#: The shape each fixture above must be reported as.
_NEW_SHAPE_EXPECTED: dict[str, str] = {
    "fstring_prefix": "fstring_prefix",
    "fstring_prefix_via_local": "fstring_prefix",
    "const_prefix": "const_prefix",
    "const_prefix_attribute_form": "const_prefix",
    "loop_prefix_over_dict_items": "loop_prefix",
    "loop_prefix_over_tuple_of_pairs": "loop_prefix",
    "loop_prefix_over_literal_tuple": "loop_prefix",
    "substring": "substring",
    "substring_fstring": "substring",
    "substring_via_local": "substring",
    "taxonomy_literal_tuple": "startswith",
    "glob_default": "glob_default",
    "sql_prefix_starts_with": "sql_prefix",
    "sql_prefix_like": "sql_prefix",
    "sql_prefix_split_part": "sql_prefix",
    "fnmatch_attribute": "fnmatch",
    "fnmatchcase_bare": "fnmatch",
    "derived_helper_not_a_typed_primitive": "primitive_call",
    "derived_helper_is_conformant": "primitive_call",
}


@pytest.mark.parametrize("fixture_id", sorted(_NEW_SHAPE_FIXTURES))
def test_each_new_shape_is_matched_and_named(
    fixture_id: str, tmp_path: pathlib.Path,
) -> None:
    source, lineno = _NEW_SHAPE_FIXTURES[fixture_id]
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    assert _scan.scan_file(sample) == [(lineno, _NEW_SHAPE_EXPECTED[fixture_id])]


@pytest.mark.parametrize("fixture_id", sorted(_NEW_SHAPE_FIXTURES))
def test_a_new_site_of_each_shape_fails_the_census_naming_the_shape(
    fixture_id: str, tmp_path: pathlib.Path,
) -> None:
    """Test Plan scenario: the census, run over a fixture tree that holds one
    new raw site, refuses it as UNLABELLED and the message names the shape."""
    source, lineno = _NEW_SHAPE_FIXTURES[fixture_id]
    sample = tmp_path / "new_site.py"
    sample.write_text(source, encoding="utf-8")
    live = _scan.live_counter({"src/nexus/new_site.py": _scan.scan_file(sample)})
    unlabelled, stale = _scan.diff_ledger(live, {})
    shape = _NEW_SHAPE_EXPECTED[fixture_id]
    assert not stale
    assert unlabelled == [
        f"src/nexus/new_site.py:{lineno} shape={shape}: "
        "UNLABELLED, add a ledger entry with a class"
    ]


_NEGATIVE_FIXTURES: dict[str, str] = {
    "docstring_comment_and_bare_string_hold_the_tokens": (
        'def f():\n    """code__ docs__ __x__ code__* name.startswith("code__")"""\n'
        '    # name.startswith("code__") and "__x__" in name\n'
        '    "code__*"\n    return 1\n'),
    "mcp_tool_name_is_not_a_prefix": (
        'def f(t):\n    return t.startswith("mcp__")\n'),
    "fstring_without_a_dunder_tail": (
        'def f(name, x):\n    return name.startswith(f"{x}-")\n'),
    "loop_over_a_non_prefix_literal": (
        'def f(name):\n    return [p for p in ("a", "b") if name.startswith(p)]\n'),
    "dict_constant_with_no_prefix_key": (
        '_MAP = {"a": 1}\n\ndef f(name):\n    return name.startswith(_MAP)\n'),
    "nested_function_parameter_shadows_the_outer_loop_name": (
        # taxonomy_cmd._find_by_prefix (229-research-39): a doc-id prefix
        # test whose parameter name collides with a loop name in its parent.
        '_PFX = ("code__",)\n\ndef outer(names):\n    for prefix in _PFX:\n'
        '        pass\n    def inner(prefix):\n'
        '        return names[0].startswith(prefix)\n    return inner\n'),
    "prose_that_says_like_and_from": (
        # mcp/core.py (229-research-39): user-facing text, not SQL.
        'MSG = "Pass names FROM here: prefixes like \'knowledge\' or \'code\' miss."\n'),
    "substring_with_a_single_leading_dunder_only": (
        'def f(name, o):\n    return f"__{o}" in name\n'),
    "local_f_string_in_a_list_membership": (
        # corpus.py/migration.py (229-research-21): `x in matches` is a list.
        'def f(matches, u):\n    prefix = f"{u}__"\n    return prefix in matches\n'),
}


@pytest.mark.parametrize("fixture_id", sorted(_NEGATIVE_FIXTURES))
def test_negative_fixtures_match_nothing(
    fixture_id: str, tmp_path: pathlib.Path,
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(_NEGATIVE_FIXTURES[fixture_id], encoding="utf-8")
    assert _scan.scan_file(sample) == []


