# Post-Mortem: RDR-028 — Language Registry Unification

**Closed**: 2026-03-09
**Reason**: Implemented
**PR**: #76
**Bead**: nexus-bqjj

## What Was Done

Unified three drifted language-to-extension maps into a single `LANGUAGE_REGISTRY`
in `src/nexus/languages.py`:

1. Created `LANGUAGE_REGISTRY` (44 extensions → 30 languages) and `GPU_SHADER_EXTENSIONS`
2. Migrated `chunker.py`, `indexer.py`, `classifier.py` to import from the registry
3. Fixed `.tsx` inconsistency: consistently `"tsx"` with matching DEFINITION_TYPES and _COMMENT_CHARS
4. Fixed `c_sharp` parser lookup: chunker now maps to `"csharp"` for tree-sitter-language-pack
5. Fixed 4 extensions (`.lua`, `.cxx`, `.kts`, `.sc`) missing from classifier
6. Added 16 new extensions across 11 languages: elixir, erlang, haskell, clojure, ocaml, elisp, dart, zig, julia, perl, proto
7. Added DEFINITION_TYPES for 6 new languages (dart, haskell, julia, ocaml, perl, erlang)
8. Added _COMMENT_CHARS for all 12 new language values
9. Derived `_CODE_EXTENSIONS` from `LANGUAGE_REGISTRY.keys() | GPU_SHADER_EXTENSIONS` to prevent future drift

## Plan vs. Actual Divergences

| Planned | Actual | Impact |
|---------|--------|--------|
| Backward-compat aliases for AST_EXTENSIONS and _EXT_TO_LANGUAGE | User chose to skip — aliases removed directly | Simpler code, Phase 4 (deferred removal) completed immediately |
| `.proto` in Phase 2 expansion | Moved to Phase 1 — was already in `_CODE_EXTENSIONS`, removing it would regress | No impact, just reordered |
| nim language included | Dropped — tree-sitter-language-pack 0.7.1 has no nim binding | Can add when binding ships |
| 28 base entries | Actually 28 (27 + proto from old _CODE_EXTENSIONS) | Corrected in implementation |
| c_sharp parser bug not in scope | Discovered during implementation — get_parser("c_sharp") throws LookupError | Bonus fix: C# files now get AST chunking instead of silent fallback to line-based |

## Discoveries

- **c_sharp parser was broken**: `get_parser("c_sharp")` fails because tree-sitter-language-pack
  uses `"csharp"`. The chunker's `try/except` silently caught the LookupError and fell back to
  line-based chunking. C# files have never had AST chunking in nexus. Fixed with a parser name
  mapping in `_make_code_splitter`.

- **ocaml_interface is a separate parser**: `.mli` files need `ocaml_interface`, not `ocaml`.
  tree-sitter treats OCaml interface files as a distinct grammar.

## Test Coverage

- 59 tests in `tests/test_languages.py` covering registry consistency, parser availability,
  DEFINITION_TYPES/COMMENT_CHARS subset relationships, and classifier derivation
- Updated `test_classifier.py` to verify derivation property instead of hardcoded set
- Full suite: 1954 passed, 0 failed

## Process Notes

This RDR went through the full lifecycle: draft → gate (2 rounds — first found factual
errors about Tracks A/B being unimplemented, second found classifier gap) → accept → implement → close.
The gate process caught a real bug (classifier not updating for new languages) that would have
been a silent regression.
