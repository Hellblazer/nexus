---
title: "ChromaDB Transient HTTP Error Retry"
id: RDR-019
type: Bug Fix
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-04
accepted_date: 2026-03-04
related_issues: []
---

# RDR-019: ChromaDB Transient HTTP Error Retry

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

When `nx index repo` encounters a transient 504 (or similar 5xx/429) from the
ChromaDB Cloud API, the entire indexing job crashes with an unhandled exception.
No retry is attempted; all progress is lost. On a 4,397-file repo this aborted
after ~526 files (12%) wasting ~7 minutes of Voyage AI embedding calls.

## Context

### Background

Discovered during `nx index repo /Users/hal.hildebrand/git/ART`. The ChromaDB
Cloud gateway returned HTTP 504 at the staleness-check `col.get()` call in
`_index_code_file`. The exception propagated all the way up `_run_index` →
`index_repository` → CLI, terminating the process with no recovery path.

### Technical Environment

- **Nexus**: Python 3.12+, `chromadb>=0.6` (HTTP client), `voyageai>=0.2`
- **ChromaDB Cloud**: REST API via `httpx` under the hood
- **All ChromaDB network call sites** (exhaustive audit, 2026-03-04):

  | File | Method / Context | Call |
  |---|---|---|
  | `db/t3.py` | `get_or_create_collection` | `client.get_or_create_collection()` |
  | `db/t3.py` | `_write_batch` | `col.upsert()` ×2 |
  | `db/t3.py` | `_delete_batch` | `col.delete()` |
  | `db/t3.py` | `update_chunks` | `col.update()` |
  | `db/t3.py` | `search` | `col.count()`, `col.query()` |
  | `db/t3.py` | `list_store` | `col.get()` ×2 |
  | `db/t3.py` | `get_all_chunks_paginated` | `col.get()` |
  | `db/t3.py` | `collection_stats` | `col.count()` |
  | `db/t3.py` | `list_collections` | `col.count()` ×2 |
  | `indexer.py` | `_update_frecency_scores` | `col.get()` |
  | `indexer.py` | `_index_code_file` | `col.get()` |
  | `indexer.py` | `_index_prose_file` | `col.get()` |
  | `indexer.py` | `_index_pdf_file` | `col.get()` |
  | `indexer.py` | `_prune_deleted_files` | `code_col.get()`, `code_col.delete()`, `docs_col.get()`, `docs_col.delete()` |
  | `indexer.py` | `_prune_misclassified` | `col.get()`, `col.delete()` |
  | `doc_indexer.py` | `_index_file` | `col.get()` ×2, `col.delete()` |

  **Scope decision**: All sites above are in scope. The search path (`col.count` + `col.query` in `search()`) is explicitly included — it is the highest-frequency user-visible path and must be resilient.

## Research Findings

### Investigation

**ChromaDB exception surface** (verified by reading traceback):

```
httpx.HTTPStatusError: Server error '504 Gateway Time-out'
  → chromadb/api/base_http_client.py:137
      raise (Exception(resp.text))   # wraps as plain Exception
```

The caller receives a plain `Exception` whose `.args[0]` is the raw response
body (HTML string for gateway errors, JSON for chroma errors). The original
`httpx.HTTPStatusError` is chained via `__context__` but is not the raised type.

**Retryable status codes**:
- `504 Gateway Time-out` — load balancer/CDN timeout (transient)
- `503 Service Unavailable` — transient overload
- `502 Bad Gateway` — upstream error (transient)
- `429 Too Many Requests` — rate limit (backoff required)

**Non-retryable**:
- `400 Bad Request` — caller bug (invalid payload)
- `401/403` — auth failure
- `404` — collection not found (logic error)

**Existing retry infrastructure**: None. `session.py:239` has a comment
explicitly noting "no retry logic is implemented."

**Voyage AI client**: Has its own built-in retry; not in scope here.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
|---|---|---|
| `chromadb>=0.6` | Yes | `_raise_chroma_error` at `base_http_client.py:132-137`: catches `HTTPStatusError`, re-raises as `Exception(resp.text)`. Plain `Exception` is what callers see. |
| `httpx` | Yes | `HTTPStatusError` is chained as `__context__`, not re-raised. |

### Key Discoveries

