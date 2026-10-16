---
paths:
  - "src/nexus/**"
  - "tests/**/*.py"
---

## External service limits — check before every call

The single source of truth is `src/nexus/db/limits.py` (`QUOTAS: ServiceLimits`). Only `SAFE_CHUNK_BYTES` and `MAX_QUERY_RESULTS` have module-level aliases; the rest are `QUOTAS.<FIELD>`.

| Operation | Limit | Constant |
|---|---|---|
| paging (`limit=N`) | N ≤ 300 | `MAX_QUERY_RESULTS` |
| query (`n_results=N`) | N ≤ 300 | `MAX_QUERY_RESULTS` |
| batch write (`ids=[...]`) | ≤ 300 records | `QUOTAS.MAX_RECORDS_PER_WRITE` |
| Concurrent reads / writes per collection | ≤ 10 each | `QUOTAS.MAX_CONCURRENT_READS/WRITES` |
| Document size | ≤ 16384 bytes | `QUOTAS.MAX_DOCUMENT_BYTES` (use `SAFE_CHUNK_BYTES = 12288`) |
| Query string | ≤ 256 chars | `QUOTAS.MAX_QUERY_STRING_CHARS` |
| `where` predicates | ≤ 8 top-level | `QUOTAS.MAX_WHERE_PREDICATES` |
| Embedding dims | ≤ 4096 | `QUOTAS.MAX_EMBEDDING_DIMENSIONS` |

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 1,000 inputs per request (`VoyageEmbedder.MAX_BATCH_TEXTS`). Use `nexus.retry._voyage_with_retry` for transient failures. Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop. Provenance and the retired-client notes: [external-service-limits](docs/agents/external-service-limits.md).
