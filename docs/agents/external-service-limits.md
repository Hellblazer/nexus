# External service limits, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); the short rules-only form is [`.claude/rules/external-service-limits.md`](../../.claude/rules/external-service-limits.md), which links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

## External service limits — check before every call

The single source of truth is `src/nexus/db/limits.py` (`QUOTAS: ServiceLimits`). `chroma_quotas.py` and its `QuotaValidator`/`ChromaError` were DELETED at RDR-155 P4b P3 with no replacement — these are generic PG-serving-path ceilings now, Chroma provenance historical only (see the module's own docstring). Only `SAFE_CHUNK_BYTES` and `MAX_QUERY_RESULTS` got module-level aliases; the rest are `QUOTAS.<FIELD>`.

The limits table lives in one place, [`.claude/rules/external-service-limits.md`](../../.claude/rules/external-service-limits.md); this file keeps the provenance and the retired-client notes.

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 1,000 inputs per request (the engine plans batches under a token budget; `VoyageEmbedder.MAX_BATCH_TEXTS`). The old "128 inputs/batch" figure described the retired client-embed path. Use `nexus.retry._voyage_with_retry` for transient failures.
