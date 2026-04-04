---
title: "Git-Backed Xanadu-Inspired Catalog for T3"
id: RDR-049
type: Architecture
status: accepted
accepted_date: 2026-04-04
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-04
related_issues:
  - "RDR-037 - Single-Database Migration (closed)"
  - "RDR-042 - AgenticScholar Enhancements (closed)"
---

# RDR-049: Git-Backed Xanadu-Inspired Catalog for T3

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus indexes a growing docuverse — code repositories, prose documentation, RDR decision records, research papers, and curated knowledge — into T3 (ChromaDB). Today this docuverse has 126 collections in a single database, with no catalog layer connecting them. Collection names serve simultaneously as addresses, categories, and physical storage locations. There is no way to ask "what documents relate to this code file?" or "what papers informed this design decision?" — because there is no shared addressing scheme or link graph across content types.

The prefix convention (`code__`, `docs__`, `knowledge__`, `rdr__`) was designed for embedding model routing, not for organizing knowledge. It cannot express relationships between a code file and the RDR that designed it, or between an RDR and the paper that inspired it. Reorganizing collections requires moving data because identity is bound to collection names.

T2 (SQLite) could host a catalog, but T2 is purely local. The catalog must be at least as durable and accessible as the storage it indexes.

## Context

### The Docuverse

Most content in nexus is **git-mediated** — it lives in git repositories and evolves through commits:

| Content | Source | How it enters T3 | Git-tracked? |
|---|---|---|---|
| Code files | Git repos | `nx index repo` → `code__` | Yes |
| Prose/markdown | Git repos | `nx index repo` → `docs__` | Yes |
| RDR documents | Git repos | `nx index repo` or `nx index rdr` → `rdr__` | Yes |
| Research papers | PDF files | `nx index pdf` → `docs__` or `knowledge__` | No |
| Knowledge entries | Manual / agent | `nx store put` → `knowledge__` | No |

Git-mediated content shares a lifecycle: created in a repo, evolves through commits, re-indexed periodically. `registry.py` already tracks `head_hash` per repo. Git IS the version history for this content.

Standalone content (papers, knowledge) lacks this lifecycle — the catalog must provide it.

### Background: Ted Nelson's Xanadu

Ted Nelson's Xanadu system (Literary Machines, 1981) introduced **tumblers** — hierarchical multi-part numbers that serve as permanent addresses. Nelson's core principles:

- Tumblers are **independent of subject and category** — "nothing to do with the kind of indexing people do by subject"
- Tumblers are **independent of mechanism** — an addressing scheme, not a storage system
- Tumblers are **independent of time** — "time is kept track of separately"
- **Two system directories only**: author and title — "keep categorizing directories out of the system level. This is user business."
- **Links are first-class**: typed, bidirectional, spanning multiple regions
- **Every byte knows its origin**: transclusion provenance
- **Ghost elements**: addresses exist in tumbler-space before content is stored

The Xanadu tumbler hierarchy: `server.user.document.version.element` — encoding **identity and ownership**, not classification.

### Reference Implementation

A working Java 25 implementation exists at `~/git/xanadu` (AGPL-3.0):

| Type | Java | Purpose |
|---|---|---|
| `Humber` | `record Humber(BigInteger[] digits)` | Hierarchical compound numbers; zero-separated segments; `insertAfter()`/`insertBetween()` for infinite expansion |
| `AddressTumbler` | `record AddressTumbler(Humber server, Humber user, Humber document, Humber version, Humber element)` | 5-level permanent address |
| `XanaduLink` | `record XanaduLink(SpanSet fromSet, SpanSet toSet, LinkType type)` | Typed bidirectional links spanning multiple regions |
| `Span` | `record Span(AddressTumbler start, DifferenceTumbler width)` | Contiguous range with set operations |
| `OriginTracker` | Traces transclusion chains to ultimate origin | Provenance |
| `HistoricalEnfilade` | Version storage with diff/comparison | Version history per document |
| `DocumentStore` | `ConcurrentHashMap` | Decoupled from addressing |

Key design: addressing (`core/`) is completely separated from storage (`storage/`).

### Technical Environment

- **T3**: ChromaDB Cloud (single DB, 126 collections) or PersistentClient (local mode)
- **T2**: SQLite + FTS5 (local only — memory, plans)
- **Embedding models**: `voyage-code-3` (code), `voyage-context-3` (CCE for prose/knowledge/RDR), `voyage-4` (general)
- **Collection naming**: `{prefix}__{name}` convention, 63-char limit
- **Registry**: `registry.py` tracks repo → collection mapping with `head_hash`
- **Collection resolution**: Two functions in `corpus.py`:
  - `t3_collection_name()` (line 84) — write/direct-access: bare name → `knowledge__name`
  - `resolve_corpus()` (line 95) — search: prefix → all matching `{prefix}__*`
  - Plus `embedding_model_for_collection()` and `index_model_for_collection()` for model routing

### Current T3 Census

