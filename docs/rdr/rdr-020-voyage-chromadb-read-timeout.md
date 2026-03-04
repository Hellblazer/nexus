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

**Fix options (in preference order):**
1. Subclass `VoyageAIEmbeddingFunction`, call `super().__init__(...)`, then overwrite:
   `self._client = voyageai.Client(api_key=self.api_key, timeout=N)`
2. Post-construct patching: create instance, then `ef._client = voyageai.Client(..., timeout=N)`.
   Works but relies on `_client` remaining public — fragile.

Option 1 (subclass) is clean and upgrade-safe.

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

### Decision

Confirmed approach (Option A + subclass):

1. `voyageai.Client(api_key=key, timeout=read_timeout)` in `T3Database.__init__` — covers CCE path.
2. Subclass `VoyageAIEmbeddingFunction` as `_TimeoutVoyageAIEF` — overwrite `self._client`
   with `timeout=read_timeout` — covers code collection embedding path.
3. `CloudClient(..., settings=Settings(chroma_query_request_timeout_seconds=read_timeout))`
   — future-proofs ChromaDB timeout configuration.
4. `read_timeout` default: **120 seconds** (generous for large batches; 600s default is
   unacceptably long; 60s may be tight for very large files).
5. Resulting `voyageai.error.Timeout` (wraps `requests.Timeout`) is **not** an
   `httpx.TransportError` — must verify what exception class is raised and update
   `_is_retryable_chroma_error` if needed.

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

1. **Update `_is_retryable_chroma_error`** to handle `voyageai.error.Timeout`:
   soft-import `voyageai.error.Timeout` at module level; add `isinstance` check.
   TDD: write failing test for `voyageai.error.Timeout` being retried first.

2. **Add `read_timeout_seconds` to `T3Database.__init__`** (default: 120 seconds).

3. **Wire `timeout` into `voyageai.Client`** in `T3Database.__init__` (CCE path):
   `voyageai.Client(api_key=..., timeout=read_timeout_seconds)`.

4. **Subclass `VoyageAIEmbeddingFunction`** as `_TimeoutVoyageAIEF`:
   call `super().__init__(...)` then overwrite `self._client` with
   `voyageai.Client(api_key=self.api_key, timeout=read_timeout_seconds)`.
   Use in place of `VoyageAIEmbeddingFunction` in `_query_ef()`.

5. **Pass `Settings` to `CloudClient`** with `chroma_query_request_timeout_seconds`
   set to `read_timeout_seconds`.

6. **TDD tests:**
   - `_is_retryable_chroma_error` returns True for `voyageai.error.Timeout`
   - Mock `voyageai.Client.embed` to raise `Timeout` → verify retry fires
   - Mock `VoyageAIEmbeddingFunction.__call__` → same

7. **Add `read_timeout_seconds` to `~/.config/nexus/config.yml` schema** and load
   it in `T3Database.__init__`.

## Acceptance Criteria

- [ ] `nx index repo` never hangs indefinitely on a Voyage AI or ChromaDB call.
- [ ] A stalled API call raises `ReadTimeout` within N seconds (default 60).
- [ ] `_chroma_with_retry` catches the `ReadTimeout` and retries.
- [ ] Unit test covers the retry-on-timeout path.
- [ ] Timeout is configurable in `~/.config/nexus/config.yml`.
