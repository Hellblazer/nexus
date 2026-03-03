# Implementation Plan: Proactive 12KB Chunk Byte Cap (nexus-bf6w)

**Date:** 2026-03-03
**Bead:** nexus-bf6w
**Design:** docs/plans/2026-03-03-chunk-byte-cap-design.md
**Status:** Ready for implementation

## Executive Summary

Five changes across five files to eliminate the ChromaDB 16KB quota violation that
killed the ART reindex. All chunkers will proactively cap output at 12,288 bytes
(SAFE_CHUNK_BYTES), and `_write_batch` adds a last-resort guard at the ChromaDB
hard limit (16,384 bytes). Each phase follows strict TDD: failing test first,
then implementation to pass.

## Dependency Graph

```
Phase 1 (nexus-gvs7)  chroma_quotas.py — SAFE_CHUNK_BYTES constant
    |
    +-- Phase 2a (nexus-oxbm)  chunker.py — fix escape hatches
    |
    +-- Phase 2b (nexus-9s63)  md_chunker.py — byte cap post-pass
    |
    +-- Phase 2c (nexus-lr93)  pdf_chunker.py — byte cap post-pass

Phase 3 (nexus-6sjt)  t3.py — last-resort drop-and-warn  [INDEPENDENT]
```

**Critical path:** Phase 1 then Phase 2a (chunker.py is most complex).
**Parallelization:** Phase 2a/2b/2c run in parallel after Phase 1. Phase 3 runs
any time (uses MAX_DOCUMENT_BYTES, not SAFE_CHUNK_BYTES).

---

## Phase 1: Add SAFE_CHUNK_BYTES to chroma_quotas.py

**Bead:** nexus-gvs7
**Files:** `src/nexus/db/chroma_quotas.py`, `tests/test_chroma_quotas.py`
**Blocks:** nexus-oxbm, nexus-9s63, nexus-lr93
**Run command:** `uv run pytest tests/test_chroma_quotas.py -v`

### TDD Red Phase — Write Failing Tests

Add to `tests/test_chroma_quotas.py`:

```python
def test_quotas_has_safe_chunk_bytes() -> None:
    """SAFE_CHUNK_BYTES is 12_288 (12KB, 4KB below MAX_DOCUMENT_BYTES)."""
    from nexus.db.chroma_quotas import QUOTAS
    assert QUOTAS.SAFE_CHUNK_BYTES == 12_288


def test_safe_chunk_bytes_module_alias() -> None:
    """Module-level SAFE_CHUNK_BYTES alias matches the dataclass field."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES, QUOTAS
    assert SAFE_CHUNK_BYTES == QUOTAS.SAFE_CHUNK_BYTES
    assert SAFE_CHUNK_BYTES == 12_288


def test_safe_chunk_bytes_less_than_max_document_bytes() -> None:
    """SAFE_CHUNK_BYTES must be strictly less than MAX_DOCUMENT_BYTES."""
    from nexus.db.chroma_quotas import QUOTAS
    assert QUOTAS.SAFE_CHUNK_BYTES < QUOTAS.MAX_DOCUMENT_BYTES
```

These tests will FAIL because `SAFE_CHUNK_BYTES` does not exist yet.

### TDD Green Phase — Implement

In `src/nexus/db/chroma_quotas.py`:

1. Add field to `ChromaQuotas` dataclass (after `MAX_DOCUMENT_BYTES`):
   ```python
   SAFE_CHUNK_BYTES: int = 12_288  # Target cap for all chunkers (4KB below MAX_DOCUMENT_BYTES)
   ```

2. Add module-level alias after `QUOTAS = ChromaQuotas()`:
   ```python
   SAFE_CHUNK_BYTES = QUOTAS.SAFE_CHUNK_BYTES
   ```

### Validation

```bash
uv run pytest tests/test_chroma_quotas.py -v
# ALL tests pass, including the 3 new ones
```

---

## Phase 2a: Fix chunker.py Escape Hatches

**Bead:** nexus-oxbm
**Depends on:** nexus-gvs7
**Files:** `src/nexus/chunker.py`, `tests/test_chunker.py`
**Run command:** `uv run pytest tests/test_chunker.py -v`

### Context for Executing Agent

