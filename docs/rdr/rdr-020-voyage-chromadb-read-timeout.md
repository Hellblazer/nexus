---
id: RDR-020
title: "Voyage AI and ChromaDB Client Read Timeouts"
type: Bug Fix
status: accepted
accepted_date: 2026-03-04
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
for a dead code path. The timeout must be applied directly at the four `voyageai.Client`
construction sites identified in Finding 5.

### Finding 3: `chromadb.CloudClient` — no client-configurable HTTP timeout

`chromadb.CloudClient` has no `timeout` parameter and the `Settings` timeout fields
(`chroma_query_request_timeout_seconds`, etc.) do **not** apply to the HTTP client path.

Source inspection confirms: `chromadb/api/fastapi.py:85-91` constructs
`httpx.Client(timeout=None)` unconditionally — the Settings object is not consulted.
`chroma_query_request_timeout_seconds` is consumed exclusively by
`chromadb/execution/executor/distributed.py` (gRPC distributed executor), not the
HTTP client used by `CloudClient`.

**Conclusion:** There is currently no public API to set a read timeout on `CloudClient`
HTTP connections. The real hang risk is on the Voyage AI side (embedding), not ChromaDB
directly — the socket evidence shows the ChromaDB-side connections were `CLOSE_WAIT`
(already server-closed) while the Voyage AI connection was the active blocker.

**Action:** No Settings change. ChromaDB HTTP timeout is not configurable via the
current public API. Future ChromaDB versions may add this; do not add code that appears
to configure it but has no effect.

### Finding 5: Four independent `voyageai.Client` instances — none have a timeout

All four are constructed without `timeout=`:

| Site | File | Line | Call path |
|---|---|---|---|
| `T3Database.__init__` | `db/t3.py` | ~129 | CCE (`_cce_embed`) — `docs__`/`knowledge__` |
| `index_repository` | `indexer.py` | 1153 | Code file batch embed — `code__` (observed hang) |
| `_embed_with_fallback` | `doc_indexer.py` | 115 | Prose/PDF embed — `docs__` |
| `_voyage_client()` | `scoring.py` | 114 | Reranking (`nx search --rerank`) |

`indexer.py:1153` is the hot path for code indexing (the WitnessBootstrap.java hang).
`doc_indexer._embed_with_fallback` covers prose/PDF. `scoring.py:_voyage_client()` is
a lazy-init singleton used by `rerank_results()` on the interactive search path.

**`_embed_with_fallback` — three Voyage AI call sites, all unprotected:**
- Line 127: `client.contextualized_embed(...)` — inside `try/except Exception`. A timeout
  here currently degrades silently to voyage-4 without retrying.
- Line 137: `client.embed(texts=sub, model="voyage-4", ...)` — inside the CCE except
  block (fallback path). A timeout here propagates uncaught out of the except block.
- Lines 144-148: standard embedding path (non-CCE, or single-chunk fallback) — batch loop
  calling `client.embed()` with no try/except at all. A timeout aborts indexing for that file.

All three must be wrapped with `_voyage_with_retry`. The CCE retry at line 127 means a
transient timeout retries rather than degrading; the fallback at line 137 protects the
secondary CCE path; the standard path at lines 144-148 is the most exposed (no existing
exception handling).

**`scoring.py` config threading**: `_voyage_client()` reads the API key via `get_credential()`,
not through `make_t3()`. The `read_timeout_seconds` config must be read directly from the
nexus config in `_voyage_client()` (same `~/.config/nexus/config.yml` key).

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

1. Add `timeout=read_timeout_seconds, max_retries=3` (timeout default: **120s**) to all four
   `voyageai.Client` instances: `T3Database.__init__`, `indexer.py:1153`,
   `doc_indexer._embed_with_fallback:115`, and `scoring.py:_voyage_client():114`.
   The built-in `max_retries=3` handles `Timeout`, `RateLimitError`, `ServiceUnavailableError`
   with exponential backoff — no separate outer retry wrapper needed for these (Finding 8).
