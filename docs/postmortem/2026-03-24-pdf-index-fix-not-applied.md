# Postmortem: nx index pdf fix not applied in v2.4.0

**Date**: 2026-03-24
**Severity**: High — believed-fixed bug still present in installed version
**Related**: [2026-03-23 original postmortem](2026-03-23-pdf-index-collection-mismatch.md)
**Impact**: Round-trip test after "fix" confirmed the bug persists in nx 2.4.0

## Summary

After the 2026-03-23 incident (10,036 chunks indexed to wrong collection with wrong embedding model), a fix was reported as applied to the nx codebase. However, a round-trip verification test on 2026-03-24 confirmed the fix is **NOT present** in the installed `nx` version 2.4.0. The bug remains: `nx index pdf --collection knowledge` still creates a bare `"knowledge"` collection with `voyage-4` embeddings instead of `"knowledge__knowledge"` with `voyage-context-3`.

## Reproduction Steps

### Prerequisites
- nx version 2.4.0 installed (`nx --version`)
- Chroma Cloud configured (`~/.config/nexus/config.yml` has `chroma_api_key`)
- A test PDF file (any small PDF will do)

### Step 1: Confirm baseline state
```bash
# Check current knowledge collection
nx collection info knowledge__knowledge
# Expected: Documents: N, Index model: voyage-context-3
```

### Step 2: Index a test PDF
```bash
nx index pdf /path/to/test.pdf --collection knowledge
# Output: "Indexed N chunk(s)." — appears successful
```

### Step 3: Check if chunks landed in the right collection
```bash
nx collection info knowledge__knowledge
# EXPECTED (if fixed): Documents: N + chunks
# ACTUAL (bug): Documents: N (unchanged — chunks went elsewhere)
```

### Step 4: Check where chunks actually went
```bash
nx collection info knowledge
# ACTUAL: Documents: chunks, Index model: voyage-4
# This bare "knowledge" collection should NOT exist
```

### Step 5: Verify search cannot find indexed content
```bash
# Search for distinctive text from the indexed PDF
nx search "some unique phrase from the PDF" --corpus knowledge
# ACTUAL: Returns only pre-existing manual T3 entries, NOT the PDF chunks
```

## Root Cause (unchanged from original postmortem)

In `nexus/commands/index.py`, the `index_pdf_cmd` function passes the `--collection` argument directly to `doc_indexer.index_pdf()` without calling `t3_collection_name()`:

```python
# Line ~249 in commands/index.py (v2.4.0)
n = index_pdf(path, corpus=corpus, collection_name=collection, force=force)
```

The `collection` variable is the raw user input `"knowledge"`. It should be transformed via:

```python
from nexus.corpus import t3_collection_name
collection = t3_collection_name(collection)  # "knowledge" → "knowledge__knowledge"
```

This same transformation is performed by `store put` (via `commands/store.py`) and `search` (via `corpus.py:resolve_corpus()`), but NOT by `index pdf`.

## Two Bugs From One Missing Call

### Bug 1: Wrong collection name
- `"knowledge"` is created instead of `"knowledge__knowledge"`
- `nx search --corpus knowledge` resolves to `knowledge__*` prefix → finds `knowledge__knowledge` → never sees bare `knowledge`

### Bug 2: Wrong embedding model
- `index_model_for_collection("knowledge")` returns `voyage-4` (default for unknown prefixes)
- `index_model_for_collection("knowledge__knowledge")` returns `voyage-context-3` (correct for `knowledge__*`)
- Chunks embedded with `voyage-4` cannot be meaningfully searched with `voyage-context-3` queries

## Required Fix

In `nexus/commands/index.py`, function `index_pdf_cmd`, add before any use of `collection`:

```python
if collection is not None:
    from nexus.corpus import t3_collection_name
    collection = t3_collection_name(collection)
```

This must be applied in THREE code paths within `index_pdf_cmd`:
1. Line ~197 (dry-run path): `index_pdf(path, corpus=corpus, t3=local_t3, collection_name=collection, ...)`
2. Line ~235 (monitor path): `index_pdf(path, corpus=corpus, collection_name=collection, force=force, ...)`
3. Line ~249 (normal path): `index_pdf(path, corpus=corpus, collection_name=collection, force=force)`

The simplest fix: add the transformation immediately after `path = path.resolve()` (line 181), before any branch:

```python
path = path.resolve()

# Normalize collection name to T3 convention (e.g. "knowledge" → "knowledge__knowledge")
if collection is not None:
    from nexus.corpus import t3_collection_name
    collection = t3_collection_name(collection)
```

Similarly check `index md` command (line ~254+) for the same bug.

## Verification After Fix

After applying the fix, re-run the reproduction steps above. Expected results:

1. `nx index pdf test.pdf --collection knowledge` should report "Indexed N chunk(s)."
2. `nx collection info knowledge__knowledge` should show increased document count
3. `nx collection info knowledge` should return "not found" or 0 documents
4. `nx search "phrase from PDF" --corpus knowledge` should return the indexed chunks
5. The returned chunks should show `embedding_model: voyage-context-3`

## Cleanup Required

After confirming the fix works:

1. Delete the orphaned bare `"knowledge"` collection (18 chunks from failed test):
```python
from nexus.db import make_t3
t3 = make_t3()
t3.delete_collection('knowledge')
```

2. Re-index all ART papers (~68 PDFs + CMRB 771 pages) with `--collection knowledge`

3. Verify round-trip search for each major paper

## Workaround (Until Fix Is Applied)

Use the fully-qualified collection name:
```bash
nx index pdf /path/to/paper.pdf --collection knowledge__knowledge
```

This bypasses `t3_collection_name()` entirely (the `__` check in the function returns the input as-is). Both the collection name and embedding model will be correct.
