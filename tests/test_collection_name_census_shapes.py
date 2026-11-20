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


#: Review round 1 (RDR-229 P1.1): fixture id -> (source, line, expected shape).
_REVIEW_FIXTURES: dict[str, tuple[str, int, str]] = {
    # item 1: concatenated and formatted prefixes
    "concat_plus": (
        'def f(entry, parent):\n    return entry.startswith(parent + "__")\n',
        2, "concat_prefix"),
    "concat_percent": (
        'def f(name, o):\n    return name.startswith("%s__" % o)\n', 2, "concat_prefix"),
    "concat_format": (
        'def f(name, o):\n    return name.endswith("{}__".format(o))\n',
        2, "concat_prefix"),
    "concat_via_local": (
        'def f(name, o):\n    p = o + "__"\n    return name.startswith(p)\n',
        3, "concat_prefix"),
    # item 2: bindings beyond a plain Assign, and the prefix sources
    "binding_annassign": (
        'def f(name, x):\n    p: str = f"{x}__"\n    return name.startswith(p)\n',
        3, "fstring_prefix"),
    "binding_walrus": (
        'def f(name, x):\n    if (p := f"{x}__") and name.startswith(p):\n'
        '        return 1\n', 2, "fstring_prefix"),
    "binding_walrus_inline": (
        'def f(name, x):\n    return name.startswith(p := f"{x}__")\n',
        2, "fstring_prefix"),
    "binding_tuple_unpack": (
        'def f(name, x, y):\n    a, b = f"{x}__", f"{y}__"\n'
        '    return name.startswith(b)\n', 3, "fstring_prefix"),
    "binding_local_literal": (
        'def f(name):\n    p = "code__"\n    return name.startswith(p)\n',
        3, "local_prefix"),
    "binding_local_literal_tuple": (
        'def f(name):\n    ps = ("code__", "docs__")\n    return name.endswith(ps)\n',
        3, "local_prefix"),
    "const_from_generator": (
        '_TYPES = ("a", "b")\n_PFX = tuple(f"{t}__" for t in _TYPES)\n\n'
        'def f(name):\n    return name.startswith(_PFX)\n', 5, "const_prefix"),
    # item 3: strip-prefix, find, split(sep=), a named "__" constant
    "strip_removeprefix": (
        'def f(name):\n    return name.removeprefix("docs__")\n', 2, "strip_prefix"),
    "strip_removesuffix": (
        'def f(name):\n    return name.removesuffix("rdr__")\n', 2, "strip_prefix"),
    "strip_replace": (
        'def f(name):\n    return name.replace("knowledge__", "")\n', 2, "strip_prefix"),
    "find_dunder": (
        'def f(name):\n    return name.find("__")\n', 2, "dunder_find"),
    "index_dunder": (
        'def f(name):\n    return name.index("__")\n', 2, "dunder_find"),
    "count_dunder": (
        'def f(name):\n    return name.count("__")\n', 2, "dunder_find"),
    "split_sep_keyword": (
        'def f(name):\n    return name.split(sep="__")\n', 2, "split"),
    "split_named_constant": (
        '_SEP = "__"\n\ndef f(name):\n    return name.split(_SEP)\n', 4, "split"),
    "in_named_constant": (
        '_SEP = "__"\n\ndef f(name):\n    return _SEP in name\n', 4, "in"),
    # item 6 and 12
    "hash_suffix_rsplit": (
        'def f(collection_name):\n    return collection_name.rsplit("-", 1)\n',
        2, "hash_suffix"),
    "hash_suffix_endswith": (
        'def f(collection_name, h):\n    parts = collection_name.rsplit("-", 1)\n'
        '    m = lambda n: n.endswith(f"-{h}")\n    return parts, m\n',
        3, "hash_suffix"),
    "endswith_family_marker": (
        'def f(scope):\n    return scope.endswith("__")\n', 2, "endswith"),
}
for _fid, (_src, _ln, _shape) in _REVIEW_FIXTURES.items():
    _NEW_SHAPE_FIXTURES[_fid] = (_src, _ln)
    _NEW_SHAPE_EXPECTED[_fid] = _shape

#: A fixture whose source holds a second hit of the same shape (the hash-suffix
#: ``endswith`` leg needs the ``rsplit`` that names it).
_EXTRA_HITS: dict[str, list[tuple[int, str]]] = {
    "hash_suffix_endswith": [(2, "hash_suffix")],
}


def _expected_hits(fixture_id: str, lineno: int) -> list[tuple[int, str]]:
    return sorted(
        [(lineno, _NEW_SHAPE_EXPECTED[fixture_id]), *_EXTRA_HITS.get(fixture_id, [])]
    )


