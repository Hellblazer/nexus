# RDR-028: Language Registry Unification -- Implementation Plan

**RDR**: docs/rdr/rdr-028-code-search-recall.md (status: accepted)
**Parent bead**: nexus-bqjj (in_progress)
**Estimated LOC**: ~185 (new + changed)
**Risk**: Low -- dict unification, no architectural changes

## Executive Summary

Unify three drifted language-to-extension maps (`AST_EXTENSIONS`, `_EXT_TO_LANGUAGE`, `_CODE_EXTENSIONS`) into a single `LANGUAGE_REGISTRY` in a new `src/nexus/languages.py` module. Fix the `.tsx` inconsistency, close the 4-extension classifier gap, and add ~14 new languages from arcaneum's coverage set. All changes are backward-compatible via import aliases.

## Phase Breakdown

### Phase 1: Registry Foundation + Migration (~70 LOC)
**Bead**: nexus-luhd (P1)
**Rationale**: Establish the single source of truth before expanding. Fix existing bugs (.tsx inconsistency, 4 missing classifier extensions) immediately.

### Phase 2: Language Expansion (~80 LOC)
**Bead**: nexus-kyah (P2, blocked by nexus-luhd)
**Rationale**: With the registry in place, adding languages is a pure data addition -- no structural changes.

### Phase 4 (deferred): Alias Removal
**Bead**: nexus-mnzi (P3, blocked by nexus-kyah)
**Rationale**: After one release cycle, remove `AST_EXTENSIONS` and `_EXT_TO_LANGUAGE` aliases if no external imports exist.

## Dependency Graph

```
nexus-bqjj (parent epic, in_progress)
  |
  +-- nexus-luhd (Phase 1: Registry Foundation + Migration)
        |
        +-- nexus-kyah (Phase 2: Language Expansion)
              |
              +-- nexus-mnzi (Phase 4: Alias Removal, deferred)
```

Critical path: Phase 1 -> Phase 2 (sequential, no parallelization -- TDD requires test-then-implement ordering).

## Detailed Task Specifications

---

### Task 1.1: Write Failing Consistency Tests (TDD Red)

**Bead**: nexus-luhd
**File**: `tests/test_languages.py` (NEW)
**Command**: `uv run pytest tests/test_languages.py -v` (expect failures)

Write the following tests before any implementation exists:

```python
# tests/test_languages.py -- ~40 LOC

# Test A: LANGUAGE_REGISTRY importable, >= 27 entries
def test_registry_exists_and_has_minimum_entries():
    from nexus.languages import LANGUAGE_REGISTRY
    assert len(LANGUAGE_REGISTRY) >= 27

# Test B: Every entry has valid tree-sitter parser
@pytest.mark.parametrize("ext,lang", LANGUAGE_REGISTRY.items())
def test_registry_entry_has_valid_parser(ext, lang):
    from tree_sitter_language_pack import get_parser
    parser = get_parser(lang)
    assert parser is not None

# Test C: .tsx maps to "tsx" (not "typescript")
def test_tsx_maps_to_tsx():
    from nexus.languages import LANGUAGE_REGISTRY
    assert LANGUAGE_REGISTRY[".tsx"] == "tsx"

# Test D: Every DEFINITION_TYPES key is a LANGUAGE_REGISTRY value
def test_definition_types_subset_of_registry():
    from nexus.languages import LANGUAGE_REGISTRY
    from nexus.indexer import DEFINITION_TYPES
    registry_values = set(LANGUAGE_REGISTRY.values())
    for lang in DEFINITION_TYPES:
        assert lang in registry_values, f"{lang} in DEFINITION_TYPES but not in LANGUAGE_REGISTRY values"

# Test E: Every _COMMENT_CHARS key is a LANGUAGE_REGISTRY value
def test_comment_chars_subset_of_registry():
    from nexus.languages import LANGUAGE_REGISTRY
    from nexus.indexer import _COMMENT_CHARS
    registry_values = set(LANGUAGE_REGISTRY.values())
    for lang in _COMMENT_CHARS:
        assert lang in registry_values, f"{lang} in _COMMENT_CHARS but not in LANGUAGE_REGISTRY values"

# Test F: Every LANGUAGE_REGISTRY key is in _CODE_EXTENSIONS
def test_all_registry_keys_in_code_extensions():
    from nexus.languages import LANGUAGE_REGISTRY
    from nexus.classifier import _CODE_EXTENSIONS
    for ext in LANGUAGE_REGISTRY:
        assert ext in _CODE_EXTENSIONS, f"{ext} in LANGUAGE_REGISTRY but not in _CODE_EXTENSIONS"

# Test G: .tsx has DEFINITION_TYPES entry
def test_tsx_has_definition_types():
    from nexus.indexer import DEFINITION_TYPES
    assert "tsx" in DEFINITION_TYPES

# Test H: .tsx has _COMMENT_CHARS entry
def test_tsx_has_comment_chars():
    from nexus.indexer import _COMMENT_CHARS
    assert "tsx" in _COMMENT_CHARS
    assert _COMMENT_CHARS["tsx"] == "//"
```

