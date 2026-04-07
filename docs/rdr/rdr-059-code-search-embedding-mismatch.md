---
title: "Code Search Embedding Model Mismatch"
id: RDR-059
type: Bug
status: draft
priority: critical
author: Hal Hildebrand
created: 2026-04-07
related_issues: [RDR-056, RDR-014]
---

# RDR-059: Code Search Embedding Model Mismatch

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Code search against `code__*` collections returns random noise. All results cluster at distances 0.858-0.931 with only 0.038 spread — no discrimination between relevant and irrelevant chunks. This was discovered empirically during RDR-056 baseline measurements (RF-13).

**Root cause**: Index-time and query-time embedding models are incompatible.

- **Index**: `voyage-code-3` with `input_type="document"` (`code_indexer.py:410`)
- **Query**: `voyage-4` with `input_type=None` (`t3.py:_embedding_fn()`)

`corpus.py:52` claims "the semantic spaces are compatible enough for effective retrieval." **This is empirically falsified.** The voyage-code-3 and voyage-4 vector spaces do not overlap meaningfully — query vectors are orthogonal to all indexed vectors, producing uniformly high distances.

Additionally, `VoyageAIEmbeddingFunction` is constructed with `input_type=None`, meaning no retrieval prompt is prepended even within the voyage-4 space.

### Impact

Every `nx search --corpus code` and every MCP `search(corpus="code")` call returns noise. Agent workflows that include code in their search scope (the default `corpus="knowledge,code,docs"`) receive 33% noise results from code collections. This has been silently degrading search quality.

## Research Findings

### RF-1: Empirical Evidence of Mismatch

**Source**: RDR-056 RF-13 baseline measurement (10 queries × 20 results)

All 40 code results: distances 0.858-0.931, mean 0.889, spread 0.038. Queries like "verify collection deep health check" returned Java style sheets and GPU shaders — completely irrelevant. The 0.038 spread is indistinguishable from random sampling.

### RF-2: Embedding Model Architecture

**Source**: `src/nexus/corpus.py:45-81`, `src/nexus/db/t3.py:152-173`

Two functions control model routing:
- `index_model_for_collection(name)`: code__ → `voyage-code-3`, CCE collections → `voyage-context-3`
- `embedding_model_for_collection(name)`: code__ → `voyage-4` (the QUERY model)

The asymmetry is intentional but incorrect. `_embedding_fn()` creates `VoyageAIEmbeddingFunction(model_name=model)` with no `input_type` parameter — defaults to `None`.

### RF-3: voyage-code-3 Is Code-Centric, Not Cross-Modal

**Source**: Voyage AI documentation via Context7

voyage-code-3 is optimized for code-related tasks and programming documentation. Not designed for NL→code cross-modal retrieval. Models trained on code corpora produce code-centric vector spaces where NL queries are orthogonal.

## Proposed Fix

### Option A: Match Query Model to Index Model (recommended, immediate)

Change `_embedding_fn()` for code__ collections to use `voyage-code-3` with `input_type="query"`:

```python
# t3.py — _embedding_fn()
def _embedding_fn(self, collection_name: str):
    model = embedding_model_for_collection(collection_name)
    input_type = "query"  # always use retrieval mode
    return VoyageAIEmbeddingFunction(model_name=model, input_type=input_type)
```

And update `corpus.py`:
```python
def embedding_model_for_collection(name: str) -> str:
    # Query with the SAME model used for indexing
    if name.startswith("code__"):
        return "voyage-code-3"  # was: "voyage-4"
    ...
```

**Pros**: No re-indexing. Fixes code-to-code search immediately. Spaces match.
**Cons**: NL→code still limited (voyage-code-3 is code-centric). But at least the spaces are coherent.

### Option B: Re-Index with voyage-4 (medium-term, better NL support)

Re-index all code__ collections with `voyage-4` `input_type="document"`, query with `voyage-4` `input_type="query"`.

**Pros**: Single coherent space. NL→code works. Standard retrieval mode.
**Cons**: Full re-index required. voyage-4 is general-purpose — code-to-code similarity may be slightly lower than voyage-code-3 for pure code queries.

### Option C: Dual-Collection (not recommended)

Index code twice: once with voyage-code-3 (for code-to-code), once with voyage-4 (for NL-to-code). Route based on query type.

**Cons**: Doubles storage. Requires query classification. Complex. Not worth it without ChromaDB multi-vector support.

## Recommendation

**Ship Option A immediately** — it's a 2-line fix that makes code search functional. Evaluate Option B as a follow-up if NL→code quality remains insufficient after Option A.

## Success Criteria

- [ ] Code search returns discriminating distances (spread > 0.2, not 0.038)
- [ ] "verify collection deep" query returns actual verify_collection_deep code
- [ ] Query and index models match for code__ collections
- [ ] input_type="query" set for all query-time embeddings
