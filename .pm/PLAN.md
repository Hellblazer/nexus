# Nexus Implementation Plan

## Epic: nexus-u25

Nexus: Self-hosted semantic search, knowledge management, and PM infrastructure.

Python 3.12+ CLI tool replacing Mixedbread (cloud ingest) and MCP memory bank with a locally-controlled indexing pipeline, ChromaDB cloud as permanent knowledge store, and integrated PM infrastructure.

Spec: /Users/hal.hildebrand/git/nexus/spec.md

---

## Phase Summary

| Phase | Bead | Title | Dependencies | Features |
|-------|------|-------|--------------|----------|
| 1 | nexus-bjm | Project foundation + T2 SQLite + nx memory | Epic | 6 |
| 2 | nexus-cas | T1 EphemeralClient + nx scratch + sessions | Phase 1 | 4 |
| 3 | nexus-odd | T3 CloudClient + nx store + basic search | Phase 1 | 5 |
| 4 | nexus-rr8 | nx serve + code indexing pipeline | Phase 3 | 6 |
| 5 | nexus-ejp | PDF/markdown indexing pipelines | Phase 3 | 4 |
| 6 | nexus-683 | Hybrid search + cross-corpus reranking | Phase 4, 5 | 5 |
| 7 | nexus-c28 | nx pm full lifecycle | Phase 1, 3 | 5 |
| 8 | nexus-roj | Claude Code plugin + hooks + doctor | Phase 2, 6, 7 | 4 |

Total: 1 epic + 8 phases + 39 feature tasks = 48 beads.

---

## Dependency Graph

```
                    nexus-u25 (Epic)
                         |
                    nexus-bjm (Phase 1)
                    /    |    \
          nexus-cas   nexus-odd   nexus-c28 (Phase 7)
          (Phase 2)   (Phase 3)    |
              |       /      \     |
              |  nexus-rr8  nexus-ejp
              |  (Phase 4)  (Phase 5)
              |       \      /
              |     nexus-683 (Phase 6)
              |         |
              |     nexus-roj (Phase 8)
              |         |
              +---------+
```

### Parallelization Opportunities

1. **Phase 2 and Phase 3** can run in parallel after Phase 1 completes
2. **Phase 4 and Phase 5** can run in parallel after Phase 3 completes
3. **Phase 7** depends only on Phase 1 + Phase 3 (not 4/5/6) -- can start early
4. Within Phase 5: PDF and markdown pipelines are independent
5. Within Phase 4: frecency scoring and AST chunking are independent after repo registry

### Critical Path

Epic -> Phase 1 -> Phase 3 -> Phase 4 -> Phase 6 -> Phase 8

This is the longest dependency chain (~6 sequential phases).

---

## Phase 1: Project Foundation + T2 SQLite + nx memory

**Phase Bead**: nexus-bjm (P1, feature)
**Scope**: Python project scaffold, config system, T2 SQLite schema, nx memory CRUD.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-9o6 | Scaffold Python 3.12+ project with click CLI entry point | P1 | nexus-u25 |
| nexus-07a | Implement config system with global and per-repo YAML merge | P1 | nexus-9o6 |
| nexus-5uc | Implement T2 SQLite schema with WAL mode, FTS5, and triggers | P1 | nexus-9o6 |
| nexus-uox | Implement nx memory put with upsert, TTL parsing, and auto-capture | P1 | nexus-5uc, nexus-07a |
| nexus-rp6 | Implement nx memory get and list with filtering | P1 | nexus-uox |
| nexus-5a0 | Implement nx memory search, expire, and promote | P1 | nexus-rp6 |

### Intra-phase Dependency Chain
```
nexus-9o6 (scaffold)
  ├── nexus-07a (config)
  └── nexus-5uc (T2 schema)
       └── nexus-uox (memory put) [also depends on nexus-07a]
            └── nexus-rp6 (memory get/list)
                 └── nexus-5a0 (memory search/expire/promote)
```

---

## Phase 2: T1 EphemeralClient + nx scratch + Sessions

**Phase Bead**: nexus-cas (P1, feature)
**Scope**: Session ID management, T1 in-memory ChromaDB, nx scratch commands.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-6te | Implement session ID management with UUID4 generation | P1 | nexus-5a0 |
| nexus-owu | Implement T1 EphemeralClient with DefaultEmbeddingFunction | P1 | nexus-6te |
| nexus-q2j | Implement nx scratch put/get/search/list/clear commands | P1 | nexus-owu |
| nexus-5m9 | Implement nx scratch flag/unflag/promote for T2 flush | P2 | nexus-q2j |

### Intra-phase Dependency Chain
```
nexus-6te (session ID) -> nexus-owu (T1 client) -> nexus-q2j (scratch CRUD) -> nexus-5m9 (flag/promote)
```

---

## Phase 3: T3 CloudClient + nx store + Basic Search