**Acceptance criteria**:
- [ ] File exists at `tests/test_languages.py`
- [ ] All tests fail with ImportError or AssertionError (red phase)
- [ ] No other tests broken

---

### Task 1.2: Create Registry + Migrate Consumers (TDD Green)

**Bead**: nexus-luhd
**Files**:
- `src/nexus/languages.py` (NEW, ~30 LOC)
- `src/nexus/chunker.py` (MODIFY lines 12-51)
- `src/nexus/indexer.py` (MODIFY lines 32-60, 63-82, 87-179)
- `src/nexus/classifier.py` (MODIFY lines 22-29)

**Command**: `uv run pytest tests/test_languages.py -v` (expect pass)

#### Step A: Create `src/nexus/languages.py`

```python
# src/nexus/languages.py

"""Unified language registry for code indexing.

Single source of truth for extension-to-language mapping, consumed by
chunker.py (AST splitting), indexer.py (context extraction), and
classifier.py (CODE vs PROSE classification).
"""

LANGUAGE_REGISTRY: dict[str, str] = {
    # Core web/scripting
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",           # tree-sitter has separate tsx grammar
    # Systems languages
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    # JVM family
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    # .NET
    ".cs": "c_sharp",
    # Shell / scripting
    ".sh": "bash",
    ".bash": "bash",
    # Mobile / cross-platform
    ".swift": "swift",
    ".m": "objc",
    # Interpreted
    ".rb": "ruby",
    ".php": "php",
    ".r": "r",
    ".lua": "lua",
}

# Extensions classified as CODE but not in LANGUAGE_REGISTRY
# (no tree-sitter grammar or AST chunking needed).
GPU_SHADER_EXTENSIONS: frozenset[str] = frozenset({
    ".cl", ".comp", ".frag", ".vert", ".metal", ".glsl", ".wgsl", ".hlsl",
})
```

#### Step B: Migrate `src/nexus/chunker.py`

Replace the `AST_EXTENSIONS` dict literal (lines 12-51) with:

```python
from nexus.languages import LANGUAGE_REGISTRY

# Backward-compat alias; will be removed after one release cycle.
AST_EXTENSIONS = LANGUAGE_REGISTRY
```

Remove the comment block at lines 12-15 that explains the old dict.

#### Step C: Migrate `src/nexus/indexer.py`

Replace the `_EXT_TO_LANGUAGE` dict literal (lines 32-60) with:

```python
from nexus.languages import LANGUAGE_REGISTRY

# Backward-compat alias; will be removed after one release cycle.
_EXT_TO_LANGUAGE = LANGUAGE_REGISTRY
```

Add `"tsx"` to `_COMMENT_CHARS` (after line 66, alongside "typescript"):

```python
"tsx": "//",
```

Add `"tsx"` to `DEFINITION_TYPES` (after the "typescript" entry, line 107):

```python
"tsx": {
    "function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "arrow_function": "function",
},
```

#### Step D: Derive `_CODE_EXTENSIONS` in `src/nexus/classifier.py`

Replace the hardcoded `_CODE_EXTENSIONS` frozenset (lines 22-29) with:

```python
from nexus.languages import LANGUAGE_REGISTRY, GPU_SHADER_EXTENSIONS

# Derived from LANGUAGE_REGISTRY to prevent drift.
_CODE_EXTENSIONS: frozenset[str] = frozenset(LANGUAGE_REGISTRY.keys()) | GPU_SHADER_EXTENSIONS
```

This automatically adds the 4 previously missing extensions: `.lua`, `.cxx`, `.kts`, `.sc`.

