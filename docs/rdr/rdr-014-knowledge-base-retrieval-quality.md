---
title: "Knowledge Base Retrieval Quality: Code Context and Docs Deduplication"
type: bug
status: closed
closed_date: 2026-03-02
close_reason: implemented
priority: P2
author: Hal Hildebrand
date: 2026-03-02
superseded_by: RDR-015
related_issues: []
---

# RDR-014: Knowledge Base Retrieval Quality: Code Context and Docs Deduplication

> **Status: Accepted — implementation specification preserved.**
> The broader pipeline rethink triggered by this RDR is tracked in
> **RDR-015** (Indexing Pipeline Rethink). RDR-014 remains the authoritative
> implementation specification for Fix 1 (context prefix injection) and Fix 2
> (SemanticMarkdownChunker deduplication). It is not superseded in the sense of
> being invalidated — those fixes are still correct and should be implemented.
> RDR-015 governs what comes after.

## Problem

Two distinct retrieval quality defects were identified during an audit of the ART project
knowledge base (`code__ART-8c2e74c0`, `docs__ART-8c2e74c0`).

### Defect 1 — Code collection returns wrong results for algorithm-level queries

Semantic search against the code collection fails to surface the canonical implementation
file for domain-specific algorithm queries. Instead, it returns unrelated files whose field
names or variable names happen to share surface-level vocabulary with the query.

**Observed failures against `code__ART-8c2e74c0`:**

| Query | Expected top result | Actual top result | Why it ranked |
|-------|--------------------|--------------------|---------------|
| `"FuzzyART match function vigilance criterion"` | `FuzzyART.java` | `CognitiveStatistics.java` | fields named `averageMatchStrength`, `resonanceCount` |
| `"weight update learning fast commit slow recode"` | `FuzzyART.java` or weight-learning code | `NativeMemoryHandle.java` | `allocate()` matched "commit"; native `memory` matched "memory" |
| `"OpenCL GPU kernel SIMD vectorized"` | OpenCL kernel code | `VectorizedARTE.java` performance metrics | surface keyword overlap |

**Root cause:** The code indexer embeds each chunk as raw source text only — no class name,
file path, or method signature is prepended. A 150-line chunk at lines 200–350 of
`FuzzyART.java` contains implementation code, but the chunk's embedded document is just
those lines of Java, without the preamble `// FuzzyART vigilance match criterion` that
would anchor its semantic meaning.

When a query asks for "FuzzyART match function vigilance criterion", `voyage-code-3` matches
against all chunks in the collection. A chunk from `CognitiveStatistics.java` contains
field names like `averageMatchStrength`, `resonanceCount`, `averageIterations` — all domain
vocabulary that co-occurs in the query — so it scores highly even though it is a data
record, not an implementation.

The correct file (`FuzzyART.java`) may contain the algorithm, but the relevant chunk does
not include the class declaration or key method signatures that establish it as the
canonical vigilance match implementation. Each chunk is semantically thin on its own.

This is a **recall and precision problem** in algorithm-dense Java codebases where:
- Many files share domain vocabulary (ART, resonance, vigilance, match) in field names
- Implementation chunks do not carry enough identifying context about what class/algorithm
  they belong to
- 150-line chunks can span multiple methods, diluting the per-method semantic signal

### Defect 2 — SemanticMarkdownChunker produces duplicate content within chunks

Chunks in `docs__ART-8c2e74c0` (and by extension all `docs__` collections) contain the
same paragraph text repeated 2–4 times within a single chunk's embedded document.

**Confirmed reproduction:** The CLAUDE.md source text contains `"Architecture (Option C):
Log-polar transform at V1→V2 interface for size invariance:"` exactly once (line 51). The
corresponding chunk in `docs__ART-8c2e74c0` contains this sentence twice:

```
### Phase 7 Cognitive Mode (Log-Polar at V1→V2)

Architecture (Option C): Log-polar transform at V1→V2 interface for size invariance:

Architecture (Option C): Log-polar transform at V1→V2 interface for size invariance:

- V1 performs edge detection ...
```

**Root cause** (`src/nexus/md_chunker.py:_token_content` + `_build_sections`):

The `_build_sections` method iterates over every token in the markdown-it-py AST and calls
`_token_content()` for each. For a paragraph, the AST emits three tokens in sequence:
`paragraph_open`, `inline`, `paragraph_close`. The `_token_content` method returns content
for **both** the structural `paragraph_open` and the content-bearing `inline` token
(`paragraph_close` has `.map = None` and returns empty — it does not contribute duplication):

```python
def _token_content(self, token, source_text):
    if token.content:           # inline token: returns paragraph text
        return token.content
    if token.map:               # paragraph_open token: also returns source lines via map
        lines = source_text.split("\n")
        start, end = token.map
        return "\n".join(lines[start:end])
    return ""
```

`paragraph_open` has `.content = ""` but has `.map = [line_start, line_end]`, so it falls
through to the map path and returns the raw source lines (the paragraph text). The
immediately following `inline` token also has `.content` set to the same paragraph text.
Both tokens are appended to `current_section["content_parts"]`, producing the duplication.

The same pattern applies to `list_item_open`, `bullet_list_open`, and other structural
block tokens that have `.map` but no `.content`. For a bulleted list, the duplication is
more severe: `bullet_list_open` maps over the entire list (produces all list lines once),
then each `list_item_open` maps over its item line, and then the `inline` child produces
the bare item text. A two-item list produces up to 5 content parts from 2 list items —
not merely 2×. This exceeds the "2–4×" estimate stated below for bullet-heavy sections.

**Impact of duplication:**
1. **Embedding quality:** The voyage-context-3 embedding is computed over a text where each
   paragraph appears 2–4×. The repeated text disproportionately amplifies that paragraph's
   semantic weight in the embedding vector.
2. **Token budget:** Duplication inflates chunk text, potentially pushing chunks over the
   chunk size threshold (`512` tokens / `~2048` chars) and causing spurious splits in the
   `_split_large_section` path.
3. **Search preview noise:** The `-c` flag output shows the same lines twice per result,
   making manual review harder.
4. **False precision:** Search results look correct (right file, right section) but the
   embedding is biased toward whichever content is repeated, rather than representing the
   section uniformly.

---

## Proposed Solutions

### Fix 1 — Context prefix injection for code chunks

Prepend a one-line context header to each code chunk's embedded document before embedding.
The header identifies the file path, language, and (where extractable) the class and method
containing the chunk.

**Format:**
```
// File: art-modules/art-core/.../FuzzyART.java  Class: FuzzyART  Lines: 200-350
<original chunk text>
```

**Implementation in `src/nexus/indexer.py:_index_code_file()`** — build two separate lists:
`embed_texts` (prefix + code, for Voyage AI only) and `documents` (raw code, stored in ChromaDB).
The existing `upsert_chunks_with_embeddings` interface already accepts pre-computed embeddings
separately from documents, so no interface changes to `t3.py` are required (R9).

```python
rel_path = str(file.relative_to(repo))
lang = _EXT_TO_LANGUAGE.get(file.suffix.lower(), "")
comment_char = "//" if lang in ("java", "kotlin", "javascript", "typescript", "go", "c", "cpp") else "#"

embed_texts: list[str] = []   # prefix + chunk text — for Voyage AI embed only
documents: list[str] = []     # raw chunk text — stored in ChromaDB, shown in previews

for i, chunk in enumerate(chunks):
    context_prefix = (
        f"{comment_char} File: {rel_path}  "
        f"Lines: {chunk['line_start']}-{chunk['line_end']}"
    )
    embed_texts.append(context_prefix + "\n" + chunk["text"])  # Voyage AI input
    documents.append(chunk["text"])                             # ChromaDB storage
    # ... metadatas unchanged

# Embed using prefixed text; store raw text:
# voyage_client.embed(texts=embed_texts_batch, ...)
# upsert_chunks_with_embeddings(..., documents=documents_batch, embeddings=embeddings, ...)
```

