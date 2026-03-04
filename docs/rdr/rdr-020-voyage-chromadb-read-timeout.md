---
id: RDR-020
title: "Voyage AI and ChromaDB Client Read Timeouts"
type: Bug Fix
status: open
priority: P1
created: 2026-03-04
---

# RDR-020: Voyage AI and ChromaDB Client Read Timeouts

## Problem

`nx index repo` can hang indefinitely on a single file when the Voyage AI embedding
API or the ChromaDB Cloud API stalls mid-request. Observed in the wild: stuck on
`WitnessBootstrap.java` for >5 minutes with no progress and no error output.

Root cause: neither `voyageai.Client` nor `chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction`
nor the `chromadb.CloudClient` are constructed with a read timeout. When the remote
server accepts the TCP connection (`ESTABLISHED`) but stops sending data, the client
blocks forever in a socket `recv()` syscall. The transient-error retry helper added
in RDR-019 only fires on exceptions — a hung TCP connection never raises one.

Observed socket state at time of hang:
- 1× `ESTABLISHED` to ChromaDB Cloud (AWS EC2) — active but unresponsive
- 8× `CLOSE_WAIT` to ChromaDB Cloud — server-closed connections the client hadn't released
- 1× `ESTABLISHED` to Voyage AI (Google Cloud) — no response returning

## Research Findings

### Finding 1: `voyageai.Client` timeout parameter confirmed

`voyageai._base._BaseClient.__init__` takes `timeout: Optional[float] = None`.
Stored as `"request_timeout": timeout` in `self._params`, passed to `api_requestor`.

**Default is NOT infinite.** `api_requestor.TIMEOUT_SECS = 600` (10 minutes) is used when
`request_timeout` is `None`. The observed 5-minute hang is within that window — the process
would eventually unblock at ~10 minutes, but that's unacceptable for a per-file operation.

Fix: `voyageai.Client(api_key=key, timeout=60)` — confirmed works. Note: the parameter
is `timeout` on `Client.__init__` but stored as `request_timeout` internally.

### Finding 2: `VoyageAIEmbeddingFunction` — timeout not exposed, but trivially injectable

`chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction.__init__` (line 64):
```python
self._client = voyageai.Client(api_key=self.api_key)
```
No `timeout` param in the constructor signature. `self._client` is a public attribute.

Initial analysis suggested subclassing `VoyageAIEmbeddingFunction` to inject a timeout.
Finding 5 supersedes this: `VoyageAIEmbeddingFunction.__call__` is never invoked in this
codebase — all embed/query paths bypass the EF entirely. Subclassing would add complexity
for a dead code path. The timeout must be applied directly at the three `voyageai.Client`
construction sites identified in Findings 5 and 7.

### Finding 3: `chromadb.CloudClient` — no direct timeout param, but `Settings` works

`chromadb.CloudClient` signature has no `timeout` parameter. However it accepts
`settings: Optional[chromadb.config.Settings]`. `Settings` has:

| Field | Default |
|---|---|
| `chroma_query_request_timeout_seconds` | 60 |
| `chroma_logservice_request_timeout_seconds` | 3 |
| `chroma_sysdb_request_timeout_seconds` | 3 |

The `chroma_query_request_timeout_seconds=60` is already reasonable for query calls.
The real hang risk is on the Voyage AI side (embedding), not ChromaDB directly.

**Action:** Pass `settings=Settings(chroma_query_request_timeout_seconds=N)` to
`CloudClient` to allow future configurability; keep default at 60s.

### Finding 5: Three independent `voyageai.Client` instances — none have a timeout

All three are constructed without `timeout=`:

| Site | File | Line | Path |
|---|---|---|---|
| `T3Database.__init__` | `db/t3.py` | ~129 | CCE (`_cce_embed`) — `docs__`/`knowledge__` |
| `index_repository` | `indexer.py` | 1153 | Code file batch embed — `code__` (hot path) |
| `_embed_with_fallback` | `doc_indexer.py` | 115 | Prose/PDF embed — `docs__` |

