---
title: "RDR-073: Temporal Entity Knowledge Graph"
status: draft
type: feature
priority: P2
created: 2026-04-13
---

# RDR-073: Temporal Entity Knowledge Graph

Add entity-first knowledge representation with temporal validity to nexus. Inspired by MemPalace's temporal KG but adapted for operational knowledge (systems, decisions, dependencies) rather than personal memory (people, events, emotions).

## Problem

Nexus knows about documents but not entities. The catalog tracks *what* is indexed and *how documents relate*. The taxonomy tracks *what topics exist*. Neither answers:

- "What components does the auth system depend on?" (entity relationships)
- "When did we switch from Redis to Memcached?" (temporal facts)
- "What was the deployment architecture before the migration?" (as-of queries)
- "Who owns the indexing pipeline?" (entity attribution)

These questions require an entity-relationship model with temporal validity, not document search.

## Research

### RF-073-1: MemPalace temporal KG

Source: `mempalace/knowledge_graph.py`

SQLite schema: `entities` (id, name, entity_type, created_at) + `triples` (subject_id, predicate, object_id, valid_from, valid_to, source_drawer_id, created_at).

Key operations:
- `add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")`
- `query_entity("Max", as_of="2026-01-15")` — returns all triples valid at that date
- `invalidate("Max", "has_issue", "injury", ended="2026-02-15")` — closes the validity window

The temporal algebra is simple: `valid_from <= as_of AND (valid_to IS NULL OR valid_to >= as_of)`.

Strengths: simple, effective, SQLite-native. Weaknesses: entity extraction is manual (user or LLM creates triples explicitly), no automatic extraction from stored documents.

### RF-073-2: Entity types for operational knowledge

MemPalace's entity types are personal (person, project, tool, concept, place, event). Nexus would need operational types:

| Entity type | Examples |
|-------------|----------|
| system | auth-service, indexing-pipeline, chromadb-cluster |
| component | rate-limiter, token-validator, pdf-extractor |
| person | developer, reviewer, architect (role-based, not personal) |
| decision | "use HDBSCAN over BERTopic", "switch to Voyage AI" |
| dependency | redis, postgresql, voyage-ai-api |
| concept | eventual-consistency, centroid-ANN, c-TF-IDF |

### RF-073-3: Entity extraction approaches

Three options for populating the graph:

1. **Manual only** (MemPalace approach): MCP tool `entity_add(subject, predicate, object, valid_from)`. Agents or humans create triples explicitly. Simple but requires discipline.

2. **LLM extraction**: on every `store_put`, run a lightweight extraction prompt to identify entities and relationships. Adds latency and cost to the store path.

3. **Batch extraction**: periodic `nx entity extract --collection X` that scans stored documents and proposes triples for review. Similar to how `nx taxonomy discover` works.

Proposed: start with manual + batch extraction (options 1 + 3). Skip LLM-on-store (option 2) until the value is proven.

### RF-073-4: Integration with existing nexus features

- **Catalog links**: document-level relationships (cites, implements). Entity triples are finer-grained (component-level).
- **Taxonomy topics**: group documents by theme. Entity graph connects concepts across topics.
- **T2 memory**: free-text notes. Entity triples are structured facts extracted from notes.

The entity graph complements all three. It does not replace any of them.

## Design (sketch, needs research)

### New T2 domain store: EntityStore

Fifth domain store alongside MemoryStore, PlanLibrary, CatalogTaxonomy, Telemetry.

Tables:
- `entities` (id, name, entity_type, collection, created_at)
- `entity_triples` (id, subject_id, predicate, object_id, valid_from, valid_to, source_doc_id, created_at, created_by)
- `entity_aliases` (entity_id, alias) — for matching variations ("ChromaDB" = "chromadb" = "Chroma")

### MCP tools

- `entity_query(subject, predicate?, as_of?)` — query the graph
- `entity_add(subject, predicate, object, valid_from?)` — add a triple
- `entity_invalidate(subject, predicate, object, ended)` — close a validity window

### CLI

- `nx entity list [--type TYPE]` — browse entities
- `nx entity show NAME` — all triples for an entity
- `nx entity extract --collection X` — batch extraction with review
- `nx entity graph [--format dot]` — export for visualization

## Success Criteria

- SC-1: `entity_query("auth-service")` returns all current relationships
- SC-2: `entity_query("auth-service", as_of="2026-01")` returns historical state
- SC-3: Batch extraction proposes triples from existing documents
- SC-4: Entity graph integrates with search (entity-scoped search, like topic-scoped)

## Open Questions

1. Should entity extraction run on `store_put` (real-time) or batch-only? (Proposed: batch for v1)
2. How to handle entity resolution (dedup "ChromaDB" vs "chromadb" vs "Chroma")? (Proposed: alias table + case-insensitive matching)
3. Should entities be scoped per-collection or global? (Proposed: global with collection provenance on triples)
4. What predicate vocabulary? (Proposed: open vocabulary, not a fixed set. Common predicates emerge from usage.)
5. How to bootstrap from existing catalog links? (Proposed: `entity extract --from-catalog` converts document links to entity triples)