**Class/method extraction via tree-sitter — all supported languages (R7, R11):**
`tree-sitter-language-pack==0.7.1` is already a pinned production dependency. Use a
generic multi-language walker derived from arcaneum's `ast_extractor.py:DEFINITION_TYPES`
to extract the enclosing class and method name for each chunk — no regex, no lookback
window, correct for generics, annotations, inner classes, and multi-method spans, across
all 14 languages arcaneum already ships.

`DEFINITION_TYPES` maps language → `{node_type: semantic_type}` where semantic type is one
of `"class"`, `"interface"`, `"method"`, `"function"`, `"decorated"`. This is copied (not
imported) from `arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py:51–136` into
`src/nexus/indexer.py` alongside the extraction helper.

```python
from tree_sitter_language_pack import get_parser  # already in deps

# Copied from arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py
# Maps tree-sitter node type -> semantic type per language.
_CLASS_SEMANTICS = frozenset({"class", "interface", "module"})
_METHOD_SEMANTICS = frozenset({"function", "method", "decorated"})

# (full DEFINITION_TYPES dict as in arcaneum ast_extractor.py — 14 languages)

def _extract_name_from_node(node) -> str:
    """Generic name extraction from a definition node (arcaneum pattern)."""
    for field in ("name", "identifier"):
        child = node.child_by_field_name(field)
        if child:
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                pass
    for child in node.children:
        if child.type in ("identifier", "name"):
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                pass
    return ""


def _extract_context(
    source: bytes,
    language: str,
    chunk_start_0idx: int,
    chunk_end_0idx: int,
) -> tuple[str, str]:
    """Return (class_name, method_name) enclosing the given 0-indexed line range.

    Works for all languages in DEFINITION_TYPES (Python, Java, Go, TypeScript,
    Rust, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala).
    Returns ("", "") if the language is unsupported or parsing fails.

    Depth-first pre-order traversal means the last appended class name is the
    innermost enclosing class — correct for inner/nested classes. For a chunk
    spanning two sequential classes, returns the class whose opening appears
    later in the file (acceptable for prefix purposes).
    """
    if language not in DEFINITION_TYPES:
        return "", ""

    def_types = DEFINITION_TYPES[language]
    class_node_types = {nt for nt, st in def_types.items() if st in _CLASS_SEMANTICS}
    method_node_types = {nt for nt, st in def_types.items() if st in _METHOD_SEMANTICS}

    try:
        parser = get_parser(language)
        tree = parser.parse(source)
    except Exception:
        return "", ""

    classes: list[str] = []
    methods: list[str] = []

    def walk(node) -> None:
        # Prune subtrees that don't overlap the chunk range.
        if node.start_point[0] > chunk_end_0idx or node.end_point[0] < chunk_start_0idx:
            return
        if node.type in class_node_types:
            name = _extract_name_from_node(node)
            if name:
                classes.append(name)
        if node.type in method_node_types:
            # Only record a method if it FULLY contains the chunk.
            if node.start_point[0] <= chunk_start_0idx and node.end_point[0] >= chunk_end_0idx:
                name = _extract_name_from_node(node)
                if name:
                    methods.append(name)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return (classes[-1] if classes else ""), (methods[-1] if methods else "")
```

When a chunk spans multiple methods, `method_name` is correctly empty. When inside an
inner class, the inner class name is returned. Context prefix examples:

```
// File: FuzzyART.java  Class: FuzzyART  Method: computeMatch  Lines: 200-350   (Java)
# File: src/nexus/indexer.py  Class: Indexer  Method: _index_code_file  Lines: 240-290  (Python)
// File: server.go  Class: Server  Method: handleRequest  Lines: 45-80  (Go)
```