`indexer.py:1153` is the hot path for code indexing (the observed WitnessBootstrap.java
hang). `doc_indexer._embed_with_fallback` is the hot path for prose/PDF indexing —
equally vulnerable. `T3Database.__init__` covers the CCE path.

**Note on `_embed_with_fallback` interaction with retry**: `_embed_with_fallback` wraps
the Voyage AI call in `try/except Exception` that falls back to `voyage-4` on any error.
This means a timeout inside it would silently degrade embedding quality rather than retry.
The `_voyage_with_retry` wrapper must go around the CCE call *inside* `_embed_with_fallback`
(or replace the fallback logic), not outside it.

**VoyageAIEmbeddingFunction is structurally attached but never called**: The EF attached
to collections via `_embedding_fn()` / `_query_ef()` in `t3.py` is bypassed at all embed
and query times — `col.upsert()` receives precomputed `embeddings=`, and `col.query()`
receives `query_embeddings=`. **Do not subclass.**

### Finding 6: Full retryable Voyage AI error surface

From `voyageai/api_requestor.py` and `voyageai/error.py`:
- `requests.exceptions.Timeout` → `voyageai.error.Timeout` ← retryable
- `requests.exceptions.RequestException` → `voyageai.error.APIConnectionError` ← retryable
- HTTP 503 → `voyageai.error.ServiceUnavailableError` ← retryable
- HTTP 429 → `voyageai.error.RateLimitError` ← retryable
- `voyageai.error.TryAgain` ← retryable (semantic "retry this")
- `voyageai.error.AuthenticationError`, `InvalidRequestError`, `MalformedRequestError` ← NOT retryable

Cleaner to check `isinstance(exc, voyageai.VoyageError)` and exclude the non-retryable
subtypes than to enumerate retryable ones.

### Decision

1. Add `timeout=read_timeout_seconds` (default: **120s**) to all three `voyageai.Client`
   instances: `T3Database.__init__`, `indexer.py:1153`, and `doc_indexer._embed_with_fallback:115`.
2. Add `_voyage_with_retry` to `db/t3.py` (alongside `_chroma_with_retry`); import it in
   `indexer.py` and `doc_indexer.py`. Wrap: `_cce_embed()`, `indexer.py` batch embed,
   and the CCE call inside `_embed_with_fallback` (inside the existing `try` block, before
   the fallback, so timeout errors retry rather than silently degrade to `voyage-4`).
3. Rename `_is_retryable_chroma_error` → `_is_retryable_api_error`; soft-import
   `voyageai.VoyageError`; handle retryable subtypes (Finding 6).
4. Pass `Settings(chroma_query_request_timeout_seconds=60)` to `CloudClient` — keep the
   existing 60s default; this makes it explicitly configurable without changing behaviour.
5. **Do not** subclass `VoyageAIEmbeddingFunction` — its `__call__` is never reached.
6. Add `read_timeout_seconds` to `~/.config/nexus/config.yml`; thread through `make_t3()`.

### Finding 4: `voyageai.error.Timeout` is NOT an `httpx.TransportError` — retry helper needs update

`voyageai` uses `requests` (not httpx) internally. On timeout:
```
requests.exceptions.Timeout → caught → re-raised as voyageai.error.Timeout(VoyageError)
```
`voyageai.error.Timeout` inherits from `VoyageError(Exception)`. It is **not** an
`httpx.TransportError` and is **not** caught by the current `_is_retryable_chroma_error`.

The `_RETRYABLE_FRAGMENTS` string set also does not contain `"timed out"` or `"timeout"`,
so the string fallback won't catch it either.

**Implementation must also update `_is_retryable_chroma_error`** to handle
`voyageai.error.Timeout`, e.g.:
```python
try:
    import voyageai.error as _voyageai_error
    _VOYAGE_TIMEOUT_TYPE: type | None = _voyageai_error.Timeout
except ImportError:
    _VOYAGE_TIMEOUT_TYPE = None

def _is_retryable_chroma_error(exc: BaseException) -> bool:
    if _VOYAGE_TIMEOUT_TYPE and isinstance(exc, _VOYAGE_TIMEOUT_TYPE):
        return True
    ...
```