| Prefix | Count | Pattern | Issue |
|---|---|---|---|
| `code__` | 12 | Per-repo (hashed) | Fine |
| `docs__` | ~95 | Per-repo + per-paper | 60 individual paper collections |
| `knowledge__` | 12 | Mixed: corpora, general, test | Mixed purposes |
| `rdr__` | 7 | Per-repo | Fine |

## Proposed Solution

A **git-backed catalog** implementing Xanadu-inspired addressing and linking for the nexus docuverse. The catalog is its own system — separate API, separate storage, separate concerns — connected to T3 through physical location pointers.

### Design Principles

1. **Addressing encodes identity and ownership, not classification.** Tumbler levels are: store, owner, document. Content type (code/prose/rdr/paper) is metadata that drives physical routing — not part of the address. Faithful to Nelson: "tumblers are independent of subject and category."

2. **Git is the version engine for git-mediated content.** Git commits are versions. Git branches are forks. Git blame is origin tracking. The catalog records *which* version was indexed, not the version history itself — git already has that.

3. **The catalog and T3 are separate systems during normal operation.** The catalog does not read or write ChromaDB during normal operation (register, search, link, resolve). T3 never reads the catalog. They communicate through physical location pointers. The one exception is `nx catalog backfill`, which reads T3 collection metadata to populate the catalog from existing state — an explicit bootstrap operation, not a runtime dependency.

4. **Git-backed JSONL is the durable store; SQLite is the query cache.** JSONL diffs and merges naturally in git. SQLite is rebuilt from JSONL and gitignored. Sync via `git push`/`git pull`.

5. **Links are first-class, typed, and bidirectional.** They connect any document to any other — code to RDR, RDR to paper, paper to paper. Links optionally carry span information for chunk-level granularity.

6. **Ghost elements.** A catalog entry can exist before content is indexed — the address is valid, the shelves are empty.

### Component 1: Tumbler Addressing

New module `src/nexus/catalog/tumbler.py`.

**Hierarchy**: `store.owner.document[.chunk]`

| Level | Meaning | Xanadu equivalent | Example |
|---|---|---|---|
| `store` | Nexus instance (federation-ready) | server | `1` |
| `owner` | Repo identity or curator namespace | user/account | `1` (nexus repo), `2` (hal-research) |
| `document` | Stable ID within owner | document | `42` |
| `chunk` | Position within document (optional) | element | `7` |

**What's in the tumbler** (identity): whose is it, what is it, where in it.
**What's NOT in the tumbler** (metadata): content type, corpus, year, author, venue, embedding model, physical collection.

**Document identity**: Auto-increment per owner for ALL documents — both git-mediated and standalone. File path is metadata, not identity. This means:
- A file rename updates the `file_path` metadata field on the existing catalog entry. Same tumbler, same identity.
- `by_file_path(owner, path)` is a metadata lookup, not an identity lookup.
- No split between hash-of-path (git) and auto-increment (standalone) — one scheme for everything.

**For git-mediated content**, owner maps to `_repo_identity()` from `registry.py` — the `(basename, hash8)` pair that's already stable across worktrees. Document number is auto-incremented within that owner.

**For standalone content** (papers, knowledge), owner is a curator namespace. Same auto-increment scheme.

```python
@dataclass(frozen=True)
class Tumbler:
    """Hierarchical address: store.owner.document[.chunk]"""
    segments: tuple[int, ...]

    @property
    def store(self) -> int: return self.segments[0]
    @property
    def owner(self) -> int: return self.segments[1]
    @property
    def document(self) -> int: return self.segments[2]
    @property
    def chunk(self) -> int | None:
        return self.segments[3] if len(self.segments) > 3 else None

    def is_prefix_of(self, other: "Tumbler") -> bool:
        return other.segments[:len(self.segments)] == self.segments

    def document_address(self) -> "Tumbler":
        return Tumbler(self.segments[:3])

    def owner_address(self) -> "Tumbler":
        return Tumbler(self.segments[:2])

    def __str__(self) -> str:
        return ".".join(str(s) for s in self.segments)

    @classmethod
    def parse(cls, s: str) -> "Tumbler":
        return cls(tuple(int(x) for x in s.split(".")))
```

Simplified from the Java `Humber` — plain integers, no transfinite ordinals. If infinite insertion between existing addresses is needed later, port `Humber.insertBetween()` from the Java implementation. The simplification is noted as a known departure.

### Component 2: Git-Backed Repository

```
~/.config/nexus/catalog/              # a git repo
├── .git/
├── documents.jsonl                   # one JSON record per line
├── links.jsonl                       # typed bidirectional links
├── owners.jsonl                      # owner registry
├── .catalog.db                       # gitignored — rebuilt from JSONL
└── .gitignore                        # ignores .catalog.db
```

**JSONL record formats:**