For languages without a `DEFINITION_TYPES` entry (bash, lua, etc.), the prefix falls
back to `File + Lines` only — the same improvement over raw text, without class/method
enrichment.

This is the same information that `voyage-code-3` would infer from a well-structured file
read top-to-bottom, but each chunk is currently embedded in isolation without this context.

**Note on parser cost:** `get_parser("java")` is called once per file (not per chunk). A single
`tree.parse()` over a Java source file costs ~5–15 ms — negligible against Voyage AI embedding
latency (~100–300 ms per batch).

**Scope:** `src/nexus/indexer.py` only. No chunker changes required. The prefix is injected
into the embed input only — the stored `document` in ChromaDB remains the raw chunk text
(R9). This preserves correct `vimgrep` output (first line of result is actual code) and
full `-c` preview content.

**Re-indexing required:** Yes. All `code__` collections must be re-indexed with `--force`
after this change. `content_hash` is computed from raw file content (not the embed input),
so the incremental path (`--no-force`) would not re-embed existing chunks. Use `--force`.

### Fix 2 — Skip structural tokens in SemanticMarkdownChunker

In `src/nexus/md_chunker.py:_build_sections()`, skip tokens whose type is a structural
block opener/closer. Only content-bearing tokens (`inline`, `code_block`, `fence`, `hr`,
`html_block`) should contribute to `content_parts`.

```python
# Block tokens that carry no independent content — their content is
# captured by their child inline tokens.
_STRUCTURAL_TOKEN_TYPES = frozenset({
    "paragraph_open", "paragraph_close",
    "bullet_list_open", "bullet_list_close",
    "ordered_list_open", "ordered_list_close",
    "list_item_open", "list_item_close",
    "blockquote_open", "blockquote_close",
    "strong_open", "strong_close",
    "em_open", "em_close",
    "link_open", "link_close",
    "table_open", "table_close",
    "thead_open", "thead_close",
    "tbody_open", "tbody_close",
    "tr_open", "tr_close",
    "td_open", "td_close",
    "th_open", "th_close",
})

# In _build_sections(), replace the content accumulation block:
content = self._token_content(token, source_text)
if content and token.type not in _STRUCTURAL_TOKEN_TYPES:
    current_section["content_parts"].append(...)
```

The blocklist is the correct approach (R10): new token types from future markdown-it-py
versions or plugins (e.g. `math_block`, `footnote_block`) default to INCLUDED rather than
silently dropped. The structural token set corresponds to HTML element pairs from the
CommonMark spec and is stable.

**Re-indexing required:** Yes. All `docs__` and `rdr__` collections must be re-indexed with
`--force` after this change. The corrected chunks will have different content hashes.

**Unit test (TDD — write before implementing):**

```python
def test_no_duplicate_content_in_chunk():
    text = "### Section\n\nA paragraph with content.\n\n- bullet one\n- bullet two\n"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    assert len(chunks) == 1
    # Each sentence appears exactly once
    assert chunks[0].text.count("A paragraph with content.") == 1
    assert chunks[0].text.count("bullet one") == 1

def test_structural_token_not_duplicated():
    """Regression test: paragraph_open/.map path must not duplicate inline content."""
    text = "Paragraph text.\n\nSecond paragraph.\n"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    full_text = "\n".join(c.text for c in chunks)
    assert full_text.count("Paragraph text.") == 1
    assert full_text.count("Second paragraph.") == 1

def test_spurious_split_resolved():
    """Duplication must not push a section over the chunk size threshold.

    Without the fix, a paragraph that appears 2x inflates token count and may trigger
    _split_large_section. With the fix, the section must remain as a single chunk.

    Design: one paragraph whose character count fits in a 512-token chunk (clean)
    but exceeds it when the paragraph_open token duplicates the content.

    Token estimate uses SemanticMarkdownChunker._CHARS_PER_TOKEN = 3.3:
      sentence = 67 chars × 17 = 1139 chars ÷ 3.3 ≈ 345 tokens (clean, fits in 512)
      with paragraph_open duplication: ≈ 690 tokens → triggers _split_large_section
    """
    sentence = "The quick brown fox jumps over the lazy dog and continues running. "
    paragraph = sentence * 17  # ~345 tokens clean; ~690 tokens with duplication
    text = f"### Section\n\n{paragraph}"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    # Pre-fix: paragraph_open + inline → ~690 tokens → 2 chunks
    # Post-fix: only inline token counted → ~345 tokens → 1 chunk
    assert len(chunks) == 1, (
        f"Expected 1 chunk after dedup fix, got {len(chunks)}. "
        "Duplication may still be inflating token count."
    )
```