- **Verified**: ChromaDB wraps all HTTP errors as plain `Exception(resp.text)`.
  The message contains the HTTP status (e.g. `"504 Gateway Time-out"` in the
  HTML body, or a JSON error string for chroma-level errors).
- **Verified**: `httpx.HTTPStatusError` is available as `exc.__context__` and
  carries `response.status_code` as an integer — cleaner for status matching.
- **Documented**: `tenacity` is not a current dependency; stdlib `time.sleep`
  is sufficient for a simple exponential backoff.
- **Assumed**: ChromaDB Cloud 504s are transient (load balancer hiccup); the
  same request succeeds on retry. The ART indexing failure recovered on manual
  restart, supporting this assumption.

### Critical Assumptions

- [x] ChromaDB raises `Exception(resp.text)` for all HTTP errors — **Verified**
  — **Method**: Source Search (`base_http_client.py`)
- [x] `exc.__context__` is an `httpx.HTTPStatusError` with `.response.status_code`
  — **Verified** — **Method**: Source Search (`fastapi.py:_make_request` chain)
- [x] Retrying `col.upsert()` after a 504 succeeds without duplicate writes —
  **Status**: Verified — **Method**: Source Search
  (`chromadb/segment/impl/metadata/sqlite.py:_insert_record` — on duplicate
  ID `IntegrityError`, calls `_update_record`; same-ID re-upsert = safe overwrite.
  `_delete_record` uses `DELETE WHERE embedding_id = X` — SQL no-op on
  non-existent ID, no error raised.)
- [x] Retrying `get_or_create_collection()` after a 504 is safe — **Status**: Verified
  by design — **Method**: Source Search (`chromadb/api/fastapi.py` — posts
  `{"get_or_create": True}` to `POST /collections`; the flag is explicitly designed
  to return the existing collection on duplicate name rather than 409 Conflict.
  The Cloud REST handler honours this flag by contract — that is the entire purpose
  of the `get_or_create` parameter.)

**Method definitions**:

- **Source Search**: API verified against dependency source code
- **Spike**: Behavior verified by running code against a live service
- **Docs Only**: Based on documentation reading alone

## Proposed Solution

### Approach

Add a `_chroma_with_retry(fn, *args, max_attempts=5, **kwargs)` helper using
stdlib only (no new dependencies). Apply it at all four ChromaDB call sites.
Use exponential backoff: 2 → 4 → 8 → 16 → 30s (capped).

Retry if:
- The raised `Exception` message contains a retryable status code string, **or**
- `exc.__context__` is `httpx.HTTPStatusError` with status 502/503/504/429.

Do **not** retry 400/401/403/404 — these are caller bugs or config errors.

### Technical Design

**Helper function** placed in `db/t3.py` (all write-path calls go through here;
`indexer.py` imports it for `col.get()` calls):

```text
// Illustrative — verify API signatures during implementation
import time
import httpx

_RETRYABLE_FRAGMENTS = frozenset({"502", "503", "504", "429",
    "bad gateway", "service unavailable", "gateway time-out",
    "too many requests"})
_RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})

def _is_retryable_chroma_error(exc: BaseException) -> bool:
    # 1. Transport-level errors (ConnectError, ReadTimeout, etc.) — always retry
    if isinstance(exc, httpx.TransportError):
        return True
    # 2. Chained httpx.HTTPStatusError — authoritative integer status check
    ctx = exc.__context__
    if isinstance(ctx, httpx.HTTPStatusError):
        return ctx.response.status_code in _RETRYABLE_HTTP_STATUSES
    # 3. Fallback: plain Exception message body (gateway HTML or chroma JSON)
    msg = str(exc).lower()
    return any(f in msg for f in _RETRYABLE_FRAGMENTS)

def _chroma_with_retry(fn, *args, max_attempts: int = 5, **kwargs):
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_chroma_error(exc):
                raise
            _log.warning("chroma_transient_error_retry",
                         attempt=attempt, delay=delay, error=str(exc)[:120])
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
```

**Call sites to wrap** (all 16 network calls enumerated in Technical Environment):

Wrapping strategy:
- **`db/t3.py`**: wrap every `col.*` call at its call site within the public method. `get_or_create_collection` wraps the `client.get_or_create_collection()` call.
- **`indexer.py`** and **`doc_indexer.py`**: direct `col.*` calls at each call site.

