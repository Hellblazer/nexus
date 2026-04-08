---
title: "Catalog Path Rationalization and Link Graph Usability"
id: RDR-060
type: Feature
status: accepted
accepted_date: 2026-04-08
reviewed-by: self
priority: high
author: Hal Hildebrand
created: 2026-04-08
related_issues: [RDR-049, RDR-050, RDR-051, RDR-052, RDR-053]
related_notes: >
  RDR-049 (closed): Introduced OwnerRecord and tumbler model — E1 adds repo_root to this structure.
  RDR-050 (closed): Deferred computed similarity — E3 justifies why link boost is distinct.
  RDR-051 (closed): Implemented link CRUD — E5 builds on link_if_absent() idempotency.
  RDR-052 (closed): Implemented follow_links in query MCP — E3/E6 extend its reach.
  RDR-053 (closed): Tumbler arithmetic — E1 path resolution completes the Xanadu location-independence pattern.
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

- **Heuristic noise**: `implements-heuristic` links (87% of all links) match substrings ("session" in title) — false positives for unrelated RDRs that mention common words. These dominate the graph and drown out precise `implements` links
- **No link validation**: Stale links to deleted/renamed files persist silently
- **No coverage metrics**: No way to know which RDRs have zero code links, or which code files reference no design docs

*Note: An earlier draft cited "chunk-level duplication" as a problem. RF-7 disproved this — catalog entries are already per-file. The original observation was measuring links across multiple owners, not chunk-level duplicates.*

## Design Principles

1. **Relative paths, always** — `file_path` in catalog entries is relative to repo root. Owner tumbler → repo root mapping provides the absolute path when needed.
2. **File-level links** — Links already connect documents (files), not chunks (confirmed RF-7). A link from RDR-053 to `catalog.py` means "this RDR discusses this module." No deduplication needed.
3. **Links are first-class in search** — When searching for "tumbler arithmetic," results from files linked to RDR-053 rank higher than unlinked results.
4. **Discovery over manual curation** — Auto-linkers should find 80% of relationships. Agents and humans add the remaining 20%.

## Proposed Changes

### E1: Path Rationalization

**Scope**: Indexer, catalog registration, doctor

- All `file_path` values stored as relative paths (to repo root)
- Owner record gains `repo_root` field (mutable, absolute, machine-specific)
- `Catalog.resolve_path(tumbler) -> Path` computes absolute path: `owner.repo_root / entry.file_path`
- Migration: `nx doctor --fix-paths` rewrites absolute paths to relative for all entries under known owners

**Backwards compatibility** (OwnerRecord is already in production since RDR-049):
- `repo_root` defaults to `""` in JSONL deserialization so existing owner entries load without error
- `resolve_path()` falls back to registry lookup (`registry.py` repo_hash → Path) when `repo_root == ""`
- New `repo_root` field populated lazily on next `catalog.register()` call for each owner

**Migration sequence** (order matters — idempotency uses exact string match on `file_path`):
1. Patch all call sites that store absolute paths (`doc_indexer.py:831`, `pipeline_stages.py:476`, `mcp_server.py:1174`, `commands/catalog.py:395,825,901`) to store relative paths
2. Run `nx doctor --fix-paths` to rewrite existing absolute entries via `cat.update()` on the existing tumbler (not delete-and-reinsert) — the FTS5 `documents_fts` update trigger propagates the change
3. Verify: `nx catalog search --where "file_path LIKE '/%'"` returns zero results

**T3 metadata consistency** (critical coordination): `_prefilter_from_catalog()` (`search_engine.py:97-143`) pulls `file_path` from the catalog and uses it as a `source_path` filter against ChromaDB chunk metadata (`{"source_path": {"$in": [paths]}}`). If catalog stores relative paths but T3 chunks retain absolute `source_path` metadata, the `$in` filter silently returns zero results. Therefore:
- E1 must also patch all indexer paths to store relative `source_path` in T3 chunk metadata (same call sites as catalog: `doc_indexer.py`, `pipeline_stages.py`, `mcp_server.py`)
- Migration must update existing T3 `source_path` metadata alongside the catalog `--fix-paths` rewrite
- Verification step 3 should also check: `nx search --where "source_path LIKE '/%%'"` returns zero results across all collections

