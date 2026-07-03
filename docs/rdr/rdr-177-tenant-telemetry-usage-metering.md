---
title: "Unified Tenant-Scoped Telemetry, Observability, and Usage Metering for the Engine Service: One Stats Layer for Local Users and Cloud Tenants"
id: RDR-177
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-07-01
related_issues: [nexus-1usso]
related: [RDR-152, RDR-155, RDR-156, RDR-166, RDR-167, RDR-176]
---

# RDR-177: Unified Tenant-Scoped Telemetry, Observability, and Usage Metering

## Problem Statement

The engine service has no coherent answer to "what is in here, how big is it, and how is it behaving?" — for either of its two audiences:

- **Local mode**: the single user IS the tenant. They have no `nx` surface that says "your corpus is N documents / M vectors / K MB and grew X% this month."
- **Cloud mode**: a tenant on the managed service has exactly the same question, plus the operator (conexus) needs the aggregate view — and eventually, metering for billing.

The 2026-07-01 production migration was a field study in this absence. Every diagnostic that mattered had to be reconstructed by hand:

- **Request latency was invisible.** The migration throughput ceiling (~34.4 KB/s per connection) was diagnosed by counting bytes-per-second in `nettop` from the client side. The actual cause — `ChashHandler.handleImport` issuing 600+ sequential PG round-trips per batch request, ≈0.9 s server-side (nexus-1usso) — would have been obvious from day one with a per-endpoint latency histogram: `POST /v1/chash/import p50=900ms` is a question that asks itself.
- **The tenant inventory did not exist.** Building the source/target comparison ("the spectrum") took a dozen hand-rolled `psql` sessions (with RLS-GUC footguns producing false "0 rows" twice) plus per-store curl probes. The single most-used tool of the night was `GET /v1/catalog/stats` — the one store that HAS a stats endpoint. It was polled dozens of times because it was the only window.
- **Migration verification was `indeterminate`** (Jun-30 report) because the ETL had nothing authoritative to verify against. RDR-176 Gap 1a added `relation_counts` — for catalog only. Every other store still verifies against nothing.
- **The 502 window was invisible server-side.** No error-rate surface existed; the operator had to grep nginx logs to see the burst.
- **Nobody can answer what a tenant's data costs.** Payload sizes were approximated from `avg(length(...))` in the SQLite source. Per-tenant bytes-on-disk, row counts, and growth are unqueryable.

The existing partial surfaces prove the demand and the shape: `catalog/stats`, `relation_counts` (RDR-176), `queue/pending_count`, `topics/count_assignments`, `schema_changeset_count` + `service_release_version` in daemon status (which caught the stale-dev-JAR incident), and the client-side `search_telemetry` T2 store. Each was built one incident too late. This RDR consolidates them into one designed layer instead of growing a sixth fragment at the next incident.

## Context

### Background

- RDR-152/155/156 put all persistent state behind the Java engine service on Postgres with RLS tenant isolation. Every table carries `tenant_id`; local mode uses tenant `default`. This is the enabling fact: **the local user and the cloud tenant are the same code path**, so one tenant-scoped stats surface serves both audiences with zero mode-branching.
- RDR-166 defined the managed-service consumer journeys; metering/usage was explicitly out of scope there and has no home yet.
- RDR-167 (draft) covers the *correlation* half of observability (X-Request-ID, MDC, structured logs, readiness). This RDR is the *aggregation* half (metrics, inventory, usage). They compose; neither subsumes the other.
- The engine is a GraalVM native image running the JDK built-in `HttpServer` with a filter chain (`AuthFilter` precedent) — a metrics filter is one more link, no framework needed.

### Technical Environment

- Java 25 engine (`service/`), jOOQ over HikariCP → PG17 + pgvector, Liquibase-managed schema, RLS on 24 tenant tables.
- Handlers: one per domain (Memory/Plans/Taxonomy/Aspect/Chash/Catalog/Vector...), all behind `AuthFilter` + `RequestContext` (tenant already resolved per-request — the metrics dimension is free).
- Native-image constraint: metrics libraries must be reflection-light. Micrometer works under native-image with configuration; a hand-rolled `LongAdder`/HDR-histogram registry is the zero-dependency fallback. Decide in research.

## Research Findings

### Key Discoveries (from the 2026-07-01 incident, evidence-grade)