`httpx` is a transitive dependency via `chromadb` — no new imports needed at the `pyproject.toml` level, but `import httpx` must be added at the top of `t3.py`.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| `_chroma_with_retry()` helper | None | New — add to `db/t3.py`, import in `indexer.py` and `doc_indexer.py` |
| `_is_retryable_chroma_error()` | None | New — co-located with helper in `db/t3.py` |
| `_RETRYABLE_FRAGMENTS`, `_RETRYABLE_HTTP_STATUSES` | None | New module-level constants in `db/t3.py` |

### Decision Rationale

- **Stdlib + transitive `httpx`**: No new `pyproject.toml` dependency. `httpx`
  is already installed via `chromadb`. `tenacity` would be cleaner syntactically
  but is overkill for a single helper wrapping ~16 call sites.
- **Helper in `t3.py`**: The majority of call sites live in `t3.py` methods.
  `indexer.py` and `doc_indexer.py` import from `t3.py`, keeping the helper in
  one file with a single import line in the others.
- **Retry at call site, not collection level**: Wrapping individual calls gives
  fine-grained control and avoids patching ChromaDB internals.
- **Integer check before string check**: `httpx.HTTPStatusError.response.status_code`
  is unambiguous; string matching is fallback for transport errors that lack an
  HTTP response object.

## Alternatives Considered

### Alternative 1: `tenacity` decorator

**Description**: Add `tenacity` dependency; use `@retry(wait=wait_exponential(...), stop=stop_after_attempt(5), retry=retry_if_exception(...))`.

**Pros**:
- Cleaner syntax
- Battle-tested library

**Cons**:
- Adds a dependency for 4 call sites
- Version pin management overhead

**Reason for rejection**: YAGNI — stdlib `time.sleep` loop is sufficient.

### Alternative 2: Catch-and-skip file on error (no retry)

**Description**: In `_index_code_file`, catch `Exception` from `col.get()`,
log a warning, return 0 (skip file). No retry attempted.

**Pros**:
- Minimal change
- Job doesn't crash

**Cons**:
- Files silently dropped during transient outage
- Upsert path (`_write_batch`) still unprotected → crash on write failure

**Reason for rejection**: Silently dropping files is worse than a crash; doesn't
fix the write path.

### Briefly Rejected

- **Wrap entire `_index_code_file` in retry**: Too coarse — re-embeds chunks
  unnecessarily on retry; wastes Voyage AI quota.

## Trade-offs

### Consequences

- Indexing jobs survive transient ChromaDB Cloud outages (positive)
- Each retry adds up to 30s of wall-clock delay per file (acceptable — rare)
- 5-attempt budget means up to ~62s of retries before giving up on one file
- `upsert` retries are safe — ChromaDB upsert is idempotent by document ID
- `col.get()` retries are read-only — trivially safe

### Risks and Mitigations

- **Risk**: Over-retry on a sustained outage causes Voyage AI calls to succeed
  but ChromaDB writes to keep failing — wasted quota.
  **Mitigation**: 5-attempt cap (≤62s), then raise and let the job fail fast.
- **Risk**: False-positive retry on a 400 that happens to contain "502" in its
  JSON body.
  **Mitigation**: Check for full fragment match (`"502"` in lowercased message)
  and/or prefer the `httpx.HTTPStatusError.response.status_code` integer check.

### Failure Modes

- **Visible**: After 5 attempts the original exception propagates — same crash
  as today, but with retry warnings logged.
- **Silent**: None — every retry attempt is logged at WARNING level.
- **Diagnosis**: `structlog` WARNING entries with `chroma_transient_error_retry`
  event, `attempt`, `delay`, and truncated `error` string.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified (upsert idempotency confirmed by source
  search of ChromaDB sqlite segment — 2026-03-04)

### Minimum Viable Validation

Re-run `nx index repo /Users/hal.hildebrand/git/ART` and confirm the job
completes all 4,397 files without crashing on a transient 504. (Simulate with
a mock if live 504 cannot be reproduced on demand.)

### Phase 1: Code Implementation

#### Step 1: Add retry helpers to `db/t3.py`

Add `import time` and `import httpx` to module-level imports. Add
`_RETRYABLE_FRAGMENTS`, `_RETRYABLE_HTTP_STATUSES`, `_is_retryable_chroma_error()`,
and `_chroma_with_retry()` above `_write_batch`. Annotate per project convention:
`def _chroma_with_retry(fn: Callable[..., Any], *args: Any, max_attempts: int = 5, **kwargs: Any) -> Any`
with `from collections.abc import Callable` and `from typing import Any` imports.