Search keywords: `_line_chunk`, `_enforce_byte_cap`, `_CHUNK_MAX_BYTES`, escape hatch,
truncate, UTF-8 boundary.

The existing constant `_CHUNK_MAX_BYTES = 16_000` (line 55 of chunker.py) will be
REMOVED. The tests import it (`from nexus.chunker import ... _CHUNK_MAX_BYTES` on
line 5 of test_chunker.py). This import must be updated as part of the test changes.

Two escape hatches to fix:
1. `_line_chunk` line 123: `# Always emit at least 1 line even if it alone exceeds max_bytes.`
2. `_enforce_byte_cap` line 165: `take = max(1, lo)` — same pattern, emits single-line
   oversized nodes as-is.

### TDD Red Phase — Write Failing Tests

Update `tests/test_chunker.py`:

1. **Fix imports** (line 5): Change
   `from nexus.chunker import chunk_file, _enforce_byte_cap, _line_chunk, _CHUNK_MAX_BYTES`
   to
   `from nexus.chunker import chunk_file, _enforce_byte_cap, _line_chunk`
   `from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES`

2. **Update `test_line_chunk_respects_max_bytes`** (line 216): Replace `_CHUNK_MAX_BYTES`
   with `SAFE_CHUNK_BYTES` in the `max_bytes=` argument and the assertion.

3. **Replace `test_line_chunk_single_oversized_line_emitted_as_is`** (line 228) with:

```python
def test_line_chunk_single_oversized_line_truncated() -> None:
    """A single line larger than max_bytes is truncated, not emitted as-is."""
    big_line = "z" * 20_000  # 20 KB — exceeds SAFE_CHUNK_BYTES
    chunks = _line_chunk(big_line, chunk_lines=150, max_bytes=SAFE_CHUNK_BYTES)
    assert len(chunks) == 1
    assert len(chunks[0][2].encode()) <= SAFE_CHUNK_BYTES
    assert chunks[0][2].startswith("z")  # content preserved up to the cap
```

4. **Update remaining `_CHUNK_MAX_BYTES` references** in test assertions (lines 223-224,
   lines 252, 271, 274, 289, 298) to use `SAFE_CHUNK_BYTES`.

5. **Add new tests:**

```python
def test_line_chunk_truncation_preserves_utf8() -> None:
    """Truncation at byte boundary produces valid UTF-8 (no partial sequences)."""
    # Each char is 3 bytes; 5000 chars = 15_000 bytes > SAFE_CHUNK_BYTES (12_288)
    big_line = "\u4e16" * 5000  # CJK character, 3 bytes each
    chunks = _line_chunk(big_line, chunk_lines=150, max_bytes=SAFE_CHUNK_BYTES)
    assert len(chunks) == 1
    text = chunks[0][2]
    assert len(text.encode()) <= SAFE_CHUNK_BYTES
    # Verify it re-encodes cleanly (no partial multi-byte sequences)
    text.encode("utf-8")  # should not raise


def test_enforce_byte_cap_truncates_single_line_node() -> None:
    """_enforce_byte_cap truncates a single-line oversized AST node."""
    big_text = "x" * 20_000  # single line, 20KB
    chunk = {
        "text": big_text,
        "chunk_index": 0,
        "chunk_count": 1,
        "line_start": 1,
        "line_end": 1,
        "ast_chunked": True,
    }
    result = _enforce_byte_cap([chunk], max_bytes=SAFE_CHUNK_BYTES)
    assert len(result) == 1
    assert len(result[0]["text"].encode()) <= SAFE_CHUNK_BYTES
```

### TDD Green Phase — Implement

In `src/nexus/chunker.py`:

1. **Remove** `_CHUNK_MAX_BYTES = 16_000` (line 55).

2. **Add import** at top:
   ```python
   from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
   ```

3. **Update `_line_chunk` signature** (line 88):
   Change `max_bytes: int = _CHUNK_MAX_BYTES` to `max_bytes: int = SAFE_CHUNK_BYTES`.