```jsonl
// owners.jsonl — maps owner tumbler prefix to identity
{"owner":"1.1","name":"nexus","type":"repo","repo_hash":"571b8edd","description":"Nexus project"}
{"owner":"1.2","name":"hal-research","type":"curator","description":"Research papers curated by Hal"}
{"owner":"1.3","name":"arcaneum","type":"repo","repo_hash":"2ad2825c","description":"Arcaneum project"}

// documents.jsonl — one entry per document
{"tumbler":"1.1.42","title":"indexer.py","author":"","year":0,"content_type":"code","file_path":"src/nexus/indexer.py","corpus":"","physical_collection":"code__nexus-571b8edd","chunk_count":15,"head_hash":"e753374","indexed_at":"2026-04-04T...","meta":{}}
{"tumbler":"1.1.43","title":"architecture.md","author":"","year":0,"content_type":"prose","file_path":"docs/architecture.md","corpus":"","physical_collection":"docs__nexus-571b8edd","chunk_count":8,"head_hash":"e753374","indexed_at":"2026-04-04T...","meta":{}}
{"tumbler":"1.1.44","title":"RDR-049: Git-Backed Catalog","author":"Hal Hildebrand","year":2026,"content_type":"rdr","file_path":"docs/rdr/rdr-049-git-backed-catalog.md","corpus":"","physical_collection":"rdr__nexus-571b8edd","chunk_count":12,"head_hash":"e753374","indexed_at":"2026-04-04T...","meta":{}}
{"tumbler":"1.2.1","title":"Inverting Schema Mappings","author":"Fagin et al","year":2007,"content_type":"paper","file_path":"","corpus":"schema-evolution","physical_collection":"docs__LFagin-2007-LInverting-LSchema-LMappings","chunk_count":153,"head_hash":"","indexed_at":"2026-03-15T...","meta":{"venue":"SIGMOD","doi":"10.1145/..."}}

// links.jsonl — typed bidirectional links (created_by required per RF-8)
{"from":"1.1.44","to":"1.2.1","type":"cites","from_span":"","to_span":"","created_by":"user","created":"2026-04-04T...","meta":{"note":"RDR-049 cites Xanadu tumbler concept"}}
{"from":"1.1.42","to":"1.1.38","type":"relates","from_span":"","to_span":"","created_by":"index_hook","created":"2026-04-04T...","meta":{"note":"indexer calls chunker"}}
{"from":"1.2.1","to":"1.2.2","type":"cites","from_span":"3-7","to_span":"12-15","created_by":"bib_enricher","created":"2026-04-04T...","meta":{}}
```

**Design notes:**
- `from_span` and `to_span` are optional chunk ranges — empty string means document-level link, "3-7" means chunks 3 through 7. This preserves Xanadu's SpanSet granularity without requiring full SpanSet arithmetic.
- `head_hash` on document entries records the git commit at which the file was last indexed. Empty for non-git content.
- `file_path` is relative to the repo root for git content, empty for standalone.
- `corpus` is optional free-text metadata — a user-level classification, not system-level. Used for grouping/filtering, not addressing.
- `physical_collection` is the primary collection. If a document's chunks span multiple collections (unlikely in current architecture but possible), the catalog entry tracks the primary; the others are discoverable via chunk metadata in T3.
- JSONL is append-for-updates: last line wins for a given tumbler. Compacted on rebuild.
- **Write path**: Each mutating method (`register`, `update`, `link`) acquires an exclusive file lock (`fcntl.flock`) on the JSONL file, appends the record, then immediately upserts into SQLite in the same method call. This ensures JSONL and SQLite are always consistent. `rebuild()` is a recovery tool, run at startup and by `pull()`, that reloads SQLite from JSONL as the source of truth.
- **Concurrent writers**: The file lock serializes writes across processes. This is the same pattern as `registry.py`'s atomic write via `mkstemp` + `os.replace`, but simpler since JSONL is append-only.

### Component 3: SQLite Query Cache

New module `src/nexus/catalog/catalog_db.py`. Gitignored, rebuilt from JSONL.

```sql
CREATE TABLE owners (
    tumbler_prefix TEXT PRIMARY KEY,   -- "1.1"
    name TEXT NOT NULL UNIQUE,
    owner_type TEXT NOT NULL,          -- repo, curator
    repo_hash TEXT,                    -- for repo owners: hash8 from _repo_identity()
    description TEXT
);

CREATE TABLE documents (
    tumbler TEXT PRIMARY KEY,          -- "1.1.42"
    title TEXT NOT NULL,
    author TEXT,
    year INTEGER,
    content_type TEXT,                 -- code, prose, rdr, paper, knowledge
    file_path TEXT,                    -- relative path (git content) or empty
    corpus TEXT,                       -- optional user-level grouping
    physical_collection TEXT,          -- ChromaDB collection name
    chunk_count INTEGER,
    head_hash TEXT,                    -- git commit hash at last index
    indexed_at TEXT,
    origin_tumbler TEXT,               -- provenance: derived from this document
    metadata JSON
);

CREATE VIRTUAL TABLE documents_fts USING fts5(
    title, author, corpus, file_path,
    content=documents, content_rowid=rowid
);

CREATE TABLE links (
    id INTEGER PRIMARY KEY,
    from_tumbler TEXT NOT NULL REFERENCES documents(tumbler),
    to_tumbler TEXT NOT NULL REFERENCES documents(tumbler),
    link_type TEXT NOT NULL,           -- cites, supersedes, quotes, relates, comments, implements
    from_span TEXT,                    -- optional chunk range ("3-7") or empty
    to_span TEXT,                      -- optional chunk range or empty
    created_by TEXT NOT NULL,          -- origin: "user", "bib_enricher", "index_hook", agent name
    created_at TEXT,
    metadata JSON
);
CREATE INDEX idx_links_from ON links(from_tumbler);
CREATE INDEX idx_links_to ON links(to_tumbler);
CREATE INDEX idx_links_type ON links(link_type);
CREATE INDEX idx_links_created_by ON links(created_by);
```

