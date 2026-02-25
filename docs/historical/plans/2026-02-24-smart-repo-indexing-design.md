# Smart Repository Indexing Design

## Problem Statement

`nx index code` treats every file in a git repo as code, embedding all content with voyage-code-3. This produces poor retrieval quality for non-code content — markdown docs, RDRs, config prose, READMEs, PDFs. Repos also contain RDR documents that require a separate manual `nx index rdr` invocation that's easy to forget.

## Scope

The repository indexing pipeline. Specifically:

- Extension-based file classification (code vs prose vs PDF)
- Per-class embedding model selection (voyage-code-3 vs voyage-context-3)
- Two general collections per repo (`code__` and `docs__`), plus optional `docs__rdr__`
- RDR directory discovery and T3 indexing (report-only for T2 concerns)
- `.nexus.yml` per-repo config for extension and RDR path overrides
- HEAD polling triggers the unified pipeline

**Out of scope**:

- T2 RDR lifecycle (rdr-* skills, `nx memory`) — entirely untouched
- Explicit document indexing (`nx index docs`, `nx index pdf`) — unchanged
- Search engine changes — collection routing already works via prefix
- Any automatic T2 operations

## File Classification

A canonical set of code extensions defines the boundary. Files with these extensions get voyage-code-3 and AST chunking. PDFs get extracted and chunked via the existing pipeline. Everything else that reads as UTF-8 text gets voyage-context-3 (CCE with voyage-4 fallback) and line/semantic chunking.

### Default Code Extensions

The small, greppable list (union of current `_EXT_TO_LANGUAGE` and `_AST_EXTENSIONS`):

```
.py .js .jsx .ts .tsx .java .go .rs .cpp .cc .c .h .hpp
.rb .cs .sh .bash .kt .swift .scala .r .m .php
```

Everything not on this list is prose.

### Classification Decisions

- `.toml`, `.yaml`, `.yml`, `.json`, `.xml` — **prose**. They contain meaningful natural language (descriptions, comments, keys) that benefits from general-purpose embeddings. Removed from the code set even though tree-sitter can parse them.
- `.sql`, `.proto`, `.graphql` — **prose** by default. Overridable via `.nexus.yml`.
- `.pdf` — **PDF pipeline**. Routed through `PDFExtractor` + `PDFChunker`.
- Binary files — skipped (existing UnicodeDecodeError catch). No change.

### Three Content Classes

| Class | Extensions | Embedding Model | Chunking | Target Collection |
|-------|-----------|----------------|----------|-------------------|
| Code | `_CODE_EXTENSIONS` set | voyage-code-3 | AST (tree-sitter) with line fallback | `code__repo-hash` |
| PDF | `.pdf` | voyage-context-3 (CCE, voyage-4 fallback) | PyMuPDF4LLM + PDFChunker | `docs__repo-hash` |
| Prose | everything else (UTF-8) | voyage-context-3 (CCE, voyage-4 fallback) | SemanticMarkdownChunker for `.md`, line-based otherwise | `docs__repo-hash` |

## Collection Scheme

Three collections per repo (up from one today):

| Collection | Contents | Embedding Model | Notes |
|-----------|----------|----------------|-------|
| `code__repo-hash` | Code files | voyage-code-3 | Same naming as today |
| `docs__repo-hash` | Prose + PDFs (minus RDR paths) | voyage-context-3 / voyage-4 fallback | New, same hash scheme |
| `docs__rdr__repo` | RDR documents only | voyage-context-3 / voyage-4 fallback | Same as today's `nx index rdr` |

### Naming

- `code__repo-hash` — unchanged from today (`code__{basename}-{sha256[:8]}`)
- `docs__repo-hash` — new, same hash derivation
- `docs__rdr__repo` — unchanged (uses repo name, not hash)

### Search Routing (no changes needed)

- `nx search --corpus code` → all `code__*` collections
- `nx search --corpus docs` → all `docs__*` collections (repo prose, RDRs, and explicitly indexed PDFs/markdown)
- `nx search "query"` (no corpus filter) → searches everything

### Embedding Models

`corpus.py` `index_model_for_collection()` already returns correct models by collection prefix:

- `code__*` → `voyage-code-3` (index time)
- `docs__*` → `voyage-context-3` (index time, CCE)
- All collections → `voyage-4` (query time, universal)

The existing `_embed_with_fallback()` in `doc_indexer.py` handles CCE's 2+ chunk requirement by falling back to voyage-4 for single-chunk files. No changes needed.

### Metadata

Every chunk carries: `source_path`, `line_start`, `line_end`, `git_commit_hash`, `git_branch`, `git_remote_url`, `content_hash`, `embedding_model`, `store_type`, `frecency_score`. This enables search to work after a repo is no longer locally available. GitHub/GitLab permalinks can be derived from `git_remote_url` + `source_path` + `git_commit_hash`.

## Unified Indexer Pipeline

`nx index code` is renamed to `nx index repo`. One file walk, three output streams:

```
nx index repo <path>
  │
  ├─ Walk repo (respecting .gitignore, ignore patterns)
  ├─ Load .nexus.yml (extension overrides, rdr_paths)
  ├─ Collect git metadata once
  ├─ Compute frecency scores once (single git log pass)
  │
  ├─ For each file:
  │   ├─ Under an rdr_path? → skip (handled separately below)
  │   ├─ .pdf? → PDF extraction + chunking → docs__repo-hash
  │   ├─ Extension in code set? → AST chunking → code__repo-hash
  │   ├─ Reads as UTF-8? → prose chunking → docs__repo-hash
  │   └─ Otherwise → skip (binary)
  │
  ├─ RDR discovery:
  │   ├─ Check each rdr_path (default: docs/rdr)
  │   ├─ Report count to user
  │   ├─ Index .md files → docs__rdr__repo (T3 only)
  │   └─ Report new/unindexed RDRs (does NOT touch T2)
  │
  ├─ Prune stale chunks:
  │   ├─ Files that changed classification (code→prose or vice versa)
  │   └─ Files deleted from repo
  │
  └─ Build ripgrep cache (all text files, sorted by frecency — unchanged)
```