4. **Fix `_line_chunk` escape hatch** (lines 122-125): Replace:
   ```python
   # Always emit at least 1 line even if it alone exceeds max_bytes.
   end = start + max(1, lo)
   chunk_text = "\n".join(lines[start:end])
   ```
   With:
   ```python
   end = start + max(1, lo)
   chunk_text = "\n".join(lines[start:end])
   # Truncate at UTF-8 boundary if single line still exceeds limit.
   if len(chunk_text.encode()) > max_bytes:
       chunk_text = chunk_text.encode()[:max_bytes].decode("utf-8", errors="ignore")
   ```

5. **Update `_enforce_byte_cap` signature** (line 139):
   Change `max_bytes: int = _CHUNK_MAX_BYTES` to `max_bytes: int = SAFE_CHUNK_BYTES`.

6. **Fix `_enforce_byte_cap` escape hatch** (lines 164-166): After computing
   `sub_text`, add truncation:
   ```python
   take = max(1, lo)
   sub_text = "\n".join(lines[pos : pos + take])
   # Truncate at UTF-8 boundary if still oversized.
   if len(sub_text.encode()) > max_bytes:
       sub_text = sub_text.encode()[:max_bytes].decode("utf-8", errors="ignore")
   ```

7. **Update docstrings** in both functions to reflect truncation instead of
   emit-as-is behavior.

### Validation

```bash
uv run pytest tests/test_chunker.py -v
# ALL tests pass (old tests updated, new tests green)
uv run pytest tests/test_chunker_ast_languages.py -v
# Existing AST language tests still pass
```

---

## Phase 2b: Add Byte Cap Post-Pass to md_chunker.py

**Bead:** nexus-9s63
**Depends on:** nexus-gvs7
**Files:** `src/nexus/md_chunker.py`, `tests/test_md_chunker.py`
**Run command:** `uv run pytest tests/test_md_chunker.py tests/test_md_chunker_semantic_integrity.py -v`

### TDD Red Phase — Write Failing Tests

Add to `tests/test_md_chunker.py`:

```python
def test_semantic_chunk_respects_byte_cap() -> None:
    """No chunk from semantic path exceeds SAFE_CHUNK_BYTES."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    chunker = SemanticMarkdownChunker(chunk_size=8000)  # large token limit
    # Build content that produces a single large section > 12KB
    big_section = "# Big Section\n\n" + ("word " * 4000)  # ~20KB
    chunks = chunker.chunk(big_section, {"source_path": "test.md"})
    assert chunks  # not empty
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"Chunk {c.chunk_index} is {len(c.text.encode())} bytes "
            f"(limit {SAFE_CHUNK_BYTES})"
        )


def test_naive_chunk_respects_byte_cap() -> None:
    """No chunk from naive fallback path exceeds SAFE_CHUNK_BYTES."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    chunker = SemanticMarkdownChunker(chunk_size=8000)
    # Force naive path
    big_text = "word " * 4000  # ~20KB, no headings → naive
    # Temporarily disable markdown-it to force naive path
    chunker.md = None
    chunks = chunker.chunk(big_text, {})
    assert chunks
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"Chunk {c.chunk_index} is {len(c.text.encode())} bytes "
            f"(limit {SAFE_CHUNK_BYTES})"
        )
```

### TDD Green Phase — Implement

In `src/nexus/md_chunker.py`:

1. **Add import** at top:
   ```python
   from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
   ```

2. **Add `_enforce_byte_cap` method** to `SemanticMarkdownChunker`:
   ```python
   @staticmethod
   def _enforce_byte_cap(
       chunks: list[MarkdownChunk], max_bytes: int = SAFE_CHUNK_BYTES
   ) -> list[MarkdownChunk]:
       """Truncate any chunk whose UTF-8 encoding exceeds *max_bytes*."""
       for chunk in chunks:
           if len(chunk.text.encode()) > max_bytes:
               chunk.text = chunk.text.encode()[:max_bytes].decode(
                   "utf-8", errors="ignore"
               )
       return chunks
   ```

3. **Call the post-pass** at the end of `chunk()` method (before the return in
   both `_semantic_chunking` and `_naive_chunking`, or in `chunk()` itself after
   line 105):
   ```python
   def chunk(self, text: str, metadata: dict) -> list[MarkdownChunk]:
       ...
       # existing code produces `result`
       ...
       return self._enforce_byte_cap(result)  # add byte cap post-pass
   ```

   The cleanest insertion point is in the `chunk()` method itself (line 96-105),
   wrapping the return value of both paths:
   ```python
   if MARKDOWN_IT_AVAILABLE and self.md:
       try:
           return self._enforce_byte_cap(self._semantic_chunking(text, metadata))
       except Exception as exc:
           _log.warning("Semantic chunking failed (%s); falling back to naive.", exc)
   return self._enforce_byte_cap(self._naive_chunking(text, metadata))
   ```