**Acceptance criteria**:
- [ ] `src/nexus/languages.py` exists with LANGUAGE_REGISTRY (27 entries)
- [ ] `chunker.py` imports from `languages.py`, AST_EXTENSIONS is alias
- [ ] `indexer.py` imports from `languages.py`, _EXT_TO_LANGUAGE is alias
- [ ] `indexer.py` has "tsx" in both DEFINITION_TYPES and _COMMENT_CHARS
- [ ] `classifier.py` derives _CODE_EXTENSIONS from LANGUAGE_REGISTRY
- [ ] `uv run pytest tests/test_languages.py -v` all pass

---

### Task 1.3: Update Existing Tests (TDD Maintain Green)

**Bead**: nexus-luhd
**File**: `tests/test_classifier.py` (MODIFY, line 67-77)
**Command**: `uv run pytest` (full suite, all green)

Rewrite `test_code_extensions_set_matches_design` to verify the derivation property rather than hardcoding the exact set:

```python
def test_code_extensions_derived_from_registry():
    """_CODE_EXTENSIONS contains all LANGUAGE_REGISTRY keys plus GPU shader extensions."""
    from nexus.languages import LANGUAGE_REGISTRY, GPU_SHADER_EXTENSIONS
    from nexus.classifier import _CODE_EXTENSIONS
    expected = frozenset(LANGUAGE_REGISTRY.keys()) | GPU_SHADER_EXTENSIONS
    assert _CODE_EXTENSIONS == expected
```

Also verify the 4 previously missing extensions now classify correctly:

```python
@pytest.mark.parametrize("filename", [
    "script.lua", "main.cxx", "build.kts", "app.sc",
])
def test_previously_missing_code_extensions(filename: str):
    """Extensions that were in LANGUAGE_REGISTRY but missing from _CODE_EXTENSIONS."""
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(filename)) == ContentClass.CODE, f"{filename} should be CODE"
```

**Acceptance criteria**:
- [ ] `test_code_extensions_set_matches_design` replaced with derivation test
- [ ] 4 previously missing extensions verified as CODE
- [ ] `uv run pytest` full suite passes (zero failures)
- [ ] Phase 1 ready to commit

---

### Task 2.1: Write Failing Expansion Tests (TDD Red)

**Bead**: nexus-kyah
**File**: `tests/test_languages.py` (MODIFY, append)
**Command**: `uv run pytest tests/test_languages.py -v` (expect new failures)

Add tests for language expansion:

```python
# New language extensions to verify
_NEW_EXTENSIONS = {
    ".proto": "proto",
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

def test_registry_expanded():
    from nexus.languages import LANGUAGE_REGISTRY
    assert len(LANGUAGE_REGISTRY) >= 44  # 27 + 17

@pytest.mark.parametrize("ext,lang", _NEW_EXTENSIONS.items())
def test_new_extension_in_registry(ext, lang):
    from nexus.languages import LANGUAGE_REGISTRY
    assert ext in LANGUAGE_REGISTRY, f"{ext} not in LANGUAGE_REGISTRY"
    assert LANGUAGE_REGISTRY[ext] == lang

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
```

**Acceptance criteria**:
- [ ] New tests added to `tests/test_languages.py`
- [ ] New tests fail (red phase)
- [ ] Existing tests still pass

---

### Task 2.2: Add New Languages (TDD Green)

**Bead**: nexus-kyah
**Files**:
- `src/nexus/languages.py` (MODIFY, add entries)
- `src/nexus/indexer.py` (MODIFY, add _COMMENT_CHARS + DEFINITION_TYPES)

**Command**: `uv run pytest` (full suite, all green)

#### Step A: Expand LANGUAGE_REGISTRY in `src/nexus/languages.py`

Add after the existing entries:

```python
    # ── New languages (RDR-028 Phase 2) ──
    # Protocol / schema
    ".proto": "proto",
    # BEAM ecosystem
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    # Functional languages
    ".hs": "haskell",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".ml": "ocaml",
    ".mli": "ocaml_interface",
    ".el": "elisp",
    # Modern systems / app languages
    ".dart": "dart",
    ".zig": "zig",
    ".jl": "julia",
    # Scripting
    ".pl": "perl",
    ".pm": "perl",
```

#### Step B: Expand `_COMMENT_CHARS` in `src/nexus/indexer.py`

Add after existing entries:

```python
    "proto": "//",
    "elixir": "#",
    "haskell": "--",
    "clojure": ";",
    "dart": "//",
    "zig": "//",
    "julia": "#",
    "elisp": ";",
    "erlang": "%",
    "ocaml": "(*",
    "ocaml_interface": "(*",
    "perl": "#",
```

#### Step C: Expand `DEFINITION_TYPES` in `src/nexus/indexer.py`

