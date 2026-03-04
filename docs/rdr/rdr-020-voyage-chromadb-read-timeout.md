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

## Investigation

### What we know

- `voyageai.Client` wraps `httpx` internally. `httpx` supports `timeout=` on the
  client constructor.
- `chromadb.CloudClient` exposes no timeout parameter in its public API as of 1.5.x.
- `VoyageAIEmbeddingFunction` (chromadb's bundled wrapper) also exposes no timeout.
- The `_chroma_with_retry` helper from RDR-019 catches `httpx.TransportError`
  (which includes `ReadTimeout`) — but only if the timeout is actually set.
  Without a timeout, `ReadTimeout` is never raised.

### Options

**Option A: Set timeout on `voyageai.Client` directly**
```python
voyageai.Client(api_key=key, timeout=60)  # or request_timeout=60 — check SDK
```
Covers CCE calls (`_cce_embed`). Does not cover the `VoyageAIEmbeddingFunction`
path used for code collections.

**Option B: Wrap embedding calls with `signal.alarm` / `threading.Timer`**
Platform-specific (`signal.alarm` Unix-only), fragile in threads. Reject.

**Option C: Replace `VoyageAIEmbeddingFunction` with direct `voyageai.Client` calls**
Gives full control over timeout and batching. More code to own. Deferred to
separate RDR if needed.

**Option D: Set timeout via httpx transport on voyageai.Client + monkey-patch VoyageAIEmbeddingFunction**
Fragile against SDK upgrades. Reject.

**Option E: Set `timeout` on `voyageai.Client` + subclass `VoyageAIEmbeddingFunction` to inject timeout**
Covers both call paths. Surgical. No monkey-patching.

### Recommended approach

Option A + targeted subclass for the embedding function path (Option E subset):

1. Pass `timeout=60` (or configurable via `~/.config/nexus/config.yml`) to
   `voyageai.Client(...)` in `T3Database.__init__`.
2. Subclass or wrap `VoyageAIEmbeddingFunction` to set a timeout on its internal
   client (inspect SDK source to find the right knob).
3. The resulting `ReadTimeout` (an `httpx.TransportError`) will be caught by
   `_chroma_with_retry` and retried up to 5× before failing cleanly.
4. For ChromaDB client: investigate whether `chromadb.CloudClient` supports a
   `timeout` kwarg or underlying gRPC deadline. If not, file upstream issue.

## Implementation Plan

1. Audit `voyageai` SDK source: find the `httpx.Client` construction point and
   confirm `timeout=` kwarg is threaded through.
2. Audit `VoyageAIEmbeddingFunction` source: find where it constructs its Voyage
   client and add timeout injection.
3. Add `read_timeout_seconds` to `T3Database.__init__` (default: 60).
4. Wire timeout into `voyageai.Client` and the embedding function wrapper.
5. Verify `ReadTimeout` propagates as `httpx.TransportError` and is caught by
   `_chroma_with_retry`.
6. TDD: unit test that a mock `ReadTimeout` on an embedding call triggers retry.
7. Investigate ChromaDB client timeout; add if available, document if not.
8. Add `read_timeout_seconds` to `~/.config/nexus/config.yml` schema.

## Acceptance Criteria

- [ ] `nx index repo` never hangs indefinitely on a Voyage AI or ChromaDB call.
- [ ] A stalled API call raises `ReadTimeout` within N seconds (default 60).
- [ ] `_chroma_with_retry` catches the `ReadTimeout` and retries.
- [ ] Unit test covers the retry-on-timeout path.
- [ ] Timeout is configurable in `~/.config/nexus/config.yml`.