### Component 4: Catalog API

New module `src/nexus/catalog/catalog.py`.

```python
class Catalog:
    def __init__(self, repo_path: Path, db_path: Path): ...

    # ── Lifecycle ────────────────────────────────────────────────
    def rebuild(self) -> None:
        """Rebuild SQLite from JSONL (after git pull, on startup)."""
    def sync(self, message: str = "catalog update") -> None:
        """git add + commit + push."""
    def pull(self) -> None:
        """git pull + rebuild."""

    # ── Owners ───────────────────────────────────────────────────
    def register_owner(self, name: str, owner_type: str, **meta) -> Tumbler:
        """Register a repo or curator namespace. Returns owner tumbler."""
    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        """Look up owner by repo identity hash."""

    # ── Documents ────────────────────────────────────────────────
    def register(self, owner: Tumbler, title: str, **meta) -> Tumbler:
        """Register a document under owner. Auto-assigns document number."""
    def update(self, tumbler: Tumbler, **fields) -> None:
        """Update metadata on existing entry."""
    def resolve(self, tumbler: Tumbler) -> CatalogEntry | None:
        """Look up by tumbler."""
    def find(self, query: str, **filters) -> list[CatalogEntry]:
        """FTS5 search over title, author, corpus, file_path."""
    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        """Look up git-tracked file by relative path within owner."""
    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        """All documents tagged with a corpus name."""
    def by_owner(self, owner: Tumbler) -> list[CatalogEntry]:
        """All documents belonging to an owner."""

    # ── Links ────────────────────────────────────────────────────
    def link(self, from_t: Tumbler, to_t: Tumbler, link_type: str,
             created_by: str, from_span: str = "", to_span: str = "", **meta) -> None:
        """Create a typed link. created_by is required (RF-8 junk filtering)."""
    def unlink(self, from_t: Tumbler, to_t: Tumbler, link_type: str = "") -> int:
        """Remove link(s). Returns count removed."""
    def links_from(self, tumbler: Tumbler, link_type: str = "") -> list[CatalogLink]:
        """Outgoing links from this document."""
    def links_to(self, tumbler: Tumbler, link_type: str = "") -> list[CatalogLink]:
        """Incoming links (backlinks) to this document."""
    def graph(self, tumbler: Tumbler, depth: int = 1,
              direction: str = "both", link_type: str = "") -> dict:
        """Traverse link graph to given depth."""

    # ── Physical mapping ─────────────────────────────────────────
    def collections_for(self, tumbler: Tumbler) -> list[str]:
        """Resolve tumbler (or owner prefix) to ChromaDB collection names."""
```

### Component 5: Separate Catalog API

The catalog is its own API — separate from the existing `nx` storage and search plumbing. Honoring Xanadu: the addressing system is independent of the storage mechanism.

#### 5a. CLI: `nx catalog`

| Command | Args / Flags | What it does |
|---|---|---|
| `init` | `[--remote URL]` | Create catalog git repo |
| `list` | `[--owner X] [--corpus X] [--type T] [--limit N] [--offset N]` | List catalog entries |
| `show` | `TUMBLER` or `--title "..." [--owner X]` | Full entry: metadata + links in/out |
| `search` | `QUERY [--limit N]` | FTS5 over title, author, corpus, file_path |
| `register` | `--title T --owner O [--author A] [--year Y] [--type T] [--file-path P]` | Assign tumbler, create entry |
| `update` | `TUMBLER [--title T] [--author A] [--year Y] [--corpus C] [--meta JSON]` | Update entry metadata |
| `link` | `FROM TO --type TYPE [--from-span S] [--to-span S] [--meta JSON]` | Create typed link |
| `unlink` | `FROM TO [--type TYPE]` | Remove link(s) |
| `links` | `TUMBLER [--direction in\|out\|both] [--type TYPE] [--depth N]` | Show link graph |
| `owners` | `[--list]` | List registered owners |
| `backfill` | `[--dry-run]` | Populate catalog from existing T3 |
| `sync` | `[--message MSG]` | `git add && commit && push` |
| `pull` | | `git pull && rebuild SQLite` |
| `stats` | | Owner counts, document counts, link counts |

