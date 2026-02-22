# Nexus

## Vision

Nexus is a self-hosted semantic search and knowledge system that replaces expensive cloud ingest
(Mixedbread) with a locally-controlled indexing pipeline, while keeping ChromaDB in the cloud as
the permanent knowledge store. Embeddings and long-term storage are cloud-backed (Voyage AI,
ChromaDB cloud); the ingest, chunking, and retrieval logic run locally — no raw content leaves the
machine. It synthesizes the best of mgrep, SeaGOAT, and Arcaneum into a single, integrated tool
for Claude Code agents.

**North star**: Agents should be able to index, search, remember, and synthesize — cheaply,
without vendor lock-in, without a Swiss army knife.

## What We Keep from Each Tool

| Tool | What Nexus borrows |
|---|---|
| **mgrep** | UX patterns, citation format (`<cite i="N">`), watch mode, Claude Code SKILL.md integration |
| **SeaGOAT** | Git frecency scoring (`exp(-0.01 * days_passed)`), hybrid ripgrep+vector search, persistent server pattern |
| **Arcaneum** | PDF extraction + chunking pipeline (PDFExtractor, PDFChunker, OCREngine); Claude Code slash command plugin structure — **storage layer (Qdrant) and embedding layer (fastembed/local ONNX) are not borrowed; both are replaced** |
| **Mixedbread** | Read-only fan-out via `nx search --mxbai` for existing Mixedbread-indexed collections — zero new ingest spend |

> **Arcaneum clarification**: Arcaneum uses Qdrant as its vector backend and `fastembed`/SentenceTransformers
> (local ONNX, Jina/Stella models) for embeddings — no ChromaDB, no Voyage AI. Nexus borrows only the
> extraction and chunking logic (PyMuPDF4LLM, pdfplumber fallback, OCR, SemanticMarkdownChunker). The
> storage layer is reimplemented for ChromaDB; the embedding layer is replaced with Voyage AI. The `arc store`
> memory pattern is also not borrowed: Arcaneum explicitly persists content to local disk for re-indexability,
> which is the opposite of Nexus's design (vectors + chunk text only, no local raw copy).

## Architecture

```
Nexus
├── nx CLI (Python)               — primary interface, Claude Code plugin
│
├── Storage tiers
│   ├── T1: In-memory ChromaDB    — session scratch, agentic working state
│   ├── T2: Local SQLite          — memory bank replacement, structured + fast
│   └── T3: Cloud ChromaDB        — permanent knowledge, agent artifacts, planning docs
│
├── Indexing pipelines
│   ├── Code repos                — git frecency (SeaGOAT) + voyage-code-3 embeddings → T3
│   ├── PDFs / documents          — Arcaneum extraction/chunking logic + voyage-4 embeddings → T3
│   └── Markdown / notes          — Arcaneum markdown chunking logic + voyage-4 embeddings → T3
│
├── Search
│   ├── Semantic                  — ChromaDB (T1/T3 depending on scope)
│   ├── Full-text (code)          — ripgrep line cache locally (SeaGOAT pattern, no cloud needed)
│   └── Hybrid                    — semantic + ripgrep results merged and scored
│
└── Q&A                           — Haiku: search results → synthesis → cited answer
```

## Storage Tiers in Detail

### T1 — In-memory ChromaDB (session scratch)

- Lives only for the duration of a Claude Code session
- Used by agents for working state: hypotheses, intermediate findings, agentic search refinement
- Zero persistence cost; wiped on session end
- **Embedding strategy**: uses ChromaDB's bundled `DefaultEmbeddingFunction` (all-MiniLM-L6-v2, local ONNX) — NOT Voyage AI. T1 is session scratch; adding a network round-trip to every `nx scratch search` call defeats the purpose. Semantic fidelity is secondary to speed here.
- Accessed via `nx scratch put/get/search/list/clear`

### T2 — Local SQLite (memory bank replacement)

- Replaces the current MCP memory bank
- Survives restarts, no network dependency
- Accessed via `nx memory put/get/search`
- WAL mode enabled on open (`PRAGMA journal_mode=WAL`) — supports multiple concurrent readers (multiple Claude Code sessions) without writer blocking

#### TTL format

`--ttl` accepts: `Nd` (N days), `Nw` (N weeks), `permanent` or `never` (NULL). Examples: `30d`, `4w`, `permanent`. Default when omitted: `30d`.

#### Schema

```sql
-- Enable WAL for concurrent session access
PRAGMA journal_mode=WAL;

-- Main table: structured metadata with B-tree indexes
-- project + title are never tokenized (FTS5 would mangle 'BFDB_active' → 'BFDB' + 'active')
CREATE TABLE memory (
    id        INTEGER PRIMARY KEY,
    project   TEXT    NOT NULL,          -- namespace, e.g. 'BFDB_active'
    title     TEXT    NOT NULL,          -- filename/key, e.g. 'active-context.md'
    session   TEXT,                      -- Claude Code session ID (auto-captured)
    agent     TEXT,                      -- agent name (auto-captured)
    content   TEXT    NOT NULL,          -- full markdown text
    tags      TEXT,                      -- comma-separated tags
    timestamp TEXT    NOT NULL,          -- ISO 8601 write time
    ttl       INTEGER                    -- days from write; NULL = permanent
);

CREATE UNIQUE INDEX idx_memory_project_title ON memory(project, title);
CREATE INDEX        idx_memory_project       ON memory(project);
CREATE INDEX        idx_memory_agent         ON memory(agent);
CREATE INDEX        idx_memory_timestamp     ON memory(timestamp);

-- FTS5 virtual table: keyword search over content + tags only
-- External content mode — no duplication; project/title excluded intentionally
CREATE VIRTUAL TABLE memory_fts USING fts5(
    content,
    tags,
    content='memory',
    content_rowid='id'
);

-- Keep FTS5 in sync via triggers
CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
```

#### Query patterns

```sql
-- 1. Deterministic retrieval by name (primary pattern) — pure B-tree, O(log n)
SELECT * FROM memory WHERE project = ? AND title = ?;

-- 2. List all entries in a project
SELECT id, title, agent, timestamp FROM memory WHERE project = ? ORDER BY timestamp DESC;

-- 3. Keyword search across all projects — FTS5 only
SELECT m.* FROM memory m
JOIN memory_fts ON memory_fts.rowid = m.id
WHERE memory_fts MATCH ?
ORDER BY rank;

-- 4. Keyword search scoped to a project — FTS5 + B-tree filter combined
SELECT m.* FROM memory m
JOIN memory_fts ON memory_fts.rowid = m.id
WHERE memory_fts MATCH ?
  AND m.project = ?
ORDER BY rank;

-- 5. TTL expiry cleanup
DELETE FROM memory
WHERE ttl IS NOT NULL
  AND julianday('now') - julianday(timestamp) > ttl;
```

- Large analysis docs (300–500+ lines) store fine in SQLite TEXT
- For semantic search across large analysis docs, use T3 ChromaDB `knowledge__` collections instead (via `nx store` or `nx memory promote`)

### T3 — Cloud ChromaDB (permanent knowledge)