1. **Per-endpoint latency would have caught nexus-1usso immediately.** Measured: 1 request/s per connection, ≈0.9 s server-side per 200-row import batch. No server-side surface recorded this; diagnosis required client-side packet accounting.
2. **`GET /v1/catalog/stats` is the proven prototype.** Cheap (single aggregate query), tenant-scoped, JSON, consumed by humans, scripts, and monitors alike during the incident. The design below is "that, for every store, plus bytes."
3. **Per-tenant bytes in shared RLS tables is a scan, not a lookup.** `pg_total_relation_size` gives per-TABLE totals (operator view, cheap). Per-TENANT slices need `count(*)` + `sum(pg_column_size(t.*))` filtered by tenant — too expensive on-demand for 448k-row tables; must be snapshot-based.
4. **Snapshots are also the metering foundation.** A nightly `tenant_usage_snapshots` row per (tenant, store) gives growth-over-time for the user AND the billing-grade usage record the managed service will need. Observability and metering are the same table; designing them together avoids building it twice.
5. **The ETL verify gap (RDR-153's `verification: indeterminate`) closes for free** once every store has an authoritative count endpoint: verify = compare source counts to `GET /v1/stats/tenant` per-store counts.

### Critical Assumptions

- A-1: One aggregate-count query per store per stats call is cheap enough to serve on-demand (catalog/stats already proves this at 17k docs / 130k manifest rows). Bytes estimates are NOT computed on-demand (see Discovery 3).
- A-2: Native-image compatibility of the chosen metrics approach must be proven in P1 research before committing to Micrometer vs hand-rolled. (Verify: build + smoke a native image with the registry in place.)
- A-3: The Prometheus `/metrics` endpoint is operator-facing and NOT tenant-authenticated in local mode; in cloud mode it must be reachable only from the operator network (nginx allowlist), never through the tenant edge. Security review required.
- A-4: Snapshot cadence (nightly) is sufficient for growth/metering v1; no streaming usage accounting.

## Proposed Approach (pillars — refine in planning)

### P1 — Tenant inventory endpoint + `nx stats`

`GET /v1/stats/tenant` (auth: any tenant token; returns THAT tenant's view):

```json
{
  "stores": {
    "memory":   {"rows": 3557,  "last_write": "..."},
    "plans":    {"rows": 116,   "last_write": "..."},
    "taxonomy": {"topics": 447, "assignments": 190115, "links": ...},
    "aspects":  {"rows": 802, "queue": {"pending": 4, "in_progress": 0, "failed": 2}},
    "chash":    {"rows": 448434, "distinct_chashes": 371112, "collections": 199},
    "catalog":  {"docs": 17829, "links": 1779, "owners": 46, "collections": 76, "manifest_rows": 138327},
    "vectors":  {"chunks_by_dim": {"1024": 139715, "768": 0}, "collections": 64, "by_model": {...}}
  },
  "usage": {"estimated_bytes": ..., "as_of": "<latest snapshot>"},
  "service": {"release_version": "0.1.18", "schema_changesets": 142}
}
```

- `nx stats` renders it (human view); `nx doctor` and the migration verify step consume the same JSON (machine view). One surface, three consumers.
- Implementation: one aggregate query per store behind the existing repositories; RLS makes tenant scoping automatic.

### P2 — Handler-layer request metrics + operator view

- A metrics `Filter` in the JDK httpserver chain (after AuthFilter, so tenant is a dimension): per (endpoint-group, method) → request count, error count by status class, latency histogram, bytes in/out. Registry: Micrometer if native-image-clean, else hand-rolled LongAdder + fixed-bucket histograms.
- `GET /metrics` — Prometheus exposition, operator-network-only in cloud (A-3).
- `GET /v1/stats/service` — operator JSON: PG size totals (`pg_total_relation_size` per table), Hikari pool gauges, uptime, version/changesets, rolled-up request metrics. Local mode: the user is the operator.

### P3 — Usage snapshots (growth + metering foundation)

- `tenant_usage_snapshots(tenant_id, store, rows, bytes_estimate, taken_at)` — nightly job in the engine (same scheduler as the t1-ttl-sweep precedent), per-tenant `count(*)` + sampled `pg_column_size` sums.
- `GET /v1/stats/tenant?history=30d` returns the series; `nx stats --history` renders growth.
- Explicitly documented as ESTIMATES (shared-table overhead, indexes, TOAST amortized per-table not per-tenant).

### P4 — Retrofit the consumers

- Migration ETL verify: per-store source-count vs `stats/tenant` count — kills `verification: indeterminate` for all eight stores (completes RDR-176 Gap 1a).
- `nx doctor`: replace ad-hoc client-side psql checks (RLS footgun class) with stats-endpoint reads where applicable.
- Retire/alias the fragment endpoints (`catalog/stats` stays for compat; new consumers use the unified surface).

## Alternatives Considered

1. **Prometheus-only (no JSON stats endpoints).** Rejected: the tenant view must be consumable by `nx` and by tenants without a metrics stack; Prometheus text format is the wrong interface for `nx stats` and for ETL verification.
2. **Client-side aggregation (nx queries each store endpoint).** Rejected: that is the status quo that failed — N round-trips, no bytes, no history, RLS footguns when it falls back to psql.
3. **Full APM (OpenTelemetry traces).** Deferred: RDR-167 owns correlation; distributed tracing is overkill for a single-service topology today. The metrics filter emits what OTel would want if adopted later.
4. **Exact per-tenant storage accounting (row-level byte ledger).** Rejected for v1: write-path overhead on every insert for a number that only needs nightly resolution.

## Consequences

**Positive:** one designed surface instead of incident-driven fragments; the nexus-1usso class becomes visible in minutes not hours; migration verification becomes authoritative for all stores; the managed service gets its metering substrate before billing needs it; local users finally get `nx stats`.

**Negative / risks:** the metrics filter is on the hot path (must be allocation-light); native-image compatibility work for the registry; snapshot job adds a nightly scan (bounded, off-peak); `/metrics` exposure is a new security surface requiring the A-3 network discipline.

## Open Questions

- OQ-1: Micrometer vs hand-rolled registry under native-image (P1 research spike decides).
- OQ-2: Should vector search latency/recall metrics (RDR-156 territory) ride this layer or stay in `search_telemetry`? Lean: request metrics here, relevance telemetry stays client-side.
- OQ-3: Per-collection (not just per-store) tenant inventory — needed for the vector view; how deep before the stats query stops being cheap?
- OQ-4: Does conexus want a push (remote-write) path for cloud metrics, or is scrape sufficient? (Their infra call — relay at planning.)