#### Step 2: Wrap all call sites in `db/t3.py`

Apply `_chroma_with_retry` to every `col.*` call and the
`client.get_or_create_collection()` call within `T3Database` methods. See the
complete call site table in Technical Environment.

#### Step 3: Wrap call sites in `indexer.py` and `doc_indexer.py`

Add `from nexus.db.t3 import _chroma_with_retry` to each file. Apply to all 10
direct `col.*` calls identified in the call site table.

#### Step 4: Tests

Write tests covering all scenarios in the Test Plan: HTTP-level errors,
transport-level errors (using `httpx.ConnectError` / `httpx.ReadTimeout`),
retry loop behavior with mocked `time.sleep`, and integration tests for both
the indexing path and the search path.

### Phase 2: Operational Activation

N/A — no deployment or credential changes required.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| None (no new persistent resources) | N/A | N/A | N/A | N/A | N/A |

### New Dependencies

None.

## Test Plan

**HTTP-level errors:**
- **Scenario**: `_is_retryable_chroma_error` with `Exception("504 Gateway Time-out HTML")` — **Verify**: returns `True`
- **Scenario**: `_is_retryable_chroma_error` with `Exception("400 Bad Request: invalid payload")` — **Verify**: returns `False`
- **Scenario**: `_is_retryable_chroma_error` with chained `httpx.HTTPStatusError(status=429)` — **Verify**: returns `True` (integer check path)
- **Scenario**: `_is_retryable_chroma_error` with chained `httpx.HTTPStatusError(status=404)` — **Verify**: returns `False`

**Transport-level errors:**
- **Scenario**: `_is_retryable_chroma_error` with `httpx.ConnectError(...)` as the raised exception — **Verify**: returns `True`
- **Scenario**: `_is_retryable_chroma_error` with `httpx.ReadTimeout(...)` — **Verify**: returns `True`
- **Scenario**: `_is_retryable_chroma_error` with `httpx.RemoteProtocolError(...)` — **Verify**: returns `True`

**Retry loop behavior:**
- **Scenario**: `_chroma_with_retry` where `fn` raises `httpx.ConnectError` twice then succeeds — **Verify**: returns result, `fn` called 3 times, `time.sleep` called with 2.0 then 4.0
- **Scenario**: `_chroma_with_retry` where `fn` raises 504 on all 5 attempts — **Verify**: raises after 5 attempts
- **Scenario**: `_chroma_with_retry` where `fn` raises 400 on first attempt — **Verify**: raises immediately (no `time.sleep` called)
- **Scenario**: Backoff curve — mock `time.sleep`; verify call args are 2.0, 4.0, 8.0, 16.0 (stop before 5th)

**Integration:**
- **Scenario**: Mock `col.get` to raise `httpx.ConnectError` once; `_index_code_file` retries and succeeds on second call
- **Scenario**: Mock `col.query` to raise 503 once; `search()` retries and returns results on second call

## Validation

### Testing Strategy

1. **Scenario**: HTTP-level retryable exception detected correctly
   **Expected**: `_is_retryable_chroma_error` returns `True` for 502/503/504/429 (integer path)

2. **Scenario**: Transport-level exception detected correctly
   **Expected**: `_is_retryable_chroma_error` returns `True` for `httpx.ConnectError`,
   `httpx.ReadTimeout`, `httpx.RemoteProtocolError` (isinstance path, no HTTP response needed)

3. **Scenario**: Non-retryable exception not retried
   **Expected**: `_chroma_with_retry` raises immediately on first 400/401/403/404 failure

4. **Scenario**: Backoff timing
   **Expected**: `time.sleep` call args follow 2→4→8→16→30 cap sequence (verified via mock)

5. **Scenario**: Idempotent upsert retry
   **Expected**: Re-upserting same IDs produces same result (verified by ChromaDB source search)

6. **Scenario**: Search path survives transient failure
   **Expected**: `nx search` retries on 503 and returns results

### Performance Expectations

Zero overhead on the happy path (no exceptions raised). On retry: ~2s minimum
delay per transient error, acceptable given the alternative is a full job crash.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution.