- Already running in the cloud — no new infra
- Stores: indexed code repos, indexed PDFs, long-term agent knowledge
- Collections namespaced by type: `code__{repo}`, `docs__{corpus}`, `knowledge__{topic}`
- Accessed via `nx search`, `nx store`, `nx index`

## Indexing Pipelines

### Server Architecture (nx serve — multi-repo)

`nx serve` is a single persistent Flask/Waitress process managing multiple repositories:

- **Repo registry**: `~/.config/nexus/repos.json` — list of registered repo paths with per-repo state
- `nx index code <path>` adds the path to the registry and triggers initial indexing
- Each repo has its own T3 collection (`code__{repo-name}`) and ripgrep line cache file
- HEAD polling runs per-repo every 10 seconds (configurable via `server.headPollInterval`); stale repos are re-indexed automatically
- **Concurrent access**: polling threads and Flask handlers share three resources requiring explicit locking: (1) `repos.json` — use `threading.RLock` + atomic write (`repos.json.tmp` → `os.replace()`); (2) ripgrep cache file — use per-repo `threading.RLock`; exclusive lock during rebuild, shared lock during hybrid search reads; (3) indexing progress counters — update atomically or hold the per-repo lock. Waitress runs with `threads=1` (eliminates Flask-level concurrency) but does not protect against concurrent polling threads
- Optional: `nx install claude-code` sets a post-commit hook as an additional trigger alongside polling
- `nx serve status` shows each repo's indexing state and estimated accuracy (SeaGOAT sigmoid pattern)

### Code Repositories

