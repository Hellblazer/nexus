---
title: "Claude Adoption: Session Context Gaps and Search Tool Guidance"
id: RDR-007
type: feature
status: draft
priority: high
author: Hal Hildebrand
reviewed-by:
created: 2026-02-28
related_issues:
  - RDR-006
---

## RDR-007: Claude Adoption: Session Context Gaps and Search Tool Guidance

## Summary

The nx plugin infrastructure for Claude is architecturally sound but leads
Claude into its worst-performing pathways in practice. Four specific adoption
gaps were identified through empirical testing: (1) the session hook surfaces
T2 memory under the bare repo name but the RDR workflow populates
`{repo}_rdr`, so the hook silently shows empty T2 when 19 RDRs are loaded;
(2) T2 entries are surfaced as title lists with no content preview, requiring
round-trip `get` calls for every entry before Claude can act; (3) the `code__`
T3 collection returns zero relevant results on 7 of 7 canonical queries — the
skill documentation recommends it as a primary search mode with no quality
caveat; (4) `nx memory search` uses FTS5 keyword matching only, but the skill
presents it as a general-purpose search tool without documenting this
constraint, causing silent empty-result failures on conceptual queries.

**Scope of resolution**: Findings 1, 2, and 4 are fully resolved by this RDR.
Finding 3 (code collection precision failure) is **mitigated** — RDR-007 adds
a skill caveat and an index-time warning — but not resolved. Full resolution
of Finding 3 requires RDR-006 (chunk size configuration) to ship and the
affected collections to be re-indexed.

**Evidence base**: Single session empirical test against `code__arcaneum`,
`docs__arcaneum`, `rdr__arcaneum`, and the `arcaneum_rdr` T2 namespace. All
findings are directly observable and reproducible.

## Motivation

The nx plugin exists to give Claude persistent, cross-session knowledge about
a project. Its value proposition depends entirely on Claude actually using it
and getting correct results. Neither is currently reliable.

The session hook is the primary mechanism for surfacing nx context. If it
surfaces the wrong namespace, Claude sees "No entries found" and concludes the
memory store is empty — which is wrong. It then proceeds without project
context, defeating the purpose of T2.

The `code__` T3 collection precision failure is a silent liability. An agent
following the documented workflow will search the code collection and receive
results that look plausible but are structurally wrong (large-file chunks
dominate regardless of query). The agent has no signal that the results are
unreliable. This is worse than no results — at least empty results trigger
fallback behavior.

The FTS5 documentation gap causes Claude to issue conceptual queries against
T2 and silently conclude the store has no relevant content when it may have
exactly the content needed under different vocabulary.

Findings 1, 2, and 4 are fully fixable within this RDR — hook tweaks, skill
text updates, no architectural changes. Finding 3 receives mitigation here
(caveat in skill documentation, warning at index time); its underlying cause
is addressed by RDR-006. Until RDR-006 ships and collections are re-indexed,
the code collection will continue to return imprecise results.

## Evidence Base

### Testing Methodology

The following was tested in a single session against the arcaneum project:

1. Loaded 19 arcaneum RDRs into T2 under project `arcaneum_rdr`
2. Ran `nx memory list --project arcaneum` (bare name, what the hook uses) →
   "No entries found"
3. Ran `nx memory list --project arcaneum_rdr` (`_rdr` suffix, actual
   namespace) → 19 entries listed
4. Ran 7 targeted queries against `code__arcaneum-2ad2825c` (the code T3
   collection); verified expected canonical file in top-3 results for each
5. Ran equivalent queries against `rdr__arcaneum-2ad2825c` (RDR T3
   collection); verified expected canonical file in top-3 results for each
6. Ran `nx memory search` with conceptual phrases vs. keyword-literal phrases
   against loaded T2 content

### Finding 1: T2 Namespace Mismatch in SessionStart Hook

`session_start_hook.py` derives project name from `git rev-parse
--show-toplevel` → `Path(toplevel).name`. For repo `arcaneum`, this produces
`arcaneum`. The hook then calls `nx memory list --project arcaneum`.