@pytest.mark.parametrize("fixture_id", sorted(_NEW_SHAPE_FIXTURES))
def test_each_new_shape_is_matched_and_named(
    fixture_id: str, tmp_path: pathlib.Path,
) -> None:
    source, lineno = _NEW_SHAPE_FIXTURES[fixture_id]
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    assert _scan.scan_file(sample) == _expected_hits(fixture_id, lineno)


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
    assert not stale
    assert unlabelled == [
        f"src/nexus/new_site.py:{ln} shape={shape}: "
        "UNLABELLED, add a ledger entry with a class"
        for ln, shape in _expected_hits(fixture_id, lineno)
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


_REVIEW_NEGATIVES: dict[str, str] = {
    # item 11: a Python dunder is not a collection owner segment
    "pycache_in_path_parts": (
        'def f(p):\n    return "__pycache__" in p.parts\n'),
    "init_in_name": ('def f(name):\n    return "__init__" in name\n'),
    "main_in_argv": ('def f(a):\n    return "__main__" in a\n'),
    # item 6: a hash tail of something that is not a collection name
    "rsplit_hyphen_on_a_model_token": (
        'def f(token):\n    return token.rsplit("-", 1)[-1]\n'),
    "endswith_hyphen_fstring_without_a_collection_rsplit": (
        'def f(name, h):\n    return name.endswith(f"-{h}")\n'),
    # item 3: replace of an ordinary string, a plain concat
    "replace_of_an_ordinary_token": (
        'def f(name):\n    return name.replace("_", " ")\n'),
    "startswith_concat_without_a_dunder": (
        'def f(name, p):\n    return name.startswith(p + "_")\n'),
    "local_prefix_literal_that_is_not_a_prefix": (
        'def f(name):\n    p = "mcp__"\n    return name.startswith(p)\n'),
    "walrus_binding_that_is_not_a_prefix": (
        'def f(name, x):\n    return name.startswith(p := f"{x}-")\n'),
}
for _fid, _src in _REVIEW_NEGATIVES.items():
    _NEGATIVE_FIXTURES[_fid] = _src


@pytest.mark.parametrize("fixture_id", sorted(_NEGATIVE_FIXTURES))
def test_negative_fixtures_match_nothing(
    fixture_id: str, tmp_path: pathlib.Path,
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(_NEGATIVE_FIXTURES[fixture_id], encoding="utf-8")
    assert _scan.scan_file(sample) == []




# ---------------------------------------------------------------------------
# The production path (review round 1, item 5), and the unparsed-file guard.
# ---------------------------------------------------------------------------


def _write_tree(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    src = root / "src" / "nexus"
    for rel, text in files.items():
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return src


def test_production_path_names_a_cross_file_constant_shape(tmp_path: pathlib.Path) -> None:
    """``raw_sites`` over a tmp ``src/``: a prefix constant defined in one file
    and imported into another is matched as ``const_prefix`` in the importer
    (constants are collected across the tree), and ``diff_ledger`` refuses it
    naming the shape."""
    src = _write_tree(tmp_path, {
        "consts.py": '_PFX = ("docs__", "rdr__")\n',
        "user.py": (
            "from nexus.consts import _PFX\n\n"
            "def is_prose(name):\n    return name.startswith(_PFX)\n"
        ),
    })
    raw = _scan.raw_sites(src, tmp_path)
    assert raw == {"src/nexus/user.py": [(4, "const_prefix")]}
    unlabelled, stale = _scan.diff_ledger(_scan.live_counter(raw), {})
    assert not stale
    assert unlabelled == [
        "src/nexus/user.py:4 shape=const_prefix: UNLABELLED, add a ledger entry with a class"
    ]


def test_production_path_refuses_a_file_that_does_not_parse(tmp_path: pathlib.Path) -> None:
    src = _write_tree(tmp_path, {"ok.py": "x = 1\n", "broken.py": "def f(:\n"})
    with pytest.raises(ValueError, match=r"did not parse.*broken\.py"):
        _scan.raw_sites(src, tmp_path)


def test_a_dunder_name_constant_is_followed_across_files(tmp_path: pathlib.Path) -> None:
    src = _write_tree(tmp_path, {
        "seps.py": 'SEP = "__"\n',
        "user.py": "from nexus.seps import SEP\n\ndef f(n):\n    return n.split(SEP)\n",
    })
    assert _scan.raw_sites(src, tmp_path) == {"src/nexus/user.py": [(4, "split")]}