2. **Do NOT rename** `_is_retryable_chroma_error` — it is correct for ChromaDB call sites.
   Add a separate `_is_retryable_voyage_error` (soft-imports `voyageai.error`) that returns
   `True` for `APIConnectionError` and `TryAgain` only. `_chroma_with_retry` keeps its own
   oracle; `_voyage_with_retry` uses the new one. The two wrappers operate on disjoint call
   stacks and must not share a combined oracle.
3. Add `_voyage_with_retry` to `db/t3.py` (alongside `_chroma_with_retry`), using
   `_is_retryable_voyage_error` and `max_attempts=3` (lower than `_chroma_with_retry`'s 5 —
   rerank path relies on outer `try/except` for degradation; a 3-attempt ceiling limits
   worst-case blocking on the interactive search path). Import in `indexer.py` and `doc_indexer.py`.
   **Do NOT add Voyage AI error types to `_is_retryable_chroma_error`.**
   Add `_reset_voyage_client()` to `scoring.py` for testability.
4. **No change to `CloudClient`**: ChromaDB HTTP timeout is not configurable via the public
   API (`fastapi.py` hardcodes `httpx.Client(timeout=None)`; `chroma_query_request_timeout_seconds`
   applies only to the gRPC distributed executor — see Finding 3). Do not pass `Settings`.
5. **Do not** subclass `VoyageAIEmbeddingFunction` — its `__call__` is never reached.
6. Add `read_timeout_seconds: 120` to the `voyageai:` section of `config.py`'s `_DEFAULTS`
   (add `voyageai:` section if absent). Add env var override `NX_VOYAGEAI_READ_TIMEOUT_SECONDS`
   to `_ENV_OVERRIDES`. Thread through `make_t3()` and `T3Database.__init__`.
   For `_embed_with_fallback`, add `timeout: float = 120.0` parameter to the function signature
   and update both callers in `indexer.py` (lines 776 and 871) to pass `read_timeout_seconds`
   read from `load_config()` at the top of `index_repository()`.
   For `scoring.py:_voyage_client()`, read directly from `load_config()["voyageai"]["read_timeout_seconds"]`.
   The singleton has no reconfiguration path after first call; provide `_reset_voyage_client()`
   (test-only module-private) for testability.

### Finding 7: `voyageai.error.Timeout` is NOT an `httpx.TransportError` — retry helper needs update

`voyageai` uses `requests` (not httpx) internally. On timeout:
```
requests.exceptions.Timeout → caught → re-raised as voyageai.error.Timeout(VoyageError)
```
`voyageai.error.Timeout` inherits from `VoyageError(Exception)`. It is **not** an
`httpx.TransportError` and is **not** caught by the current `_is_retryable_chroma_error`.

The `_RETRYABLE_FRAGMENTS` string set also does not contain `"timed out"` or `"timeout"`,
so the string fallback won't catch it either.

**Do NOT modify `_is_retryable_chroma_error`** — add a separate `_is_retryable_voyage_error`
(Decision item 2). With `max_retries=3` on the Client, `Timeout`/`RateLimitError`/
`ServiceUnavailableError` are handled internally by the built-in retry. The outer wrapper
covers only `APIConnectionError` and `TryAgain` (Finding 8), e.g.:
```python
try:
    import voyageai.error as _voyageai_error
    _VOYAGE_ERROR_TYPES: tuple[type, ...] | None = (
        _voyageai_error.APIConnectionError,
        _voyageai_error.TryAgain,
    )
except ImportError:
    _VOYAGE_ERROR_TYPES = None

def _is_retryable_voyage_error(exc: BaseException) -> bool:
    return bool(_VOYAGE_ERROR_TYPES and isinstance(exc, _VOYAGE_ERROR_TYPES))
```

This is the highest-priority item in the implementation — without it, `APIConnectionError`
will propagate as unhandled exceptions and abort the indexing run rather than retrying.

### Finding 8: `voyageai.Client` has a built-in `max_retries` parameter — use it instead of an outer wrapper