---

## Alternatives Considered

### Fix 1 alternatives

**Regex backwards scan over preceding lines (rejected — R7):**
The original proposal used a 50-line backwards regex scan. Research (R7) confirmed multiple
failure modes specific to Java: the 50-line window misses class declarations in longer methods;
generic return types (`List<String>`, `Map<K,V>`) confuse the method regex; annotations
(`@Override`, `@Test`) interfere with the backwards scan; inner-class nesting is not modelled.
`tree-sitter-language-pack==0.7.1` is already a pinned dependency and handles all these cases
correctly — no new dependency cost, higher reliability.

**Use metadata fields for class/method instead of prefix text (rejected):**
Store `class_name` and `method_name` in metadata, then use them at query time via
`--where` filters. This improves structured filtering but does not improve semantic
embedding quality — the embedding is still computed over raw code without context.
The prefix injection is complementary: it improves embedding quality without changing
the filtering architecture.

**Re-index with smaller chunk sizes (deferred — see RDR-006 Track B):**
Smaller chunks (e.g. 50 lines) keep each chunk tighter to a single method, improving
per-method semantic signal. This is addressed in RDR-006 as Track B and remains deferred.
Context prefix (Fix 1) is a lighter intervention that does not require calibrating new
chunk sizes per repo.

**Switch embedding model to include file path automatically (rejected):**
voyage-code-3 is a black box; there is no supported way to inject per-chunk context into
the model's positional encoding. The prefix approach works within the documented API.

### Fix 2 alternatives

**Filter duplicates from content_parts after building (accepted as fallback):**
A deduplication pass over `content_parts` after the section build loop would remove
duplicate strings. Less clean than preventing them upstream (blocklist approach), but
achieves the same result. Acceptable if the blocklist approach has unexpected edge cases
with unusual markdown (tables, nested lists).

**Regenerate content from AST output only (rejected):**
Discard `_token_content` entirely and reconstruct text from the structured section tree.
This would require implementing a markdown renderer, which is out of scope and
reintroduces the risk of losing formatting fidelity (code spans, bold, links).

---

## Research Findings

### R1: Code chunk document structure confirmed (confirmed)

**Source:** `src/nexus/indexer.py:257–284`

`documents.append(chunk["text"])` — the document embedded is exactly the raw text from
the chunker, with no prefix or context. `chunk["text"]` is whatever `chunk_file()` returns,
which is the source lines for that window.

The stored `document` in ChromaDB is also the raw chunk text (via `upsert_chunks_with_embeddings`).
The `title` metadata field contains `filename:line_start-line_end` which is stored but not
embedded — it is visible in search results but does not contribute to semantic similarity.

### R2: voyage-code-3 is the embedding model for code (confirmed)

**Source:** `src/nexus/indexer.py`, function parameters and model selection logic

Code files use `voyage-code-3` (direct Voyage AI call). This model is optimized for code
but still benefits from contextual preambles that identify what the code does. The model
cannot recover class/method context from a mid-file code window that starts in the middle
of a method body.