**Phase Bead**: nexus-odd (P1, feature)
**Scope**: ChromaDB cloud client, Voyage AI embeddings, nx store, basic semantic search, collection management.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-1vd | Implement T3 CloudClient with VoyageAIEmbeddingFunction | P1 | nexus-5a0 |
| nexus-ryq | Implement nx store for T3 knowledge persistence with TTL | P1 | nexus-1vd |
| nexus-46j | Implement nx store expire with guarded query for permanent entries | P1 | nexus-ryq |
| nexus-qdq | Implement basic nx search with single-corpus semantic search | P1 | nexus-1vd |
| nexus-tnu | Implement nx collection management commands | P2 | nexus-1vd |

### Intra-phase Dependency Chain
```
nexus-1vd (T3 client)
  ├── nexus-ryq (nx store) -> nexus-46j (store expire)
  ├── nexus-qdq (basic search)
  └── nexus-tnu (collection mgmt)
```

Note: nx store, basic search, and collection mgmt can run in parallel after T3 client.

---

## Phase 4: nx serve + Code Indexing Pipeline

**Phase Bead**: nexus-rr8 (P1, feature)
**Scope**: Persistent server, repo registry, git frecency, AST chunking, code embeddings, ripgrep cache.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-pgf | Implement nx serve daemon with Flask/Waitress and PID lifecycle | P1 | nexus-qdq |
| nexus-lfe | Implement repo registry and HEAD polling for auto-reindex | P1 | nexus-pgf |
| nexus-4l4 | Implement git frecency scoring from commit history | P1 | nexus-lfe |
| nexus-aiz | Implement AST code chunking via CodeSplitter with line-based fallback | P1 | nexus-lfe |
| nexus-wn2 | Implement code embedding via voyage-code-3 and T3 code__ upsert | P1 | nexus-4l4, nexus-aiz |
| nexus-3a8 | Implement ripgrep line cache with 500MB soft cap and mmap reader | P2 | nexus-wn2 |

### Intra-phase Dependency Chain
```
nexus-pgf (serve daemon) -> nexus-lfe (repo registry)
  ├── nexus-4l4 (frecency)
  └── nexus-aiz (AST chunking)
       └── nexus-wn2 (code embedding) [depends on both]
            └── nexus-3a8 (ripgrep cache)
```

Note: frecency and AST chunking can run in parallel.

---

## Phase 5: PDF and Markdown Indexing Pipelines

**Phase Bead**: nexus-ejp (P1, feature)
**Scope**: Arcaneum extraction/chunking ports, PDF and markdown indexing to T3.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-20t | Port Arcaneum PDF extraction pipeline | P1 | nexus-1vd |
| nexus-otg | Implement PDF chunking and T3 docs__ upsert with voyage-4 | P1 | nexus-20t |
| nexus-aw5 | Port Arcaneum SemanticMarkdownChunker with YAML frontmatter | P1 | nexus-1vd |
| nexus-5ab | Implement markdown indexing with SHA256 incremental sync | P1 | nexus-aw5 |

### Intra-phase Dependency Chain
```
nexus-1vd (T3 client, from Phase 3)
  ├── nexus-20t (PDF extraction) -> nexus-otg (PDF chunking + upsert)
  └── nexus-aw5 (markdown chunker) -> nexus-5ab (markdown indexing)
```

Note: PDF and markdown pipelines are fully parallel.

---

## Phase 6: Hybrid Search + Cross-Corpus Reranking

**Phase Bead**: nexus-683 (P1, feature)
**Scope**: Hybrid scoring, cross-corpus reranking, Mixedbread fan-out, agentic and answer modes.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-6ba | Implement hybrid search scoring with vector + frecency weighting | P1 | nexus-3a8, nexus-qdq |
| nexus-bh5 | Implement cross-corpus retrieval and Voyage rerank-2.5 unification | P1 | nexus-otg, nexus-qdq |
| nexus-4p6 | Implement Mixedbread fan-out with graceful degradation | P2 | nexus-bh5 |
| nexus-8sx | Implement agentic search mode with Haiku-driven query refinement | P2 | nexus-bh5 |
| nexus-d31 | Implement answer mode with Haiku synthesis and citation formatting | P1 | nexus-bh5, nexus-8sx |

### Intra-phase Dependency Chain
```
nexus-6ba (hybrid scoring) [depends on Phase 4 ripgrep cache]
nexus-bh5 (cross-corpus reranking) [depends on Phase 5 docs + Phase 3 search]
  ├── nexus-4p6 (mxbai fan-out)
  └── nexus-8sx (agentic search) -> nexus-d31 (answer mode)
```

Note: hybrid scoring and cross-corpus reranking can start in parallel. Mxbai fan-out is independent of agentic/answer modes.

---

## Phase 7: nx pm Full Lifecycle

