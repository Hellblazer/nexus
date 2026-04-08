---
title: "Catalog Path Rationalization and Link Graph Usability"
id: RDR-060
type: Feature
status: draft
priority: high
author: Hal Hildebrand
created: 2026-04-08
related_issues: [RDR-049, RDR-050, RDR-051, RDR-052, RDR-053]
---

# RDR-060: Catalog Path Rationalization and Link Graph Usability

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The catalog link graph has 12,600+ links but they're largely invisible and unusable. Three root problems:

### 1. Inconsistent File Paths

Code entries store relative paths (`src/nexus/indexer.py`), but RDR/prose entries store absolute paths (`/Users/hal/git/nexus/docs/rdr/rdr-053.md`). This breaks portability — the catalog is tied to one machine's filesystem layout. The owner tumbler already maps to a repo; file paths should be relative to that repo root.

**Current state** (nexus repo):
- Code entries: 208, all relative ✓
- RDR entries: 168, all absolute ✗ (7 now point to renamed/missing files)
- Prose entries: 150, all absolute ✗

### 2. Links Exist But Don't Surface

5,913 nexus links exist (5,145 implements-heuristic, 765 implements, 3 relates) but:
- No CLI command shows "what code implements this RDR?" or "what RDRs touch this file?"
- No search integration — `nx search` doesn't use links to boost related results
- No agent workflow uses links — agents search by text, ignoring the graph
- The `query` MCP tool has `follow_links` but it's rarely invoked because agents don't know what links exist

### 3. Link Quality Is Low

- **Chunk-level duplication**: Each code chunk gets its own catalog entry, so `session.py` (35 chunks) × 3 RDRs = 105 links for what should be 3 file→RDR relationships
- **Heuristic noise**: `implements-heuristic` links match substrings ("session" in title) — false positives for unrelated RDRs that mention common words
- **No link validation**: Stale links to deleted/renamed files persist silently
- **No coverage metrics**: No way to know which RDRs have zero code links, or which code files reference no design docs

## Design Principles

1. **Relative paths, always** — `file_path` in catalog entries is relative to repo root. Owner tumbler → repo root mapping provides the absolute path when needed.
2. **File-level links, not chunk-level** — Links connect documents (files), not chunks. A link from RDR-053 to `catalog.py` means "this RDR discusses this module."
3. **Links are first-class in search** — When searching for "tumbler arithmetic," results from files linked to RDR-053 rank higher than unlinked results.
4. **Discovery over manual curation** — Auto-linkers should find 80% of relationships. Agents and humans add the remaining 20%.

## Proposed Changes

### E1: Path Rationalization

**Scope**: Indexer, catalog registration, doctor

- All `file_path` values stored as relative paths (to repo root)
- Owner record gains `repo_root` field (mutable, absolute, machine-specific)
- `Catalog.resolve_path(tumbler) -> Path` computes absolute path: `owner.repo_root / entry.file_path`
- Migration: `nx doctor --fix-paths` rewrites absolute paths to relative for all entries under known owners

### E2: File-Level Link Deduplication

**Scope**: Link generator, catalog

- Links connect file-level document tumblers, not chunk tumblers
- `generate_code_rdr_links` operates on deduplicated file entries (one tumbler per source file, not per chunk)
- Existing chunk-level link duplicates cleaned up via `nx catalog link-audit --dedup`

### E3: Link-Aware Search Boost

**Scope**: search_engine.py, scoring.py

- When search returns chunks from file X, check if X has catalog links to any RDR matching the query context
- Linked results get a configurable boost (similar to quality_score — additive to hybrid_score)
- Opt-in via `link_boost` config key (default: enabled for `query` MCP, disabled for `nx search`)

### E4: Discovery Tools

**Scope**: CLI commands, MCP tools

- `nx catalog orphans` — files in T3 with no catalog entry or no links
- `nx catalog coverage` — per-repo report: files with links vs without, RDRs with code links vs without
- `nx catalog suggest-links` — semantic similarity between RDR content and code files that aren't yet linked (uses T3 embeddings, no new API calls)

### E5: Incremental Linker Improvements

**Scope**: link_generator.py, auto_linker.py

- Filepath linker runs incrementally on every `nx index repo` (not just `nx catalog setup`)
- Auto-linker seeds from RDR `related_issues` frontmatter field (currently ignored)
- New `nx catalog link-generate` CLI command to run all linkers on demand

### E6: Agent Integration

**Scope**: Skills, session hooks