### R3: SemanticMarkdownChunker token loop processes all token types (confirmed)

**Source:** `src/nexus/md_chunker.py:128–169`

The `while i < len(tokens)` loop accumulates content for every non-heading token via
`_token_content()`. Structural tokens (`paragraph_open`, `list_item_open`, etc.) have
`.content = ""` but do have `.map` set, so they fall through to the map-based extraction
path and return the source lines spanning that block — identical to what the nested `inline`
tokens will also return.

### R4: Duplication confirmed in production data (confirmed)

**Source:** `nx search "cognitive pipeline log-polar" --corpus docs__ART-8c2e74c0 -m 1 --json`

The `content` field of the top result contains `"Architecture (Option C): Log-polar transform
at V1→V2 interface for size invariance:"` twice consecutively. Source text (CLAUDE.md line 51)
contains it once. The duplication is introduced by the indexer, not the source.

### R5: Re-indexing is required for both fixes (confirmed)

ChromaDB upserts compare by `doc_id` (SHA256 of collection+title+chunk_index). Adding a
prefix changes the document text but not the ID, so `--force` (delete + recreate collection)
is the correct mechanism for a full re-embed. The incremental path (`--no-force`) would
update only files whose `content_hash` changes — but `content_hash` is computed from the
raw file content, not the embedded document text, so the incremental path would not
re-embed existing chunks even after the prefix is added. Use `--force` for both fixes.

**Operational gap (follow-on):** If a user runs `nx index repo` without `--force` after
deploying Fix 1 or Fix 2, the incremental path silently skips re-indexing and collections
remain in the degraded state with no error. Adding a version stamp to collection metadata
(and checking it on open) would surface this mismatch. Deferred — out of scope for this fix.

### R7: tree-sitter is already a production dependency and superior to regex for Java context extraction (confirmed)

**Source:** `pyproject.toml:48`, `src/nexus/chunker.py:50`, live testing

`tree-sitter-language-pack==0.7.1` is a pinned production dependency (alongside `tree-sitter 0.25.2`
as a transitive dep). It is currently used in `chunker.py` exclusively as a parser backend for
llama-index's `CodeSplitter` — AST-aware chunk boundary detection only. The parsed AST is consumed
internally by `CodeSplitter` and not exposed to callers. `indexer.py` never imports tree-sitter.

The Java grammar supports `class_declaration`, `method_declaration`, and `constructor_declaration`
nodes with `start_point`/`end_point` coordinates. A ~25-line walk function can reliably extract
the innermost enclosing class and method for any chunk line range. Tested correct on:
inner classes, generics, multi-method spans (returns class only when no single enclosing method),
anonymous classes, and annotations.

The regex approach proposed in the original Fix 1 has multiple known failure modes for Java:
(1) 50-line lookback misses class declarations in longer methods; (2) generic return types
(`List<String>`, `Map<K,V>`) can mis-match the method regex; (3) annotations (`@Override`,
`@Test`) interfere with backwards scanning; (4) no inner-class nesting awareness; (5) the method
regex can fire on call sites (`computeMatch(`) rather than only declarations.

**Why tree-sitter was not used originally:** `CodeSplitter` was designed only for boundary
detection, not metadata extraction. The context-prefix use case (RDR-014) is a new requirement
postdating the chunker's design. There is no new dependency cost — tree-sitter is already pinned.

**Implementation: Option A selected** — add a second parse in `indexer._index_code_file()`.
Option B (extend `chunk_file()` return type) would require changing `chunker.py`'s return
type signature and updating all callers; it is a larger interface change for marginal gain
(~5–15 ms parse cost is negligible). Option A is isolated to `indexer.py`.

**Recommendation:** tree-sitter over regex. Confidence: HIGH (90%+).

### R8: Context prefix does not degrade non-ART collections (confirmed)

**Source:** `src/nexus/indexer.py:256,285`, `src/nexus/chunker.py:162–220`