**Phase Bead**: nexus-c28 (P1, feature)
**Scope**: PM infrastructure init, phase management, archive/restore/reference lifecycle.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-17h | Implement nx pm init, resume, and status commands | P1 | nexus-5a0, nexus-07a |
| nexus-b2k | Implement nx pm phase transitions, search, and blocker management | P1 | nexus-17h |
| nexus-rf3 | Implement nx pm archive with Haiku synthesis and two-phase commit | P1 | nexus-b2k, nexus-1vd |
| nexus-wpl | Implement nx pm restore and reference commands | P1 | nexus-rf3 |
| nexus-6qo | Implement nx pm expire, promote, and close commands | P2 | nexus-wpl |

### Intra-phase Dependency Chain
```
nexus-17h (pm init/resume/status)
  -> nexus-b2k (pm phase/search/block)
    -> nexus-rf3 (pm archive) [also depends on T3 client nexus-1vd]
      -> nexus-wpl (pm restore/reference)
        -> nexus-6qo (pm expire/promote/close)
```

---

## Phase 8: Claude Code Plugin + Hooks + Doctor

**Phase Bead**: nexus-roj (P1, feature)
**Scope**: SKILL.md, SessionStart/End hooks, slash commands, health check.

### Features

| Bead | Title | Priority | Blocked By |
|------|-------|----------|------------|
| nexus-kzt | Implement nx install/uninstall claude-code with SKILL.md generation | P1 | nexus-d31, nexus-6qo |
| nexus-1xg | Implement SessionStart hook with T1 init and PM-aware context injection | P1 | nexus-kzt, nexus-6te, nexus-17h |
| nexus-21o | Implement SessionEnd hook with T1 flush and TTL expiry | P1 | nexus-1xg, nexus-5m9, nexus-46j |
| nexus-cxn | Implement nx doctor health check for all dependencies | P2 | nexus-kzt, nexus-pgf |

### Intra-phase Dependency Chain
```
nexus-kzt (install/uninstall)
  ├── nexus-1xg (SessionStart) -> nexus-21o (SessionEnd)
  └── nexus-cxn (doctor)
```

Note: SessionEnd depends on scratch flag (Phase 2) and store expire (Phase 3). Doctor depends on serve (Phase 4).

---

## Risk Factors

1. **tree-sitter + llama-index version pinning**: Known breaking incompatibilities between package versions. Must test version combinations before proceeding with Phase 4 AST chunking.

2. **Voyage AI model name verification**: Model names (voyage-code-3, voyage-4, rerank-2.5) must be verified against current API catalog. SDK accepts any string and fails at first call.

3. **ChromaDB CloudClient authentication**: T3 operations require CHROMA_API_KEY and network. Integration tests need a real or mocked cloud instance.

4. **Arcaneum extraction port complexity**: The PDFExtractor/PDFChunker/OCR pipeline is ported from Arcaneum which uses Qdrant/fastembed -- storage and embedding layers must be rewritten for ChromaDB/Voyage AI.

5. **nx store expire guard**: Empty string sorts BEFORE ISO timestamps lexicographically. The guard (ttl_days > 0) is critical to prevent deleting permanent entries.

6. **Session ID**: CLAUDE_SESSION_ID does not exist in Claude Code. The UUID4 generation + file persistence pattern is the workaround.

---

## Spec Coverage Verification

| Spec Section | Covered By |
|---|---|
| T1 In-memory ChromaDB | nexus-owu, nexus-q2j, nexus-5m9 |
| T2 Local SQLite | nexus-5uc, nexus-uox, nexus-rp6, nexus-5a0 |
| T3 Cloud ChromaDB | nexus-1vd, nexus-ryq, nexus-46j |
| nx memory | nexus-uox, nexus-rp6, nexus-5a0 |
| nx scratch | nexus-q2j, nexus-5m9 |
| nx store | nexus-ryq, nexus-46j |
| nx search (basic) | nexus-qdq |
| nx search (hybrid) | nexus-6ba |
| nx search (cross-corpus) | nexus-bh5 |
| nx search (--mxbai) | nexus-4p6 |
| nx search (--agentic) | nexus-8sx |
| nx search (-a/--answer) | nexus-d31 |
| nx serve | nexus-pgf, nexus-lfe |
| nx index code | nexus-4l4, nexus-aiz, nexus-wn2 |
| nx index pdf | nexus-20t, nexus-otg |
| nx index md | nexus-aw5, nexus-5ab |
| Ripgrep line cache | nexus-3a8 |
| nx pm init/resume/status | nexus-17h |
| nx pm phase/search/block | nexus-b2k |
| nx pm archive | nexus-rf3 |
| nx pm restore/reference | nexus-wpl |
| nx pm expire/promote/close | nexus-6qo |
| nx install claude-code | nexus-kzt |
| SessionStart hook | nexus-1xg |
| SessionEnd hook | nexus-21o |
| nx doctor | nexus-cxn |
| Config system | nexus-07a |
| nx collection mgmt | nexus-tnu |
| Session ID mgmt | nexus-6te |
| Project scaffold | nexus-9o6 |