The RDR workflow (via `/rdr-create` and the `rdr_hook.py`) populates T2 under
`{repo_name}_rdr` (e.g., `arcaneum_rdr`). These are different keys in the T2
store.

**Observed result**: 19 RDRs loaded in T2 under `arcaneum_rdr`. Hook queries
`arcaneum`. Hook reports: `No memory entries for 'arcaneum'`. Claude begins the
session with no RDR context.

**Affected scopes**: The session hook only queries the bare name. No other hook
or skill queries `{repo}_rdr` at session start. The `rdr_hook.py` handles RDR
reconciliation correctly but does not surface content into session context.

**Adjacent gap**: The PM workflow uses `{repo}_pm` (or the configured PM
project name). The session hook queries `{repo}` only. If a project uses any
namespaced sub-project (e.g., `myrepo_pm`, `myrepo_rdr`), none of those
entries surface at session start.

### Finding 2: T2 Entries Surfaced as Title Lists Only

When the session hook does find T2 entries (e.g., if content were stored under
the bare repo name), it surfaces up to 8 lines of `[id] title (timestamp)`.

Claude receives a list of titles it cannot act on without an explicit
`nx memory get --project {repo} --title {title}` call per entry. For a project
with 19 RDR entries, that is 19 round trips before Claude knows which entries
are relevant. In practice, Claude skips this and starts from zero context.

**Contrast**: The PM context injected by `nx pm resume` includes actual content
— phase, blockers, continuation notes. Claude can act on it immediately. T2
titles are not actionable.

### Finding 3: Code Collection Precision Failure — No Warning

7 targeted queries against `code__arcaneum-2ad2825c` were run with known
canonical target files:

| Query | Expected | Top file returned |
| ----- | -------- | ----------------- |
| `embedding model GPU acceleration FastEmbed` | `embeddings/client.py` | `errors.py` (65 chunks) |
| `chunk overlap tokenizer source code indexing` | `indexing/markdown/chunker.py` | `main.py` (80 chunks) |
| `MeiliSearch full text search index documents` | `fulltext/client.py` | `qdrant_indexer.py` (84 chunks) |
| `class EmbeddingClient` | `embeddings/client.py` | `uploader.py` (60 chunks) |
| `class SourceCodePipeline` | `indexing/source_code_pipeline.py` | `output.py` (81 chunks) |
| `Qdrant vector collection creation named vectors` | `collections/client.py` | `ssl_config.py` (55 chunks) |
| `markdown document sync directory indexing` | `indexing/markdown/` | `test_search_text.py` (39 chunks) |

**0 of 7 canonical files appear in top-3 results.** Large files produce
hundreds of chunks; those chunks dominate semantic similarity for any query
because they collectively cover more topic surface area. Root cause is
documented in nexus RDR-006 (chunk size configuration).

The `nexus` reference skill recommends `nx search "query" --corpus code` as a
primary workflow step with no caveat. An agent following this guidance will
receive misleading results with no signal that anything is wrong.

**Contrast**: The same queries run against `rdr__arcaneum` (focused documents)
returned the correct canonical RDR as top-1 hit in 5 of 6 cases. The precision
problem is specific to `code__` collections indexed with default chunk sizes.

**Contrast**: Grep (`rg` via the Grep tool) correctly locates `class
EmbeddingClient` in `embeddings/client.py` with zero false positives. For code
navigation, Grep is currently more reliable than T3 semantic search.

### Finding 4: T2 FTS5 Constraint Undocumented

`nx memory search` uses FTS5 full-text search. Multi-word queries are tokenized
and OR-matched against stored content. This means:

- Query `"retain slash commands memory"` → finds RDR-015 (contains all four
  literal tokens) ✓
- Query `"semantic search"` → returns 19 results (both tokens appear in almost
  every RDR) — low precision
- Query `"embedding startup failure"` → finds nothing (no stored document
  contains these exact tokens even if the content covers the concept)

This is expected FTS5 behavior, but it is not documented in the `nexus` skill
or the `memory search` help text. Claude issues conceptual paraphrases as
queries, gets empty results, and concludes the store is empty. The actual
failure is vocabulary mismatch, not missing content.

**Observed in this session**: Multiple queries that semantically matched stored
content returned zero results when the query terms didn't appear verbatim in
the stored text.