#### 5b. MCP Tools

Own namespace, registered alongside but separate from existing `nx` MCP tools.

```python
@tool
def catalog_search(
    query: str,                # FTS5 search text
    owner: str = "",           # filter to owner name
    corpus: str = "",          # filter to corpus
    content_type: str = "",    # filter: code, prose, rdr, paper, knowledge
    limit: int = 20,
) -> list[dict]:
    """Search the catalog by title, author, corpus, or file path.
    Returns catalog entries — NOT content. Use `search` for semantic content search."""

@tool
def catalog_show(
    tumbler: str = "",         # exact tumbler
    title: str = "",           # title match (alternative)
    owner: str = "",           # disambiguate title matches
) -> dict | None:
    """Full catalog entry including links in and out."""

@tool
def catalog_list(
    owner: str = "",           # filter to owner
    corpus: str = "",          # filter to corpus
    content_type: str = "",    # filter by type
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List catalog entries with optional filters."""

@tool
def catalog_register(
    title: str,
    owner: str,                # owner name (must exist)
    content_type: str = "paper",
    author: str = "",
    year: int = 0,
    file_path: str = "",
    corpus: str = "",
    physical_collection: str = "",
    meta: str = "",
) -> dict:
    """Register a document. Assigns tumbler. Ghost elements:
    physical_collection can be empty (not yet indexed)."""

@tool
def catalog_update(
    tumbler: str,
    title: str = "",
    author: str = "",
    year: int = 0,
    corpus: str = "",
    physical_collection: str = "",
    meta: str = "",
) -> dict:
    """Update a catalog entry's metadata."""

@tool
def catalog_link(
    from_tumbler: str,
    to_tumbler: str,
    link_type: str,            # cites, supersedes, quotes, relates, comments, implements
    created_by: str = "user",  # origin: user, bib_enricher, index_hook, agent name
    from_span: str = "",       # optional chunk range ("3-7")
    to_span: str = "",
    meta: str = "",
) -> dict:
    """Create a typed link. created_by tracks origin for junk filtering (RF-8)."""

@tool
def catalog_links(
    tumbler: str,
    direction: str = "both",   # in, out, both
    link_type: str = "",
    depth: int = 1,
) -> list[dict]:
    """Links to/from a catalog entry.
    depth > 1 traverses the graph."""

@tool
def catalog_resolve(
    tumbler: str = "",         # resolve tumbler to collections
    owner: str = "",           # resolve all collections for an owner
    corpus: str = "",          # resolve all collections for a corpus
) -> list[str]:
    """Resolve to physical ChromaDB collection names.
    Returns collection names usable with the `search` tool."""
```

#### 5c. Boundary: Catalog ↔ T3

```
Catalog (git-backed)                T3 (ChromaDB)
┌──────────────────────┐            ┌──────────────────┐
│ documents.jsonl       │──physical──→ collections      │
│ links.jsonl           │  location  │ chunks           │
│ owners.jsonl          │  pointers  │ embeddings       │
│ .catalog.db (cache)   │            │ metadata         │
└──────────────────────┘            └──────────────────┘
      ↕ git sync                         ↕ ChromaDB API
      remote repo                        cloud / local
```

During normal operation, the catalog does not read or write ChromaDB. It stores *where* things are, not *what* they contain. T3 never reads the catalog. The application layer — `nx` commands, MCP tools, agents — consults both.

**Example workflow (catalog → T3 integration)**: An agent wants to search schema evolution papers by Fagin:
1. `catalog_search(query="fagin schema", corpus="schema-evolution")` → returns catalog entries with tumblers
2. `catalog_resolve(corpus="schema-evolution")` → returns `["docs__schema-evolution"]`
3. `search(query="chase procedure optimization", corpus="docs__schema-evolution")` → targeted semantic search in T3

The catalog narrows; T3 retrieves content. The existing query planner (RDR-042) gains `catalog_search`, `catalog_traverse`, and `catalog_resolve` as new plan operations in RDR-050.

### Component 6: Git-Mediated Content Integration

When `nx index repo` runs against a repository:

1. **Existing behavior (unchanged)**: Walk git-tracked files, classify (code/prose/PDF/RDR), chunk, embed, upsert to T3 collections. Registry records `head_hash`.

2. **New catalog hook (opt-in, graceful absence)**: If the catalog repository is not initialized (`~/.config/nexus/catalog/` does not exist), the hook is silently skipped with a `DEBUG`-level log. Catalog integration requires `nx catalog init` first. When the catalog IS initialized, after successful indexing, for each indexed file:
   - Look up owner by repo hash → create owner if first time
   - Look up document by `(owner, file_path)` → create entry if new file, update if existing
   - Set `head_hash`, `chunk_count`, `indexed_at`, `physical_collection`
   - Content type set from classification (code/prose/rdr)

**Re-indexing after a commit** updates the catalog entry — same tumbler, new `head_hash`, possibly new `chunk_count`. The tumbler is permanent; the version state is metadata. Git is the version history.

