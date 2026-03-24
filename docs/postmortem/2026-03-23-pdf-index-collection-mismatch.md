# Postmortem: nx index pdf stores chunks in wrong collection

**Date**: 2026-03-23
**Severity**: High — 10,036 chunks silently stored in unsearchable collection
**Duration**: ~4 hours of indexing work before discovery
**Impact**: All `nx index pdf --collection knowledge` operations stored chunks where `nx search --corpus knowledge` cannot find them

## Summary

`nx index pdf --collection knowledge` stores chunks in a Chroma collection named `"knowledge"`. But `nx search --corpus knowledge` resolves to collections matching `"knowledge__*"`, finding only `"knowledge__knowledge"`. The 10,036 PDF chunks were invisible to search.

## Root Cause

Collection naming inconsistency between `index` and `search` commands:

**Index path** (`commands/index.py` line 249):
```python
n = index_pdf(path, corpus=corpus, collection_name=collection, force=force)
```
`collection_name="knowledge"` is passed directly to `doc_indexer.index_pdf()`, which at line 191:
```python
col = db.get_or_create_collection(collection_name)  # creates "knowledge"
```

**Search path** (`mcp_server.py` line 124):
```python
target = resolve_corpus(corpus, _get_collection_names())
```
`resolve_corpus("knowledge", ...)` in `corpus.py`:
```python
prefix = f"{corpus}__"
matches = [c for c in all_collections if c.startswith(prefix)]
# Returns ["knowledge__knowledge"], NOT ["knowledge"]
```

**Store put path** (`commands/store.py` via `t3_collection_name()`):
```python
def t3_collection_name(user_arg: str) -> str:
    if "__" in user_arg:
        return user_arg
    return f"knowledge__{user_arg}"  # "knowledge" → "knowledge__knowledge"
```

The `--collection` flag in `index pdf` does NOT call `t3_collection_name()`. It passes the raw user input directly to Chroma. `store put` and `search` both use the `knowledge__` prefix convention. `index pdf` does not.

## Fix Options

### Option A (Recommended): Apply `t3_collection_name()` in index command

In `commands/index.py`, before passing to `index_pdf`:
```python
if collection is not None:
    from nexus.corpus import t3_collection_name
    collection = t3_collection_name(collection)
```

This ensures `--collection knowledge` becomes `knowledge__knowledge`, matching what search expects.

### Option B: Make search also check bare collection names

In `corpus.py` `resolve_corpus()`:
```python
if not matches:
    # Fallback: check bare collection name
    matches = [c for c in all_collections if c == corpus]
```

Less clean — perpetuates the naming inconsistency.

### Option C: Document the fully-qualified requirement

Update `--collection` help text: "Must be fully-qualified with __ separator (e.g., knowledge__knowledge)."

Least desirable — user-hostile.

## Recovery

Manually moved 10,036 chunks from `"knowledge"` to `"knowledge__knowledge"` using:
```python
from nexus.db import make_t3
t3 = make_t3()
src = t3.get_or_create_collection("knowledge")
dst = t3.get_or_create_collection("knowledge__knowledge")
# batch upsert from src to dst
```

The bare `"knowledge"` collection should be deleted after confirming the move.

## Timeline

- 20:24 — Started indexing PDFs with `nx index pdf ... --collection knowledge`
- 20:24-21:30 — Indexed 68 papers, ~7,000 chunks. All reported "Indexed N chunk(s)." — appeared successful.
- 21:30 — Indexed CMRB (3,054 chunks, 771 pages)
- 22:15 — Attempted to audit indexed content. `nx search` returned no PDF results.
- 22:30 — Investigated. Found bare `"knowledge"` collection with 10,036 entries.
- 22:45 — Identified root cause: `--collection` flag bypass of `t3_collection_name()`.
- 23:00 — Moved all 10,036 chunks to correct collection.

## Lessons

1. **Silent success is worse than loud failure.** The index command reported "Indexed N chunk(s)" — it succeeded at storing, just in the wrong place. A post-index verification step (search the indexed content, confirm it's findable) would have caught this immediately.

2. **Collection naming conventions must be enforced at every entry point.** The `__` separator convention exists in `t3_collection_name()` but isn't called by all code paths.

3. **Test with round-trip.** Index a document, then search for it. If the search doesn't find it, the index didn't really work.

## Update: Embedding Model Corruption (discovered same session)

The collection name mismatch caused a SECOND failure: `index_model_for_collection("knowledge")` returns `voyage-4` (default), not `voyage-context-3` (required for `knowledge__*` collections). All 10,036 PDF chunks were embedded with the wrong model.

When we moved the chunks from `"knowledge"` to `"knowledge__knowledge"`, we created a mixed-model collection: 312 voyage-context-3 entries + 10,036 voyage-4 entries. Search uses voyage-context-3 for queries, so the voyage-4 entries would return poor similarity scores.

**Recovery**: Deleted all 10,036 voyage-4 entries from `knowledge__knowledge`. Collection restored to 312 clean entries.

**Required**: Re-index all PDFs with correct collection name (`knowledge__knowledge`) so they get `voyage-context-3` embeddings. This requires either:
1. Fix the `--collection` flag bug first, then re-run `nx index pdf --collection knowledge`
2. Or use `--collection knowledge__knowledge` (fully-qualified) as a workaround

**Estimated re-index time**: ~30 minutes (the indexing itself is fast, the embedding API calls dominate)

## Cascading Failure Chain

```
--collection knowledge (user input)
  → Missing t3_collection_name() call
    → Collection "knowledge" created (wrong name)
      → index_model_for_collection("knowledge") = voyage-4 (wrong model)
        → 10,036 chunks embedded with voyage-4
          → Chunks invisible to search (wrong collection)
            → Manual move to knowledge__knowledge
              → Mixed embedding spaces (corrupted)
                → Manual deletion to restore clean state
                  → Need to re-index everything
```

One missing function call → 6 hours of work lost.