### Critical Assumptions

- [x] The session hook T2 namespace mismatch is the primary cause of Claude
  seeing empty T2 at session start — **Status**: Verified — **Method**: Direct
  observation (19 entries in `arcaneum_rdr`, 0 in `arcaneum`)
- [x] Code collection precision failure affects any repo indexed with default
  chunk size and large files — **Status**: Verified — **Method**: Source
  inspection (RDR-006 root cause analysis) + 7/7 empirical query failures
- [x] Grep is more reliable than T3 code search for exact code navigation
  (class/function lookup) — **Status**: Verified — **Method**: Direct
  comparison in this session
- [ ] T2 FTS5 would correctly surface content if Claude used literal
  vocabulary from stored documents — **Status**: Documented (FTS5 behavior
  is well-specified) — **Method**: FTS5 specification + partial test results
- [ ] Adding content snippets to T2 session surfacing reduces round-trip
  `get` calls in practice — **Status**: Assumed — **Method**: Behavioral
  inference; not yet tested with Claude in a live session

## Proposed Solution

### Design Approach

Four targeted fixes, no architectural changes:

1. **Surface all populated sub-namespaces at session start** (hook change)
2. **Include 1-line content snippet per T2 entry** (hook change)
3. **Add FTS5 caveat and tool selection guide to nexus skill** (skill text)
4. **Warn on `nx index repo` for code collections without `--chunk-size`**
   (CLI guard)

### Fix 1: Surface All Sub-Namespaces in SessionStart Hook

`session_start_hook.py` currently queries only `{repo}`. Change it to
discover and surface all T2 namespaces matching the prefix `{repo}` using
a SQL `LIKE` prefix scan against the T2 SQLite database.

**Resolution of design choice**: The `nx memory list --project` CLI does not
support wildcards. However, the underlying T2 store is SQLite and the session
hook (`session_start_hook.py`) already imports `nexus.db.t2.T2Database`
directly (as demonstrated in `rdr_hook.py`). The hook must use the Python
T2 API with a prefix query, not suffix enumeration. Verified: `SELECT DISTINCT
project FROM memory WHERE project LIKE '{repo}%'` correctly returns
`arcaneum_rdr` when querying `arcaneum%`.

Suffix enumeration (`['', '_rdr', '_pm']`) is **rejected** — it creates a
silent maintenance trap where any new namespace convention (e.g., `_agent`,
`_sprint`) is permanently invisible until the hardcoded list is updated.

```python
# Proposed — T2Database prefix scan
from nexus.commands._helpers import default_db_path
from nexus.db.t2 import T2Database

with T2Database(default_db_path()) as db:
    namespaces = db.get_projects_with_prefix(project_name)
    # e.g., ['arcaneum_rdr', 'arcaneum_pm'] for prefix 'arcaneum'

for ns in namespaces:
    entries = db.get_all(project=ns)
    if entries:
        output_lines.append(f"## T2 Memory ({ns})")
        ...
```

If `get_projects_with_prefix()` does not yet exist in the T2Database API,
it must be added as part of Phase 1 implementation. The SQL is:
`SELECT DISTINCT project FROM memory WHERE project LIKE ? ORDER BY project`
with parameter `f'{project_name}%'`.

**Scope**: `session_start_hook.py` and `subagent-start.sh`. The shell script
(`subagent-start.sh`) currently uses the `nx memory list` CLI directly; it
must be converted to call a thin wrapper script (or `python3 -c`) that
performs the prefix scan, matching the Python hook's behavior.

### Fix 2: Content Snippet in T2 Session Surfacing

The hook currently surfaces `[id] title (timestamp)` per entry. Add the first
non-empty, non-heading line of content as a snippet (truncated to 120 chars):

```text
## T2 Memory (arcaneum_rdr)
[40] RDR-001-project-structure — CLI tool for Qdrant + MeiliSearch with named vectors and dual indexing
[42] RDR-015-retain-memory-management — Lightweight memory layer wrapping arc infrastructure for AI agent persistent memory
...
```

This allows Claude to triage relevance from session context without a round
trip per entry.