**File renames**: If `git mv` renames a file, the re-index hook detects the rename (via git diff or matching chunk content) and updates `file_path` on the existing catalog entry. Same tumbler, same identity — only the metadata changes. If rename detection fails (content also changed), a new entry is created and a `supersedes` link connects old → new.

**File deletion**: Catalog entry remains (ghost element). `chunk_count` goes to 0 on next re-index. The address was once valid; the catalog remembers.

### Component 7: Standalone Content Integration

For PDFs and knowledge entries (non-git content):

**`nx index pdf`**: After indexing, register document in catalog under the appropriate curator owner. Title from PDF metadata or filename. `corpus` from `--corpus` flag if provided.

**`nx store put`**: Register in catalog if the entry doesn't exist. Knowledge entries get tumblers too.

**`nx enrich`**: After bibliographic enrichment, update catalog entry with year, venue, authors. Auto-create `cites` links from Semantic Scholar `references` field.

**Versioning**: Non-git content versions are tracked by the catalog's own git history. JSONL changes are committed and visible in `git log`.

### Component 8: Backfill

Populate the catalog from existing T3 state:

1. **Repos**: Walk `registry.all_info()` → create owner per repo, document per known file. Use collection metadata and registry entries to reconstruct file lists.

2. **Per-paper collections**: Walk `docs__L*` collections → create curator owner (e.g., `hal-research`), one document per collection. Extract title/author/year from chunk metadata where available.

3. **Knowledge collections**: Walk `knowledge__*` → register each collection as a document or set of documents.

4. **Links**: Auto-generate from `bib_semantic_scholar_id` metadata where present. Manual curation for code ↔ RDR links.

### Component 9: Collection Consolidation (Future)

With the catalog tracking document identity independently of collection names, per-paper collections can be consolidated into corpus-level collections:

1. Create corpus collection (`docs__schema-evolution`)
2. Migrate chunks with embeddings from per-paper collections
3. Update catalog entries' `physical_collection` pointers
4. Delete empty old collections

Target: ~95 `docs__` collections → ~15-20. Safe because identity is in the catalog, not the collection name.

### Component 10: Multi-Database Routing (Deferred)

The catalog knows each document's `physical_collection`. A future `physical_db` field would enable routing to different ChromaDB databases per embedding model affinity. `T3Database._client_for()` would consult the catalog. Deferred until catalog is proven.

## Xanadu Fidelity

### Faithful

| Principle | How honored |
|---|---|
| Addressing independent of category | Tumbler encodes identity (store.owner.document), not classification. Content type and corpus are metadata. |
| Addressing independent of mechanism | Catalog is separate from T3. Documents can move between collections without changing tumbler. |
| Time tracked separately | Git commits are version history. `head_hash` is metadata, not an address component. |
| Two system directories: author, title | Core fields on every catalog entry. Everything else is optional metadata. |
| Links are first-class and typed | `links.jsonl` with typed bidirectional links and optional span granularity. |
| Ghost elements | Catalog entry can exist before content is indexed (`physical_collection` empty, `chunk_count` 0). |
| Owned numbers | Owner tumbler level — each repo/curator owns their document numbering. |
| Separation of addressing from storage | `catalog/` package is entirely separate from `db/t3.py`. |

### Known Departures

| Xanadu feature | Departure | Rationale |
|---|---|---|
| Humber arithmetic (infinite insertion) | Plain integers | Simplicity. Port `insertBetween()` from Java if needed. |
| SpanSet links (multi-region ↔ multi-region) | Optional single span per endpoint | Full SpanSet arithmetic is complex; chunk-range strings cover the common case. |
| Origin tracking (every byte knows its source) | `origin_tumbler` field on documents | Byte-level origin tracking is in the Java implementation; document-level is sufficient for the catalog. Git blame covers intra-file provenance. |
| Version in tumbler (server.user.doc.VERSION.element) | Version is metadata (`head_hash`) | Git is the version engine. Embedding version in the tumbler would create address instability on every commit. |
| Links have home documents | Links are free-floating rows | Pragmatic simplification. Home-document semantics can be added via metadata if needed. |
| FEBE 4D link filter (home, from, to, type) | 3D filter (from/to, direction, type) | No home-set because links have no home documents. Sufficient until competing organizational views (intercomparison documents) require disambiguation. |

## Alternatives Considered

### T2 (local SQLite) as catalog (rejected)
Purely local — less durable than the content it indexes.

### Dolt as catalog store (deferred)
Full SQL + git-like sync, already used for beads. Adds coupling; heavier than plain git + JSONL. Reconsider if JSONL merge conflicts become a problem.

### Catalog as ChromaDB collection (rejected)
No JOINs, no graph traversal. Link queries require scatter-gather.

### Full Xanadu server as catalog (deferred)
Java implementation could serve over HTTP. Adds JVM dependency. Available for future federation.

### PostgreSQL / Turso / Neon (rejected)
Cloud-native SQL but adds infrastructure. Git gives sync for free.