`voyageai.Client.__init__` accepts `max_retries: int = 0`. When `max_retries > 0`, the
client activates a `tenacity`-based retry loop inside `embed()`, `contextualized_embed()`,
and `rerank()` covering `RateLimitError`, `ServiceUnavailableError`, and `Timeout`
(`client.py:36-86`). Retry uses `wait_exponential_jitter(initial=1, max=16)` with `reraise=True`.

`APIConnectionError` and `TryAgain` are **not** covered by the built-in retry — they
propagate directly if they occur.

**Design implication**: Using both `max_retries=N` on the `Client` AND an outer
`_voyage_with_retry` wrapper creates multiplicative retry behavior (up to N×outer_attempts).
The correct approach is to use `max_retries` on the Client for the three covered error types,
and optionally add a thin outer catch for `APIConnectionError` and `TryAgain` only.

**Decision update**: Use `voyageai.Client(api_key=key, timeout=read_timeout_seconds, max_retries=3)`
at all four construction sites. This activates built-in retry for `Timeout`, `RateLimitError`,
`ServiceUnavailableError`. Add a separate `_is_retryable_voyage_error` covering only
`APIConnectionError` and `TryAgain` for the outer `_voyage_with_retry` wrapper. Do NOT modify
`_is_retryable_chroma_error` — the two error spaces are disjoint and must not share an oracle.

**`rerank_results` exception-swallowing**: `scoring.py:rerank_results()` wraps its call in
`try/except Exception` and returns unranked results on any failure — intentional degraded-mode
behavior for the interactive search path. `_voyage_with_retry` firing inside this except block
will retry but ultimately the outer except catches any raised exception. This means
`rerank_results` never propagates Voyage AI errors; it always degrades gracefully. This is the
correct behavior for an interactive path and should be documented as intentional, not a bug.

## Implementation Plan

1. **Add `read_timeout_seconds` to `~/.config/nexus/config.yml` schema** (default: 120s)
   and load it in `make_t3()`. Thread through `T3Database.__init__` and `make_t3()`.
   For `scoring.py:_voyage_client()`, read directly from the nexus config (not via `make_t3()`).

2. **Add `_is_retryable_voyage_error`** to `db/t3.py`; soft-import `voyageai.error`; return
   `True` for `APIConnectionError` and `TryAgain` only. Do NOT modify `_is_retryable_chroma_error`.
   TDD: write failing tests for `APIConnectionError` and `TryAgain` returning True;
   `Timeout`, `RateLimitError`, `ServiceUnavailableError` returning False from this function.
   Also write regression tests: `_is_retryable_chroma_error` still returns True for
   `httpx.TransportError` and `httpx.HTTPStatusError` 503/429 (unchanged behavior).

3. **Add `_voyage_with_retry` to `db/t3.py`** (alongside `_chroma_with_retry`).
   `max_attempts=3`, using `_is_retryable_voyage_error`. Import in `indexer.py` and `doc_indexer.py`.
   Add `_reset_voyage_client()` to `scoring.py` (sets `_voyage_instance = None`) for testability.

4. **Wire `timeout=read_timeout_seconds, max_retries=3` into all four `voyageai.Client` instances:**
   - `T3Database.__init__` (~line 129): `voyageai.Client(api_key=..., timeout=read_timeout_seconds, max_retries=3)`
   - `indexer.py:1153`: `voyageai.Client(api_key=..., timeout=read_timeout_seconds, max_retries=3)`
     Read `read_timeout_seconds` from `load_config()` at the top of `index_repository()`; pass down.
   - `doc_indexer._embed_with_fallback:115`: add `timeout: float = 120.0` parameter; use it in Client construction;
     update both `indexer.py` callers (lines 776 and 871) to pass `read_timeout_seconds`.
   - `scoring.py:_voyage_client():114`: read from `load_config()["voyageai"]["read_timeout_seconds"]`.

