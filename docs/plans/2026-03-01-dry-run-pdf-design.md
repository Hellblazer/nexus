# Design: `--dry-run` flag for `nx index pdf`

**Date:** 2026-03-01
**Status:** Approved

## Problem

`nx index pdf` always writes to ChromaDB Cloud (T3) and requires Voyage AI + Chroma credentials.
There is no way to smoke-test PDF extraction locally without API keys.

## Approved Approach: Option B — `embed_fn` parameter on `_index_document`

Add an `embed_fn` keyword argument to the shared `_index_document` pipeline.
When provided, it replaces `_embed_with_fallback` and bypasses the credential guard.
The CLI `--dry-run` flag builds a local T3 (EphemeralClient + DefaultEmbeddingFunction)
and a matching `embed_fn`, then queries the ephemeral collection to display a chunk preview.

No patching. No new functions. Three files changed.

## Changes

### `src/nexus/doc_indexer.py`

1. `_index_document(... embed_fn=None)` — new keyword-only parameter
   - Skip `_has_credentials()` when `embed_fn is not None`
   - Branch: if `embed_fn` → call it; else → credential check + `_embed_with_fallback`

2. `index_pdf(... embed_fn=None)` — thread `embed_fn` through to `_index_document`

### `src/nexus/commands/index.py`

3. `index_pdf_cmd` — add `--dry-run` flag
   - Build `ef = DefaultEmbeddingFunction()`
   - Build `local_t3 = make_t3(_client=EphemeralClient(), _ef_override=ef)`
   - Build `embed_fn = lambda texts, model: ([v.tolist() for v in ef(texts)], model)`
   - Call `index_pdf(path, corpus=corpus, t3=local_t3, embed_fn=embed_fn)`
   - After indexing, query local collection and print chunk preview

## Output Format (approved)

```
Dry-run mode — local ONNX, no cloud writes.
Indexing /path/to/paper.pdf…

  Chunks : 7  Pages: 1–3  Title: "My Document"

  [1] p.1  Hello World. This is a test document…
  [2] p.2  Database transactions ensure ACID…
  …

(no cloud write)
```

## Embed Fn Signature

```python
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]
# (texts, model) -> (embeddings, actual_model)
```

## What Is NOT Changed

- `index_markdown` — no `--dry-run` flag (not requested)
- `_index_pdf_file` (in indexer.py) — not involved
- `make_t3`, `T3Database` — no changes
- No new test files — E2E suite already validates the same code path
