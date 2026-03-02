# Implementation Plan: `nx index pdf --dry-run`

**Date:** 2026-03-01
**Design:** docs/plans/2026-03-01-dry-run-pdf-design.md

## Phase 1 — `_index_document` embed_fn parameter

**Files:** `src/nexus/doc_indexer.py`

1. Add `embed_fn: Callable[[list[str], str], tuple[list[list[float]], str]] | None = None`
   as a keyword-only parameter to `_index_document`.
2. Change the credential guard from:
   ```python
   if not _has_credentials():
       return 0
   ```
   to:
   ```python
   if embed_fn is None and not _has_credentials():
       return 0
   ```
3. Replace the Voyage embed block:
   ```python
   from nexus.config import get_credential
   voyage_key = get_credential("voyage_api_key")
   if not voyage_key:
       raise RuntimeError(...)
   embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key)
   ```
   with:
   ```python
   if embed_fn is not None:
       embeddings, actual_model = embed_fn(documents, target_model)
   else:
       from nexus.config import get_credential
       voyage_key = get_credential("voyage_api_key")
       if not voyage_key:
           raise RuntimeError("voyage_api_key must be set — unreachable if _has_credentials() passed")
       embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key)
   ```

**Success criteria:** existing tests still pass (embed_fn=None path unchanged).

## Phase 2 — Thread embed_fn through index_pdf

**Files:** `src/nexus/doc_indexer.py`

Add `embed_fn=None` to `index_pdf` signature and thread to `_index_document`.

**Success criteria:** `index_pdf(..., embed_fn=local_fn)` calls `_index_document` with it.

## Phase 3 — CLI `--dry-run` flag and chunk preview output

**Files:** `src/nexus/commands/index.py`

Add `--dry-run` flag to `index_pdf_cmd`:
- Print dry-run banner
- Build `ef = DefaultEmbeddingFunction()`
- Build `local_t3 = make_t3(_client=chromadb.EphemeralClient(), _ef_override=ef)`
- Build `embed_fn = lambda texts, model: ([v.tolist() for v in ef(texts)], model)`
- Call `index_pdf(path, corpus=corpus, t3=local_t3, embed_fn=embed_fn)`
- Query local collection for metadatas + documents
- Print summary line: `Chunks: N  Pages: X–Y  Title: "..."`
- Print per-chunk preview: `[N] p.{page}  {text[:80]}…`
- Print `(no cloud write)` footer

**Success criteria:** `nx index pdf some.pdf --dry-run` runs without credentials,
prints chunk preview, exits 0.