This is the highest-priority item in the implementation — without it, timeouts will
propagate as unhandled exceptions and abort the indexing run rather than retrying.

## Implementation Plan

1. **Add `read_timeout_seconds` to `~/.config/nexus/config.yml` schema** (default: 120s)
   and load it in `make_t3()`. Thread through `T3Database.__init__` and `make_t3()`.

2. **Rename `_is_retryable_chroma_error` → `_is_retryable_api_error`** and update it:
   soft-import `voyageai.VoyageError` at module level; add check to return `True` for
   retryable `VoyageError` subtypes (`Timeout`, `APIConnectionError`, `ServiceUnavailableError`,
   `RateLimitError`, `TryAgain`) and `False` for non-retryable ones
   (`AuthenticationError`, `InvalidRequestError`, `MalformedRequestError`).
   TDD: write failing tests for each retryable/non-retryable Voyage AI error first.

3. **Add a `_voyage_with_retry` helper** (or extract `_with_retry` as shared)
   that wraps Voyage AI call sites. Can reuse `_chroma_with_retry` body since
   `_is_retryable_api_error` now covers both surfaces — but separate naming avoids
   confusion at call sites.

4. **Wire `timeout` into all three `voyageai.Client` instances:**
   - `T3Database.__init__` (~line 129): `voyageai.Client(api_key=..., timeout=read_timeout_seconds)`
   - `indexer.py:1153`: `voyageai.Client(api_key=..., timeout=read_timeout_seconds)`
   - `doc_indexer._embed_with_fallback:115`: `voyageai.Client(api_key=..., timeout=read_timeout_seconds)`

5. **Wrap Voyage AI call sites with `_voyage_with_retry`:**
   - `T3Database._cce_embed()`: wrap `self._voyage_client.contextualized_embed(...)`
   - `indexer.py`: wrap `voyage_client.embed(...)` at the batch embed call site
   - `doc_indexer._embed_with_fallback`: wrap the `client.contextualized_embed(...)` call
     *inside* the existing `try` block so timeout errors retry rather than fall back silently

6. **Pass `Settings` to `CloudClient`**:
   `Settings(chroma_query_request_timeout_seconds=read_timeout_seconds)`.

7. **TDD integration tests:**
   - `_is_retryable_api_error` returns True for each retryable Voyage AI error type.
   - Mock `voyageai.Client.embed` to raise `voyageai.error.Timeout` → verify retry fires
     and ultimately raises after max attempts.
   - Mock `voyageai.Client.contextualized_embed` → same via `_cce_embed`.

## Acceptance Criteria

- [ ] `nx index repo` never hangs indefinitely on a Voyage AI or ChromaDB call.
- [ ] Voyage AI calls raise `voyageai.error.Timeout` within 120s (default); configurable.
- [ ] ChromaDB Cloud queries respect `chroma_query_request_timeout_seconds` (default: 120s).
- [ ] `_is_retryable_api_error` returns True for `voyageai.error.Timeout`, `APIConnectionError`,
      `ServiceUnavailableError`, `RateLimitError`, `TryAgain`; False for auth/invalid errors.
- [ ] All three `voyageai.Client` instances are constructed with `timeout=read_timeout_seconds`.
- [ ] `_cce_embed()`, `indexer.py` batch embed, and `doc_indexer._embed_with_fallback` CCE call are wrapped with `_voyage_with_retry`.
- [ ] Timeout in `_embed_with_fallback` retries rather than silently falling back to `voyage-4`.
- [ ] Unit tests cover retryable/non-retryable Voyage AI error classification.
- [ ] Integration test: mock Voyage AI timeout → retry fires → fails cleanly after max attempts.
- [ ] Test asserts `voyageai.Client` is instantiated with the configured `timeout` value.
- [ ] Timeout configurable via `read_timeout_seconds` in `~/.config/nexus/config.yml`.
