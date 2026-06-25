---
title: "Docuverse Storage: Reference-Only Chunks — Retention Enum, Nullable Content, Reference-Only Search DTO, and the URI-Resolver / Embed-Without-Store Surface"
id: RDR-169
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date:
related_issues: [nexus-ssm3p, nexus-3kybd, nexus-mt9p8]
related: [RDR-053, RDR-096, RDR-108, RDR-103, RDR-152, RDR-155]
---

# RDR-169: Docuverse Storage — Reference-Only Chunks

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A second class of bridge consumer — Conductus (the Obsidian plugin, peer to conexus over
the RDR-152 bridge) and any future reference-heavy client — wants nexus to be the **index
and graph** over content it does **not** own: the bytes live in the user's vault (or any
addressable source), and nexus should store the chunk's *identity* (address + span + chash
+ metadata + embedding) without duplicating its *content*. Today every nexus chunk requires
its text (`chunks_<dim>.chunk_text NOT NULL`), there is no retention model, the search
return assumes content is present, and there is no on-demand content resolver. This RDR
designs the engine-side "docuverse storage" model — **reference-only chunks** — and the
bridge surface that makes them usable, co-designed with conexus on the one shared-schema
slice.

This is the nexus-engine response to **item 5** of the conexus↔nexus Conductus relay
(2026-06-25; T2 `nexus/nexus-to-conexus-conductus-relay-2026-06-25` r1/r3). The schema slice
lands in the multitenant Postgres conexus operates and migrates, so it is gated by conexus's
RLS / ETL / freeze constraints (recorded below).

### Enumerated gaps to close

#### Gap 1: No retention model — chunk content cannot be absent

`chunks_<dim>.chunk_text` is `NOT NULL`, and nothing distinguishes a fully-stored chunk from
one whose content lives elsewhere. A reference-only consumer cannot register a chunk's
address + embedding + metadata without also handing nexus the bytes. Deliver a `retention`
classifier `{reference-only | full}` plus nullable `chunk_text`, so a chunk row can carry
identity without content. (A third `snapshot` value is deferred — no named consumer yet;
added via a one-line `CHECK` change when an offline-snapshot use case is specified.)

#### Gap 2: The search-return DTO assumes content is present

Search/serving returns `chunk_text` inline. A reference-only hit has no stored text — the
DTO must return the *address* (collection + chash + source_uri + span) and metadata, with a
nullable content field, so existing consumers are unaffected (additive) and reference-aware
consumers can resolve on demand. Must stay RDR-152-bridge-compatible.

#### Gap 3: No URI-scheme resolver registry for on-demand content

Resolving a reference-only chunk's bytes requires a pluggable, read-time resolver keyed on
the `source_uri` scheme (`file://`, `chroma://`, `x-devonthink-item://` exist in the aspect
resolvers today; `obsidian://` and
others are needed). There is no registry to register/dispatch these.

#### Gap 4: No embed-without-store / upsert-precomputed-vector path

A consumer that has already embedded a chunk (its own model, or to avoid shipping content)
wants to register the vector + identity directly. Today the only ingest path re-embeds and
stores content. Deliver an additive bridge route to upsert a precomputed vector for a
reference-only chunk.

#### Gap 5: span / source_uri / chash are not first-class on the bridge

The engine already has chash-addressed spans (RDR-053), `source_uri` identity (RDR-096), and
the catalog/T3 manifest split (RDR-108), but these are not surfaced as first-class fields on
the bridge for editor/plugin clients (e.g. mapping a vault `[[wikilink]]`/heading-ref to a
catalog span). Surface them additively.

#### Gap 6: No staleness / dangling lifecycle for externally-held content

A reference-only chunk's bytes can change or disappear outside nexus. There is no
freshness/dangling check tying the reference to its source (`source_mtime` is tracked;
`allow_dangling` exists for links). Define a read-time/maintenance staleness signal.

## Context

### Background

Discovered via the Conductus handoff relayed through conexus (2026-06-25). Conductus is a
SECOND consumer of the RDR-152 bridge; its "ask-your-vault" / docuverse use case needs the
index without the content. nexus owns the engine design; conexus owns the multitenant
deployment, the copy-not-move T2/T3 ETL, and the RDR lifecycle. nexus's read on the relay
(supportive, architecturally aligned) and conexus's binding constraints are recorded in
`nexus/nexus-to-conexus-conductus-relay-2026-06-25` (r1/r3) and
`conexus/conexus-to-nexus-conductus-relay-2026-06-25-r2`.

### Technical Environment

- T3 chunks live in `nexus.chunks_{384,768,1024}` (PK `(tenant_id, collection, chash)`,
  `chunk_text NOT NULL`, `metadata JSONB`, `chunk_tsv` generated from `chunk_text`,
  `embedding vector(dim) NOT NULL`) under FORCE ROW LEVEL SECURITY (RDR-155).