1. `nx index code <path>` registers the repo with the persistent `nx serve` process
2. `git log` to compute frecency scores per file: `sum(exp(-0.01 * days_passed))`
3. Files chunked: AST-first via `llama_index.core.node_parser.CodeSplitter` (which wraps `tree-sitter-language-pack` internally) for 30+ languages including Python, JS/TS, Java, Go, Rust, C, C++, C#, PHP, Ruby, Kotlin, Scala, Swift, and more — line-based fallback for unsupported extensions. Target ~150 lines per chunk; no overlap at function/class boundaries; 15% overlap for line-based fallback. Install: `pip install llama-index-core tree-sitter-language-pack`. **Version pinning required**: known breaking incompatibilities exist between these packages at certain version combinations (see llama_index issues #13521, #17567); pin to a verified-good pair in `pyproject.toml` and test before upgrading.
4. Chunks embedded via **Voyage AI** using `VoyageAIEmbeddingFunction(model_name="voyage-code-3")`
5. Upserted into T3 ChromaDB collection `code__{repo-name}`
6. Ripgrep line cache built locally: flat `path:line:content\n` text file, memory-mapped for hybrid search — 500MB cap (SeaGOAT pattern)
7. `nx serve` polls HEAD hash every 10 seconds (default, matching SeaGOAT's `SECONDS_BETWEEN_MAINTENANCE`); re-indexes on change. Configurable via `server.headPollInterval` in `~/.config/nexus/config.yml`.

ChromaDB natively supports `VoyageAIEmbeddingFunction` (`pip install voyageai`; env var: `VOYAGE_API_KEY`).

**Frecency score staleness**: `frecency_score` is computed and stored at index time. If a file has not changed (no content reindex) but other files in the repo have recent commits, its relative frecency score becomes stale. Known limitation: frecency-only reindex (updating scores without re-embedding) is not implemented in v1. Stable files may rank lower than their true recency warrants until the next content change triggers reindex.

**Ripgrep 500MB line cache**: The 500MB cap is a soft limit (matching SeaGOAT's `MAX_MMAP_SIZE`). When the cache file exceeds the cap, low-frecency files written last are omitted with a logged warning — they remain searchable via semantic search but not via ripgrep hybrid. This is not enforced as a hard error.

> **Model name verification**: Verify `voyage-code-3` and `voyage-4` against the current Voyage AI model
> catalog before use. The ChromaDB `VoyageAIEmbeddingFunction` default is `"voyage-01"` — names must
> be set explicitly. The SDK does not enumerate valid names at import time; an invalid name fails at the
> first API call.

### PDFs and Documents

1. `nx index pdf <path>` reads PDFs **directly from their source path** — no local copy stored
2. Text extracted and chunked in-process using **ported Arcaneum extraction logic**: PyMuPDF4LLM → markdown (primary), pdfplumber (complex tables fallback), Tesseract/EasyOCR (scanned fallback)
3. **Only the extracted text chunks + embeddings + metadata are stored in T3 ChromaDB** — raw PDF bytes never leave the machine
4. Chunks embedded via `VoyageAIEmbeddingFunction(model_name="voyage-4")`
5. Upserted into T3 collection `docs__{corpus-name}`

Arcaneum's extraction and chunking logic (PDFExtractor, PDFChunker, OCREngine) is **ported** — not imported as a library. The storage layer calls (Qdrant `PointStruct`, `upload_points`, scroll-based sync) must be rewritten as ChromaDB `collection.upsert()` calls. The embedding layer (`fastembed` local ONNX) is replaced with `VoyageAIEmbeddingFunction`. The extraction and chunking logic itself (PyMuPDF4LLM calls, pdfplumber fallback, OCR orchestration) ports with minimal changes.

Since ChromaDB stores the chunk text (`documents` field), result display and `--content` work without
re-reading the source file. Re-indexing (`nx index pdf <path>` again) requires the source path to still
be accessible — same as mgrep's `--sync`.

> **Re-embedding note**: Raw content is not stored locally. Re-embedding with a future model version
> requires re-reading the source files (acceptable for PDFs; they remain accessible). For `knowledge__`
> chunks from agent outputs (no source file), re-embedding is not possible without re-running the agent.

### Markdown / Notes

1. `nx index md <path>` with YAML frontmatter extraction
2. Semantic chunking preserving document structure (ported from Arcaneum's SemanticMarkdownChunker)
3. Incremental sync via SHA256 content hashing
4. Chunks embedded via `VoyageAIEmbeddingFunction(model_name="voyage-4")`, upserted into T3 `docs__{corpus-name}`

## ChromaDB Metadata Schema

ChromaDB metadata values are flat (`str | int | float | bool` only — no nested objects).
All structural parse context ("high context") from the extraction pipeline is preserved
as flat metadata fields alongside each chunk. This replicates the richness of Mixedbread's
`generated_metadata` and means search results carry full structural provenance.

### Document chunks (`docs__*` collections)

```
# Source identity
source_path          str   Absolute path to source file (PDF, markdown, etc.)
source_title         str   Title from PDF metadata or markdown frontmatter
source_author        str   Author from PDF metadata or frontmatter
source_date          str   Publication date (ISO 8601) from metadata
corpus               str   Collection name (e.g. "my-papers")
store_type           str   "pdf" | "markdown"

# Document structure
page_count           int   Total pages in document
page_number          int   Page this chunk is on (1-indexed; 0 if not applicable)
section_title        str   Nearest heading above this chunk (markdown-extracted)
format               str   "markdown" | "normalized" | "plain"
extraction_method    str   "pymupdf4llm_markdown" | "pymupdf_normalized" | "pdfplumber" | "ocr"

# Chunk position
chunk_index          int   Position of this chunk within the document (0-based)
chunk_count          int   Total chunks in this document
chunk_start_char     int   Character offset of chunk start in full document text
chunk_end_char       int   Character offset of chunk end

# Embedding provenance
embedding_model      str   "voyage-4"
indexed_at           str   ISO 8601 timestamp of indexing
content_hash         str   SHA256 of source file at index time (for change detection)
```

### Code chunks (`code__*` collections)

```
# Source identity
file_path            str   Absolute path to source file
filename             str   Basename (e.g. "auth.py")
file_extension       str   e.g. ".py", ".java"
programming_language str   e.g. "python", "java"
corpus               str   Collection name (e.g. "myrepo")
store_type           str   "code"

# Git context
git_project_name     str   Repository name
git_branch           str   Branch name
git_commit_hash      str   Full 40-char SHA at index time
git_remote_url       str   Sanitized remote origin URL

# File structure
line_start           int   First line of this chunk (1-indexed)
line_end             int   Last line of this chunk (1-indexed)
chunk_index          int   Position within file (0-based)
chunk_count          int   Total chunks in this file
ast_chunked          bool  True if AST parsing succeeded (vs line-based fallback)
has_functions        bool  Chunk contains function/method definitions
has_classes          bool  Chunk contains class definitions
has_imports          bool  Chunk contains import statements

# Frecency
frecency_score       float sum(exp(-0.01 * days_since_commit)) at index time

# Embedding provenance
embedding_model      str   "voyage-code-3"
indexed_at           str   ISO 8601 timestamp
content_hash         str   git object ID (for staleness detection)
```

### Knowledge / agent memory chunks (`knowledge__*` collections)

```
source_agent         str   Agent name that stored this (e.g. "codebase-deep-analyzer")
session_id           str   Claude Code session ID
title                str   Human-provided title (e.g. "Archive: myrepo" for pm-archive chunks)
category             str   e.g. "security", "architecture", "planning" — caller-provided via `--category <value>` on `nx store`; optional (empty string if omitted)
tags                 str   Comma-separated tags
store_type           str   "knowledge" | "pm-archive"
indexed_at           str   ISO 8601 timestamp of indexing
expires_at           str   ISO 8601 expiry timestamp; empty string = permanent
ttl_days             int   TTL in days at store time; 0 = permanent

# Additional fields set only when store_type = "pm-archive"
project              str   Repository/project name (e.g. "myrepo")
status               str   "completed" | "paused" | "cancelled"
archived_at          str   ISO 8601 timestamp of archive operation (same value as indexed_at for pm-archive)
phase_count          int   Number of phases reached at archive time
chunk_index          int   Only present when synthesis exceeds 1200 tokens and is split; 0-based index among split siblings
```

#### TTL sentinel translation (`nx memory promote`)

T2 SQLite uses `NULL` for permanent TTL. T3 knowledge__ uses `ttl_days=0` and `expires_at=""` for permanent. When `nx memory promote` copies a T2 entry to T3:
- T2 `ttl IS NULL` → T3 `ttl_days=0, expires_at=""`
- T2 `ttl = N` → T3 `ttl_days=N, expires_at=<ISO 8601 computed from timestamp + N days>`
- CLI keywords `permanent` and `never` both map to the NULL / 0 / "" sentinel.

## Search

### `nx search <query> [path]`

Core flags (all env-overridable, e.g. `NX_ANSWER=1`):

| Flag | Default | Description |
|---|---|---|
| `-a, --answer` | off | Synthesize answer via Haiku after search (not a separate command) |
| `-c, --content` | off | Show matched text inline under each result |
| `-m, --max-results N` | 10 | Maximum result lines |
| `--corpus <name>` | all | Scope to collection; repeatable (`--corpus code --corpus docs`) |
| `--hybrid` | off | Merge semantic + ripgrep results for code |
| `--mxbai` | off | Fan out to existing Mixedbread-indexed collections (read-only) |
| `--agentic` | off | Multi-step query refinement before returning results |
| `--no-rerank` | off | Disable result reranking |
| `-B N` | 3 | Lines of context above each result |
| `-A N` | 3 | Lines of context below each result |
| `-C N` | — | Lines of context above and below (shorthand for `-A N -B N`) |
| `-r, --reverse` | off | Reverse result order (most relevant at bottom) |
| `--no-color` | auto | Disable color/highlighting (auto-off in pipes) |
| `--vimgrep` | off | `path:line:col:content` format for editor integration |
| `--json` | off | Emit JSON for scripting |
| `--files` | off | Return unique file paths only, not individual lines |
| `--where <field>=<value>` | — | Filter results by ChromaDB metadata field; repeatable; maps to `where={field: value}` in the ChromaDB query. Examples: `--where store_type=pm-archive`, `--where status=completed`. Multiple flags are ANDed. |

`[path]` positional argument scopes results to files under that path (equivalent to Mixedbread's `starts_with` metadata filter).

### --corpus resolution

`--corpus <name>` uses **prefix matching** against collection names:
- `--corpus code` → all `code__*` collections
- `--corpus docs` → all `docs__*` collections
- `--corpus knowledge` → all `knowledge__*` collections
- `--corpus code__myrepo` → exactly the `code__myrepo` collection (fully-qualified)

When multiple `--corpus` flags are used, each corpus is queried separately (they may use different embedding models), results are combined, then reranked — see Cross-corpus search.

### Cross-corpus search

`code__*` collections use `voyage-code-3`; `docs__*` and `knowledge__*` use `voyage-4`. These embedding spaces are not directly comparable — similarity scores across models are meaningless when combined naively.

Resolution strategy:
1. Each corpus queried independently using its own embedding function
2. Top-k results retrieved per corpus: each corpus fetches `max(5, (max_results // num_corpora) * 2)` results. Over-fetching is harmless since reranking follows; the multiplier ensures sufficient candidates for the reranker even with many corpora.
3. Combined result set reranked using `voyageai.Client().rerank(query=query, documents=[c.text for c in combined], model="rerank-2.5", top_k=max_results)` to produce a unified ranked list. `rerank-2.5` verified against voyageai.com/docs/pricing 2026-02-21 ($0.05/1M tokens; 200M free). `rerank-2.5-lite` ($0.02/1M) is a lower-cost alternative configurable via `embeddings.rerankerModel`.
4. `--no-rerank` skips step 3 and interleaves results round-robin instead

`min_max_normalize` in hybrid scoring is computed over the **combined result set** after per-corpus retrieval, not per-corpus windows. This preserves relative quality differences across corpora (e.g., high-confidence code results vs lower-confidence doc results remain distinguishable after normalization).

### Search output format

Default (color terminal): uses `bat` for syntax-highlighted line rendering if installed, falls back to `pygments`.

Plain format (no-color / pipe):
```
./path/to/file.py:42:    def authenticate(user, token):
./path/to/file.py:43:        return validate_token(token)
```

Vimgrep format: `path:line:0:content`

### Hybrid search scoring (code)

```
# Per-chunk score combining vector similarity and file frecency (code__ corpora only)
vector_norm   = min_max_normalize(cosine_similarity, combined_result_window)   # → [0, 1]
frecency_norm = min_max_normalize(file_frecency_score, combined_result_window)  # unbounded → [0, 1]
score = 0.7 * vector_norm + 0.3 * frecency_norm

# For docs__ and knowledge__ chunks: frecency_score is undefined (not in their metadata schema).
# --hybrid is ignored for non-code corpora; their score = 1.0 * vector_norm.
# If --hybrid is used but NO code__ corpus is in the search scope, a warning is printed:
#   "Warning: --hybrid has no effect — no code corpus in scope."
# --hybrid --corpus code --corpus docs applies frecency only to code results (no warning printed).

# min_max_normalize(x, window): (x - min) / (max - min + ε)
#   computed over the COMBINED result window across all corpora (not per-corpus)
# frecency_score is per-file; all chunks from the same file share the file's frecency score
# Ripgrep exact-match chunks: vector_norm is set to 1.0 before the weighted sum
```

### Answer mode (`-a / --answer`)

`nx search "how does auth work" --corpus code -a`

- Same query drives both retrieval and synthesis (no separate command)
- Top-k results passed to Haiku with instruction to cite sources
- Output:

```
Authentication is handled in two layers: token validation <cite i="0"> and
session management <cite i="1-2">.

0: ./auth/validator.py:42-67 (94.3% match)
   def validate_token(token):
       ...
1: ./auth/session.py:12-34 (87.1% match)
2: ./auth/session.py:89-102 (81.5% match)
```

- `<cite i="N">` single source; `<cite i="N-M">` range of consecutive sources
- `--content` shows matched text under each cited source

### Agentic mode (`--agentic`)

Multi-step query refinement loop (max 3 iterations) powered by Haiku:
1. Initial query → retrieve top results
2. Haiku reads results, responds with JSON `{"done": true}` (sufficient) or `{"query": "<refined query>"}` (continue)
3. Refined query → retrieve additional results → merge + deduplicate → repeat up to 3 total iterations
4. Final combined result set returned (reranked if applicable)

Uses `ANTHROPIC_API_KEY` via the `anthropic` Python SDK. Pairs with `--answer` for synthesis after retrieval.

### Mixedbread fan-out (`--mxbai`)

Fan-out to existing Mixedbread-indexed collections (read-only). Python SDK implementation:

```python
from mixedbread import Mixedbread
client = Mixedbread(api_key=os.environ["MXBAI_API_KEY"])
# Per store (from mxbai.stores config list):
results = client.stores.search(store_id=store_id, query=query, top_k=per_corpus_k)
# Result format: results.chunks[i].content.text, results.chunks[i].score
```

Mxbai results are converted to Nexus result objects and included in the combined result set before the Voyage AI reranker step. Multiple stores (from `mxbai.stores` config) are queried with the same `per_corpus_k` over-fetch. If `MXBAI_API_KEY` is unset: print warning `"Warning: MXBAI_API_KEY not set — skipping Mixedbread fan-out"` and skip. `nx doctor` verifies Mixedbread SDK auth when `--mxbai` has been configured.

## Session Scratch (`nx scratch`)

T1 in-memory ChromaDB, cleared at session end:

```bash
nx scratch put "content" --tags "hypothesis,phase1"
nx scratch put "content" --tags "finding" --persist   # flag for auto-flush to T2 on SessionEnd (auto-destination)
nx scratch put "content" --tags "finding" --persist --project BFDB_active --title findings.md   # explicit T2 destination
nx scratch get <id>
nx scratch search "query"
nx scratch list
nx scratch flag <id>                                          # mark for SessionEnd flush; destination: project=scratch_sessions, title={session_id}_{id}
nx scratch flag <id> --project BFDB_active --title findings.md  # explicit T2 destination
nx scratch unflag <id>                     # unmark (clears destination)
nx scratch clear                           # explicit clear; also happens automatically on SessionEnd
nx scratch promote <id> --project BFDB_active --title findings.md   # → T2 immediately (manual)
```

- Uses `DefaultEmbeddingFunction` (local ONNX, no API call) — fast, no network dependency
- Session ID: generated as a UUID4 by the SessionStart hook and written to `~/.config/nexus/sessions/{ppid}.session` (PID-scoped, where `ppid` is the Claude Code process PID obtained via `os.getppid()` in the hook subprocess). `nx` subcommands discover their session by reading `~/.config/nexus/sessions/{os.getppid()}.session`. Using a per-PID path prevents concurrent Claude Code windows from overwriting each other's session ID (a shared `current_session` file would cause window B's hook to overwrite window A's ID, corrupting T1 metadata and potentially triggering deletion of the wrong window's scratch entries). Orphaned session files are cleaned up by the SessionEnd hook. `CLAUDE_SESSION_ID` does **not** exist in Claude Code (open feature requests #13733, #17188 — unresolved as of 2026-02). T1 is a shared EphemeralClient; session ID is stored as metadata on each T1 document, enabling per-session filtering. When flagged entries are flushed to T2 with no explicit destination, they go to project `scratch_sessions`, title `{session_id}_{id}`.
- On crash: T1 is in-memory; data is lost by design — scratch is ephemeral

## Memory Bank Replacement

`nx memory` replaces the MCP memory bank:

```bash
# Write — named file within a project (maps to memory bank's project+filename key)
nx memory put "content" --project BFDB_active --title active-context.md --tags "phase1" --ttl 30d
echo "# Findings..." | nx memory put - --project BFDB_active --title findings.md  # stdin (requires --title)

# Read by name (primary access pattern — deterministic)
nx memory get --project BFDB_active --title active-context.md

# Read by ID (secondary)
nx memory get <id>

# Keyword search via FTS5
nx memory search "query"
nx memory search "memory leak" --project Prime-Mover_active

# List entries
nx memory list --agent codebase-analyzer
nx memory list --project BFDB_active

# Promote to T3 for semantic search
nx memory promote <id> --collection knowledge --tags "architecture"

# Housekeeping
nx memory expire          # clean up TTL-expired entries
```

- Backed by T2 SQLite with FTS5 for keyword search
- Schema key is `(project, title)` for deterministic retrieval — mirrors current memory bank's project + filename pattern
- Agent name, session ID, timestamp captured automatically
- Content can be any size; 30–500+ line markdown docs all store fine
- For semantic search across large analysis docs, `nx store` (or `nx memory promote`) persists to T3 ChromaDB `knowledge__` collections

## Project Management Infrastructure (`nx pm`)

`nx pm` provides first-class support for the structured `.pm/` project management infrastructure used by Claude Code agents. Active PM documents live in T2 (`nx memory`), under the `{repo}_pm` project namespace — no new storage tier for day-to-day use. However, three commands touch T3 (ChromaDB cloud): `nx pm archive` (writes synthesis chunk), `nx pm reference` (queries archived syntheses), and `nx pm promote` (elevates PM docs to semantic search). T3 access requires `CHROMA_API_KEY` and a live network connection; the T2-only commands (`init`, `resume`, `status`, `phase`, `search`, `expire`, `restore`) work fully offline.

### Why T2 is the natural fit

The `.pm/` directory is a named-file project workspace — exactly the model `nx memory` implements. Nexus replaces the raw filesystem with T2 and adds:

- **FTS5 keyword search** across all PM docs (fast, no API call): `nx pm search "caching decision"`
- **Cross-project search**: `nx memory search "database schema"` finds decisions across every project's PM namespace
- **TTL management**: phase docs can auto-expire when a project closes
- **Agent provenance**: every write records which agent and session produced the content
- **On-demand semantic search**: `nx pm promote` pushes PM docs to T3 `knowledge__pm__*` for cross-project semantic queries

T1 (in-memory) is not used — PM docs must survive restarts. T3 is opt-in via `nx pm promote`, not default.

### Convention

PM projects use the `{repo}_pm` namespace in T2. Tags follow the pattern `pm,phase:N,<doc-type>` for filtered listing.

| Old `.pm/` file | T2 equivalent |
|---|---|
| `.pm/CONTINUATION.md` | `nx memory get --project {repo}_pm --title CONTINUATION.md` |
| `.pm/METHODOLOGY.md` | `nx memory get --project {repo}_pm --title METHODOLOGY.md` |
| `.pm/AGENT_INSTRUCTIONS.md` | `nx memory get --project {repo}_pm --title AGENT_INSTRUCTIONS.md` |
| `.pm/CONTEXT_PROTOCOL.md` | `nx memory get --project {repo}_pm --title CONTEXT_PROTOCOL.md` |
| `.pm/phases/phase-1/context.md` | `nx memory get --project {repo}_pm --title phases/phase-1/context.md` |

### Commands

```bash
# Scaffold standard PM docs (replaces project-management-setup agent writing .pm/ files)
# --project is optional; auto-detected via: basename $(git rev-parse --show-toplevel)
# Falls back to basename of cwd for non-git directories.
# If auto-detection is ambiguous (e.g. monorepo), --project must be supplied explicitly.
#
# Documents created (all: ttl=permanent, tags=pm):
#   CONTINUATION.md          — "# Continuation\n\nProject: {repo}\nCreated: {date}\n\n## Current State\n(Fill in)\n\n## Next Action\n(Fill in)"
#   METHODOLOGY.md           — engineering methodology and workflow (standard content embedded in binary)
#   AGENT_INSTRUCTIONS.md    — "# Agent Instructions\n\nRead CONTINUATION.md first. Use nx pm commands for all PM operations. ..."
#   CONTEXT_PROTOCOL.md      — "# Context Protocol\n\n## Storage Hierarchy\n1. Beads — task tracking ..."
#   phases/phase-1/context.md — "# Phase 1 Context\n\n(Describe phase goals and current state here.)" (tags=pm,phase:1,context)
# Templates are embedded in the nx binary; no external template files are required.
#
# The phase-1 doc ensures MAX(phase tag integer) = 1 on a fresh project so that
# `nx pm phase next` works immediately after init without returning NULL.
nx pm init [--project myrepo]

# Session resumption — outputs CONTINUATION.md content for session injection
nx pm resume [--project myrepo]

# Human-readable status: current phase, last-updated agent, open blockers
# - Current phase: MAX(phase tag integer) across pm-tagged docs
# - Last-updated agent: agent field on most recently written T2 entry in the project
# - Open blockers: bullet list from {repo}_pm/BLOCKERS.md in T2
#     missing BLOCKERS.md → "none" (treat absent as zero blockers, same as empty)
nx pm status [--project myrepo]

# Blocker management (appends bullet to BLOCKERS.md; creates it if absent)
nx pm block "waiting on ChromaDB cloud credentials"
nx pm unblock 1              # remove blocker by line number (as shown by nx pm status)

# Phase management
nx pm phase 2                          # retrieve phase-2 context doc
nx pm phase next                       # transition to next phase:
                                       #   1. reads current N as MAX(phase tag integer) across all docs tagged phase:N in the project
                                       #   2. creates new T2 entry title=phases/phase-{N+1}/context.md,
                                       #      tags=pm,phase:{N+1},context, ttl=permanent
                                       #      initial content: "# Phase {N+1} Context\n\n(Describe phase goals and current state here.)\n\nPrevious phase: {N}"
                                       #   3. updates CONTINUATION.md to reference phase N+1
                                       #   (does NOT mass-update tags on existing docs)

# FTS5 keyword search scoped to PM docs (no API call)
# Without --project: searches all T2 entries WHERE project GLOB '*_pm'
#   (PM namespaces only — does not bleed into BFDB_active or other non-PM projects)
#   Note: GLOB not LIKE — SQLite LIKE's `_` matches any single char; GLOB's `_` is literal.
# With --project: adds AND project = '{repo}_pm'
nx pm search "what did we decide about caching"
nx pm search "auth" --project myrepo   # scoped to one project

# Promote PM docs to T3 for cross-project semantic search
nx pm promote --collection knowledge --tags "decision,architecture"

# Lifecycle cleanup
nx pm expire                           # remove TTL-expired phase docs
```

### SessionStart hook — PM-aware behavior

The canonical SessionStart hook definition is in the "Agent integration installers" section under Management Commands. Summary: PM detection runs a T2 SQL query (not a filesystem check); if `{repo}_pm/CONTINUATION.md` exists in T2, its content is injected (2000 char cap); otherwise the generic 10-entry memory summary is printed.

### Slash commands

```
/nx:pm resume          — inject CONTINUATION.md into session context
/nx:pm status          — show phase, blockers, last-agent summary
/nx:pm phase next      — advance to next phase
/nx:pm search <query>  — FTS5 search across PM docs
/nx:pm archive         — archive current project (synthesize → T3, decay T2)
/nx:pm close           — alias for /nx:pm archive --status completed
/nx:pm restore <proj>  — restore archived project (within decay window)
/nx:pm reference <q>   — semantic search across all archived project syntheses
```

### Lifecycle management

PM infrastructure has three distinct states:

```
Active ──── nx pm archive ──── Archived (T2, 90d decay) + Synthesis (T3, permanent)
  ↑                                       │ within 90d
  └──────── nx pm restore ────────────────┘
                                           │ after 90d (T2 expired)
                                           └── Synthesis only (T3, permanent)
```

**Active**: T2 `{repo}_pm` namespace, permanent TTL — hot path, fully editable.

**Archive**: `nx pm archive` is a two-phase operation (T2 SQLite and T3 ChromaDB cloud have no shared transaction coordinator — true atomicity is impossible):

1. **Synthesize → T3 first**: Haiku reads all PM docs from T2. Selection: always include the 5 standard init docs (CONTINUATION.md, METHODOLOGY.md, AGENT_INSTRUCTIONS.md, CONTEXT_PROTOCOL.md, phases/phase-1/context.md), then fill remaining capacity with other docs sorted by most-recently-written. Overall cap: 100 docs or 100K total characters (whichever is smaller). Produces a structured synthesis chunk stored in `knowledge__pm__{repo}`. The T3 `title` field is set to `"Archive: {repo}"`.
   - If Haiku synthesis **fails** (API error, rate limit, content policy): the archive is aborted. T2 is left untouched. The error is printed; the user can retry. This is the safe failure mode — raw PM docs remain accessible.
   - If T3 **write fails** after synthesis: same abort behavior.
2. **Decay T2 second**: Only after T3 write succeeds. Runs as a single SQLite transaction: `UPDATE memory SET ttl = {NX_PM_ARCHIVE_TTL}, tags = replace(tags, 'pm,', 'pm-archived,') WHERE project = '{repo}_pm'`. If this T2 update fails after T3 succeeds: the T3 chunk is orphaned but not harmful — a retry of `nx pm archive` checks for an existing T3 chunk with `title = "Archive: {repo}"` before synthesizing. Idempotency check: query T3 for `title="Archive: {repo}"` ordered by `indexed_at DESC LIMIT 1`; compare `pm_doc_count` and `pm_latest_timestamp` metadata fields against current T2 state (count of active PM docs and MAX(timestamp) of those docs). If they match, the existing synthesis is current — skip re-synthesis regardless of age and proceed directly to the T2 decay step. A time-based window (e.g., "skip if written within 5 minutes") is insufficient because a user investigating a crash for longer than the window would trigger duplicate synthesis accumulation. The T3 synthesis chunk must carry `pm_doc_count` (int) and `pm_latest_timestamp` (ISO 8601 string) metadata fields for this check.

`NX_PM_ARCHIVE_TTL` accepts an **integer number of days** (e.g. `90`). Default: `90`. Same unit as the T2 `ttl` column.

**Restore**: `nx pm restore <project>` reverses step 2 within the decay window:
```sql
UPDATE memory
   SET ttl = NULL,
       tags = replace(tags, 'pm-archived,', 'pm,')
 WHERE project = '{repo}_pm'
```
- Does **not** delete the T3 synthesis chunk (it remains as a reference point).
- If some but not all docs have already TTL-expired (partial decay): restores surviving docs; prints a warning listing the expired titles. Does not abort.
- If all docs have expired: fails with "raw docs fully expired — use `nx pm reference {project}` to access the synthesis". Suggests re-running `nx pm init` if the project is being restarted.
- Re-archiving a restored project creates a new T3 synthesis chunk. Older synthesis chunks for the same project accumulate as historical records (no automatic deduplication). To inspect: `nx pm reference {project}` (bare-identifier dispatch lists all synthesis chunks). To wipe all syntheses for a project: `nx collection delete knowledge__pm__{project} --confirm` (nuclear option — no undo).

#### Archive synthesis format

Haiku is prompted to extract from all PM docs:

```
# Project Archive: {repo}
Status: completed | paused | cancelled
Date Range: {started_at} → {archived_at}
Final Phase: N of M

## Key Decisions
- [concise decision + rationale, one line each]

## Architecture Choices
- [structural choices that future projects should know about]

## Challenges & Resolutions
- [non-obvious problems encountered + how resolved]

## Outcome
[2-3 sentences: what was built, current state, notable gaps]

## Lessons Learned
- [concrete, reusable takeaways]
```

Target size: **400–800 tokens, hard cap 1200 tokens** — aim for one ChromaDB chunk. The Haiku prompt instructs it to use brief bullets (one line per item). If the synthesis exceeds 1200 tokens, it is split at section boundaries into at most 3 chunks (Key Decisions + Architecture, Challenges + Outcome, Lessons Learned), each stored as a separate T3 document with an additional `chunk_index` metadata field. Semantically rich so vector search finds it on topical queries even without knowing the project name.

T3 metadata: `store_type="pm-archive"`, `project="{repo}"`, `status="completed|paused|cancelled"`, `archived_at` (ISO 8601, same as `indexed_at`), `phase_count` (int), `ttl_days=0` (permanent).

Template variable `{started_at}` in the Haiku prompt is computed as `MIN(timestamp) WHERE project = '{repo}_pm'` from T2 — the timestamp of the oldest PM doc in the project.

#### Archive and restore commands

```bash
# Archive current project (synthesize → T3 + start T2 decay)
# --status defaults to "completed" if omitted
nx pm archive [--project myrepo] [--status completed|paused|cancelled]

# Archive + mark complete (alias for: nx pm archive --status completed)
nx pm close [--project myrepo]

# Restore from archived T2 docs (within decay window only)
nx pm restore <project>

# Query institutional memory — semantic search across all archived syntheses
nx pm reference                      # prompts for query; semantic search across all pm-archives
nx pm reference "caching decisions"  # direct semantic query
nx pm reference myrepo               # retrieve by project name (uses --where project=, not semantic)
```

`nx pm reference` dispatch rules:
- **No argument**: prompts interactively, then runs semantic query
- **Quoted string or contains spaces/`?`**: treated as semantic query → `nx search <query> --corpus knowledge --where store_type=pm-archive`
- **Bare identifier (no spaces, no `?`)**: treated as project name → metadata-only lookup via `collection.get(where={"store_type": "pm-archive", "project": "<arg>"})` — no embedding call, no `query_texts` parameter

The `--where` flag maps to ChromaDB `where={"store_type": "pm-archive", ...}` filters. This is the institutional memory query point: *"how did we handle rate limiting in past projects?"* finds the right synthesis even without knowing which project to look in.

#### Why raw PM docs don't go to T3

Storing all 50 phase docs raw in T3 would:
- Pollute semantic search results across unrelated queries
- Waste Voyage AI token budget embedding ephemeral drafts and intermediate notes
- Produce many near-duplicate chunks (phase docs iterate on each other)

The synthesis-on-archive pattern distills signal from noise: one chunk per completed project, semantically rich, permanently queryable.

### What changes for the `project-management-setup` agent

The agent calls `nx pm init` instead of writing `.pm/*.md` files directly. The `.pm/` directory is no longer required on disk. Agents that previously read `cat .pm/CONTINUATION.md` use `nx pm resume` instead (or the SessionStart hook injects it automatically). The `/pm-archive`, `/pm-restore`, and `/pm-close` slash commands map directly to `nx pm archive`, `nx pm restore`, and `nx pm close`.

## Agent Memory (`nx store`)

For agent outputs that should persist beyond a session:

```bash
nx store analysis.md --collection knowledge --tags "security,audit"
nx store analysis.md --collection knowledge --tags "security,audit" --ttl 90d
echo "# Findings..." | nx store - --collection knowledge --title "Auth Analysis"
nx search "security vulnerabilities" --corpus knowledge

# Lifecycle management
nx store expire           # remove knowledge__ chunks whose expires_at has passed
                          # Implementation: collection.get(where={"$and": [{"ttl_days": {"$gt": 0}},
                          #   {"expires_at": {"$ne": ""}},
                          #   {"expires_at": {"$lt": <current ISO time>}}]})
                          # Required guard: expires_at="" for permanent entries sorts BEFORE any ISO
                          # timestamp lexicographically — omitting {"expires_at": {"$ne": ""}} causes
                          # permanent entries to be deleted silently on every expire call.
                          # Automated: nx serve schedules this daily; also run by SessionEnd hook.
```

- `--ttl` format: `Nd`, `Nw`, or `permanent` (default: `permanent` for `nx store`; `30d` for `nx memory`)
- Stored chunks carry `expires_at` and `ttl_days` metadata fields for cleanup
- stdin (`-`) requires `--title` to be provided
- **No local copy**: only vectors + chunk text land in ChromaDB. Re-embedding with a future model requires re-running the agent or re-reading the source.

This replaces the current pattern of agents manually writing to memory bank files.

## Management Commands

### Server lifecycle

```bash
nx serve start [--port N]    # start persistent server (background)
nx serve stop
nx serve status              # show indexed repos, accuracy %, uptime
nx serve logs                # tail log file: ~/.config/nexus/serve.log
```

`nx serve start` daemonizes using `subprocess.Popen(..., start_new_session=True)` — no double-fork, no external process manager required. PID is written to `~/.config/nexus/server.pid`. On start, if a stale PID file exists, the process is checked via `kill(pid, 0)` — if not running the stale file is removed; if running the start is a no-op. `nx serve stop` sends `SIGTERM` to the PID and removes the file.

Accuracy display while indexing (SeaGOAT pattern):
```
Warning: Nexus is still analyzing your repository.
Results have an estimated accuracy of 73%.
```

### Collection management

```bash
nx collection list                        # all T3 collections with doc counts
nx collection info <name>                 # size, embedding model, last indexed
nx collection delete <name> [--confirm]
nx collection verify <name>               # spot-check embeddings health
```

### Configuration

```bash
nx config show                            # current config (merged global + repo)
nx config set server.port 7890
```

Per-repo config: `.nexus.yml` (same merge pattern as SeaGOAT's `.seagoat.yml`).
Global config: `~/.config/nexus/config.yml`.

Key settings: `server.port`, `server.ignorePatterns`, `embeddings.codeModel` (default: `voyage-code-3`),
`embeddings.docsModel` (default: `voyage-4`), `embeddings.rerankerModel` (default: `rerank-2.5`),
`pm.archiveTtl` (default: `90`, days; overridable via `NX_PM_ARCHIVE_TTL` env var),
`mxbai.stores` (list of Mixedbread store identifiers to query when `--mxbai` is used, e.g. `["art", "docs"]`; required for `--mxbai` — if unset, `--mxbai` prints a warning and skips fan-out),
`chromadb.tenant`, `chromadb.database`, `client.host`, `server.headPollInterval` (default: `10`).

Required env vars (not stored in config files): `CHROMA_API_KEY`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`.
Optional env var: `MXBAI_API_KEY` (required only when `--mxbai` is used; if unset, `--mxbai` prints a warning and skips fan-out).

Convenience env var overrides:

| Variable | Effect |
|---|---|
| `NX_ANSWER` | Set to any non-empty value (e.g. `1`) to enable `--answer` mode globally for `nx search` |
| `NX_PM_ARCHIVE_TTL` | Override PM archive TTL (integer days; default: `90`) |
| `NX_SERVER_PORT` | Override server port (default: `7890`) |
| `NX_SERVER_HEAD_POLL_INTERVAL` | Override head poll interval in seconds (default: `10`) |
| `NX_EMBEDDINGS_CODE_MODEL` | Override code embedding model (default: `voyage-code-3`) |
| `NX_EMBEDDINGS_DOCS_MODEL` | Override docs embedding model (default: `voyage-4`) |
| `NX_EMBEDDINGS_RERANKER_MODEL` | Override reranker model (default: `rerank-2.5`) |
| `NX_CLIENT_HOST` | Override client host for server connection (default: `localhost`) |

### Health check

```bash
nx doctor    # verify: nx serve running, ChromaDB cloud reachable (CHROMA_API_KEY set),
             #         Voyage API key valid (VOYAGE_API_KEY set),
             #         Anthropic API key valid (ANTHROPIC_API_KEY set),
             #         ripgrep on PATH, git available,
             #         Mixedbread SDK authenticated (only when --mxbai has been used;
             #           if --mxbai is used without SDK auth: warning printed, mxbai results silently skipped)
```

### Agent integration installers

```bash
nx install claude-code      # install SKILL.md + SessionStart/SessionEnd hooks
nx uninstall claude-code
nx install codex            # future integrations
```

`nx install claude-code` writes:
- `~/.claude/skills/nexus/SKILL.md` — agent usage guide (how to use `nx search`, `nx memory`, `nx store`, `nx scratch`, `nx pm`)
- SessionStart hook entry in `~/.claude/settings.json`: initialize T1 scratch; PM-aware context injection (see below)
- SessionEnd hook entry: flush T1 scratch entries that have a T2 destination (explicit project+title, or auto-destination `scratch_sessions/{session_id}_{id}`) to T2; run `nx memory expire` and `nx store expire`

**SessionStart hook — canonical behavior** (single definition; the `nx pm` section references this):

PM detection is a T2 SQL query, not a filesystem check:
```sql
SELECT 1 FROM memory WHERE project = '{repo}_pm' AND title = 'CONTINUATION.md' LIMIT 1
```
where `{repo}` is auto-detected from `git rev-parse --show-toplevel | xargs basename`.

```
Nexus ready. T1 scratch initialized (session: {session_id}).

# If {repo}_pm CONTINUATION.md found in T2 (PM project):
{CONTINUATION.md content, capped at 2000 chars}

# Otherwise (non-PM project):
Recent memory ({project}, last 10 entries):
  - {title} ({agent}, {N}d ago)
  ...
  [capped at 10 entries × 500 chars each]
```

## Claude Code Integration

- Claude Code **plugin** with slash commands (Arcaneum plugin structure: `.claude-plugin/plugin.json` + `commands/*.md`)
- **SKILL.md** for agents to understand how to use Nexus (mgrep pattern)
- **SessionStart hook**: initialize T1 scratch; inject CONTINUATION.md if PM project detected (capped at 2000 chars); otherwise print T2 memory summary (capped at 10 entries × 500 chars)
- **SessionEnd hook**: flush T1 scratch to T2 if flagged, expire old T2 entries

### Slash commands

```
/nx:search <query>              — semantic search across T3
/nx:search <query> --hybrid     — hybrid search (semantic + ripgrep)
/nx:search <query> --mxbai      — include Mixedbread read-only fan-out
/nx:search <query> -a           — search + Haiku answer synthesis
/nx:store <content>             — persist to T3 knowledge
/nx:memory <content>            — write to T2 SQLite
/nx:index code <path>           — index a code repo (registers with nx serve)
/nx:index pdf <path>            — index PDFs
/nx:scratch <content>           — write to T1 (session only)
/nx:doctor                      — health check
/nx:pm resume                   — inject PM CONTINUATION.md into session context
/nx:pm status                   — show current phase, last-agent, open blockers
/nx:pm phase next               — advance to next project phase
/nx:pm search <query>           — FTS5 keyword search across PM docs
/nx:pm archive                  — synthesize project → T3, start T2 decay
/nx:pm close                    — archive + mark completed
/nx:pm restore <project>        — restore archived project (within decay window)
/nx:pm reference [<query>]      — semantic search across all archived project syntheses
```

## What's Out of Scope

- **General web search** — `--mxbai` provides fan-out to existing Mixedbread-indexed collections (including the Mixedbread public web corpus if included in the user's plan) but is not a general-purpose live web search engine
- **Auth / OAuth** — no cloud vendor account management
- **MeiliSearch** — ripgrep handles code full-text locally; not worth running another service
- **Qdrant** — ChromaDB is already in the cloud; one vector backend only
- **mgrep `--sync` / file upload model** — replaced by `nx index` pipelines
- **Real-time collaboration / multi-user** — single-user local tool
- **Collection export/import** — future if needed; out of scope v1
- **`nx watch` / file-watch mode** — out of scope v1; `nx index` with HEAD polling covers the auto-reindex use case for code; inotify/FSEvents file watching for markdown/PDFs is future work

## Technologies

- **Python 3.12+** — CLI, server, indexing pipelines, SeaGOAT frecency logic
- **ChromaDB** — T1 (`chromadb.EphemeralClient` + `DefaultEmbeddingFunction`) and T3 (`chromadb.CloudClient(tenant=..., database=..., api_key=CHROMA_API_KEY)` → ChromaDB cloud + `VoyageAIEmbeddingFunction`)
- **SQLite + FTS5** — T2 memory bank (stdlib `sqlite3`, WAL mode, no ORM)
- **Voyage AI** — embedding API: `voyage-code-3` for code, `voyage-4` for docs/PDFs; reranker: `rerank-2.5` for cross-corpus result merging (verified against voyageai.com/docs/pricing 2026-02-21)
  - **Verified model names**: `voyage-code-3` ✓, `voyage-4` ✓, `rerank-2.5` ✓ (also available: `rerank-2.5-lite` at lower cost/quality). ChromaDB wrapper default is `"voyage-01"` — names must be set explicitly; SDK accepts any string and fails at first API call with an invalid name.
  - **Free tier**: 200M tokens/month for all current-gen models including rerankers (verified 2026-02-21). Applies to `voyage-4`, `voyage-code-3`, `rerank-2.5`, and `rerank-2.5-lite`.
  - **Pricing beyond free tier**: `voyage-code-3` $0.18/1M tokens; `voyage-4` $0.06/1M tokens; `rerank-2.5` $0.05/1M tokens; `rerank-2.5-lite` $0.02/1M tokens. Batch API gives 33% discount.
  - Env var: `VOYAGE_API_KEY`; native `VoyageAIEmbeddingFunction` in ChromaDB (`pip install voyageai`); checks `VOYAGE_API_KEY` first, then `CHROMA_VOYAGE_API_KEY` — no custom glue
- **Claude Haiku** (`claude-haiku-4-5-20251001`) — Q&A synthesis via `anthropic` Python SDK
- **Mixedbread SDK** — read-only fan-out for existing Mixedbread-indexed collections (`--mxbai` flag)
- **ripgrep** — full-text code search via flat mmap line cache (local, 500MB cap)
- **Git** — frecency computation from commit history
- **Flask + Waitress** — persistent `nx serve` process (SeaGOAT pattern)
- **PyMuPDF4LLM + pdfplumber + Tesseract/EasyOCR** — PDF extraction (ported from Arcaneum)
- **tree-sitter + llama-index-core** — AST-based code chunking; uses `llama_index.core.node_parser.CodeSplitter` as the interface (which wraps `tree-sitter-language-pack` internally); installing bare `tree-sitter` alone is not sufficient. **Version pinning required** in `pyproject.toml` — known breaking incompatibilities exist between package versions (issues #13521, #17567 in llama_index repo)
- **Environment variables**: `VOYAGE_API_KEY` (embeddings + reranker), `CHROMA_API_KEY` (ChromaDB cloud auth — required for all T3 operations), `ANTHROPIC_API_KEY` (Haiku synthesis)

## Decisions Log

| # | Decision | Rationale |
|---|---|---|
| 1 | Voyage AI embedding API (not local ONNX) | Eliminates ~2GB model downloads and GPU setup complexity. **Verified** (2026-02-21): free tier is 200M tokens/month for all current-gen models (`voyage-4`, `voyage-code-3`, `rerank-2.5`, `rerank-2.5-lite`). Beyond free tier: $0.18/1M (code), $0.06/1M (docs), $0.05/1M (reranker). Re-verify at voyageai.com/docs/pricing if significant time has passed — free tier terms have changed historically. |
| 2 | Persistent `nx serve` process | Faster repeated queries; ripgrep line cache stays warm; HEAD polling for auto-reindex |
| 3 | SQLite T2 = memory bank only | Don't over-engineer; T3 ChromaDB handles knowledge storage naturally |
| 4 | Mixedbread fan-out via `--mxbai` flag on `nx search` | Opt-in so normal searches stay fully local; `nx ask` is not a separate command — answer synthesis is `-a` on `nx search` |
| 5 | HEAD detection via 10s polling in `nx serve` | Default matches SeaGOAT's `SECONDS_BETWEEN_MAINTENANCE = 10`; configurable via `server.headPollInterval`. Post-commit hook is an optional additional trigger installed by `nx install`; polling is the guaranteed baseline; inotify/FSEvents is out of scope v1 |
| 6 | Single `nx serve` manages multiple repos | Per-repo registry in `~/.config/nexus/repos.json`; each repo has its own T3 collection and ripgrep line cache; `--corpus` routes queries |
| 7 | T1 uses `DefaultEmbeddingFunction` (local ONNX) | Session scratch doesn't need Voyage AI's semantic fidelity; a network call on every scratch search defeats the purpose of an in-memory store |
| 8 | Cross-corpus: separate retrieval + reranking | `voyage-code-3` and `voyage-4` produce incomparable similarity scores; independent retrieval per corpus followed by reranking is the correct merge strategy |
| 9 | `nx pm` uses T2 (not a new storage tier) | PM docs are a named-file project workspace — exactly T2's model. No new infrastructure; FTS5 covers keyword search needs; T3 is opt-in via `nx pm promote` for cross-project semantic queries |
| 10 | Archive synthesizes to T3 rather than dumping raw PM docs | Raw phase docs are drafts/iterations — noisy, redundant, pollute semantic search. Haiku synthesis at archive time extracts signal (decisions, challenges, outcome) into one semantically rich chunk per project. T2 raw docs decay over 90d for restore flexibility; after that, the T3 synthesis is the permanent reference. |
| 11 | T3 = ChromaDB CloudClient (not Qdrant, not self-hosted Chroma) | Already running in the existing Claude Code toolchain (same ChromaDB instance used by current agents); no new infrastructure required. `chromadb.CloudClient` provides tenant+database isolation. Qdrant rejected (Arcaneum port only; would require new infra). Self-hosted Chroma rejected (adds operational burden, defeats "already running" benefit). |
