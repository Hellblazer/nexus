---
title: "RDR-073: Temporal Entity Knowledge Graph"
status: draft
type: feature
priority: P2
created: 2026-04-13
---

# RDR-073: Temporal Entity Knowledge Graph

Entity-first knowledge with temporal validity. Agents and operators record structured facts about systems, decisions, and dependencies. As-of queries answer "what was true about X at time Y?"

## Problem

Nexus tracks documents (catalog), topics (taxonomy), and notes (memory). None answers:

- "What components does the auth system depend on?"
- "When did we switch from Redis to Memcached?"
- "Who owns the indexing pipeline?"

These require an entity-relationship model with temporal validity.

## Research

### RF-073-1: MemPalace knowledge graph

Source: `mempalace/knowledge_graph.py`

SQLite schema: `entities` + `triples` with `valid_from`/`valid_to`. Simple temporal algebra: `valid_from <= as_of AND (valid_to IS NULL OR valid_to >= as_of)`. ~400 lines total.

Population: manually seeded from hardcoded `ENTITY_FACTS` dict + MCP `add_triple` tool. No automatic extraction from stored documents.

### RF-073-2: MemPalace palace_graph (DO NOT PORT)

`palace_graph.py` does a full ChromaDB collection scan (`col.get(limit=1000)` in a loop) on every call to build a navigable room/wing graph. O(N) per invocation, no caching. At scale this is prohibitively expensive and architecturally unsound. Do not port.

### RF-073-3: Entity types for operational knowledge

| Entity type | Examples |
|-------------|----------|
| system | auth-service, indexing-pipeline, chromadb-cluster |
| component | rate-limiter, pdf-extractor |
| decision | "use HDBSCAN over BERTopic" |
| dependency | redis, postgresql, voyage-ai-api |
| concept | eventual-consistency, centroid-ANN |

### RF-073-4: Extraction approach

Three options:
1. **Manual only**: MCP tool `entity_add`. Agents/humans create triples explicitly. Simple. MemPalace does this.
2. **LLM extraction on store_put**: adds latency and cost to every write. Premature.
3. **Batch extraction**: periodic CLI command. Deferred.

**Decision: manual only for v1.** Skip extraction until the manual graph proves its value.

## Design

### T2 EntityStore (5th domain store)

~200 lines. Three tables:

```sql
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_triples (
    id INTEGER PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL REFERENCES entities(id),
    valid_from TEXT,
    valid_to TEXT,
    source_doc_id TEXT,
    created_by TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
```

### MCP tools (3)

- `entity_add(subject, predicate, object, valid_from?)` — auto-creates entities, adds triple
- `entity_query(subject, predicate?, as_of?)` — returns all matching triples
- `entity_invalidate(subject, predicate, object, ended)` — sets valid_to

### CLI (3 commands)

- `nx entity list [--type TYPE]` — browse entities
- `nx entity show NAME` — all triples for an entity, current by default
- `nx entity show NAME --as-of 2026-01` — historical state

### What is NOT in scope (v1)

- Automatic entity extraction from documents (deferred)
- Palace-style graph traversal (rejected, O(N) per call)
- Graph visualization (deferred)
- Entity-scoped search (deferred, build on taxonomy pattern if needed)

## Success Criteria

- SC-1: `entity_add("auth-service", "depends_on", "redis")` creates entities + triple
- SC-2: `entity_query("auth-service")` returns all current relationships
- SC-3: `entity_query("auth-service", as_of="2026-01")` returns historical state
- SC-4: `entity_invalidate("auth-service", "depends_on", "redis", ended="2026-03")` closes validity

## Open Questions

1. Should entity_add auto-create entities? (Proposed: yes, like git tracks files implicitly)
2. Case-insensitive matching via alias table or COLLATE NOCASE? (Proposed: COLLATE NOCASE on name, aliases for abbreviations)
3. Should the entity store share the T2 SQLite file or have its own? (Proposed: share, like the other 4 domain stores)