Add after existing entries:

```python
    "dart": {
        "class_definition": "class",
        "method_signature": "method",
        "function_signature": "function",
    },
    "haskell": {
        "function": "function",
        "data_type": "class",
    },
    "julia": {
        "function_definition": "function",
        "struct_definition": "class",
    },
    "ocaml": {
        "value_definition": "function",
        "type_definition": "class",
        "module_definition": "module",
    },
    "perl": {
        "subroutine_declaration_statement": "function",
    },
    "erlang": {
        "function_clause": "function",
    },
```

**Note**: Languages without DEFINITION_TYPES (proto, elixir, clojure, zig, elisp, ocaml_interface) still get AST chunking and comment-char prefixes but no class/method context extraction. This is acceptable degradation per the RDR.

**Acceptance criteria**:
- [ ] LANGUAGE_REGISTRY has 44 entries (27 + 17)
- [ ] _COMMENT_CHARS covers all 30 LANGUAGE_REGISTRY language values
- [ ] DEFINITION_TYPES has entries for dart, haskell, julia, ocaml, perl, erlang
- [ ] `uv run pytest` full suite passes (zero failures)
- [ ] Phase 2 ready to commit

---

### Phase 4 (deferred): Alias Removal

**Bead**: nexus-mnzi (blocked by nexus-kyah)
**When**: After one release cycle following Phase 2 merge.

Steps:
1. Grep for imports of `AST_EXTENSIONS` and `_EXT_TO_LANGUAGE` outside the nexus package
2. If no external consumers, remove aliases:
   - `chunker.py`: Change `AST_EXTENSIONS = LANGUAGE_REGISTRY` to direct `LANGUAGE_REGISTRY` usage at line 200
   - `indexer.py`: Change `_EXT_TO_LANGUAGE = LANGUAGE_REGISTRY` to direct `LANGUAGE_REGISTRY` usage at line 519
3. Update tests that import `AST_EXTENSIONS` (currently `test_chunker.py` does NOT import it directly)
4. Run full test suite

---

## Risk Factors and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `.tsx` metadata value change breaks search results | Existing chunks have `programming_language: "typescript"`, new chunks get `"tsx"` | Document in CHANGELOG: `--force` reindex needed for TSX repos |
| tree-sitter parser quirks for new languages | AST chunking may produce suboptimal chunks | Existing `try/except` in chunker falls back to line-based |
| OCaml `(*` comment char looks odd in prefix | Embedding sees `(* File: path` without closing `*)` | Embedding model treats it as metadata context; not a correctness issue |
| `ocaml_interface` as language name may confuse users | `.mli` files show `ocaml_interface` in metadata | Correct: `.mli` has different AST structure; tree-sitter requires separate parser |
| `nim` dropped (no tree-sitter binding) | nim files not indexed | Can add when tree-sitter-language-pack ships nim binding |

## Verification Checklist

After each phase, verify:
- [ ] `uv run pytest` -- full suite passes
- [ ] `uv run pytest tests/test_languages.py -v` -- all consistency tests pass
- [ ] `uv run pytest tests/test_classifier.py -v` -- classifier tests pass
- [ ] `uv run pytest tests/test_chunker_ast_languages.py -v` -- AST chunking tests pass

## Files Changed Summary

| File | Phase | Change |
|------|-------|--------|
| `src/nexus/languages.py` | 1+2 | NEW: LANGUAGE_REGISTRY, GPU_SHADER_EXTENSIONS |
| `src/nexus/chunker.py` | 1 | Replace dict with import alias |
| `src/nexus/indexer.py` | 1+2 | Replace dict with import alias, expand DEFINITION_TYPES + _COMMENT_CHARS |
| `src/nexus/classifier.py` | 1 | Derive _CODE_EXTENSIONS from registry |
| `tests/test_languages.py` | 1+2 | NEW: consistency + expansion tests |
| `tests/test_classifier.py` | 1 | Update hardcoded test to derivation test |

## References

- RDR: `docs/rdr/rdr-028-code-search-recall.md`
- Parent bead: nexus-bqjj
- Phase 1 bead: nexus-luhd
- Phase 2 bead: nexus-kyah
- Phase 4 bead: nexus-mnzi (deferred)
- Arcaneum LANGUAGE_MAP: `/Users/hal.hildebrand/git/arcaneum/src/arcaneum/indexing/ast_chunker.py:40-95`
- tree-sitter-language-pack bindings: 158 languages available (v0.7.1)