- The catalog/T3 split (RDR-108): documents addressed by tumbler; chunks content-addressed
  by `chash[:32]`; the `document_chunks` manifest joins them.
- `source_uri` identity (RDR-096) and chash spans (RDR-053) already model "where content
  lives" and "which span of it."
- The bridge is the RDR-152 thin HTTP `/v1` contract behind conexus's edge proxy.

## Research Findings

### Key Discoveries

- **The FTS path already tolerates NULL content** — `chunk_tsv` is a nullable generated
  column; a reference-only row (NULL `chunk_text`) yields a NULL tsvector that the `@@`
  match and the GIN index both skip. The schema slice is therefore just `retention` +
  `chunk_text DROP NOT NULL`; the generated `chunk_tsv` expression is untouched.
- **embed-without-store is already half-built** — `PgVectorRepository.upsertChunksWithVectors`
  (RDR-166 same-model passthrough) stores caller-supplied vectors verbatim with a fail-loud
  dim check. Reference-only ingest reuses this; the only addition is a nullable-content upsert
  branch under `retention='reference-only'`.
- Both schema-track Critical Assumptions clear at the source/schema level; the remaining
  verification (CA-1 reconcile) is the cross-repo co-design step with conexus, not engine work.

### Critical Assumptions

- [x] **The retention slice is purely additive and reconcile-safe** — **Status**: Verified
  (co-design relay 2026-06-25, `conexus/conexus-to-nexus-rdr169-docuverse-codesign-2026-06-25`
  A2). The conexus copy-not-move T3 reconcile (`etl_t3.py:489+`) selects ONLY
  `(collection, chash)` and checksums ONLY `chash` (`sha256(sorted(chashes))`) — it never
  reads `chunk_text` or `retention`, so the additive column + nullable content are invisible
  to reconcile by construction (additive-column class, not the conexus-lzm FK/dedup class).
  conexus will run a seeded-sample dry-run during the paired-PR review; the SELECT makes the
  outcome deterministic regardless.
- [x] **`chunk_tsv` (FTS) degrades gracefully when `chunk_text` is NULL** — **Status**:
  Verified — **Method**: schema spike (research-1). `chunk_tsv` carries no `NOT NULL`, so
  `to_tsvector('english', NULL)` → NULL tsvector → excluded from FTS (`@@` on NULL is false)
  and skipped by the GIN index. The generated-column expression needs **no** change; only
  `chunk_text DROP NOT NULL`. The earlier `COALESCE(chunk_text,'')` idea is unnecessary
  (empty vs NULL tsvector both exclude the row); keep the expression unchanged.
- [x] **A reference-only chunk is still embeddable** — **Status**: Verified-feasible —
  **Method**: source spike (research-1). The precomputed-vector primitive already exists:
  `PgVectorRepository.upsertChunksWithVectors` (RDR-166 passthrough — stores caller-supplied
  embeddings verbatim, dim-validated, no embedder). The embed path is text→vector, decoupled
  from storage. Remaining work is only a nullable-content upsert branch under
  `retention='reference-only'` + the schema slice — not a blocker.
- [ ] **RLS is unconditional** — `tenant_id` stays `NOT NULL` on reference-only rows; content
  nullability is orthogonal to tenancy — **Status**: Confirmed mutual (relay) — **Method**:
  schema invariant + RLS behavioral test.

## Proposed Solution

### Approach