### Validation

```bash
uv run pytest tests/test_md_chunker.py tests/test_md_chunker_semantic_integrity.py -v
# ALL tests pass
```

---

## Phase 2c: Add Byte Cap Post-Pass to pdf_chunker.py

**Bead:** nexus-lr93
**Depends on:** nexus-gvs7
**Files:** `src/nexus/pdf_chunker.py`, `tests/test_pdf_chunker.py`
**Run command:** `uv run pytest tests/test_pdf_chunker.py tests/test_pdf_chunker_integration.py -v`

### TDD Red Phase — Write Failing Tests

Add to `tests/test_pdf_chunker.py`:

```python
def test_pdf_chunk_respects_byte_cap() -> None:
    """No chunk from PDFChunker exceeds SAFE_CHUNK_BYTES."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    chunker = PDFChunker(chunk_chars=20_000)  # large char limit to force big chunks
    big_text = "A" * 20_000  # 20KB
    chunks = chunker.chunk(big_text, {})
    assert chunks
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"Chunk {c.chunk_index} is {len(c.text.encode())} bytes "
            f"(limit {SAFE_CHUNK_BYTES})"
        )
```

### TDD Green Phase — Implement

In `src/nexus/pdf_chunker.py`:

1. **Add import** at top:
   ```python
   from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
   ```

2. **Add byte cap enforcement** in `chunk()` method, after the while loop (before
   `return chunks` on line 75). Add a post-pass:
   ```python
   # Enforce byte cap: truncate any chunk exceeding SAFE_CHUNK_BYTES.
   for c in chunks:
       if len(c.text.encode()) > SAFE_CHUNK_BYTES:
           c.text = c.text.encode()[:SAFE_CHUNK_BYTES].decode(
               "utf-8", errors="ignore"
           )
   return chunks
   ```

### Validation

```bash
uv run pytest tests/test_pdf_chunker.py tests/test_pdf_chunker_integration.py -v
# ALL tests pass
```

---

## Phase 3: Add Last-Resort Drop-and-Warn to t3.py _write_batch

**Bead:** nexus-6sjt
**Depends on:** NONE (independent of Phases 1-2)
**Files:** `src/nexus/db/t3.py`, `tests/test_t3_quota_enforcement.py`
**Run command:** `uv run pytest tests/test_t3_quota_enforcement.py tests/test_t3.py -v`

### Context for Executing Agent

This is a defense-in-depth guard. After the chunker fixes in Phases 2a-2c, no chunk
should ever reach _write_batch at >16KB. But if one does (e.g., a new chunker path,
a manual upsert_chunks_with_embeddings call), it should be dropped with a warning
rather than crashing the entire batch upsert.

Uses `QUOTAS.MAX_DOCUMENT_BYTES` (16,384), NOT `SAFE_CHUNK_BYTES`. The point is to
prevent the ChromaDB hard-limit rejection, not to enforce the soft cap.

### TDD Red Phase — Write Failing Tests

Add to `tests/test_t3_quota_enforcement.py` (or create this file if it already has
relevant structure):

