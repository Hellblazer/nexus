---
title: "Code Search Recall — Language Registry Unification"
id: RDR-028
type: Enhancement
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-006", "RDR-014", "RDR-015", "RDR-016"]
related_tests: []
implementation_notes: ""
---

# RDR-028: Code Search Recall — Language Registry Unification

## Problem Statement

Nexus maintains two separate language-to-extension maps that have drifted:

1. **`chunker.py:AST_EXTENSIONS`** (27 entries) — controls which files get AST-based chunking
2. **`indexer.py:_EXT_TO_LANGUAGE`** (27 entries) — controls which files get context prefix metadata
3. **`classifier.py:_CODE_EXTENSIONS`** (29 entries) — controls which files are classified as CODE vs PROSE

These maps are nearly identical but have subtle inconsistencies:
- `.tsx` maps to `"tsx"` in `AST_EXTENSIONS` but `"typescript"` in `_EXT_TO_LANGUAGE`
- Neither map covers languages available in arcaneum's 53-entry `LANGUAGE_MAP`: `.el` (elisp), `.clj` (clojure), `.ex/.exs` (elixir), `.erl` (erlang), `.hs` (haskell), `.ml` (ocaml), `.proto`, `.dart`, `.zig`, `.jl` (julia), and more

Having three maps invites future drift — a new language added to one but not the others would silently degrade classification, chunking, or context extraction. Currently, `_CODE_EXTENSIONS` is missing `.lua`, `.cxx`, `.kts`, `.sc` (present in both other maps), meaning those files are classified as PROSE and routed to the wrong indexing path. New languages added to `LANGUAGE_REGISTRY` must also be added to `_CODE_EXTENSIONS` or they will be silently indexed as prose.

### Previously Proposed Tracks (Now Implemented)

This RDR originally proposed three tracks. During gate review, Tracks A and B were found to be already implemented:

- **Track A (DONE)**: `DEFINITION_TYPES` (16 languages) ported to `indexer.py:84-179`. `_extract_name_from_node()` at `indexer.py:185-218`. `_extract_context()` at `indexer.py:221-281`. Embed-only prefix uses `Class: {class_ctx}  Method: {method_ctx}` at `indexer.py:552-556`.
- **Track B (DONE)**: Prose embed-only prefix `## Section: {header_path}` at `indexer.py:714-718`. PDF embed-only prefix `## Document: {source_title}  Page: {page_number}` at `indexer.py:845-854`.

**Only Track C (Language Registry Unification) remains.**

## Context

- Both maps exist because they were created independently — `AST_EXTENSIONS` for the chunker, `_EXT_TO_LANGUAGE` for the indexer's context extraction
- `_COMMENT_CHARS` (indexer.py:63-82) currently covers all 18 languages in `_EXT_TO_LANGUAGE` (including bash and objc which DEFINITION_TYPES lacks). When new languages are added to LANGUAGE_REGISTRY, `_COMMENT_CHARS` must be updated simultaneously or new languages will silently use `#` as comment char
- Arcaneum's `LANGUAGE_MAP` at `ast_chunker.py` has 53 entries covering tree-sitter-language-pack's full set
- `tree-sitter-language-pack==0.7.1` (pinned in pyproject.toml) provides parsers for 30+ languages

## Research Findings

### F1: Current Map Comparison (Verified — source search)

Both maps contain the same 27 extensions. The single value mismatch is `.tsx`:

| Extension | AST_EXTENSIONS | _EXT_TO_LANGUAGE | Mismatch? |
|-----------|---------------|-----------------|-----------|
| `.tsx` | `"tsx"` | `"typescript"` | **Yes** — chunker uses `tsx` grammar, indexer treats as typescript |
| All others | Identical | Identical | No |

The `.tsx` discrepancy is intentional for the chunker (tree-sitter has a separate `tsx` grammar) but the indexer should also use `"tsx"` for DEFINITION_TYPES lookup — except `DEFINITION_TYPES` has no `"tsx"` entry (it has `"typescript"`). This means `.tsx` files get AST chunking via the `tsx` parser but context extraction falls through to `DEFINITION_TYPES["typescript"]` via `_EXT_TO_LANGUAGE["tsx"] = "typescript"`. This happens to work because TypeScript definition types match TSX, but the coupling is fragile.

### F2: Arcaneum Language Coverage (Verified — source search)

Languages in arcaneum's `LANGUAGE_MAP` that nexus could support (tree-sitter parsers available):
- `.proto` (protobuf) — common in microservice codebases
- `.ex/.exs` (elixir) — growing ecosystem
- `.hs` (haskell), `.ml/.mli` (ocaml) — functional languages
- `.clj/.cljs/.cljc` (clojure) — JVM ecosystem
- `.dart` (dart) — Flutter ecosystem
- `.zig` (zig) — systems language
- `.jl` (julia) — scientific computing
- `.el` (elisp) — Emacs ecosystem
- `.erl/.hrl` (erlang) — BEAM ecosystem
- `.nim` (nim) — systems language