Two tracks, separated by schema impact (per conexus's gating):

1. **Schema-touching (co-designed with conexus, build gated on the freeze):** the retention
   enum + nullable `chunk_text` (Gaps 1, partial 6) and the reference-only search-return DTO
   (Gap 2). Additive, nullable, RDR-152-compatible.
2. **Non-schema (proceeds independently):** the URI-scheme resolver registry (Gap 3),
   embed-without-store / upsert-precomputed-vector bridge route (Gap 4), span/source_uri/chash
   first-class surfacing (Gap 5), and the read-time staleness signal (Gap 6) keyed on the
   already-tracked `source_mtime` + dangling handling.

### Technical Design

- **Retention column (per chunks_<dim> table):** `retention TEXT NOT NULL DEFAULT 'full'
  CHECK (retention IN ('reference-only','full'))`; `ALTER chunk_text DROP NOT NULL`. The
  `chunk_tsv` generated expression is **unchanged** — `to_tsvector('english', chunk_text)`
  evaluates to NULL when `chunk_text` is NULL, which excludes the row from FTS and the GIN
  index by construction (verified CA-2; no generated-column rewrite). `embedding` stays
  `NOT NULL` (a reference-only chunk is still a vector); `tenant_id NOT NULL` unchanged.
  (`'snapshot'` was dropped as speculative — no named consumer; a future value is a
  one-line `DROP/ADD CONSTRAINT` when an offline-snapshot use case is actually specified.
  Tracked as a follow-on if/when named.)
- **Re-index / retention-transition semantics:** a `full → reference-only` transition is
  **prohibited** via the engine ingest path — the existing `ON CONFLICT (tenant_id,
  collection, chash) DO UPDATE SET chunk_text = EXCLUDED.chunk_text` would silently NULL
  stored content if a previously-full chunk were re-submitted reference-only. The client
  must explicitly delete + re-insert to change retention; the reference-only ingest branch
  (below) rejects a write whose `chash` already holds full content (fail-loud, not silent
  overwrite). `reference-only → reference-only` re-writes (metadata/embedding refresh) are
  fine.
- **Reference-only search DTO:** the `/v1` search/serving return gains a nullable
  `chunk_text` and a `retention` field plus the always-present address (`collection`,
  `chash`, `source_uri`, span). Existing consumers ignore the new nullable fields
  (RDR-152-additive).
- **URI-scheme resolver registry:** an engine-side registry mapping a `source_uri` scheme to
  a read-time resolver; `obsidian://` (vault-relative) joins `file://` / `chroma://` /
  `x-devonthink-item://`. Resolution is read-time and has no shared-schema impact.
  **Multi-tenant scope:** the registry is a **global `scheme → handler`** map; the handler
  receives tenant context and dispatches per-tenant (e.g. `obsidian://` resolves against the
  requesting tenant's vault). This avoids per-tenant registration storage (which would be a
  schema touch); cross-tenant resolution is impossible because the handler only ever sees the
  request's tenant.
- **embed-without-store:** an additive bridge route to upsert `(collection, chash,
  source_uri, span, embedding, metadata, retention='reference-only')` with `chunk_text=NULL`.
  Note: the existing `PgVectorRepository.upsertChunksWithVectors` passthrough takes a
  non-null `List<String> documents` and `stripNul`s each element (NPE on null), so this is
  **not** a drop-in — implement a dedicated `upsertReferenceOnlyChunk(...)` (or a null-content
  guard before the dedup loop) that binds `chunk_text=NULL` + `retention='reference-only'`
  and rejects overwriting an existing full-content chash (see re-index semantics above).
- **Staleness:** a read-time signal comparing the reference's recorded `source_mtime` to the
  resolver's current view; dangling references handled analogously to `allow_dangling` links.

### Binding constraints (conexus — verified at co-design 2026-06-25, relay r2 + codesign A1–A4)

1. **Cross-repo lockstep — the load-bearing paired edit is NOT the column line.** The cutover
   copy needs no ETL edit (the column is omitted from `etl_t3.py:_insert_batch`'s explicit
   INSERT list, so `NOT NULL DEFAULT 'full'` fills it — correct for all full-content cutover
   rows). The real paired edit, shipped with the nexus changeset, is: **(a)** add `retention`
   to the `_insert_batch` column list + row tuple, and **(b)** relax the
   `etl_t3.py:226-227` null/empty-doc **skip** to a reference-only passthrough (emit
   `chash` + `chunk_text=None` + `retention='reference-only'`) — otherwise reference-only
   chunks silently never copy. (b) is the non-obvious load-bearing change.
2. **Sequencing — STANDALONE POST-CUTOVER, gated on `aqb-done`** (conexus A3 preference). The
   slice is independent (zero backfill, default-covered, reconcile-invisible) and adds nothing
   by folding into the highest-risk cutover. Order: cutover copies the pre-retention schema →
   conexus signals `aqb-done` (local-data-frozen + cutover verified; tracked conexus-od6a,
   blocked on conexus-xr7.8) → nexus lands the additive `ALTER` on the live cloud (instant)
   **paired** with the conexus `_insert_batch` edit. Post-cutover the paired edit serves future
   reference-only writes + rollback-rerun, not the cutover copy.
3. **RLS unconditional** (`tenant_id NOT NULL` on reference-only rows).
4. **DTO strictly additive + nullable, no framing change** (conexus A4). The edge proxy
   streams the response body verbatim (never parses JSON), so additive nullable fields pass
   through untouched — but the DTO addition MUST NOT change `Content-Type` or response framing;
   add `chunk_text` (nullable) + `retention` to the existing response shape only.

## Alternatives Considered

### Alternative 1: Always store content (status quo)

Keep `chunk_text NOT NULL`; reference consumers ship their bytes. **Rejected**: defeats the
docuverse use case (duplicates vault content into the multitenant store, with privacy +
size cost) and cannot represent "indexed but not stored."

### Alternative 2: Sentinel empty content (`chunk_text=''`) instead of NULL + retention

Store `''` for reference-only. **Rejected**: loses the explicit retention distinction,
pollutes FTS with empty docs, and gives no honest signal that content lives elsewhere — a
silent-degradation trap.