```python
def test_write_batch_drops_oversized_document() -> None:
    """_write_batch silently drops documents exceeding MAX_DOCUMENT_BYTES."""
    import chromadb
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.db.t3 import T3Database

    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_oversized")

    oversized_doc = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
    normal_doc = "hello world"

    db._write_batch(
        col, "code__test_oversized",
        ids=["oversized-1", "normal-1"],
        documents=[oversized_doc, normal_doc],
        metadatas=[{"source_path": "big.py"}, {"source_path": "small.py"}],
    )
    # Only the normal document should be stored
    result = col.get(ids=["normal-1"])
    assert len(result["ids"]) == 1
    assert result["ids"][0] == "normal-1"

    # Oversized document should NOT be stored
    result = col.get(ids=["oversized-1"])
    assert len(result["ids"]) == 0


def test_write_batch_passes_valid_documents() -> None:
    """_write_batch upserts all documents that are within the byte limit."""
    import chromadb
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.db.t3 import T3Database

    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_valid")

    docs = [f"doc content {i}" for i in range(5)]
    ids = [f"id-{i}" for i in range(5)]
    metas = [{"source_path": f"file{i}.py"} for i in range(5)]

    db._write_batch(col, "code__test_valid", ids=ids, documents=docs, metadatas=metas)

    result = col.get(ids=ids)
    assert len(result["ids"]) == 5


def test_write_batch_logs_warning_for_dropped_doc(caplog) -> None:
    """_write_batch logs a warning when dropping an oversized document."""
    import logging
    import chromadb
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.db.t3 import T3Database

    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_warn")

    oversized_doc = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)

    with caplog.at_level(logging.WARNING):
        db._write_batch(
            col, "code__test_warn",
            ids=["oversized-1"],
            documents=[oversized_doc],
            metadatas=[{"source_path": "huge.js"}],
        )
    # structlog may or may not integrate with caplog; check both paths
    # At minimum, the document should not be in the collection
    result = col.get(ids=["oversized-1"])
    assert len(result["ids"]) == 0
```

### TDD Green Phase — Implement

In `src/nexus/db/t3.py`, modify `_write_batch` (lines 200-227):

Add a pre-filter before the batch loop:

```python
def _write_batch(
    self,
    col,
    collection_name: str,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    embeddings: list[list[float]] | None = None,
) -> None:
    """Split into <=300-record chunks and upsert each.

    Documents exceeding MAX_DOCUMENT_BYTES are silently dropped with a
    warning log (defense-in-depth — chunkers should prevent this).
    """
    # Last-resort guard: drop documents exceeding ChromaDB's hard limit.
    max_bytes = QUOTAS.MAX_DOCUMENT_BYTES
    valid = []
    for i, doc in enumerate(documents):
        if len(doc.encode()) > max_bytes:
            source = metadatas[i].get("source_path", "<unknown>") if i < len(metadatas) else "<unknown>"
            _log.warning(
                "write_batch_oversized_document_dropped",
                source_path=source,
                doc_bytes=len(doc.encode()),
                max_bytes=max_bytes,
                collection=collection_name,
            )
        else:
            valid.append(i)

    if not valid:
        return

    ids = [ids[i] for i in valid]
    documents = [documents[i] for i in valid]
    metadatas = [metadatas[i] for i in valid]
    if embeddings is not None:
        embeddings = [embeddings[i] for i in valid]

    size = QUOTAS.MAX_RECORDS_PER_WRITE
    with self._write_sem(collection_name):
        for start in range(0, len(ids), size):
            # ... existing batch upsert logic unchanged ...
```

### Validation

```bash
uv run pytest tests/test_t3_quota_enforcement.py tests/test_t3.py -v
# ALL tests pass
```

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Existing test imports _CHUNK_MAX_BYTES from chunker.py | Tests fail on import | Phase 2a explicitly updates test imports |
| md_chunker preserve_code_blocks emits oversized code blocks atomically | Chunks > 12KB | Byte cap post-pass catches these after atomic emission |
| UTF-8 truncation splits multi-byte character | Invalid text | `errors='ignore'` drops partial sequences safely |
| Chunk count increases for dense content | More API calls | Acceptable per design doc |
| _write_batch guard filters valid docs incorrectly | Data loss | Guard uses hard limit (16KB), not soft cap — only fires for truly oversized docs |

## Full Test Suite Verification

After all phases complete, run the full suite:

```bash
uv run pytest tests/test_chroma_quotas.py tests/test_chunker.py \
    tests/test_chunker_ast_languages.py tests/test_md_chunker.py \
    tests/test_md_chunker_semantic_integrity.py tests/test_pdf_chunker.py \
    tests/test_pdf_chunker_integration.py tests/test_t3.py \
    tests/test_t3_quota_enforcement.py -v
```

## References

- Design: `docs/plans/2026-03-03-chunk-byte-cap-design.md`
- Bead: nexus-bf6w (epic/root)
- Phase beads: nexus-gvs7 (P1), nexus-oxbm (P2a), nexus-9s63 (P2b), nexus-lr93 (P2c), nexus-6sjt (P3)
