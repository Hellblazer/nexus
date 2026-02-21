# Nexus

## Vision

Nexus is a self-hosted semantic search and knowledge system that replaces expensive cloud ingest
(Mixedbread) with local-first indexing, while keeping ChromaDB in the cloud as the permanent
knowledge store. It synthesizes the best of mgrep, SeaGOAT, and Arcaneum into a single,
integrated tool for Claude Code agents.

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
- For semantic search across large analysis docs, use T3 ChromaDB `knowledge::` collections instead (via `nx store` or `nx memory promote`)

### T3 — Cloud ChromaDB (permanent knowledge)

- Already running in the cloud — no new infra
- Stores: indexed code repos, indexed PDFs, long-term agent knowledge
- Collections namespaced by type: `code::{repo}`, `docs::{corpus}`, `knowledge::{topic}`
- Accessed via `nx search`, `nx store`, `nx index`

## Indexing Pipelines

### Server Architecture (nx serve — multi-repo)

`nx serve` is a single persistent Flask/Waitress process managing multiple repositories:

- **Repo registry**: `~/.config/nexus/repos.json` — list of registered repo paths with per-repo state
- `nx index code <path>` adds the path to the registry and triggers initial indexing
- Each repo has its own T3 collection (`code::{repo-name}`) and ripgrep line cache file
- HEAD polling runs per-repo every 30 seconds; stale repos are re-indexed automatically
- Optional: `nx install claude-code` sets a post-commit hook as an additional trigger alongside polling
- `nx serve status` shows each repo's indexing state and estimated accuracy (SeaGOAT sigmoid pattern)

### Code Repositories

1. `nx index code <path>` registers the repo with the persistent `nx serve` process
2. `git log` to compute frecency scores per file: `sum(exp(-0.01 * days_passed))`
3. Files chunked: AST-first for Python/JS/TS/Java/Go/Rust (tree-sitter); line-based fallback for others. Target ~150 lines per chunk; no overlap at function/class boundaries; 15% overlap for line-based fallback.
4. Chunks embedded via **Voyage AI** using `VoyageAIEmbeddingFunction(model_name="voyage-code-3")`
5. Upserted into T3 ChromaDB collection `code::{repo-name}`
6. Ripgrep line cache built locally: flat `path:line:content\n` text file, memory-mapped for hybrid search — 500MB cap (SeaGOAT pattern)
7. `nx serve` polls HEAD hash every 30 seconds; re-indexes on change

ChromaDB natively supports `VoyageAIEmbeddingFunction` (`pip install voyageai`; env var: `VOYAGE_API_KEY`).

> **Model name verification**: Verify `voyage-code-3` and `voyage-4` against the current Voyage AI model
> catalog before use. The ChromaDB `VoyageAIEmbeddingFunction` default is `"voyage-large-2"` — names must
> be set explicitly. The SDK does not enumerate valid names at import time; an invalid name fails at the
> first API call.

### PDFs and Documents

1. `nx index pdf <path>` reads PDFs **directly from their source path** — no local copy stored
2. Text extracted and chunked in-process using **ported Arcaneum extraction logic**: PyMuPDF4LLM → markdown (primary), pdfplumber (complex tables fallback), Tesseract/EasyOCR (scanned fallback)
3. **Only the extracted text chunks + embeddings + metadata are stored in T3 ChromaDB** — raw PDF bytes never leave the machine
4. Chunks embedded via `VoyageAIEmbeddingFunction(model_name="voyage-4")`
5. Upserted into T3 collection `docs::{corpus-name}`

Arcaneum's extraction and chunking logic (PDFExtractor, PDFChunker, OCREngine) ports directly. Arcaneum's
storage layer (Qdrant `PointStruct`, `upload_points`, scroll-based sync) is **not** used — replaced with
ChromaDB `collection.upsert()`. Arcaneum's embedding layer (`fastembed` local ONNX) is **not** used —
replaced with `VoyageAIEmbeddingFunction`.

Since ChromaDB stores the chunk text (`documents` field), result display and `--content` work without
re-reading the source file. Re-indexing (`nx index pdf <path>` again) requires the source path to still
be accessible — same as mgrep's `--sync`.

> **Re-embedding note**: Raw content is not stored locally. Re-embedding with a future model version
> requires re-reading the source files (acceptable for PDFs; they remain accessible). For `knowledge::`
> chunks from agent outputs (no source file), re-embedding is not possible without re-running the agent.

### Markdown / Notes

