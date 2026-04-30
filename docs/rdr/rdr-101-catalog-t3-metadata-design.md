---
title: "RDR-101: Catalog/T3 Metadata Field Ownership and Drift Elimination"
id: RDR-101
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-30
related_issues: [nexus-3e4s, nexus-p03z, nexus-v9az, ART-lhk1]
related_tests: []
related: [RDR-004, RDR-060, RDR-086, RDR-087, RDR-096]
---

# RDR-101: Catalog/T3 Metadata Field Ownership and Drift Elimination

The catalog (T2: SQLite + JSONL, document-keyed) and T3 (ChromaDB, chunk-keyed) hold overlapping metadata about the same source files. When the two stores disagree on a join field, most acutely `source_path` / `source_uri`, every retrieval path that joins them silently returns "empty." nexus-3e4s, ART-lhk1, nexus-v9az, and the misclassified rows in nexus-p03z were all manifestations of the same drift class. This RDR proposes a field-ownership rule, a closed list of duplicated fields, and a phased migration to make drift structurally impossible rather than continually patched.

This RDR is bounded to metadata stored at index time about source files. It does not propose changes to: chunk-text storage, embedding model selection, the link graph (catalog-internal), the tumbler addressing scheme, or any T1/T2 store outside the document-aspect / catalog tables.

## Problem Statement

### What drift looks like in production

The catalog row for one source document and the T3 chunks for the same document each hold their own copy of `source_path`, `content_hash`, `chunk_count`, `title`, and `indexed_at`. The fields are populated at index time by two different write paths and updated by two different lifecycle paths thereafter. There is no constraint that they remain equal, and no tool that re-asserts equality on demand.

The 2026-04-29 incident confirmed this in the live host catalog:

- `code__ART-8c2e74c0`: 4,182 catalog rows reported `chunk_count = 0`; T3 held 63,077 chunks for the same 4,395 source files. The catalog hook never wrote chunk_count back after the indexer's chunk-count call. T3 had truth; catalog disagreed silently.
- `docs__ART-8c2e74c0`: 271 catalog rows had `source_uri` rooted on `/Users/.../nexus/`; the same chunks in T3 carried correct `source_path` rooted on `/Users/.../ART/`. The catalog was wrong; T3 was right. The drift was invisible until aspect extraction joined the two and reported `140/140 skipped (empty)`.
- After cleanup via the recovered `--from-t3` path (PR #388), the docs__ART catalog rows had `file_path` relative to the repo root, while T3 chunks still keyed `source_path` as absolute. The chroma reader joined on string equality → 365/365 empty (nexus-v9az, fixed in PR #389 with a `lookup_path` shim).

Each of the three episodes patched its own join site. None addressed the underlying invariant: the duplicated fields can drift, and the system has no guardrail that flags the drift before user-facing operations join across the boundary.

### Field-by-field overlap

| Conceptual field | Stored in catalog as | Stored in T3 chunk metadata as | Identical when fresh? | Drift mechanism |
|---|---|---|---|---|
| Source identity | `documents.source_uri`, `documents.file_path` | `source_path`, `corpus` | Catalog usually relative, T3 absolute. Catalog `source_uri` derived at register; T3 `source_path` set by indexer. | The two writers compute the value independently. nexus-3e4s let CWD-anchoring drift them apart. |
| Content hash | `documents.head_hash` | `content_hash` | Yes at index time | `nx catalog update --head-hash` mutates catalog only; chunk-side stays. |
| Chunk count | `documents.chunk_count` | implicit (`COUNT(*) WHERE source_path=...`) | T3 is ground truth; catalog is a denormalized cache | Catalog hook can fail or be skipped. 4,182 ART rows had count=0. |
| Title | `documents.title` (per-doc) | `title` (per-chunk, e.g. `CLAUDE.md:chunk-0`) | Different shapes by design | Document title vs chunk title are different concepts; the field name collision is the bug. |
| Index timestamp | `documents.indexed_at` | `indexed_at` | Yes at index time | Re-indexing one chunk leaves the catalog row stale. |
| Git provenance | not stored | `git_project_name`, `git_branch`, `git_commit_hash`, `git_remote_url`, `git_meta` (RDR-087-era flat-keys + JSON blob) | T3 owns | Operational queries by branch/commit have to scan T3. Catalog cannot answer "who indexed this and when on what branch." |
| Tumbler / alias / link graph | catalog only | n/a | Catalog owns | Not a drift surface. |
| Embedding model | not stored on catalog | `embedding_model` | T3 owns | Re-embedding requires a new T3 collection but the catalog row's identity does not change. |
| Source mtime | `documents.source_mtime` (RDR-087) | not stored | Catalog owns | Stale-source detection looks at the catalog only; operationally fine. |

### Why "single source of truth" is the wrong frame

A naïve fix would be "kill the duplicates, pick one store." That breaks both stores. The catalog has data T3 cannot represent (tumblers, aliases, link types, soft-delete tombstones); T3 has data the catalog never sees (embeddings, per-chunk position, frecency). Each store is the unique authority for some subset of fields.

The drift only occurs on the **overlap**: fields where both stores hold an independent copy of the same conceptual value. The right fix is to assign each overlapping field to exactly one store as the authority and have the other store either drop the field or treat it as a read-through cache that is rebuilt, never independently mutated.

### Why this keeps re-occurring

Three independent failure modes have produced overlap drift in the past 60 days:

1. **Different write paths populate independently.** `nx index repo` writes T3 chunks first (correct `source_path`), then the catalog hook writes a catalog row (recomputes `source_uri` via a separate code path that had a CWD-anchoring bug). The two writes are not derived from the same value.
2. **Update operations are asymmetric.** `nx catalog update --source-uri` mutates the catalog only. There is no `nx index update --source-path` (and there should not be: T3 chunk identity is content-hashed). Asymmetric update is fine if the catalog is a *cache* of T3; it is broken if the catalog is *authoritative* for a field T3 also holds.
3. **Recovery operations cannot trust the catalog.** PR #388's `nx catalog backfill --from-t3` exists because the catalog cannot be reconstructed without reading T3. That is a strong signal that T3 is the truth-bearer for the fields backfill rebuilds: `source_path`, `content_hash`, chunk-count.

## Research Findings

### Field-ownership pattern in similar systems

Two-store designs with stable join keys (one for identity, one for vectors) are common in the LLM/RAG ecosystem. The pattern that survives is:

- The vector store owns physical chunk data (text, embedding, position) and the join keys it computed at index time.
- The metadata / identity store owns logical document identity, lifecycle, and relationships.
- Fields that exist in both are read by the metadata store from the vector store on demand, or they are eagerly mirrored at write time but never independently mutated.

This is what `chunk_text_hash` already does in nexus (RDR-086): T3 owns the per-chunk hash; the catalog stores it nowhere; spans use `chash:<hex>` to address chunks. RDR-101 generalizes that pattern to the rest of the overlap.

### What T3 demonstrably owns correctly

The 2026-04-29 cleanup proved that for the live host catalog:

- T3 `source_path` was correct for **all** 365 unique docs__ART files and **all** 4,395 unique code__ART files. The chunks were always anchored on the indexed repo at index time.
- T3 `content_hash` round-tripped correctly via `chunk_text_hash` (RDR-086 backfill).
- T3 implicit `chunk_count` (per-source `COUNT(*)`) was the only place where the catalog could be reconstructed; the recovery scripts grouped by `source_path` and used `chunk_count` from chunk metadata as the source of truth.

In every drift episode, T3 was right and the catalog was wrong. This is a strong empirical signal for which store should own the field.

### What the catalog demonstrably owns alone

These have no T3 counterpart and no drift surface:

- `tumbler` (the catalog's per-doc identity)
- `alias_of` (canonical-doc redirection; nexus-s8yz)
- `physical_collection` (which T3 collection holds this doc's chunks). T3 chunks know their own collection but the catalog row's "this doc lives in collection X" is a higher-level fact than per-chunk membership.
- The link graph (`links.jsonl`, `documents.alias_of`)
- Soft-delete tombstones (`_deleted: true` JSONL records; RDR-085 era)
- `source_mtime` (RDR-087 stale-source detection)
- `source_uri` for non-`file://` schemes: `chroma://` (catalog-internal), `https://`, `x-devonthink-item://` (RDR-099). T3 chunk metadata cannot represent these schemes meaningfully because the chunks live under a single `corpus` value.

### What the field-ownership approach unlocks

If T3 owns `source_path` and the catalog reads it on demand:

- The cross-project register-time guard (nexus-3e4s) becomes a tautology; nothing can disagree because the catalog never independently stores the value.
- `nx catalog audit-membership` becomes a structural conformance check (does each catalog row's chunks all live in the expected collection?), not a heuristic.
- Recovery becomes the canonical write path, not a separate `--from-t3` flag.
- The chroma reader joins on a single canonical key (`chunk_text_hash` already; `source_path` newly).

### Risks discovered during research

1. **Read amplification.** Every catalog `resolve` becomes a T3 read for fields that today are local SQLite lookups. ChromaDB Cloud `get(where={chunk_text_hash: ...})` is indexed but not free.
2. **T3 unavailability.** A network blip in the catalog read path is a denial-of-service for any catalog operation that resolves the now-T3-owned fields.
3. **Migration blast radius.** Every existing catalog row needs the duplicated columns retired or re-marked as cache-only. The JSONL append-only log makes this non-destructive but voluminous (the host catalog has ~7,500 rows).
4. **Compatibility for offline / catalog-only consumers.** `nx catalog show <tumbler>` works without T3 today (modulo span resolution). Some operations should keep their offline character.

## Proposed Solution

A four-phase migration. Each phase ships independently and is reversible until phase 4.

### Phase 1: Field-ownership decision matrix, no code changes

Land this RDR with a binding ownership table. The table is the spec; subsequent phases implement to it. Per-field decisions:

| Field | Owner | Catalog disposition | Migration |
|---|---|---|---|
| `source_path` (file URIs) | T3 | Drop from catalog, query on demand by `chunk_text_hash` lookup → infer from chunk's `source_path` | Phase 3 |
| `source_uri` (non-file schemes: chroma, https, devonthink) | Catalog | Keep | None |
| `content_hash` / `head_hash` | T3 | Drop from catalog; query on demand | Phase 3 |
| `chunk_count` | T3 (implicit) | Drop from catalog; compute on demand via `COUNT(*) WHERE source_path=...` or cache with TTL | Phase 3 |
| `title` (per-document) | Catalog | Keep; T3 chunk `title` field renamed to `chunk_title` to remove the name collision | Phase 2 |
| `indexed_at` (per-document event) | Catalog | Keep; T3 chunk `indexed_at` is per-chunk-event and that's fine | None (rename clarifies the distinction) |
| `physical_collection` | Catalog | Keep | None |
| `embedding_model` | T3 | Stays in T3 only | None |
| Git provenance | T3 | Stays in T3 only; catalog can read on demand | None |
| `tumbler`, `alias_of`, links, tombstones | Catalog | Keep | None |
| `source_mtime` | Catalog | Keep | None |

### Phase 2: Decouple the per-chunk vs per-document title collision

T3 chunk metadata's `title` field today holds values like `CLAUDE.md:chunk-0`. The catalog's `documents.title` holds `"CLAUDE.md"` (document-level). They have the same key name but different semantics, and code that touches either has had to disambiguate via the colon. Rename the T3 field to `chunk_title` (with backfill via existing migration tooling). The catalog `title` becomes the unambiguous document title.

This phase is independent of the field-ownership shift and can ship first as a low-risk cleanup.

### Phase 3: Catalog reads T3-owned fields through a cached lookup

Implement `Catalog.resolve_with_fields(tumbler, fields=...)` that returns a record with both catalog-native fields (tumbler, alias_of, physical_collection, etc.) and T3-derived fields (source_path, content_hash, chunk_count) populated by joining on `physical_collection + tumbler` to T3 metadata. Cache the lookup with a short TTL (60s default) inside the catalog session.

Existing call sites of `entry.source_uri` and `entry.head_hash` migrate to `resolve_with_fields(...)`. The columns stay in the catalog DB schema during phase 3 (we are read-through, not yet stripping); they are demoted from authoritative-write to written-by-indexer-only-as-cache.

Add a structural-conformance check: `nx catalog doctor --t3-drift` flags any catalog row whose cached `source_uri` disagrees with T3's `source_path` for the same `chunk_text_hash` set. Run daily.

### Phase 4: Strip the duplicate columns from the catalog schema

After phase 3 has been live for one release cycle and the conformance check has reported zero unintentional drift across all known collections, drop the redundant columns. The append-only JSONL log retains them for archaeological purposes; the SQLite schema sheds them and the indexer's catalog-write path stops setting them.

This phase is irreversible without reverting the migration. Gate behind operator opt-in (`nexus.config.catalog.t3_owned_fields = true`) for one minor version before enabling by default.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Read amplification slows catalog operations | Phase 3 cache TTL; benchmark `nx catalog show` and `nx enrich aspects` with the join enabled before phase 4. Reject the migration if median latency degrades > 30%. |
| ChromaDB Cloud quota exhaustion from new joins | Batch joins by `chunk_text_hash` set per catalog query (one `get(where={chunk_text_hash: {$in: [...]}})` instead of N gets). Quota use scales with catalog operations, not chunks. |
| T3 unavailability breaks catalog reads | Fail-soft: when T3 is unreachable, return the cached column values with a warning. The cache survives the migration as a fallback. |
| Existing scripts that touch `cat.update(head_hash=...)` keep mutating a now-cache-only column | Phase 3 emits a deprecation log on each call; phase 4 raises `ValueError`. Search the codebase + plugin marketplace for callers before phase 4. |
| Recovery (`--from-t3`) diverges from the new write path | After phase 3, `--from-t3` becomes the canonical reconstruction; no separate code path. |
| Blast radius on existing catalogs | All migrations are additive (phase 2 adds a column; phase 3 reads through; phase 4 drops columns). JSONL log is append-only so phase 4 is recoverable by replaying older catalog versions. |

## Implementation Plan

Phased shipping; each phase is one PR. Beads created at acceptance, not at draft.

### Phase 0: Acceptance

- [ ] Land RDR-101 with this field-ownership table
- [ ] Survey downstream callers of `cat.update(source_uri=...)`, `cat.update(head_hash=...)`, `cat.update(chunk_count=...)` across the codebase, plugin marketplace, and known operator scripts. Document each.
- [ ] File a post-mortem note under `docs/rdr/post-mortem/` for nexus-3e4s referencing this RDR as the systemic fix.

### Phase 1: title field disambiguation (low risk, ship first)

- [ ] T3 metadata: rename `title` → `chunk_title` on new chunks; dual-read for one release (read both, prefer `chunk_title`).
- [ ] Migration: `nx collection backfill --rename-field title chunk_title --collection <pat>` for existing collections.
- [ ] Tests: assert any new chunk write uses `chunk_title`; assert dual-read works.

### Phase 2: Catalog read-through helper, no schema change yet

- [ ] Implement `Catalog.resolve_with_fields(tumbler, *, t3_fields=("source_path", "content_hash"))` that returns a `CatalogEntry` with T3-derived fields populated via a single batched `get(where={chunk_text_hash: {$in: [...]}})`.
- [ ] Cache the lookup in a `Catalog`-instance dict with 60s TTL. Make TTL config-overridable.
- [ ] Document the new method in `docs/catalog.md`.
- [ ] Tests: assert reads return identical values to existing fields, assert cache invalidation, assert fail-soft on T3 unavailable.
- [ ] No existing call sites change in this phase.

### Phase 3: Migrate call sites, demote columns to cache

- [ ] Migrate every call site that reads `entry.source_uri`, `entry.head_hash`, `entry.chunk_count` to use `resolve_with_fields(...)`.
- [ ] Indexer continues to populate the catalog columns (write-through cache). Operator-facing `cat.update(field, ...)` for any of the three fields emits a structlog DEPRECATION warning.
- [ ] Add `nx catalog doctor --t3-drift` that compares the cache to T3's authoritative value and reports rows whose cache is stale.
- [ ] Run the doctor against the host catalog; fix any unexpected drift before phase 4.

### Phase 4: Drop the columns

- [ ] One minor release after phase 3 with zero reported drift.
- [ ] Operator opt-in flag `[catalog].t3_owned_fields = true` (config + env var).
- [ ] Schema migration adds `source_uri`, `head_hash`, `chunk_count` to a dropped-columns list (JSONL keeps them; SQLite drops them via column-deletion or shadow-table swap).
- [ ] `cat.update(field, ...)` for those three fields raises `ValueError`.
- [ ] After one minor release with the flag enabled by default, remove the flag.

## Out of Scope

- Embedding model versioning (RDR-004, RDR-005 territory).
- Per-chunk position fields (line ranges, char offsets); T3 already owns these without overlap.
- Aspect extraction (T2 `document_aspects` table); has its own join via `source_path` that benefits from this RDR but is not changed by it.
- Knowledge / scratch metadata (T1, T2 separate stores).
- Authentication / multi-tenancy (no current cross-tenant catalog).
- Remote-catalog replication (RDR-008 scope).

## Open Questions

1. **Should the catalog cache the T3-derived fields persistently or only in-process?** In-process is simpler; persistent (`documents` columns kept as cache) survives restarts but reintroduces the drift surface. Phase 3 keeps them as a write-through persistent cache; phase 4 removes that. Should we accept the read-amp cost in phase 4 or keep persistent caching with a doctor-enforced freshness invariant?

2. **How does this interact with `nx-scratch://` URIs (RDR-096 phase 4)?** Scratch URIs have no T3 representation. They are catalog-internal. The field-ownership table treats them as catalog-owned; confirm.

3. **Do we want the `--t3-drift` doctor to be cron-scheduled or operator-invoked?** Cron requires a daemon. Operator-invoked is simpler but easier to forget. Phase 3 ships operator-invoked; phase 4 considers cron.

4. **Backwards-compat window for the title rename (phase 1).** One minor release seems short; two seems excessive. Default to one and revisit if community plugins surface that haven't migrated.

5. **Does the catalog need a "soft `source_uri`" for non-file schemes, separate from T3 lookups?** chroma://, https://, x-devonthink-item:// have no T3 chunk to look up. The proposal is to keep `source_uri` in the catalog only for non-file schemes. Confirm this is the right factoring or whether T3 should grow a `external_uri` field.
