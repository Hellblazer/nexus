# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client collection-name census derives parser roots across modules
(RDR-229 P1.R fix round, nexus-p2m1x.5). Companion to
``tests/test_collection_name_parse_census.py``; the mechanism and its limits are
in ``tests/_collection_name_census_cross.py``.

The live proofs are that the two roots the review found missing
(``corpus.resolve_corpus``, ``CollectionName.parse``) are derived, and that a
call to either from a module the census has never seen is a site. The fixture
proofs pin what is and is not a call to a root: an import-resolved call counts,
a same-named function elsewhere does not, a routing-only function is not a
root, an instance method is not resolved.
"""
from __future__ import annotations

import pathlib

import pytest

from tests import _collection_name_census_cross as _cross
from tests import _collection_name_census_scan as _scan

pytestmark = pytest.mark.lint

_COLLECTION_NAME_PY = '''\
from nexus.corpus import parse_conformant_collection_name


class CollectionName:
    @classmethod
    def parse(cls, name):
        return parse_conformant_collection_name(name)

    def render(self):
        return self.name

    def other(self, name):
        return name.upper()
'''


def _tree(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    src = tmp_path / "src" / "nexus"
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return src


def _hits(tmp_path: pathlib.Path, files: dict[str, str]) -> dict[str, list[tuple[int, str]]]:
    src = _tree(tmp_path, files)
    return _scan.raw_sites(src, tmp_path)


def test_the_two_roots_the_review_found_missing_are_derived() -> None:
    # resolve_corpus is a derived helper (its typed-value split reaches a parser). Since P2.7 it is
    # not a root of its own: its only site of its own is a label-pattern one, see the root-rule tests.
    assert "resolve_corpus" in _scan.live_parser_helpers()
    consts, dunders = _scan._live_tree_constants()
    roots = _cross.derive_roots(_scan.SRC, _scan.REPO_ROOT, consts, dunders)
    assert ("nexus.catalog.collection_name", "CollectionName", "parse") in roots.methods
    # non-vacuity: more than the two, and every root is a real module-level function or method
    assert len(roots.functions) + len(roots.methods) > 2


def test_live_callers_of_both_roots_are_counted() -> None:
    raw = _scan.raw_sites()
    sites = {(rel, ln) for rel, hits in raw.items() for ln, shape in hits if shape == "primitive_call"}
    # The eight the review named, found by content so a line shift cannot stale this.
    named = {
        "src/nexus/doctor_search.py": "return resolve_corpus(name, all_collections)",
        "src/nexus/commands/search_cmd.py": "matched = resolve_corpus(c, all_collections)",
        "src/nexus/commands/collection.py": "candidates = resolve_corpus(name, [c[\"name\"] for c in cols])",
        "src/nexus/sensitive_write.py": "return CollectionName.parse(collection_name)",
        "src/nexus/catalog/sensitivity_doctor.py": "token = CollectionName.parse(collection).embedding_model",
        # P2.2: the P1.4 fallback is legacy_names.version_of_blank_row_label now, a counted call of a derived helper
        "src/nexus/catalog/collection_name.py": 'return version_of_blank_row_label(row.get("name"), models=KNOWN_EMBEDDING_MODELS)',
    }
    for rel, text in named.items():
        lines = (_scan.REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
        at = [i + 1 for i, line in enumerate(lines) if text in line]
        assert len(at) == 1, (rel, text, at)
        assert (rel, at[0]) in sites, f"{rel}:{at[0]} is a call to a root and is not counted"
    core = (_scan.REPO_ROOT / "src/nexus/mcp/core.py").read_text(encoding="utf-8").splitlines()
    for text in ("fanned_out = resolve_corpus(part, all_names)", 'lambda: resolve_corpus("knowledge", _get_collection_names())'):
        at = [i + 1 for i, line in enumerate(core) if text in line]
        assert len(at) == 1 and ("src/nexus/mcp/core.py", at[0]) in sites, (text, at)


def test_a_new_caller_of_resolve_corpus_in_any_module_is_a_site(tmp_path: pathlib.Path) -> None:
    hits = _hits(tmp_path, {
        "newmod.py": "from nexus.corpus import resolve_corpus\n\n\ndef f(c, all_names):\n    return resolve_corpus(c, all_names)\n",
        "other.py": "import nexus.corpus as corpus_mod\n\n\ndef g(c, all_names):\n    return corpus_mod.resolve_corpus(c, all_names)\n",
    })
    assert hits == {
        "src/nexus/newmod.py": [(5, "primitive_call")],
        "src/nexus/other.py": [(5, "primitive_call")],
    }


def test_a_new_caller_of_collection_name_parse_in_any_module_is_a_site(tmp_path: pathlib.Path) -> None:
    hits = _hits(tmp_path, {
        "catalog/collection_name.py": _COLLECTION_NAME_PY,
        "newmod.py": (
            "def f(name):\n"
            "    from nexus.catalog.collection_name import CollectionName\n\n"
            "    return CollectionName.parse(name)\n"
        ),
        "viamodule.py": (
            "from nexus.catalog import collection_name\n\n\n"
            "def g(name):\n    return collection_name.CollectionName.parse(name)\n"
        ),
    })
    assert hits["src/nexus/newmod.py"] == [(4, "primitive_call")]
    assert hits["src/nexus/viamodule.py"] == [(5, "primitive_call")]


def test_a_same_named_function_elsewhere_is_not_a_call_to_the_root(tmp_path: pathlib.Path) -> None:
    hits = _hits(tmp_path, {
        "catalog/collection_name.py": _COLLECTION_NAME_PY,
        "json_like.py": "def parse(x):\n    return x\n\n\ndef f(x):\n    return parse(x)\n",
        "other_class.py": (
            "class Scope:\n    @classmethod\n    def parse(cls, x):\n        return x\n\n\n"
            "def g(x):\n    return Scope.parse(x)\n"
        ),
    })
    assert "src/nexus/json_like.py" not in hits
    assert "src/nexus/other_class.py" not in hits


def test_a_routing_only_function_is_not_a_root(tmp_path: pathlib.Path) -> None:
    """Limit 2 of the cross module: a function whose only counted site is a
    literal-prefix test answers 'which kind', it does not return a part."""
    hits = _hits(tmp_path, {
        "kinds.py": 'def is_code(n):\n    return n.startswith("code__")\n',
        "user.py": "from nexus.kinds import is_code\n\n\ndef f(n):\n    return is_code(n)\n",
    })
    assert hits == {"src/nexus/kinds.py": [(2, "startswith")]}


def test_a_decomposing_function_is_a_root_and_an_instance_method_is_not_resolved(
    tmp_path: pathlib.Path,
) -> None:
    hits = _hits(tmp_path, {
        "shape.py": (
            "def owner_of(n):\n    return n.split('__')[1]\n\n\n"
            "class Holder:\n    def owner(self, n):\n        return n.split('__')[1]\n"
        ),
        "user.py": (
            "from nexus.shape import Holder, owner_of\n\n\n"
            "def f(n, h):\n    a = owner_of(n)\n    b = h.owner(n)\n    c = Holder().owner(n)\n    return a, b, c\n"
        ),
    })
    assert hits["src/nexus/user.py"] == [(5, "primitive_call")]


def test_the_regex_prefix_shape_counts_a_compiled_collection_prefix(tmp_path: pathlib.Path) -> None:
    hits = _hits(tmp_path, {
        "rx.py": (
            "import re\n\n"
            '_Q = re.compile(r"^quarantine-(?P<origin>.+)$")\n'
            '_C = re.compile(r"^code__([a-z]+)$")\n'
            '_OTHER = re.compile(r"^abc$")\n'
        ),
    })
    assert hits == {"src/nexus/rx.py": [(3, "regex_prefix"), (4, "regex_prefix")]}


# ---------------------------------------------------------------------------
# THE ROOT RULE (RDR-229 P2.7): a function is a parser root iff it holds at
# least one counted PARSE-CLASS site now.
# ---------------------------------------------------------------------------

_LABEL_ONLY_PY = (
    "def match(corpus, names):\n"
    '    prefix = f"{corpus}__"\n'
    "    return [n for n in names if n.startswith(prefix)]\n"
)


def _spans(tmp_path: pathlib.Path, excluded: frozenset[tuple[str, int, str]]) -> list[tuple[str | None, str]]:
    src = _tree(tmp_path, {"labels.py": _LABEL_ONLY_PY})
    consts, dunders = _scan._live_tree_constants()
    return _cross._root_spans(src / "labels.py", "src/nexus/labels.py", consts, dunders, excluded)


def test_a_function_is_a_root_while_its_site_is_counted_and_stops_when_it_is_a_label_pattern(
    tmp_path: pathlib.Path,
) -> None:
    """The same function: with its site counted it is a root; with the site's key in the non-parse
    exclusions (what the ledger does for a label-pattern site) it is not."""
    src = _tree(tmp_path, {"labels.py": _LABEL_ONLY_PY})
    hit = next(
        (ln, shape) for ln, shape in _scan.scan_file(src / "labels.py") if shape in _cross.DECOMPOSING
    )
    key = ("src/nexus/labels.py", hit[0], hit[1])
    assert _spans(tmp_path, frozenset()) == [(None, "match")]
    assert _spans(tmp_path, frozenset({key})) == []


def test_the_live_exclusions_hold_exactly_the_non_parse_sites_of_the_ledger() -> None:
    """Derived from the ledger on every scan, never a list: every non-parse-class ledger site is in the
    exclusions, no parse-class (or holdover) site is, and the corpus.py label fallback is one of them."""
    from tests._collection_name_census_ledger import LEDGER  # noqa: PLC0415 - heavy import, one test

    resolved, problems = _scan.resolve_sites(_scan.REPO_ROOT, list(LEDGER))
    assert not problems
    excl = _cross.root_hit_exclusions()
    non_parse = {k for k, v in resolved.items() if v.cls in _scan.NON_PARSE_CLASSES}
    parse = {k for k, v in resolved.items() if v.cls not in _scan.NON_PARSE_CLASSES}
    assert non_parse and non_parse <= excl
    assert not (parse & (excl - _cross.excluded_hit_keys()))
    assert any(k[0] == _scan.CORPUS_REL and k[2] == "fstring_prefix" for k in non_parse)
    assert "resolve_corpus" not in _cross.corpus_roots()