### F3: tree-sitter-language-pack Compatibility (Verified — docs)

`tree-sitter-language-pack==0.7.1` bundles parsers for all the above languages. Adding entries to the registry requires no new dependencies — only extending the dict.

## Proposed Solution

Create a single `LANGUAGE_REGISTRY` in a new `src/nexus/languages.py` module:

```python
LANGUAGE_REGISTRY: dict[str, str] = {
    # Current 28 entries from AST_EXTENSIONS...
    # Plus new languages from arcaneum
    ".proto": "protobuf",
    ".ex": "elixir",
    ".exs": "elixir",
    ".hs": "haskell",
    ".clj": "clojure",
    ".dart": "dart",
    ".zig": "zig",
    ".jl": "julia",
    ".el": "elisp",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".nim": "nim",
    # ...
}
```

- `chunker.py`, `indexer.py`, and `classifier.py` all import from `languages.py`
- Replace direct usage of `_EXT_TO_LANGUAGE` and `AST_EXTENSIONS` with `LANGUAGE_REGISTRY` imports; the `.tsx` value intentionally changes from `"typescript"` to `"tsx"` (guarded by simultaneous DEFINITION_TYPES and _COMMENT_CHARS updates)
- `_CODE_EXTENSIONS` in `classifier.py` derived from `LANGUAGE_REGISTRY.keys()` plus GPU shader extensions
- `_COMMENT_CHARS` expanded to cover all new languages (including `"tsx": "//"`)
- `DEFINITION_TYPES` expanded where tree-sitter grammar supports it

## Alternatives Considered

**A. Keep two maps, add a CI test for consistency**: Lower effort but doesn't solve the coverage gap. The test prevents drift but doesn't consolidate. Rejected — the root cause is duplication.

**B. Import arcaneum's full 53-entry map**: Too aggressive. Many entries are for obscure formats (`.pde` Processing, `.v` Verilog) that most users won't index. Better to add the top ~15 languages and expand on demand.

## Trade-offs

**Benefits**:
- Single source of truth for language support — no more drift
- 15+ new languages supported with zero new dependencies
- `_COMMENT_CHARS` and `DEFINITION_TYPES` gaps become obvious when adding a language

**Risks**:
- Some tree-sitter parsers may have quirks for less-tested languages (mitigated: add one test per new language)
- Backward-compatibility: if any code imports `AST_EXTENSIONS` or `_EXT_TO_LANGUAGE` directly, the alias must be maintained temporarily

**Failure modes**:
- A new language's parser crashes on malformed input → existing `try/except` in `_extract_context` handles this
- `.tsx` fix changes chunk behavior → only affects re-indexed TSX files, existing chunks unchanged until `--force`
- If `.tsx` metadata changes from `"typescript"` to `"tsx"`, collections must be force-reindexed (`--force`) for consistent `programming_language` metadata filtering. Partial reindex leaves mixed values.

## Implementation Plan

### Phase 1: Create Unified Registry (~40 LOC) — commit atomically
1. Create `src/nexus/languages.py` with `LANGUAGE_REGISTRY`
2. Migrate all entries from `AST_EXTENSIONS`, `_EXT_TO_LANGUAGE`, and `_CODE_EXTENSIONS`
3. Fix `.tsx` to use consistent value `"tsx"` everywhere; add `"tsx"` to `DEFINITION_TYPES` and `_COMMENT_CHARS`
4. Replace `AST_EXTENSIONS` and `_EXT_TO_LANGUAGE` with imports from `languages.py`
5. Derive `_CODE_EXTENSIONS` in `classifier.py` from `LANGUAGE_REGISTRY.keys()` plus GPU shader extensions
6. Fix existing gap: add `.lua`, `.cxx`, `.kts`, `.sc` to `_CODE_EXTENSIONS`

### Phase 2: Expand Coverage (~80 LOC)
7. Add ~15 new languages to `LANGUAGE_REGISTRY` from arcaneum
8. Add `_COMMENT_CHARS` entries for all new languages
9. Add `DEFINITION_TYPES` entries where applicable (elixir, haskell, clojure, dart, etc.)
10. Add all new extensions to classifier (via LANGUAGE_REGISTRY derivation in Phase 1)

### Phase 3: Tests (~60 LOC)
11. Test: every `LANGUAGE_REGISTRY` entry has a valid tree-sitter parser
12. Test: every `DEFINITION_TYPES` language is in `LANGUAGE_REGISTRY` values
13. Test: every `_COMMENT_CHARS` language is in `LANGUAGE_REGISTRY` values
14. Test: every `LANGUAGE_REGISTRY` key is in `_CODE_EXTENSIONS`
15. Test: `.tsx` gets correct AST chunking AND context extraction AND comment char

### Phase 4 (deferred): Alias Removal
16. After one release cycle, remove `AST_EXTENSIONS` and `_EXT_TO_LANGUAGE` aliases if no external imports exist