- Session hook shows link summary: "N RDRs linked to files you're touching"
- When an agent modifies a file, check for linked RDRs and surface them as context
- `query` MCP tool's `follow_links` parameter documented in agent instructions with worked examples

## Research Findings

### RF-1: Current Link Graph State (Measured)

Nexus repo (2026-04-08):
- 5,913 total links (5,145 implements-heuristic, 765 implements, 3 relates)
- 266 filepath matches found but 0 new links (all pre-existed as heuristic)
- 505 of 818 RDR file_paths across all repos are absolute and point to non-existent files

### RF-2: Xanadu Tumbler Model Alignment

Per RDR-053, tumblers provide stable document identity independent of filesystem location. Adding `repo_root` to the owner record completes the mapping: `tumbler → owner → repo_root → file_path → absolute path`. This is the Xanadu pattern of separating content identity from content location.

### RF-3: file_path Inconsistency — 8 Call Sites, 3 Formats

**Source**: Codebase analysis (2026-04-08)

8 call sites set `file_path` on `catalog.register()`:

| Call site | Format | Example |
|-----------|--------|---------|
| `indexer.py:259` | Relative to repo root | `src/nexus/corpus.py` |
| `doc_indexer.py:831` | Absolute | `/Users/.../docs/ref.md` |
| `pipeline_stages.py:476` | Absolute (from PDF path) | `/Users/.../paper.pdf` |
| `mcp_server.py:1174` | User-provided (unvalidated) | varies |
| `commands/catalog.py:395,825,901` | Mixed | varies |

`catalog.register()` (`catalog.py:342-438`) applies **zero normalization** — file_path stored as-is. Idempotency check (`by_file_path`) does exact match, so the same file registered with absolute and relative paths creates duplicates.

### RF-4: Owner Record Missing repo_root

**Source**: `catalog/tumbler.py:135-141`

`OwnerRecord` has `repo_hash` but no `repo_root` field. The owner→filesystem mapping exists only implicitly (via the registry at `src/nexus/registry.py` which maps repo_hash → Path). Adding `repo_root` to OwnerRecord enables: `tumbler → owner.repo_root / entry.file_path → absolute path`.

DB schema change: add `repo_root TEXT` to owners table in `catalog_db.py:25`.

### RF-5: Search Already Accepts Catalog Parameter

**Source**: `search_engine.py:148-156`

`search_cross_corpus()` already has `catalog: Any | None = None` parameter (line 155). Currently used only for pre-filtering (`_prefilter_from_catalog`). Link boost can be injected at line 223 (after results retrieved, before clustering) following the `apply_quality_boost` pattern in `scoring.py:130-186`.

### RF-6: Link Generators Already Run on Every Index

**Source**: `indexer.py:273-282`

`_register_indexed_files()` calls both `generate_code_rdr_links()` and `generate_rdr_filepath_links()` after every `index_repository`. They process ALL entries every time and rely on `link_if_absent()` idempotency. Not incremental — O(n*m) scan.

### RF-7: Catalog Entries Are Per-File, Not Per-Chunk

**Source**: `catalog.py:48-63`, indexer call sites

Catalog registers ONE entry per source file with `chunk_count` metadata. All 5 indexer paths (code, prose, PDF, doc-markdown, streaming PDF) follow this pattern. The link graph is already file-level. The earlier observation of "133 links for session.py" was measuring links across all owners, not duplicated chunks.

## Open Questions

1. **Chunk-to-file mapping**: Should the catalog store one entry per file (collapsing chunks) or keep chunk-level entries with a `file_tumbler` parent? The file-level approach is simpler but loses chunk-level span resolution.
2. **Cross-repo links**: When RDR-053 in nexus references `arcaneum/ast_extractor.py`, should the link cross repo boundaries? Current tumblers support this but no linker generates cross-repo links.
3. **Link confidence scores**: Should heuristic links carry a confidence score so search boost can weight precise `implements` links higher than fuzzy `implements-heuristic`?
4. **Incremental vs batch**: Should the filepath linker run on every index (adding ~2s) or only on demand?

## Success Criteria

- [ ] Zero absolute file_path values in catalog after migration
- [ ] `nx catalog coverage` shows >80% of nexus code files linked to at least one RDR
- [ ] `nx search` with link boost returns relevant RDR context in top-5 for code queries
- [ ] `nx catalog orphans` reports <10% unlinked files for indexed repos
- [ ] Agent workflows surface linked RDRs when modifying linked code files