**Implementation**: After `nx memory list`, run `nx memory get` for entries
up to a configurable cap (default: 5 most recent). Extract first substantive
line. Display as `[id] title — {snippet}`.

**Alternative**: Extend `nx memory list` to support a `--snippet` flag that
returns id, title, and first-N-chars of content in one call. Cleaner API but
requires CLI change rather than hook logic.

**Cap**: Surface at most 8 entries with snippets; beyond that, show count
only. Prevents context bloat for large T2 stores.

### Fix 3: FTS5 Caveat and Tool Selection Guide in Nexus Skill

Add two sections to `nx/skills/nexus/reference.md`:

**Section: T2 search constraints**

```markdown
## T2 Search Constraints

`nx memory search` uses FTS5 full-text search — it matches literal tokens,
not semantic meaning. Rules:

- Use exact terms from the stored document, not conceptual paraphrases
- `"retain slash commands"` works if those words appear verbatim in content
- `"memory management plugin"` may return nothing if stored as "retain
  system" — even if the content is the same concept
- Multi-word queries are OR-matched: broad terms (e.g., "indexing") will
  match most entries
- When results are empty: try different vocabulary, not just different
  queries. Consider `nx memory list --project {repo}` to browse titles
  directly, then `nx memory get` by title.
```

**Section: Tool selection for code navigation**

```markdown
## Code Search: When to Use nx vs Grep

Use **Grep** (the Grep tool) for:
- Finding a class or function by name: `class EmbeddingClient`
- Locating all usages of a symbol: `EmbeddingClient`
- Exact text matches: error messages, config keys, import paths
- Any query where you know the literal text that will appear in the file

Use **nx search --corpus code__** for:
- Conceptual queries when you don't know the file name or function name
- Finding "what handles PDF processing" across an unfamiliar codebase
- Cross-file concept queries: "retry logic with exponential backoff"

**Current limitation**: `code__` collections indexed with default chunk
sizes have known precision issues — large files dominate results regardless
of query specificity (see RDR-006). Until re-indexed with smaller chunks,
prefer Grep for code navigation. Use `nx search --corpus rdr__` and
`--corpus docs__` freely — those collections have good precision.
```

### Fix 4: Guard in `nx index repo` for Code Collections

When indexing a code collection without `--chunk-size`, emit a warning if the
repo contains files large enough to produce chunk-count dominance.

```text
Warning: 12 files exceed the large-file threshold (largest:
src/arcaneum/cli/doctor.py, 847 lines). Default chunk size may reduce search
precision — large files produce many chunks that dominate semantic scoring.
Consider: nx index repo . --chunk-size 150
Run with --no-chunk-warning to suppress this message.
```

This surfaces the RDR-006 issue at the moment it can be prevented, rather
than after precision has already degraded. It does not block indexing.

**Implementation prerequisite**: Fix 4 is **blocked on RDR-006 acceptance**.
The threshold value (lines and/or bytes per file) must be derived from
RDR-006's chunk size analysis — specifically, the token-to-byte ratio and
default chunk token count that RDR-006 defines. The 200-line placeholder in
the warning example above must not be used as-is; it will be replaced with
the value from RDR-006 before Phase 3 begins.

**Phase 3 is therefore sequenced after RDR-006 is accepted**, not after
RDR-006 is merely drafted. Phase 1 and Phase 2 of this RDR have no dependency
on RDR-006 and may proceed independently.

**Rationale for prerequisite**: The 200-line / 8KB figure is not derived from
the empirical data in this RDR (the evidence table contains chunk counts, not
file sizes or line counts), and deriving it requires knowing the default chunk
token size — which is the subject of RDR-006. Implementing Fix 4 with an
arbitrary threshold trains users to suppress the warning, which defeats the
guard's purpose entirely.

## Alternatives Considered

### Alternative: Semantic T2 Search

Replace FTS5 with the same Voyage AI embedding pipeline used by T3. This
would eliminate Finding 4 entirely — conceptual queries would work.

**Reason for deferral**: T2 is designed to be local SQLite with no external
dependencies. Adding Voyage AI to T2 search changes the architecture
(requires API key, network call, latency). The current Fix 3 (documentation)
addresses the immediate adoption gap without architectural change. A
separate RDR should evaluate semantic T2 search as a feature.