### Design Decisions

- **One file walk**: frecency, classification, and ripgrep cache all come from the same traversal.
- **Ripgrep cache includes all text files** regardless of classification. Full-text search is model-agnostic.
- **RDR files excluded from `docs__repo-hash`** to avoid double-indexing.
- **RDR discovery is report-only for T2**. Prints findings, does not create memory entries or trigger rdr-* skills.
- **Staleness check per-file**: existing `content_hash` + `embedding_model` comparison. Handles both content changes and model reclassification.
- **HEAD polling**: `check_and_reindex()` calls the unified `index_repository()`. No changes to `polling.py`.

## `.nexus.yml` Configuration

Extends the existing per-repo config mechanism (`load_config(repo_root=repo)`):

```yaml
# .nexus.yml (in repo root)
indexing:
  # Additional extensions to treat as code (added to defaults)
  code_extensions: [.sql, .proto]

  # Force these extensions to prose (overrides defaults and code_extensions)
  prose_extensions: [.sh]

  # RDR document directories (default: ["docs/rdr"])
  rdr_paths:
    - docs/rdr
    - design/decisions
```

### Rules

- All fields optional. Absent `indexing` section = pure defaults.
- `prose_extensions` wins over `code_extensions` wins over defaults.
- `rdr_paths` defaults to `["docs/rdr"]`. Set to `[]` to disable RDR discovery.
- Extensions include the dot (`.sql` not `sql`).
- No path-pattern overrides in v1. Schema accommodates them later.

## Migration & Existing Collections

Existing `code__repo-hash` collections contain prose files embedded with voyage-code-3. On first `nx index repo` run:

1. Indexer classifies each file by extension
2. Prose/PDF files get embedded into `docs__repo-hash` (created automatically)
3. Stale prose chunks are pruned from `code__repo-hash`

**Pruning**: After indexing both collections, compare `source_path` values in each collection against the current classification. Chunks in the wrong collection get deleted. Handles reclassification in both directions.

One-time cost on first re-index. Subsequent runs use normal staleness checks.

No explicit migration command. `nx index repo <path>` converges to correct state.

## What Changes, What Doesn't

### Changes

- `indexer.py`: `_run_index()` partitions files by classification, embeds to two collections, handles PDFs, discovers RDRs
- `commands/index.py`: `nx index code` renamed to `nx index repo`; `nx index rdr` stays as standalone convenience
- `config.py`: `_DEFAULTS` gets `indexing` section with `code_extensions`, `prose_extensions`, `rdr_paths`
- `registry.py`: registry entries track docs collection name alongside code collection name
- `spec.md`, `ARCHITECTURE.md`, `README.md`, `CLAUDE.md`: update references to `nx index repo`, document new collection scheme

### Unchanged

- `corpus.py`: `index_model_for_collection()` already correct by prefix
- `doc_indexer.py`: PDF and markdown chunking pipelines reused as-is
- `search_engine.py`: cross-corpus search works by collection prefix
- `ripgrep_cache.py`: builds from all text files regardless of classification
- `polling.py`: calls `index_repository()` which now does the right thing
- `db/t3.py`: no changes
- T2: completely untouched
- `nx index docs`, `nx index pdf`: unchanged

## Testing Strategy

### Unit Tests

- File classification: given extension + config overrides, assert correct class
- Config merge: `code_extensions` and `prose_extensions` precedence rules
- RDR path detection: given `.nexus.yml` rdr_paths, assert discovery and exclusion from `docs__`
- PDF routing: `.pdf` files go through `PDFExtractor`, land in `docs__`
- Pruning: files that change classification get removed from old collection
- Collection naming: `docs__repo-hash` uses same hash as `code__repo-hash`

### End-to-End Tests (real embeddings, real ChromaDB)

These tests use real Voyage AI embeddings and real ChromaDB (ephemeral client) to verify the full pipeline including embedding quality and retrieval correctness.

- **Code retrieval**: index a repo with `.py` and `.md` files, search with a code-specific query, verify code chunks rank higher from `code__` than prose chunks
- **Prose retrieval**: same repo, search with a natural language query, verify prose chunks from `docs__` rank appropriately
- **PDF retrieval**: repo containing a `.pdf`, verify extraction + chunking + embedding + retrieval works end-to-end
- **Cross-repo search**: index two repos, verify `--corpus code` searches code across both, `--corpus docs` searches prose across both
- **RDR discovery**: repo with `docs/rdr/*.md`, verify indexed into `docs__rdr__`, not `docs__repo-hash`
- **Classification override**: repo with `.nexus.yml` overriding an extension, verify chunks land in correct collection
- **Migration**: pre-populate `code__` with prose chunks (simulating old behavior), run unified indexer, verify prose moved to `docs__` and pruned from `code__`
- **Historical repo**: index a repo, delete the local clone, verify semantic search still returns results with correct metadata (source_path, git_remote_url, git_commit_hash)
- **Staleness**: index, modify a file, re-index, verify only changed file re-embedded (content_hash check)
- **Model recorded**: verify `embedding_model` metadata matches expected model per collection type

### Existing Tests

- `test_index_rdr_cmd.py`: unchanged
- Existing indexer tests: updated to expect new collection split