## Test Plan

- Unit: `LANGUAGE_REGISTRY` consistency — every entry has a working tree-sitter parser
- Unit: `DEFINITION_TYPES` is a subset of `LANGUAGE_REGISTRY` values
- Unit: `_COMMENT_CHARS` covers all `LANGUAGE_REGISTRY` values
- Unit: every `LANGUAGE_REGISTRY` key is in `_CODE_EXTENSIONS` (classifier consistency)
- Unit: `.tsx` gets correct AST chunking AND context extraction AND `//` comment char
- Integration: index a fixture file for each new language, verify classified as CODE (not PROSE)

## Finalization Gate

### Contradiction Check
No contradictions identified. The RDR proposes consolidating two maps into one — the implementation is straightforward dict unification.

### Assumption Verification
- [x] tree-sitter-language-pack 0.7.1 supports all proposed languages — **Status**: Verified by test (Phase 3 Step 11 runs `get_parser()` for every entry)
- [x] Both maps are nearly identical — **Verified**: source comparison shows 27 entries match
- [x] `.tsx` value is the only mismatch between AST_EXTENSIONS and _EXT_TO_LANGUAGE — **Verified**: exhaustive comparison
- [x] `_CODE_EXTENSIONS` is missing `.lua`, `.cxx`, `.kts`, `.sc` — **Verified**: source search of classifier.py

### Scope Verification
Scope: one new module (`languages.py`), three import-site changes (`chunker.py`, `indexer.py`, `classifier.py`), dict expansion. No architectural changes. Phase 4 (alias removal) explicitly deferred.

### Cross-Cutting Concerns
- **Classifier**: `classifier.py:_CODE_EXTENSIONS` MUST be updated for all new languages. Without this, new languages are classified as PROSE and routed to the wrong indexer. Phase 1 Step 5 derives `_CODE_EXTENSIONS` from `LANGUAGE_REGISTRY` to prevent this class of drift permanently.
- **_COMMENT_CHARS**: Must add `"tsx": "//"` in Phase 1 to prevent `.tsx` files getting `#` as comment char after the value change.
- DEFINITION_TYPES: Not all new languages need definition types — those without will get AST chunking but no class/method context. This is acceptable degradation.

### Proportionality
~180 LOC for 15+ new languages, elimination of a three-way maintenance hazard, and fix of 4 existing misclassified extensions. Proportionate to the problem.

## References

- nexus `chunker.py:AST_EXTENSIONS`: `src/nexus/chunker.py:16-51`
- nexus `indexer.py:_EXT_TO_LANGUAGE`: `src/nexus/indexer.py:32-60`
- nexus `indexer.py:DEFINITION_TYPES`: `src/nexus/indexer.py:84-179`
- nexus `indexer.py:_COMMENT_CHARS`: `src/nexus/indexer.py:63-82`
- arcaneum `LANGUAGE_MAP`: `/Users/hal.hildebrand/git/arcaneum/src/arcaneum/indexing/code/ast_chunker.py`
- RDR-014: Context prefix implementation (Track A/B already done)

## Revision History

### Gate Review 1 (2026-03-09)

**Layer 1 — Structural Validation**: BLOCKED. RDR contained factually incorrect claims:
- Track A (DEFINITION_TYPES + _extract_name) claimed unimplemented but found at indexer.py:84-281
- Track B (prose/PDF context prefixes) claimed unimplemented but found at indexer.py:714-718 and 845-854
- Problem Statement, Proposed Solution, and Implementation Plan rewritten to scope down to Track C only
- Title updated from "Definition Extraction, Context Prefixes, and Language Coverage" to "Language Registry Unification"
- Priority downgraded from P1 to P2 (reduced scope)
- Finalization Gate section added

### Gate Review 2 (2026-03-09)

**Layer 3 — AI Critique (substantive-critic)**: 1 FAIL, 3 WARNs.

Corrections applied:
- **FAIL (Scope)**: `classifier.py:_CODE_EXTENSIONS` was absent from Implementation Plan. Phase 2 language expansion would be silently ineffective without classifier updates. Fixed: added Phase 1 Step 5 (derive _CODE_EXTENSIONS from LANGUAGE_REGISTRY) and Step 6 (fix 4 existing missing extensions).
- **WARN (Contradiction)**: Entry count corrected from 28 to 27. `_COMMENT_CHARS` "covers fewer" claim corrected — it actually covers all 18 languages.
- **WARN (Assumption)**: Parser availability verification method clarified as confirmed-by-test (Phase 3 Step 11).
- **WARN (Cross-cutting)**: Added `"tsx": "//"` to `_COMMENT_CHARS` plan. Added `.tsx` metadata inconsistency to failure modes. Reframed backward-compat alias as intentional value change.
- LOC estimate corrected from ~120 to ~180.
- Phase 4 (alias removal) added as explicit deferred step.
- Related RDR-006 kept (chunk-size penalty interacts with language coverage via file classification).