**T3 migration procedure**: `nx doctor --fix-paths` drives both catalog and T3 migration atomically per entry:
1. For each catalog entry with absolute `file_path`, derive the old absolute path and the new relative path
2. For the entry's `physical_collection`, paginate `col.get(where={"source_path": old_absolute}, limit=300)` to collect all chunk IDs and metadata (respecting Cloud's 300-record batch limit)
3. Rewrite `source_path` in each chunk's metadata dict to the new relative value
4. Call `col.update(ids=chunk_ids, metadatas=new_metadatas)` in batches of 300
5. Then update the catalog entry's `file_path` via `cat.update()`

Add a `T3Database.update_source_path(collection, old_path, new_path) -> int` helper that encapsulates steps 2-4, following the existing pagination pattern in `delete_by_source()` (`t3.py:688-716`). Returns count of chunks updated.

**Migration safety**: Run in dry-run mode first (`--fix-paths --dry-run`) to report affected entries and chunk counts without writing. The migration is idempotent — re-running on already-relative paths is a no-op (the `col.get(where={"source_path": old_absolute})` returns empty).

**Downstream callers of `file_path`**: Any code that reads `file_path` and treats it as a filesystem path must switch to `resolve_path()` after E1. Known caller: `generate_rdr_filepath_links()` (`link_generator.py:82-83`) uses `Path(rdr.file_path).is_file()` — after E1, relative paths resolve against cwd (not repo root), so this always returns False and the linker silently stops generating all `implements` links. Fix: replace with `resolved = cat.resolve_path(rdr.tumbler); if resolved is None or not resolved.is_file(): continue`. This is a direct consequence of E1 and must ship in the same PR.

**PDF exception**: Curator-owner entries (PDFs indexed via `pipeline_stages.py`) store filename-only `file_path` (e.g., `paper.pdf`). These are not resolvable to a filesystem location from catalog metadata alone. `resolve_path()` returns `None` for curator owners. `--fix-paths` skips curator-owner entries entirely.

### ~~E2: File-Level Link Deduplication~~ — REMOVED

**Rationale**: RF-7 confirms catalog entries are already per-file (ONE entry per source file with `chunk_count` metadata). All 5 indexer paths follow this pattern. The earlier observation of "chunk-level duplication" was measuring links across all owners, not duplicated chunks within one owner. The `link-audit --dedup` command would have undefined semantics against a non-existent problem and could destroy valid links. See OQ1 resolution below.

### E3: Link-Aware Search Boost

**Scope**: search_engine.py, scoring.py

**Mechanism** (concrete, testable):
- When the `query` MCP tool receives a `subtree` or tumbler context, follow links from that tumbler and include linked documents' chunks in the candidate pool — extending the existing `follow_links` parameter (RDR-052)
- When no explicit tumbler context is given: for each result chunk from file X, look up X's catalog entry and count outgoing `implements` links (NOT `implements-heuristic` — see link-type weighting below). Results with ≥1 precise link get a configurable additive boost to `hybrid_score`
- **Link-type weighting**: `implements` links get full boost; `implements-heuristic` links get zero boost (they dominate the graph at 5,145 of 5,913 links and are too noisy for scoring). `relates` and `cites` get half boost. Weights configurable via `.nexus.yml`
- Opt-in via `link_boost` config key (default: enabled for `query` MCP, disabled for `nx search`)

**Justification vs RDR-050 deferral**: RDR-050 deferred *computed pairwise similarity* (O(n²) embedding comparison to generate `similar` links). E3 uses the existing link graph — no new link generation, no embedding computation. The deferred approach asks "what's similar to X?" which semantic search already answers. E3 asks "what's *intentionally connected* to X?" which semantic search cannot answer. The signal is structural (human/auto-linker declared a relationship), not statistical.

### E4: Discovery Tools

**Scope**: CLI commands, MCP tools

- `nx catalog orphans` — files in T3 with no catalog entry or no links
- `nx catalog coverage` — per-repo report: files with links vs without, RDRs with code links vs without
- `nx catalog suggest-links` — semantic similarity between RDR content and code files that aren't yet linked

