# Design: Proactive 12KB Chunk Byte Cap (nexus-bf6w)

**Date:** 2026-03-03
**Bead:** nexus-bf6w
**Status:** Approved

## Problem

ChromaDB enforces a 16KB document size limit per chunk. Nexus has a byte cap in
`chunker.py` (`_CHUNK_MAX_BYTES = 16_000`) but it has an escape hatch that allows
oversized single-line content through. `md_chunker.py` and `pdf_chunker.py` operate
in token/char space with no byte validation at all. Result: one 17 576-byte chunk
killed the entire ART reindex.

## Decision

Proactively cap all chunks at **`SAFE_CHUNK_BYTES = 12_288`** (12KB). This gives
4KB of headroom below ChromaDB's 16KB hard limit and ~3700 tokens — well within
Voyage AI's embedding window.

## Changes

### 1. `chroma_quotas.py` — add `SAFE_CHUNK_BYTES`

Add to `ChromaQuotas`:
```python
SAFE_CHUNK_BYTES: int = 12_288  # Target cap for all chunkers (4KB below MAX_DOCUMENT_BYTES)
```
And module-level alias: `SAFE_CHUNK_BYTES = QUOTAS.SAFE_CHUNK_BYTES`

### 2. `chunker.py` — fix escape hatch, lower constant

- Change `_CHUNK_MAX_BYTES = 16_000` → import `SAFE_CHUNK_BYTES` from `chroma_quotas`
- `_line_chunk`: replace "emit at least 1 line even if oversized" with truncation at
  UTF-8 boundary: `text.encode()[:max_bytes].decode('utf-8', errors='ignore')`
- `_enforce_byte_cap`: same fix — truncate single-line nodes instead of emitting as-is

### 3. `md_chunker.py` — add byte cap post-processor

After the token-based splitting loop, apply a post-pass: any chunk whose UTF-8
byte length exceeds `SAFE_CHUNK_BYTES` is truncated at a UTF-8 boundary.

### 4. `pdf_chunker.py` — add byte cap post-processor

Same post-pass as md_chunker after char-based splitting.

### 5. `db/t3.py` — last-resort drop-and-warn in `_write_batch`

Before each sub-batch upsert, filter out any document whose UTF-8 byte length
exceeds `QUOTAS.MAX_DOCUMENT_BYTES` (16KB hard limit). Log a `structlog.warning`
with `source_path` (from metadata) and actual byte count. Defense-in-depth — the
chunker fixes should prevent this from ever firing in practice.

## What is NOT changing

- `MAX_DOCUMENT_BYTES = 16_384` stays as ChromaDB's hard limit constant
- `QuotaValidator.validate_record()` continues to raise for explicit callers
- `upsert_chunks()` raising behavior is unchanged
- Chunk *count* per file increases slightly for dense content — acceptable

## Files

- `src/nexus/db/chroma_quotas.py`
- `src/nexus/chunker.py`
- `src/nexus/md_chunker.py`
- `src/nexus/pdf_chunker.py`
- `src/nexus/db/t3.py`
- `tests/test_chunker.py`
- `tests/test_md_chunker.py`
- `tests/test_pdf_chunker.py`
- `tests/test_chroma_quotas.py`
- `tests/test_t3_write.py` (or equivalent)