1. `nx index md <path>` with YAML frontmatter extraction
2. Semantic chunking preserving document structure (ported from Arcaneum's SemanticMarkdownChunker)
3. Incremental sync via SHA256 content hashing
4. Chunks embedded via `VoyageAIEmbeddingFunction(model_name="voyage-4")`, upserted into T3 `docs::{corpus-name}`

## ChromaDB Metadata Schema

ChromaDB metadata values are flat (`str | int | float | bool` only — no nested objects).
All structural parse context ("high context") from the extraction pipeline is preserved
as flat metadata fields alongside each chunk. This replicates the richness of Mixedbread's
`generated_metadata` and means search results carry full structural provenance.

### Document chunks (`docs::*` collections)

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

### Code chunks (`code::*` collections)

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

### Knowledge / agent memory chunks (`knowledge::*` collections)

```
source_agent         str   Agent name that stored this (e.g. "codebase-deep-analyzer")
session_id           str   Claude Code session ID
title                str   Human-provided title
category             str   e.g. "security", "architecture", "planning"
tags                 str   Comma-separated tags
store_type           str   "knowledge"
indexed_at           str   ISO 8601 timestamp
expires_at           str   ISO 8601 expiry timestamp; empty string = permanent
ttl_days             int   TTL in days at store time; 0 = permanent
```

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

`[path]` positional argument scopes results to files under that path (equivalent to Mixedbread's `starts_with` metadata filter).

### --corpus resolution

`--corpus <name>` uses **prefix matching** against collection names:
- `--corpus code` → all `code::*` collections
- `--corpus docs` → all `docs::*` collections
- `--corpus knowledge` → all `knowledge::*` collections
- `--corpus code::myrepo` → exactly the `code::myrepo` collection (fully-qualified)

When multiple `--corpus` flags are used, each corpus is queried separately (they may use different embedding models), results are combined, then reranked — see Cross-corpus search.

### Cross-corpus search

`code::*` collections use `voyage-code-3`; `docs::*` and `knowledge::*` use `voyage-4`. These embedding spaces are not directly comparable — similarity scores across models are meaningless when combined naively.

Resolution strategy:
1. Each corpus queried independently using its own embedding function
2. Top-k results retrieved per corpus (proportional to `--max-results`)
3. Combined result set reranked using Voyage AI's reranker to produce a unified ranked list
4. `--no-rerank` skips step 3 and interleaves results round-robin instead

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
# Per-chunk score combining vector similarity and file frecency
vector_norm   = min_max_normalize(cosine_similarity, result_window)   # → [0, 1]
frecency_norm = min_max_normalize(file_frecency_score, result_window)  # unbounded → [0, 1]
score = 0.7 * vector_norm + 0.3 * frecency_norm

# min_max_normalize(x, window): (x - min) / (max - min + ε)
#   computed over the current query result window, not a global corpus statistic
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

Multi-step: Nexus issues multiple queries, refines based on intermediate results, then synthesizes final ranked list. Pairs with `--answer` for best results.

## Session Scratch (`nx scratch`)

T1 in-memory ChromaDB, cleared at session end:

```bash
nx scratch put "content" --tags "hypothesis,phase1"
nx scratch get <id>
nx scratch search "query"
nx scratch list
nx scratch clear                           # explicit clear; also happens automatically on SessionEnd
nx scratch promote <id> --project BFDB_active --title findings.md   # → T2
```

- Uses `DefaultEmbeddingFunction` (local ONNX, no API call) — fast, no network dependency
- Session ID determined from `CLAUDE_SESSION_ID` env var (set by SessionStart hook)
- On crash: T1 is in-memory; data is lost by design — scratch is ephemeral

## Memory Bank Replacement

`nx memory` replaces the MCP memory bank:

```bash
# Write — named file within a project (maps to memory bank's project+filename key)
nx memory put "content" --project BFDB_active --title active-context.md --tags "phase1" --ttl 30d

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
- For semantic search across large analysis docs, `nx store` (or `nx memory promote`) persists to T3 ChromaDB `knowledge::` collections

## Project Management Infrastructure (`nx pm`)

`nx pm` provides first-class support for the structured `.pm/` project management infrastructure used by Claude Code agents. It is a thin ergonomic layer over T2 (`nx memory`), storing PM documents under the `{repo}_pm` project namespace. No new storage tier is introduced.

### Why T2 is the natural fit

The `.pm/` directory is a named-file project workspace — exactly the model `nx memory` implements. Nexus replaces the raw filesystem with T2 and adds:

- **FTS5 keyword search** across all PM docs (fast, no API call): `nx pm search "caching decision"`
- **Cross-project search**: `nx memory search "database schema"` finds decisions across every project's PM namespace
- **TTL management**: phase docs can auto-expire when a project closes
- **Agent provenance**: every write records which agent and session produced the content
- **On-demand semantic search**: `nx pm promote` pushes PM docs to T3 `knowledge::pm::*` for cross-project semantic queries

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
nx pm init [--project myrepo]

# Session resumption — outputs CONTINUATION.md content for session injection
nx pm resume [--project myrepo]

# Human-readable status: current phase, last-updated agent, open blockers
nx pm status [--project myrepo]

# Phase management
nx pm phase 2                          # retrieve phase-2 context
nx pm phase next                       # transition to next phase (increments phase tag)

# FTS5 keyword search scoped to PM docs (no API call)
nx pm search "what did we decide about caching"
nx pm search "auth" --project myrepo   # scoped to one project

# Promote PM docs to T3 for cross-project semantic search
nx pm promote --collection knowledge --tags "decision,architecture"

# Lifecycle cleanup
nx pm expire                           # remove TTL-expired phase docs
```

### SessionStart hook — PM-aware behavior

When `nx install claude-code` sets up the SessionStart hook, it auto-detects PM projects and adjusts context injection:

```
Nexus ready. T1 scratch initialized (session: {session_id}).

# PM project detected ({repo}_pm/CONTINUATION.md exists):
{CONTINUATION.md content — injected directly, replaces manual `cat .pm/CONTINUATION.md`}

# No PM project → generic fallback:
Recent memory ({project}, last 10 entries):
  - {title} ({agent}, {N}d ago)
```

Content cap: 2000 chars for CONTINUATION.md injection (same 500-char-per-entry bound as generic summary, scaled for a single document).

### Slash commands

```
/nx:pm resume          — inject CONTINUATION.md into session context
/nx:pm status          — show phase, blockers, last-agent summary
/nx:pm phase next      — advance to next phase
/nx:pm search <query>  — FTS5 search across PM docs
/nx:pm archive         — archive current project (synthesize → T3, decay T2)
/nx:pm close           — archive + mark completed
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

**Archive**: `nx pm archive` does two things atomically:
1. **Synthesize → T3**: Haiku reads all PM docs (CONTINUATION.md, phase files, AGENT_INSTRUCTIONS.md) and produces a single compact synthesis chunk stored in `knowledge::pm::{repo}`. This becomes the permanent institutional memory reference.
2. **Decay T2**: Re-tags all `{repo}_pm` docs as `pm-archived`, bumps TTL to 90 days (configurable via `NX_PM_ARCHIVE_TTL`). Raw docs remain queryable during the decay window; auto-expired after.

**Restore**: `nx pm restore <project>` re-activates archived T2 docs within the decay window. After TTL expiry it fails gracefully ("raw docs expired — use `nx pm reference {project}` to access the archived synthesis").

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

Target size: 300–600 chars — one ChromaDB chunk. Semantically rich so vector search finds it on topical queries even without knowing the project name.

T3 metadata: `store_type="pm-archive"`, `project="{repo}"`, `status="completed|paused|cancelled"`, `archived_at` (ISO 8601), `phase_count` (int), `ttl_days=0` (permanent).

#### Archive and restore commands

```bash
# Archive current project (synthesize → T3 + start T2 decay)
nx pm archive [--project myrepo] [--status completed|paused|cancelled]

# Archive + mark complete (alias)
nx pm close [--project myrepo]

# Restore from archived T2 docs (within decay window only)
nx pm restore <project>

# Query institutional memory — semantic search across all archived syntheses
nx pm reference                      # interactive: "find projects about auth patterns"
nx pm reference "caching decisions"  # direct semantic query → nx search knowledge::pm::*
nx pm reference myrepo               # retrieve specific project's synthesis
```

`nx pm reference` without a project name fans out to `nx search --corpus knowledge --corpus-filter store_type=pm-archive` — leveraging T3 semantic search across all archived projects. This is the institutional memory query point: *"how did we handle rate limiting in past projects?"* finds the right synthesis even without knowing which project to look in.

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
nx store expire           # remove knowledge:: chunks whose expires_at has passed
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
nx serve logs
```

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

Key settings: `server.port`, `server.ignorePatterns`, `embeddings.codeModel`,
`embeddings.docsModel`, `chromadb.url`, `client.host`, `server.headPollInterval`.

### Health check

```bash
nx doctor    # verify: nx serve running, ChromaDB reachable, Voyage API key valid,
             #         ripgrep on PATH, git available,
             #         Mixedbread SDK authenticated (only when --mxbai has been used)
```

### Agent integration installers

```bash
nx install claude-code      # install SKILL.md + SessionStart/SessionEnd hooks
nx uninstall claude-code
nx install codex            # future integrations
```

`nx install claude-code` writes:
- `~/.claude/skills/nexus/SKILL.md` — agent usage guide (how to use `nx search`, `nx memory`, `nx store`, `nx scratch`)
- SessionStart hook entry in `~/.claude/settings.json`: initialize T1 scratch, print T2 memory summary
- SessionEnd hook entry: flush flagged T1 scratch entries to T2, run `nx memory expire`

SessionStart hook output (printed to Claude context at session start):
```
Nexus ready. T1 scratch initialized (session: {session_id}).
Recent memory ({project}, last 10 entries):
  - {title} ({agent}, {N}d ago)
  ...
```
Capped at 10 entries, 500 chars each, to bound context consumption.

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
/nx:watch <path>                — watch for file changes, sync on save
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

## Technologies

- **Python 3.12+** — CLI, server, indexing pipelines, SeaGOAT frecency logic
- **ChromaDB** — T1 (`chromadb.EphemeralClient` + `DefaultEmbeddingFunction`) and T3 (`chromadb.HttpClient` → cloud + `VoyageAIEmbeddingFunction`)
- **SQLite + FTS5** — T2 memory bank (stdlib `sqlite3`, WAL mode, no ORM)
- **Voyage AI** — embedding API: `voyage-code-3` for code, `voyage-4` for docs/PDFs
  - Verify model names against current Voyage AI catalog before use; ChromaDB wrapper default is `"voyage-large-2"`
  - Verify free tier quota at voyageai.com/pricing before relying on this; spec written assuming 200M tokens/month free
  - ~$0.18/1M tokens (code), ~$0.06/1M tokens (docs) beyond free tier; env var: `VOYAGE_API_KEY`
  - Native `VoyageAIEmbeddingFunction` in ChromaDB (`pip install voyageai`) — no custom glue
- **Claude Haiku** (`claude-3-5-haiku-20241022`) — Q&A synthesis via `anthropic` Python SDK
- **Mixedbread SDK** — read-only fan-out for existing Mixedbread-indexed collections (`--mxbai` flag)
- **ripgrep** — full-text code search via flat mmap line cache (local, 500MB cap)
- **Git** — frecency computation from commit history
- **Flask + Waitress** — persistent `nx serve` process (SeaGOAT pattern)
- **PyMuPDF4LLM + pdfplumber + Tesseract/EasyOCR** — PDF extraction (ported from Arcaneum)
- **tree-sitter** — AST-based code chunking for supported languages

## Decisions Log

| # | Decision | Rationale |
|---|---|---|
| 1 | Voyage AI embedding API (not local ONNX) | Eliminates ~2GB model downloads and GPU setup complexity; free tier covers normal usage (verify quota at voyageai.com/pricing) |
| 2 | Persistent `nx serve` process | Faster repeated queries; ripgrep line cache stays warm; HEAD polling for auto-reindex |
| 3 | SQLite T2 = memory bank only | Don't over-engineer; T3 ChromaDB handles knowledge storage naturally |
| 4 | Mixedbread fan-out via `--mxbai` flag on `nx search` | Opt-in so normal searches stay fully local; `nx ask` is not a separate command — answer synthesis is `-a` on `nx search` |
| 5 | HEAD detection via 30s polling in `nx serve` | Post-commit hook is an optional additional trigger installed by `nx install`; polling is the guaranteed baseline; inotify/FSEvents is out of scope v1 |
| 6 | Single `nx serve` manages multiple repos | Per-repo registry in `~/.config/nexus/repos.json`; each repo has its own T3 collection and ripgrep line cache; `--corpus` routes queries |
| 7 | T1 uses `DefaultEmbeddingFunction` (local ONNX) | Session scratch doesn't need Voyage AI's semantic fidelity; a network call on every scratch search defeats the purpose of an in-memory store |
| 8 | Cross-corpus: separate retrieval + reranking | `voyage-code-3` and `voyage-4` produce incomparable similarity scores; independent retrieval per corpus followed by reranking is the correct merge strategy |
| 9 | `nx pm` uses T2 (not a new storage tier) | PM docs are a named-file project workspace — exactly T2's model. No new infrastructure; FTS5 covers keyword search needs; T3 is opt-in via `nx pm promote` for cross-project semantic queries |
| 10 | Archive synthesizes to T3 rather than dumping raw PM docs | Raw phase docs are drafts/iterations — noisy, redundant, pollute semantic search. Haiku synthesis at archive time extracts signal (decisions, challenges, outcome) into one semantically rich chunk per project. T2 raw docs decay over 90d for restore flexibility; after that, the T3 synthesis is the permanent reference. |