### Alternative: Auto-populate T2 from Indexed Collections

When `nx index repo` indexes a codebase, automatically extract key
artifacts (RDRs, README, CLAUDE.md) and populate T2.

**Reason for deferral**: Auto-population raises questions about overwrite
behavior, namespace conventions, and when to re-populate. The session hook
fix (Fix 1) addresses the surfacing problem for content already in T2
without introducing auto-population complexity.

### Alternative: Single Unified Namespace (No `_rdr` Suffix)

Store all project memory under the bare repo name, eliminating the
namespace mismatch.

**Reason for rejection**: The `_rdr` suffix exists to separate concerns —
an agent querying `{repo}` for general project context doesn't need all
19 RDRs. The suffix convention allows namespaced access. Fix 1 (query
multiple namespaces at session start) preserves the separation while
ensuring all populated namespaces are surfaced.

## Trade-offs

### Consequences

- **Positive**: Claude begins sessions with actionable T2 context for the
  correct namespace, rather than seeing an empty store
- **Positive**: Skill documentation accurately represents T2 and T3 code
  collection capabilities — reduces silent failure
- **Positive**: `nx index repo` surfaces the chunk size issue before it
  causes precision degradation
- **Negative**: Session hook becomes slightly more verbose (multiple
  namespace queries) — mitigated by the 8-entry cap
- **Negative**: `nx index repo` warning may surprise users who don't know
  about chunk size configuration — mitigated by the suppress flag

### Risks and Mitigations

- **Risk**: Surfacing multiple T2 namespaces adds context bloat
  **Mitigation**: Apply the same 8-line cap per namespace; suppress
  namespaces that are empty
- **Risk**: Content snippets require additional `nx memory get` calls in
  the hook, adding latency
  **Mitigation**: Cap at 5 snippet-enriched entries; fall back to titles
  for the remainder. Or implement `nx memory list --snippet` to batch
  the retrieval
- **Risk**: "Use Grep over nx search" guidance becomes stale once
  RDR-006 is implemented
  **Mitigation**: The skill text already scopes the guidance to "until
  re-indexed with smaller chunks" — it self-expires when the collection
  is re-indexed

### Failure Modes

- **T2 sub-namespace query fails at session start**: Fail silently; the
  hook already handles individual query failures gracefully
- **Content snippet extraction finds no substantive line**: Fall back to
  title-only display; no regression from current behavior
- **`nx index repo` warning threshold miscalibrated**: User runs with
  `--no-chunk-warning` to suppress; the RDR-006 implementation can
  refine the threshold based on empirical testing

## Implementation Plan

### Phase 1: Hook Fixes (High Impact, Low Risk)

1. Update `session_start_hook.py` to query `{repo}`, `{repo}_rdr`,
   `{repo}_pm` at session start; surface non-empty namespaces separately
2. Add 1-line content snippet per T2 entry for up to 5 most recent entries
   in each namespace
3. Mirror changes in `subagent-start.sh`

### Phase 2: Skill Documentation Updates

4. Add "T2 Search Constraints" section to `nx/skills/nexus/reference.md`
5. Add "Code Search: When to Use nx vs Grep" section to same file
6. Update the `nx search --corpus code` example in the quick-reference
   table to note the precision caveat

### Phase 3: CLI Guard (prerequisite: RDR-006 accepted)

7. Obtain threshold value from RDR-006 implementation (token count per
   default chunk → bytes per chunk → large-file line/byte threshold)
8. Add large-file warning to `nx index repo` when code files exceed
   threshold and `--chunk-size` is not specified
9. Add `--no-chunk-warning` flag to suppress

## Test Plan

### Phase 1

- Load 19 RDRs into `{repo}_rdr` T2; start a new session; verify the
  session hook reports "T2 Memory (arcaneum_rdr): 19 entries" — not empty
- Verify snippet text appears for at least the first 5 entries
- Verify empty namespaces (e.g., `{repo}_pm` with no entries) do not
  appear in session context
- Verify `subagent-start.sh` shows the same T2 context as the main hook