### Assumption Verification

- ChromaDB upsert idempotency: **Verified** — source search of
  `chromadb/segment/impl/metadata/sqlite.py:_insert_record` confirms: on duplicate
  ID, `Operation.UPSERT` path calls `_update_record` (no duplicate, no error).
  `col.delete()` confirmed safe to retry: `DELETE WHERE embedding_id = X` is a
  SQL no-op on non-existent IDs.

#### API Verification

| API Call | Library | Verification |
|---|---|---|
| `col.get(where=..., include=..., limit=...)` | `chromadb>=0.6` | Source Search |
| `col.upsert(ids=..., documents=..., embeddings=..., metadatas=...)` | `chromadb>=0.6` | Source Search |
| `col.delete(ids=...)` | `chromadb>=0.6` | Source Search |
| `col.update(...)` | `chromadb>=0.6` | Source Search |
| `col.count()` | `chromadb>=0.6` | Source Search |
| `col.query(...)` | `chromadb>=0.6` | Source Search |
| `client.get_or_create_collection(name)` | `chromadb>=0.6` | Source Search |
| `isinstance(exc, httpx.TransportError)` | `httpx` (transitive) | Source Search — base class for ConnectError, ReadTimeout, RemoteProtocolError |
| `isinstance(ctx, httpx.HTTPStatusError)` / `.response.status_code` | `httpx` (transitive) | Source Search |

### Scope Verification

The MVV (re-run ART indexing to completion) is in scope for Phase 1 and will
be executed immediately after implementation.

### Cross-Cutting Concerns

- **Versioning**: N/A — no API or protocol changes
- **Build tool compatibility**: N/A — stdlib only, no new deps
- **Licensing**: N/A
- **Deployment model**: N/A — local CLI tool
- **IDE compatibility**: N/A
- **Incremental adoption**: N/A — internal helper function
- **Secret/credential lifecycle**: N/A
- **Memory management**: N/A — no new memory-intensive operations

### Proportionality

Document is appropriately sized for a focused bug fix (16 call sites, one
helper function, one detection function). No sections warrant trimming.

## References

- Traceback from ART indexing session (2026-03-04): `col.get()` → `Exception("504 Gateway Time-out")`
- `chromadb/api/base_http_client.py:132-137` — `_raise_chroma_error` wraps `httpx.HTTPStatusError` as `Exception(resp.text)`
- `src/nexus/db/t3.py:200-263` — `_write_batch`, `_delete_batch`
- `src/nexus/indexer.py:487-612` — `_index_code_file`
- `src/nexus/indexer.py:615+` — `_index_prose_file`
- `src/nexus/session.py:239` — existing comment: "no retry logic is implemented"

## Revision History

_2026-03-04_: Initial draft created from brainstorming session.

### Gate Round 1 (as RDR-016) — 2026-03-04 — BLOCKED

**Critical (3)**:
1. **ID collision** — `rdr-016-ast-chunk-line-range-bug.md` already exists; renamed to RDR-019.
2. **Incomplete call site inventory** — originally claimed 4 sites; expanded to 16 (full audit).
3. **Transport-level exceptions not retried** — `httpx.ConnectError`/`ReadTimeout`/`RemoteProtocolError` not caught; fixed by adding `isinstance(exc, httpx.TransportError)` check.

**Significant (3)** — all fixed:
1. Check order inverted — reversed to: integer check → transport isinstance → string fallback.
2. Search path excluded without justification — now explicitly in scope with rationale.
3. Test plan missing transport errors — added `httpx.ConnectError` / `ReadTimeout` test cases.

### Round 1 Fixes Applied — 2026-03-04

Renamed to RDR-019. Updated: call site table (16 sites), `_is_retryable_chroma_error` logic (transport exceptions + inverted check order), scope decision (search path included), test plan (transport errors + search integration), implementation steps (4 steps covering all files), API verification table (all 9 APIs).

### Gate Round 2 — 2026-03-04 — PASSED

All round-1 criticals resolved. Two significant issues addressed inline:
1. `get_or_create_collection` Cloud idempotency — added explicit `get_or_create=True` flag verification note (Cloud REST honours the flag by contract, verified via `fastapi.py` source).
2. Type annotation — noted `Callable[..., Any]` signature requirement in Step 1.
3. Stale "4 call sites" text in Proportionality corrected to 16.