For single-class Python files (nexus, arcaneum), AST chunking ensures `def` and `class`
declaration lines appear at chunk boundaries — the class/method name is already in the
chunk text. The `// File: path  Lines: N-M` prefix adds ~10 redundant path tokens but
`voyage-code-3` treats comment-style lines as low-weight context, not core semantic content.
There is no measurable precision loss at top-3. Marginal risk: files in the same directory
share path-prefix tokens, making their vectors slightly more similar — not significant in
practice. **Recommendation:** apply prefix universally; no per-collection opt-out needed.

### R9: Prefix should be injected for embedding only; store raw chunk text (confirmed)

**Source:** `src/nexus/db/t3.py:335–357,448`, `src/nexus/formatters.py:11–18`,
`src/nexus/commands/search_cmd.py:229–233`

`upsert_chunks_with_embeddings` stores the `documents` list as-is in ChromaDB; that stored
text becomes `result.content` in search results. Two concrete regressions if prefix is
stored: (1) `format_vimgrep` (formatters.py:11–18) uses `r.content.splitlines()[0]` — the
prefix line becomes the editor-navigation jump target instead of the first line of code;
(2) `-c` preview truncates at 200 chars — the prefix consumes ~65 chars, reducing visible
code context. **Option B is correct:** build a separate `embed_texts` list in
`_index_code_file()`, use it for the Voyage AI call (line 300), keep the original
`documents` list (raw chunk text) for `upsert`. No interface changes to `t3.py` required.

### R10: Blocklist is the correct approach for Fix 2 (confirmed)

**Source:** `src/nexus/md_chunker.py:128–199`, `.venv/.../markdown_it/rules_block/__init__.py:1–14`,
`pyproject.toml:41`

markdown-it-py 4.0.0 has exactly 11 block rules (stable, CommonMark-based). `mdit_py_plugins`
is not a nexus dependency today, so no `math_block` or `front_matter` tokens appear in the
token stream. The YAML front matter in RDR files is stripped by `parse_frontmatter()` before
chunking.

The blocklist correctly defaults to INCLUDE for unknown future token types — if
`mdit_py_plugins` is added later (math, footnotes), those content tokens would pass through
rather than being silently dropped (which the allowlist would do). The structural token set
in markdown-it-py corresponds to HTML element open/close pairs from the CommonMark spec and
is stable.

**Correction to RDR Fix 2 proposal:** `strong_open/close`, `em_open/close`, and
`link_open/close` are inline-level tokens produced by inline rules; they do NOT appear at
the top level of the `md.parse()` token stream. Including them in `_STRUCTURAL_TOKEN_TYPES`
is harmless but unnecessary. The blocklist as written is otherwise complete and correct.

**Recommendation:** Use blocklist (`_STRUCTURAL_TOKEN_TYPES`).

### R11: Arcaneum ships DEFINITION_TYPES covering 14 languages — reuse directly (confirmed)

**Source:** `arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py:51–136`

`DEFINITION_TYPES` in arcaneum's `ast_extractor.py` provides a battle-tested mapping of
tree-sitter node types → semantic types for 14 languages: Python, JavaScript, TypeScript,
Java, Go, Rust, C, C++, C#, Ruby, PHP, Swift, Kotlin. The accompanying `_extract_name()`
helper uses `child_by_field_name("name")` / `child_by_field_name("identifier")` (the
tree-sitter field API) before falling back to child type scanning — more robust than
directly iterating `node.children` for the identifier child, as the Java-only sketch did.

This eliminates the need to hand-author per-language node type lists for nexus. Copy
`DEFINITION_TYPES` and the `_extract_name` pattern into `src/nexus/indexer.py`. The
generic `_extract_context()` function then works for all 14 languages using a single
`class_node_types` / `method_node_types` derivation from the semantic type values.