5. **Wrap remaining Voyage AI call sites with `_voyage_with_retry`** (for `APIConnectionError`/`TryAgain`):
   - `T3Database._cce_embed()`: wrap `self._voyage_client.contextualized_embed(...)`
   - `indexer.py`: wrap `voyage_client.embed(...)` at the batch embed call site
   - `doc_indexer._embed_with_fallback` CCE path: wrap `client.contextualized_embed(...)`
     *inside* the existing `try` block so errors retry rather than silently fall back to `voyage-4`
   - `doc_indexer._embed_with_fallback` fallback path (line 137): wrap `client.embed(...)` in except block
   - `doc_indexer._embed_with_fallback` standard path (lines 144-148): wrap batch `client.embed(...)` calls
   - `scoring.py:rerank_results()`: wrap `_voyage_client().rerank(...)` — note: outer
     `try/except Exception` in `rerank_results` means timeouts degrade gracefully rather than
     propagating; this is intentional behavior for the interactive search path

6. **TDD integration tests:**
   - `_is_retryable_voyage_error`: True for `APIConnectionError`, `TryAgain`; False for
     `Timeout`, `RateLimitError`, `ServiceUnavailableError`, `AuthenticationError`, `InvalidRequestError`.
   - `_is_retryable_chroma_error` regression: still returns True for `httpx.TransportError`,
     `httpx.HTTPStatusError` 503/429; False for unrelated exceptions (unchanged behavior guard).
   - Mock `voyageai.Client.embed` to raise `voyageai.error.APIConnectionError` → verify
     `_voyage_with_retry` fires and ultimately raises after `max_attempts=3`.
   - Mock `voyageai.Client.contextualized_embed` → same via `_cce_embed`.
   - `_embed_with_fallback` CCE path: `contextualized_embed` raises `APIConnectionError` →
     retries; after exhaustion falls through to voyage-4 fallback (retry-then-degrade, not silent-degrade).
   - `_embed_with_fallback` fallback path (line 137): `embed()` raises `APIConnectionError` →
     `_voyage_with_retry` fires; after exhaustion propagates out of the `except` block.
   - `_embed_with_fallback` standard path (lines 144-148): `embed()` raises `APIConnectionError` →
     `_voyage_with_retry` fires; after exhaustion propagates to caller.
   - Test `voyageai.Client` is constructed with `timeout=read_timeout_seconds` and `max_retries=3`.
   - Test `_reset_voyage_client()` allows re-construction with a new timeout value.

## Acceptance Criteria

- [ ] `nx index repo` never hangs indefinitely on a Voyage AI call.
- [ ] Voyage AI calls raise `voyageai.error.Timeout` within 120s (default); configurable via `voyageai.read_timeout_seconds` in config.
- [ ] All four `voyageai.Client` instances are constructed with `timeout=read_timeout_seconds, max_retries=3` (t3.py, indexer.py, doc_indexer.py, scoring.py).
- [ ] `_is_retryable_voyage_error` returns True for `APIConnectionError`, `TryAgain`; False for `Timeout`, `RateLimitError`, `ServiceUnavailableError`, `AuthenticationError`, `InvalidRequestError`.
- [ ] `_is_retryable_chroma_error` behavior is unchanged (regression guard: still True for `httpx.TransportError`, `httpx.HTTPStatusError` 503/429).
- [ ] `_voyage_with_retry` (`max_attempts=3`) wraps: `_cce_embed()`, `indexer.py` batch embed, `doc_indexer._embed_with_fallback` CCE call (line 127), fallback call (line 137), and standard path batch loop (lines 144-148).
- [ ] `scoring.py:rerank_results()` has `_voyage_with_retry` on `rerank()` call; outer exception handling provides intentional graceful degradation.
- [ ] Timeout in `_embed_with_fallback` CCE path retries (`_voyage_with_retry`) rather than silently falling back to `voyage-4`.
- [ ] `_embed_with_fallback` has `timeout: float = 120.0` parameter; both `indexer.py` callers pass `read_timeout_seconds`.
- [ ] Unit tests cover `_is_retryable_voyage_error` for all error types; `_is_retryable_chroma_error` regression guard.
- [ ] Test asserts `voyageai.Client` is instantiated with the configured `timeout` and `max_retries=3`.
- [ ] `_reset_voyage_client()` exists in `scoring.py` for test isolation.
- [ ] `read_timeout_seconds` in `config.yml` `voyageai:` section (default: 120); env var `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` overrides.