### Content type in tumbler (rejected)
Embedding `code`/`prose`/`rdr` in the tumbler hierarchy would violate Nelson's "independent of category" principle and the Java implementation's hierarchy (which uses ownership, not classification).

## Success Criteria

- [ ] Catalog git repo initializable via `nx catalog init`
- [ ] Owners registerable (repos auto-discovered, curators manual)
- [ ] Documents registerable with permanent tumbler addresses
- [ ] FTS5 search over title, author, corpus, file_path
- [ ] Typed bidirectional links with optional span granularity
- [ ] Link traversal to depth N
- [ ] JSONL ↔ SQLite rebuild cycle correct
- [ ] Git sync: push/pull/merge work
- [ ] `nx index repo` registers/updates documents in catalog
- [ ] `nx index pdf` registers documents in catalog
- [ ] Backfill populates catalog from existing 126 collections
- [ ] `nx catalog` CLI commands operational
- [ ] MCP catalog tools operational
- [ ] Ghost elements: entry exists before content indexed
- [ ] Indexing without catalog initialized completes successfully (graceful absence)
- [ ] All existing tests pass (no regressions)

## Resolved Questions

1. **Document ID scheme**: Auto-increment per owner for all documents (git and standalone). File path is metadata, not identity. Renames update metadata on existing entry. (Resolved: gate review C2)
2. **JSONL update strategy**: Append-only, last line wins, compacted on rebuild. (Resolved: gate review S4 — already answered in Component 2)
3. **Link type `implements`**: Yes, first-class type. Already in the DDL. (Resolved: gate review O4)

## Open Questions

1. **Catalog repo location**: `~/.config/nexus/catalog/` or configurable via `.nexus.yml`?
2. **Citation auto-generation**: Auto-create `cites` links from Semantic Scholar `references` during `nx enrich`? (Likely yes — deferred to RDR-050 Layer 1)
3. **Corpus assignment for backfill**: How to group the ~60 individual paper collections? Manual curation seems right.
4. **Rename detection heuristic**: Git diff `--find-renames` output? Chunk content matching? What false-positive rate is acceptable?

## Implementation Plan

TBD — pending gate review and bead decomposition.

Estimated order:
1. **Catalog core**: Tumbler type, JSONL read/write, SQLite schema, rebuild cycle (TDD)
2. **Catalog API**: Register, update, resolve, find, link, graph traversal (TDD)
3. **Git integration**: Init, sync, pull (TDD)
4. **CLI**: `nx catalog` command group
5. **MCP tools**: Catalog tool namespace
6. **Repo indexing hook**: Post-index registration in catalog
7. **Backfill**: Populate catalog from existing T3
8. **PDF/knowledge hooks**: Post-index and post-enrich registration
9. **Link graph**: Citation auto-generation, code ↔ RDR links
10. **Collection consolidation**: Merge per-paper collections, update pointers

## Research Findings

### RF-1: ChromaDB Multi-Database Capabilities (2026-04-04)
**Classification**: Verified — API Documentation + Context7 | **Confidence**: HIGH

Multiple databases per tenant. `CloudClient(database="X")` per DB. No cross-DB queries. `PersistentClient(path="X")` per directory. 126 collections is 0.01% of 1M limit. Current search already queries collections independently.

### RF-2: Xanadu Addressing Primitives (2026-04-04)
**Classification**: Verified — Source Code Analysis (`~/git/xanadu`) | **Confidence**: HIGH

`Humber(BigInteger[])` with zero-separated segments and `insertBetween()`. `AddressTumbler(server, user, document, version, element)`. `XanaduLink(SpanSet, SpanSet, LinkType)`. `OriginTracker` with chain traversal. Complete separation of addressing from storage.

### RF-3: Nelson's Principles (2026-04-04)
**Classification**: Verified — Literary Machines (Mixedbread xanadu store) | **Confidence**: HIGH

Tumblers independent of subject, category, mechanism, time. Two system directories: author, title. Categories are user business. Sieving (attribute filtering) for query. Ghost elements in tumbler-space.

### RF-4: Git-Backed SQLite Feasibility (2026-04-04)
**Classification**: Hypothesis — Design Analysis | **Confidence**: MEDIUM

JSONL diffs/merges naturally in git. SQLite rebuilt from JSONL as query cache. Writes to both. Git provides branching, history, sync.

### RF-5: Integration Surface Area (2026-04-04)
**Classification**: Verified — Codebase Analysis | **Confidence**: HIGH

All T3 collection resolution flows through `t3_collection_name()` (8 callers) and `resolve_corpus()` (4 callers) in `corpus.py`, plus two model-selection functions. The catalog is additive — does not need to intercept these initially.

### RF-6: Xanadu Fidelity Audit (2026-04-04)
**Classification**: Verified — Cross-referencing Literary Machines, Java implementation (`~/git/xanadu`), and RDR-049 design | **Confidence**: HIGH