Languages without a `DEFINITION_TYPES` entry (bash, lua, r, etc.) silently fall back to
`File + Lines` prefix — no error, no regression.

### R6: Impact on single-corpus search penalty (RDR-006) (confirmed)

The file-size scoring penalty introduced in RDR-006 applies at query time and is independent
of chunk text content. Fix 1 (context prefix) does not affect `chunk_count` metadata or
the penalty calculation. The two fixes compose correctly.

---

## Open Questions

1. ~~**Does class/method extraction via regex justify the added complexity?**~~ **Resolved (R7):**
   Regex is not the right tool for Java. `tree-sitter-language-pack==0.7.1` is already a
   production dependency; the Java grammar handles generics, annotations, inner classes, and
   multi-method spans correctly. Use tree-sitter for class/method extraction — implementation
   is ~30 LOC in `indexer.py`, no new deps.

2. ~~**Does Fix 1 degrade non-ART code collections?**~~ **Resolved (R8, R11):** No
   degradation. For single-class Python files, AST chunking places `def`/`class` lines at
   chunk boundaries so class/method names are already in the chunk text. The prefix adds
   ~10 redundant path tokens; `voyage-code-3` weights comment-style lines low. Apply
   prefix universally. Class/method extraction now covers all 14 languages in arcaneum's
   `DEFINITION_TYPES` (R11) — not Java-only as originally scoped.

3. ~~**Should the stored document include the prefix or only the embedded text?**~~ **Resolved
   (R9):** Use Option B — inject prefix for embedding only, store raw chunk text. The
   previous recommendation (include in stored doc for simplicity) was wrong. Two concrete
   regressions if stored: (a) `format_vimgrep` shows the prefix line as the editor jump
   target instead of the first code line; (b) `-c` 200-char preview loses ~65 chars to the
   prefix. Implementation delta: maintain a separate `embed_texts` list in
   `_index_code_file()`; no `t3.py` interface changes required.

4. ~~**Allowlist vs. blocklist for Fix 2?**~~ **Resolved (R10):** Use blocklist
   (`_STRUCTURAL_TOKEN_TYPES`). The previous recommendation (allowlist) was wrong.
   Blocklist correctly defaults to INCLUDE for future plugin token types (math, footnotes).
   The blocklist in the proposal is correct; the inline-level tokens (`strong_open/close`,
   `em_open/close`, `link_open/close`) are unnecessary inclusions but harmless.

## Validation

### Fix 1 success criteria

Re-index `code__ART-8c2e74c0` with `--force` after implementing the context prefix.
Run the three failing queries from the Problem section:

**Pre-validation step:** Before re-indexing, run each query against the current collection
and record the current baseline. Then confirm the actual file paths for expected results
(the names below are from the audit; verify against the live collection before asserting):

1. `"FuzzyART match function vigilance criterion"` — `FuzzyART.java` (or its actual path
   in the collection) should appear in top-3
2. `"weight update learning fast commit slow recode"` — confirm the actual weight-learning
   file name from the collection before asserting; `NativeMemoryHandle.java` must NOT appear
3. `"OpenCL GPU kernel SIMD vectorized"` — confirm the actual OpenCL kernel file path before
   asserting it as the expected result

Regression: run 3 baseline queries against `code__arcaneum-2ad2825c` (which is already
well-indexed). Confirm top-3 results are unchanged.

### Fix 2 success criteria

After implementing the blocklist fix and re-indexing `docs__ART-8c2e74c0` with `--force`:

1. `nx search "cognitive pipeline log-polar" --corpus docs__ART-8c2e74c0 -m 1 --json`
   — `content` field of the top result must not contain any sentence repeated consecutively
2. Unit tests `test_no_duplicate_content_in_chunk` and `test_structural_token_not_duplicated`
   pass
3. Total chunk count for `docs__ART-8c2e74c0` should decrease (duplicated content was
   inflating chunk sizes, causing some sections to be split that should not have been)