### Phase 2

- Ask Claude to search T2 for a conceptual query using vocabulary not
  present verbatim in stored content; verify Claude's response acknowledges
  FTS5 limitation and suggests alternative vocabulary (not just "nothing
  found")
- Ask Claude to find a class by name in a repo; verify Claude uses Grep
  rather than `nx search --corpus code` as the first tool

### Phase 3

- Run `nx index repo` on a repo with files over threshold without
  `--chunk-size`; verify warning appears with the correct file count and
  largest file name
- Run with `--chunk-size 150`; verify no warning
- Run with `--no-chunk-warning`; verify warning suppressed

## Validation

The following scenarios should all pass after implementation:

1. **Session start with populated `_rdr` namespace**: Start a session in
   a project where `{repo}_rdr` contains RDR entries but `{repo}` is
   empty. Session context shows RDR entries with snippets.

2. **FTS5 query failure surfaces correctly**: Claude issues `nx memory
   search "embedding startup"` against a store containing content about
   embedding initialization. Response uses correct vocabulary ("T2 uses
   keyword search — try 'embedding initialization' or 'EmbeddingClient
   init'") rather than "nothing found."

3. **Code navigation uses Grep**: Ask Claude "where is class EmbeddingClient
   defined?" — Claude uses Grep, not `nx search --corpus code__`.

4. **Index warning fires**: Running `nx index repo` on arcaneum without
   `--chunk-size` emits the large-file warning listing `doctor.py`,
   `main.py`, `errors.py` as candidates for smaller chunks.

## Open Questions

- Should content snippets be added to `nx memory list` as a `--snippet`
  flag, or should the hook extract them via separate `get` calls? CLI
  change is cleaner but larger scope. Resolved choice deferred to
  implementation: start with hook-side `get` calls (no CLI change needed),
  migrate to `--snippet` flag if latency is measurable.
- Should "Use Grep over nx search" guidance be temporary (scoped to "until
  RDR-006 is resolved") or permanent (Grep is always the right first tool
  for exact code navigation)? The latter seems correct even with fixed
  chunk sizes — semantic search and exact search serve different use cases.
  Proposed answer: make the guidance permanent with two tiers — Grep for
  known-symbol lookups always; nx search for conceptual queries always.
  The RDR-006 caveat becomes a footnote, not the primary framing.

**Resolved**:

- ~~Prefix scan vs. suffix enumeration~~ — **Resolved**: Use T2Database
  prefix scan (`project LIKE '{repo}%'`). Suffix enumeration rejected as
  maintenance trap. See Fix 1.
- ~~Threshold for large-file warning~~ — **Resolved**: Phase 3 is blocked
  on RDR-006 defining the threshold. Placeholder value not used. See Fix 4.

## Revision History

### Gate Review (2026-02-28) — BLOCKED

### Critical — Resolved

**C1. Fix 1 namespace strategy left as open question — RESOLVED.** The
original draft listed prefix scan as an "alternative" without resolving the
choice, leaving implementers to pick between two approaches with materially
different maintenance implications. Fixed: resolved in favor of T2Database
prefix scan (`project LIKE '{repo}%'`). Suffix enumeration explicitly
rejected as a maintenance trap. `subagent-start.sh` conversion to Python
wrapper specified. `get_projects_with_prefix()` API addition required if not
present.

**C2. Fix 4 threshold undefined with no resolution path — RESOLVED.** The
200-line / 8KB threshold was a placeholder not derived from empirical data,
with "coordinate with RDR-006" as an unactionable instruction. Fixed: Phase 3
is now explicitly blocked on RDR-006 acceptance. The threshold must be
obtained from RDR-006's chunk size analysis before Fix 4 can be implemented.
Rationale for the prerequisite documented.

**C3. Finding 3 framing claimed full resolution when only mitigation delivered
— RESOLVED.** "These are fixable. None require architectural changes" implied
all four findings were addressed by RDR-007. Fixed: Summary now explicitly
states Finding 3 is mitigated (skill caveat + index warning) but not resolved.
Resolution requires RDR-006 to ship and collections to be re-indexed.
Motivation section corrected to match.