**`suggest-links` feasibility**: Uses `collection.get(include=["embeddings"])` on local `PersistentClient` to fetch per-document embeddings without API calls. **CloudClient limitation**: ChromaDB Cloud does not support batch embedding retrieval in the same way — `suggest-links` is local-mode only. Cloud users can run `suggest-links` after `nx pull` syncs to local storage. This constraint should be documented in CLI help text.

### E5: Incremental Linker Improvements

**Scope**: link_generator.py, auto_linker.py

**Current state (RF-6)**: Both `generate_code_rdr_links()` and `generate_rdr_filepath_links()` already run after every `index_repository` call (`indexer.py:273-282`). However, they perform O(n×m) full scans via `_all_entries(cat)` on every run, relying on `link_if_absent()` idempotency to skip existing links.

**Proposed change**: Make linkers incremental — `_register_indexed_files()` passes the set of newly registered tumblers to each linker, so each run evaluates only new entries against the full corpus. Complexity drops from O(n×m) per index to O(new_n × m), which matters as the catalog grows (currently 526 entries, heading toward thousands).

- Auto-linker seeds from RDR `related_issues` frontmatter field (currently ignored)
- New `nx catalog link-generate` CLI command to run all linkers on demand (full O(n×m) scan, for batch/setup use)

### E6: Agent Integration

**Scope**: Skills, session hooks

- Session hook shows link summary: "N RDRs linked to files you're touching"
- When an agent modifies a file, check for linked RDRs and surface them as context
- `query` MCP tool's `follow_links` parameter documented in agent instructions with worked examples

### E7: Catalog Housekeeping in Index Pipeline

**Scope**: indexer.py (`_catalog_hook`), catalog.py

**Current state (RF-8)**: `_catalog_hook()` only processes files that were just indexed. Deleted/renamed files leave orphan catalog entries and stale links. `delete_document()`, `defrag()`, `compact()` exist but are never called automatically.

**Proposed change**: Add a housekeeping phase to `_catalog_hook()` that runs after registration and link generation:

1. **Orphan detection**: Compare all catalog entries for the current owner against the set of just-indexed files. Entries whose `file_path` no longer exists in the indexed set are orphan candidates.
2. **Grace period**: Track misses via `miss_count` integer in the entry's `metadata` JSON dict (no schema migration needed — `metadata` is already a JSON column). Increment on each full index run where the entry's file is absent. Reset to 0 when the file is seen. Delete the entry (via `delete_document()`) when `miss_count >= N` (default: 2). **Partial runs** (`--on-locked=skip`) do not increment the counter — only full successful `_catalog_hook()` completions for the owner count as a run. The `metadata` dict is not indexed, but the scan is already O(entries_for_owner) so the JSON parse adds negligible cost.
3. **Rename detection** (best-effort): When a new file is indexed and an orphan candidate has the same `content_hash`, treat as a rename — update the orphan's `file_path` rather than creating a new entry. This preserves existing links.
4. **Stale link cleanup**: After orphan removal, enumerate links where `from_tumbler` or `to_tumbler` references a deleted document and call `unlink()` for each. The existing `link_audit()` method (`catalog.py:1242`, actual name `link_audit`) identifies orphaned links but has no deletion logic — E7 extends it with a `fix: bool = False` parameter that calls `unlink()` on each orphaned link when `fix=True`.
5. **CLI escape hatch**: `nx catalog gc` for manual full housekeeping outside the index pipeline.

**Design constraint**: Housekeeping must be fast enough to run in a post-commit hook (~1-2s). The orphan scan is O(entries_for_owner) with a single pass — acceptable at current scale (526 entries).

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

### RF-8: Git Hooks Do Not Maintain Catalog Consistency

**Source**: `hooks.py`, `indexer.py:1035-1081`, `catalog.py:585,1243-1508`

Nexus installs three git hooks (`post-commit`, `post-merge`, `post-rewrite`) via `nx hooks install` (`commands/hooks.py:13-20`). All three run `nx index repo` in the background, which calls `_catalog_hook()` (`indexer.py:222-284`).

**What works**: `_catalog_hook()` registers/updates catalog entries for currently-indexed files and runs both link generators post-index.

