# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the unified language registry (RDR-028)."""
from pathlib import Path

import pytest


# ── Phase 1: Registry consistency tests ──────────────────────────────────────


def test_registry_exists_and_has_minimum_entries():
    from nexus.languages import LANGUAGE_REGISTRY
    assert len(LANGUAGE_REGISTRY) >= 27


@pytest.fixture()
def registry():
    from nexus.languages import LANGUAGE_REGISTRY
    return LANGUAGE_REGISTRY


def test_tsx_maps_to_tsx(registry):
    assert registry[".tsx"] == "tsx"


def test_every_entry_has_valid_parser(registry):
    from tree_sitter_language_pack import get_parser
    # tree-sitter-language-pack uses "csharp" not "c_sharp"
    _PARSER_ALIASES = {"c_sharp": "csharp"}
    for ext, lang in registry.items():
        parser_name = _PARSER_ALIASES.get(lang, lang)
        parser = get_parser(parser_name)
        assert parser is not None, f"No parser for {ext} -> {lang}"


def test_definition_types_subset_of_registry(registry):
    from nexus.indexer import DEFINITION_TYPES
    registry_values = set(registry.values())
    for lang in DEFINITION_TYPES:
        assert lang in registry_values, (
            f"{lang} in DEFINITION_TYPES but not in LANGUAGE_REGISTRY values"
        )


def test_comment_chars_subset_of_registry(registry):
    from nexus.indexer import _COMMENT_CHARS
    registry_values = set(registry.values())
    for lang in _COMMENT_CHARS:
        assert lang in registry_values, (
            f"{lang} in _COMMENT_CHARS but not in LANGUAGE_REGISTRY values"
        )


def test_all_registry_keys_in_code_extensions(registry):
    from nexus.classifier import _CODE_EXTENSIONS
    for ext in registry:
        assert ext in _CODE_EXTENSIONS, (
            f"{ext} in LANGUAGE_REGISTRY but not in _CODE_EXTENSIONS"
        )


def test_tsx_has_definition_types():
    from nexus.indexer import DEFINITION_TYPES
    assert "tsx" in DEFINITION_TYPES


def test_tsx_has_comment_chars():
    from nexus.indexer import _COMMENT_CHARS
    assert "tsx" in _COMMENT_CHARS
    assert _COMMENT_CHARS["tsx"] == "//"


# ── Phase 2: Language expansion tests ────────────────────────────────────────

_NEW_EXTENSIONS = {
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".dart": "dart",
    ".zig": "zig",
    ".jl": "julia",
    ".el": "elisp",
    ".erl": "erlang", ".hrl": "erlang",
    ".ml": "ocaml", ".mli": "ocaml_interface",
    ".pl": "perl", ".pm": "perl",
}


def test_registry_expanded(registry):
    assert len(registry) >= 44  # 28 base + 16 new


@pytest.mark.parametrize("ext,lang", _NEW_EXTENSIONS.items())
def test_new_extension_in_registry(ext, lang, registry):
    assert ext in registry, f"{ext} not in LANGUAGE_REGISTRY"
    assert registry[ext] == lang


@pytest.mark.parametrize("ext", _NEW_EXTENSIONS.keys())
def test_new_extension_classified_as_code(ext):
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(f"file{ext}")) == ContentClass.CODE


_NEW_COMMENT_CHARS = {
    "proto": "//", "elixir": "#", "haskell": "--",
    "clojure": ";", "dart": "//", "zig": "//",
    "julia": "#", "elisp": ";", "erlang": "%",
    "ocaml": "(*", "ocaml_interface": "(*", "perl": "#",
}


@pytest.mark.parametrize("lang,char", _NEW_COMMENT_CHARS.items())
def test_new_language_has_comment_char(lang, char):
    from nexus.indexer import _COMMENT_CHARS
    assert lang in _COMMENT_CHARS, f"{lang} missing from _COMMENT_CHARS"
    assert _COMMENT_CHARS[lang] == char


_NEW_DEFINITION_TYPES_LANGS = ["dart", "haskell", "julia", "ocaml", "perl", "erlang"]


@pytest.mark.parametrize("lang", _NEW_DEFINITION_TYPES_LANGS)
def test_new_language_has_definition_types(lang):
    from nexus.indexer import DEFINITION_TYPES
    assert lang in DEFINITION_TYPES, f"{lang} missing from DEFINITION_TYPES"
    assert len(DEFINITION_TYPES[lang]) >= 1