Audit of the catalog design against both Nelson's principles and the Java reference implementation revealed critical departures that were corrected:

**Corrected**: Original design embedded `corpus` as the second tumbler level (`store.corpus.document.chunk`). Nelson explicitly states tumblers are "INDEPENDENT OF SUBJECT AND CATEGORY." The Xanadu hierarchy is `server.user.document.version.element` — identity and ownership, not classification. Corrected to `store.owner.document[.chunk]` with corpus as metadata.

**Acknowledged departures (with rationale)**:
- Humber arithmetic simplified to plain integers (sufficient at current scale; Java `insertBetween()` available if needed)
- Links are point-to-point with optional single span, not full SpanSet ↔ SpanSet (chunk-range strings cover common case)
- Origin tracking at document level (`origin_tumbler`), not byte level (git blame covers intra-file provenance)
- Version is metadata (`head_hash`), not tumbler level (git is the version engine; embedding version in tumbler creates address instability on every commit)
- Links are free-floating rows, not owned by home documents (pragmatic; home-set filtering deferred)

**Nelson's validation of the overall approach** (Ch. 3, p. 3/15): "Trying to add such things later is very different from designing them in at the start... what you really need is a system designed from the start to have all these features." The catalog is the "design it in" moment.

### RF-7: Git-Mediated Content as Primary Docuverse (2026-04-04)
**Classification**: Verified — Codebase + Architecture Analysis | **Confidence**: HIGH

The nexus docuverse is primarily git-mediated content (code, prose, RDRs) that evolves through commits, not standalone PDFs. When `nx index repo` runs:
- 50+ Python files → `code__` (voyage-code-3)
- 30+ markdown/prose → `docs__` (voyage-context-3)
- 40+ RDR documents → `rdr__` (voyage-context-3)

All share the same lifecycle: created in git, evolve through commits, re-indexed periodically. `registry.py` already tracks `head_hash`. Git IS the HistoricalEnfilade. Git branches ARE document forking.

The catalog maps naturally: owner = repo identity (from `_repo_identity()` in registry.py), document = stable file ID, version = git commit (metadata). Code and prose are siblings under the same owner — the only difference is embedding model routing, which is physical, not identity.

Standalone content (PDFs, knowledge entries) is the special case, not the norm. The catalog provides version tracking for those; git provides it for everything else.

### RF-8: Nelson on Junk Links and Origin Filtering (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 4 | **Confidence**: HIGH

Nelson anticipated the junk link problem (Ch. 4, p. 4/60): "Filtering out junk links in a universe full of them is a vital aspect of system performance. Control of incoming links by their origin is a key to eliminating garbage."

When agents auto-generate links (citation extraction, code ↔ RDR heuristics, concept tagging), junk accumulates. Nelson's answer: filter by type and origin. Every link must carry `created_by` in metadata from day one. Link types reduce context: "typed links allow the user to reduce the context of what is shown" (Ch. 3, p. 3/12).

### RF-9: Source Validation — Grounded vs. Speculative (2026-04-04)
**Classification**: Verified — Cross-referencing Literary Machines (Mixedbread xanadu store, 2 files / 692K tokens) against catalog design | **Confidence**: HIGH

Comprehensive cross-check of the catalog design against Nelson's actual text to distinguish what's grounded from what's aspirational.

**Grounded by Nelson:**
- The catalog itself: "Trying to add such things later is very different from designing them in at the start" (Ch. 3, p. 3/15). The prefix-based collection system is the "all we need" approach; the catalog is the "design it in" correction.
- Link-heavy workloads: "Link-search is deemed to be 'free'" (Ch. 4, p. 4/60). At our scale (SQLite, thousands of links), this holds trivially. Nelson says pile on links.
- Ghost elements: "It is possible to link to a node, or an account, even though there is nothing stored in the docuverse corresponding to them" (Ch. 4, p. 4/23). Directly validates catalog entries before content is indexed.
- Link navigation as primary retrieval: Nelson's system has "essentially nothing except documents and their arbitrary links. There is no system directory; rather, we encourage the on-line publishing of directory documents by users" (Ch. 4, p. 4/41). This validates the "narrow-then-search" pattern — navigate links first, semantic search second.
- Categories as user business, not system level: confirmed by multiple passages (Ch. 2 p. 2/49, Ch. 1 p. 1/21).

**Not grounded (speculative extensions):**
- Computed similarity indexes — Nelson never discusses automated similarity detection; his links are human/agent-created.
- Authority scoring — Nelson has no concept of document importance ranking; all documents are equal citizens in the docuverse.
- Materialized view documents — Nelson's "intercomparison documents" are a related concept but user-created, not system-computed.

**Nelson's FEBE search model** (Ch. 4, p. 4/63): `FINDLINKSFROMTOTHREE(home-set, from-set, to-set, type-set)` — a 4-dimensional link filter. Our `catalog_links(tumbler, direction, type)` is 3-dimensional (no home-set). Sufficient for now; home-set filtering is a known gap for when competing organizational views arise.