**What's missing**:
- **Deleted files orphan catalog entries**: `_prune_deleted_files()` (line 1035-1043) removes T3 chunks for deleted files, but catalog entries are never touched. `delete_document()` (`catalog.py:585`) exists but is never called by the indexer.
- **File renames create duplicates**: A moved file gets a new entry under its new `file_path`; the old entry persists as an orphan. No rename detection (fuzzy match on content hash or similar).
- **No automatic cleanup**: `defrag()` and `compact()` (`catalog.py:1463-1508`) exist but are manual-only CLI commands.
- **Audit detects but doesn't fix**: `audit_links()` (`catalog.py:1243-1361`) identifies orphaned/stale links but takes no corrective action.

**Impact on E1**: After path migration, any existing orphan entries with absolute paths will persist in the catalog permanently unless cleanup runs. The 7 entries pointing to renamed/missing files (Problem Statement §1) are symptoms of this gap.

### RF-9: Claude Code Plugin Hooks Do Not Touch Catalog

**Source**: `nx/hooks/hooks.json`

The Claude Code plugin hooks (`SessionStart`, `PostCompact`, `StopFailure`, `SubagentStart`) manage T1 scratch, T2 memory, and session lifecycle. None invoke catalog operations. This is appropriate — catalog maintenance belongs in git hooks, not agent session hooks.

### RF-10: Query Planner Is Path-Agnostic, But Pre-Filter Is Not

**Source**: `mcp_server.py:215-343`, `search_engine.py:97-143`

The query planning infrastructure (`query` MCP tool) routes on tumblers and collection names — `cat.descendants(subtree)`, `cat.graph(tumbler, depth)` — and never uses `file_path` for routing decisions. `follow_links` traversal is purely graph-based. **E1's path change does not affect query planning.**

However, `_prefilter_from_catalog()` bridges catalog and search by pulling `file_path` values and using them as ChromaDB `source_path` filters (`{"source_path": {"$in": paths}}`). This creates a consistency requirement: catalog `file_path` and T3 `source_path` metadata must use the same format. After E1, both must be relative. See E1's "T3 metadata consistency" section.

## Open Questions

1. ~~**Chunk-to-file mapping**~~ — **RESOLVED by RF-7**: Catalog already stores one entry per file with `chunk_count` metadata. All 5 indexer paths follow this pattern. No change needed.
2. ~~**Cross-repo links**~~ — **RESOLVED**: Tumblers already encode owner identity — a link from `1.5` (nexus RDR) to `2.3` (arcaneum file) is just a link between two tumblers in different owner spaces. The link graph is owner-agnostic by design. `resolve_path()` naturally follows the *target entry's* owner to get the correct `repo_root`, so cross-repo resolution works without special-casing. No linker currently generates cross-repo links, but the infrastructure supports them — agents or humans can create them via `catalog_link` MCP tool today. Auto-generation can be added later when there's demand.
3. ~~**Link confidence scores**~~ — **RESOLVED in E3**: Link-type weighting is baked into the search boost design. `implements` gets full boost, `implements-heuristic` gets zero (too noisy at 87% of all links), `relates`/`cites` get half. Weights are configurable via `.nexus.yml`. No separate confidence score needed — `link_type` already carries the signal.
4. ~~**Incremental vs batch**~~ — **RESOLVED in E5**: Both. Linkers already run post-index (RF-6). E5 makes them incremental (O(new_n × m) per index run). `nx catalog link-generate` provides the full O(n×m) batch scan for setup and catch-up.

## Success Criteria

- [ ] Zero absolute file_path values in catalog after migration (E1)
- [ ] `resolve_path()` returns correct absolute path for repo-owner entries, `None` for curator-owner entries (E1)
- [ ] `nx catalog coverage` shows >80% of nexus code files linked to at least one RDR (E4)
- [ ] `query` MCP tool with link boost returns relevant RDR context in top-5 for code queries (E3)
- [ ] `nx catalog orphans` reports <10% unlinked files for indexed repos (E4)
- [ ] Agent workflows surface linked RDRs when modifying linked code files (E6)
- [ ] Deleted files are removed from catalog within 2 index runs (E7)
- [ ] Renamed files preserve their catalog links (E7)
