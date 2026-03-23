---
title: "Claude Adoption: Session Context Gaps and Search Tool Guidance"
id: RDR-007
type: feature
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-02-28
accepted_date: 2026-02-28
closed_date: 2026-02-28
close_reason: implemented
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
   collection); recorded the top file returned for each query and compared
   against the expected canonical file (Finding 3 — all 7 failed)
5. Ran equivalent queries against `rdr__arcaneum-2ad2825c` (RDR T3
   collection); verified expected canonical RDR in top-3 results for each
6. Ran `nx memory search` with conceptual phrases vs. keyword-literal phrases
   against loaded T2 content

### Finding 1: T2 Namespace Mismatch in SessionStart Hook

`session_start_hook.py` derives project name from `git rev-parse
--show-toplevel` → `Path(toplevel).name`. For repo `arcaneum`, this produces
`arcaneum`. The hook then calls `nx memory list --project arcaneum`.

The RDR workflow (via `/nx:rdr-create` and the `rdr_hook.py`) populates T2 under
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
and AND-matched against stored content — all tokens must appear in a document
for it to match. This means:

- Query `"retain slash commands memory"` → finds RDR-015 (contains all four
  literal tokens) ✓
- Query `"semantic search"` → returns 19 results (both tokens appear in almost
  every RDR) — low precision
- Query `"embedding startup failure"` → finds nothing (no document contains
  all three tokens simultaneously; the query needs all tokens to co-occur)

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
support wildcards. The underlying T2 store is SQLite, and the T2Database Python
API supports direct SQL queries. The session hook (`session_start_hook.py`)
**currently uses only subprocess CLI calls** to `nx memory list` — it does
not import `nexus.db.t2.T2Database`. The existing example of direct T2Database
usage is `rdr_hook.py`, which imports and uses T2Database for RDR
reconciliation. Fix 1 requires converting `session_start_hook.py` from CLI
subprocess calls to T2Database direct API calls — this is a real implementation
step, not a trivial adjustment. The hook must use the Python T2 API with a
prefix query, not suffix enumeration. Verified: `SELECT DISTINCT project FROM
memory WHERE project LIKE '{repo}%'` correctly returns `arcaneum_rdr` when
querying `arcaneum%`.

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

`get_projects_with_prefix()` must be added to T2Database as part of Phase 1
implementation. The SQL must order namespaces by most-recent write (to support
Fix 2's recency preference in the cross-namespace cap):

```sql
SELECT project, MAX(timestamp) AS last_updated
FROM memory
WHERE project LIKE ?
GROUP BY project
ORDER BY MAX(timestamp) DESC
```

Parameter: `f'{project_name}%'`. Note: `search_glob()` uses SQLite GLOB
syntax. Resolved: `get_projects_with_prefix()` uses LIKE for the prefix
scan, as shown in the SQL above. Either works functionally; LIKE was chosen
as standard SQL for prefix queries.

**Error handling**: The T2Database context manager must be wrapped in
`try/except Exception` with silent fallback — a hook that propagates uncaught
T2Database exceptions will corrupt session startup. Pattern: `rdr_hook.py`
wraps T2Database in try/except; `session_start_hook.py` catches all exceptions
silently in `run_command()`. Implement Fix 1 to the same standard: on any
T2Database failure, return empty namespace list and log to stderr only if debug
mode is enabled.

**Scope**: `session_start_hook.py` and `subagent-start.sh`. Wrapper specification:

- **New file**: `hooks/scripts/t2_prefix_scan.py` — canonical implementation
  of the prefix scan, snippet extraction, and cap algorithm. Accepts a repo
  name as `argv[1]` and writes the formatted T2 context block to stdout.
  `session_start_hook.py` **imports and calls** a function from this module
  rather than duplicating the logic — `t2_prefix_scan.py` is the single source
  of truth for the cap algorithm; `session_start_hook.py` calls it and
  assembles it alongside PM context and beads output.
- **`subagent-start.sh` change**: Replace the `nx memory list --project
  "$PROJECT"` call with `python3 "$CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py" "$PROJECT"`
- **Unchanged in `subagent-start.sh`**: PM context injection (`nx pm resume`
  / `nx pm status`), beads display (`bd ready`) — these are separate concerns
  that do not touch T2 memory and require no modification

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

**Implementation**: Fix 1's `db.get_all(project=ns)` already returns full
entry content — no separate `nx memory get` subprocess calls are needed. For
each namespace, sort entries by recency, iterate through the list, and extract
the first non-empty, non-heading line from the `content` field, truncated to
120 chars. Display as `[id] title — {snippet}`.

**Cap algorithm** (per namespace, applied in recency order):

- Entries 1–5: title + 1-line snippet
- Entries 6–8: title only
- Beyond 8: count only — `... (N more entries — use nx memory list to browse)`
- **Cross-namespace hard cap**: 15 total entries across all namespaces in a
  single session context injection. If multiple namespaces are populated,
  allocate proportionally and favour the most recently written namespace.

**Cap interaction order**: Apply the per-namespace rules (snippet/title/count)
first to each namespace independently, then truncate the combined entry list to
15, preserving entries from the most recently written namespace first. The
snippet/title thresholds are fixed (entries 1–5 get snippets regardless of
how many namespaces are present) — only the total number of fully-rendered
entries is capped at 15.

This prevents context bloat for projects with large T2 stores while still
surfacing actionable content for the most relevant entries.

**Alternative**: Extend `nx memory list` to support a `--snippet` flag that
returns id, title, and first-N-chars of content in one call. Cleaner API but
requires CLI change. The direct `get_all()` approach has no per-entry latency
(single SQL query); the `--snippet` flag alternative is deferred until there
is a reason to add a CLI call to a workflow that the API path already handles.

### Fix 3: FTS5 Caveat, Tool Selection Guide, and Naming Convention Correction

Fix 3 makes three changes to `nx/skills/nexus/reference.md`:

**Change A — Correct the namespace naming convention.** The current
`reference.md` states: "**Project naming**: use bare `{repo}` for all project
memory (e.g., `nexus`). No `_active` or `_pm` suffixes." This directly
contradicts the actual namespace conventions in use (`{repo}_rdr`,
`{repo}_pm`). Replace with:

```markdown
**Project naming**: Use purpose-specific suffixes for different memory domains:

- bare `{repo}` — general project memory and notes
- `{repo}_rdr` — RDR documents and gate results (populated by `/nx:rdr-create`)
- `{repo}_pm` — project management context (populated by `nx pm`)

The session hook discovers all populated namespaces by prefix scan; content
stored under any `{repo}_*` namespace will surface at session start.
```

**Change B — Add "T2 Search Constraints" section** (new section, add two):

Add to `nx/skills/nexus/reference.md`:

**Change B — "T2 Search Constraints" section**

```markdown
## T2 Search Constraints

`nx memory search` uses FTS5 full-text search — it matches literal tokens,
not semantic meaning. Rules:

- Use exact terms from the stored document, not conceptual paraphrases
- `"retain slash commands"` works if those words appear verbatim in content
- `"memory management plugin"` may return nothing if stored as "retain
  system" — even if the content is the same concept
- Multi-word queries are AND-matched: all tokens must appear somewhere in the
  document. A broad single term (e.g., "indexing") matches many entries; a
  three-term query returns only documents containing all three tokens
- When results are empty: drop one term at a time to identify which token
  has no match. Consider `nx memory list --project {repo}` to browse titles
  directly, then `nx memory get` by title.
- **Title searches always return empty**: the FTS5 index covers `content`
  and `tags` only — not `title`. Searching for an entry by its title (e.g.,
  `nx memory search "RDR-007"`) will find nothing even if that entry exists.
  Use `nx memory get --project {repo} --title {title}` for title-based lookup.
```

**Change C — "Code Search: When to Use nx vs Grep" section**

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
Warning: 12 files exceed the large-file threshold (200 lines; largest:
src/arcaneum/cli/doctor.py, 847 lines). Default chunk size may reduce search
precision — large files produce many chunks that dominate semantic scoring.
Consider: nx index repo . --chunk-size 80
Run with --no-chunk-warning to suppress this message.
```

This surfaces the RDR-006 issue at the moment it can be prevented, rather
than after precision has already degraded. It does not block indexing.

**Implementation prerequisite**: Fix 4 is **blocked on RDR-006 acceptance**.
The chunker (`src/nexus/chunker.py`) operates in **lines**, not tokens
(`_CHUNK_LINES = 150`). The large-file warning threshold must therefore be
expressed in lines — not bytes or tokens. The correct derivation is: target
chunk line count defined by RDR-006 → files where total line count
significantly exceeds that target → warning threshold in lines. The 200-line
placeholder in the warning example above must not be used as-is; it will be
replaced with a value derived from RDR-006's target chunk size before Phase 3
begins. Note: RDR-006 research finding R5 identifies 60–80 lines as the likely
target (not 150, which is the current default); Fix 4's threshold must
reflect whatever value RDR-006 finalizes.

**Phase 3 is therefore sequenced after RDR-006 is accepted**, not after
RDR-006 is merely drafted. Phase 1 and Phase 2 of this RDR have no dependency
on RDR-006 and may proceed independently.

**Rationale for prerequisite**: The 200-line placeholder figure is not derived
from the empirical data in this RDR (the evidence table contains chunk counts,
not file line counts), and calibrating it correctly requires knowing the target
chunk line count — which is the subject of RDR-006. Implementing Fix 4 with
an arbitrary threshold trains users to suppress the warning, defeating the
guard's purpose entirely. An incorrect threshold is worse than no threshold.

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
- **Risk**: `get_all()` for large namespaces loads more content than the cap
  algorithm will display
  **Mitigation**: `get_all()` is a single indexed query (`project = ?` is
  indexed); for typical T2 stores the cost is negligible. If profiling shows
  measurable latency for stores with hundreds of entries, add `LIMIT N` to
  the SQL query
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

1. Convert `session_start_hook.py` from CLI subprocess calls to direct
   T2Database API usage; add `get_projects_with_prefix()` to T2Database if
   not present; use `project LIKE '{repo}%'` prefix scan to discover all
   populated namespaces; surface non-empty namespaces separately
2. Add 1-line content snippet per T2 entry: snippets for entries 1–5,
   title-only for 6–8, count-only beyond 8, cross-namespace hard cap of 15
3. Implement `hooks/scripts/t2_prefix_scan.py` wrapper; update
   `subagent-start.sh` to call it instead of `nx memory list`

### Phase 2: Skill Documentation Updates

4. Add "T2 Search Constraints" section to `nx/skills/nexus/reference.md`
5. Add "Code Search: When to Use nx vs Grep" section to same file
6. Update the `nx search --corpus code` example in the quick-reference
   table to note the precision caveat

### Phase 3: CLI Guard (prerequisite: RDR-006 accepted)

7. Obtain target chunk line count from accepted RDR-006 (chunker uses
   lines — `_CHUNK_LINES`; threshold = file line count that significantly
   exceeds the target chunk line count)
8. Add `--chunk-size N` option to `nx index repo` that overrides
   `_CHUNK_LINES` for the indexed repository — confirm whether RDR-006
   already adds this flag; if so, skip this step
9. Add large-file warning to `nx index repo` when code files exceed
   threshold and `--chunk-size` is not specified
10. Add `--no-chunk-warning` flag to suppress

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

- ~~Content snippets via `--snippet` flag or hook-side calls?~~ —
  **Resolved**: hook-side `get_all()` call (single SQL query returns full
  content for all entries; no per-entry calls and no CLI change needed).
  Migrate to `--snippet` flag only if a use case requires batch snippet
  retrieval from a CLI context.
- ~~Should "Use Grep over nx search" guidance be temporary or permanent?~~
  — **Resolved**: implemented as temporary in Fix 3 Change C ("until
  re-indexed with smaller chunks"). The permanent two-tier framing (Grep
  for known-symbol lookups always; nx search for conceptual queries always)
  is the long-term target; re-evaluate after RDR-006 ships and collections
  are re-indexed. The RDR-006 caveat is the primary framing for now.

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

### Gate Review 2 (2026-02-28) — BLOCKED

### Critical Issues — Resolved (Gate 2)

**NC1. Fix 1 falsely claimed `session_start_hook.py` already imports T2Database
— RESOLVED.** Fix 1 stated "the session hook already imports
`nexus.db.t2.T2Database` directly (as demonstrated in `rdr_hook.py`)." This
is factually wrong: `session_start_hook.py` uses only subprocess CLI calls
throughout; only `rdr_hook.py` imports T2Database. Fixed: Fix 1 now explicitly
states the hook **currently uses subprocess CLI only** and that converting it
to T2Database direct API calls is a real implementation step. Phase 1, step 1
updated to match (removes suffix enumeration language; specifies T2Database
conversion and `get_projects_with_prefix()` addition).

### Significant — Resolved

**NS1. Snippet cap numbers inconsistent across the document — RESOLVED.** Fix
2 cited three different numbers (5 most recent, 8 entries with snippets, 5
snippet-enriched with fallback) with no canonical algorithm. Fixed: Fix 2 now
defines a single explicit algorithm — entries 1–5 get snippets, entries 6–8
get titles only, beyond 8 shows count only, with a cross-namespace hard cap of
15 total. Risk mitigation and Phase 1 step 2 updated to match.

**NS2. Fix 3 omitted the contradictory naming guidance in `reference.md` —
RESOLVED.** `reference.md` line reads "No `_active` or `_pm` suffixes"
directly contradicting the `_rdr` and `_pm` namespace conventions. Fix 3 now
includes Change A: replacing that line with the correct three-tier namespace
documentation (`{repo}`, `{repo}_rdr`, `{repo}_pm`) before adding the two new
sections (FTS5 caveat and code search guide).

**NS3. `subagent-start.sh` conversion underspecified — RESOLVED.** Fix 1
Scope section now names the new wrapper (`hooks/scripts/t2_prefix_scan.py`),
specifies its interface (argv[1] = repo name; stdout = structured namespace/
entry data), documents the exact shell substitution, and explicitly identifies
which portions of the shell script are unchanged (PM context injection, beads
display).

### Gate Review 3 (2026-02-28) — BLOCKED

### Critical Issues — Resolved (Gate 3)

**NC3-1. Fix 4 threshold derivation chain used tokens, not lines — RESOLVED.**
Fix 4 described the threshold derivation as "token count per default chunk →
bytes per chunk → large-file threshold." The actual chunker (`chunker.py`)
operates in lines (`_CHUNK_LINES = 150`), not tokens. Fixed: derivation chain
now reads "target chunk line count from RDR-006 → files where line count
significantly exceeds target → warning threshold in lines." Phase 3 step 1 and
the Rationale paragraph updated to match. RDR-006 R5 finding (60–80 lines
as likely target) noted explicitly. Warning example updated: `--chunk-size 150`
(the current default — no improvement) replaced with `--chunk-size 80`.

**NC3-2. "200-line placeholder" referenced nonexistent text — RESOLVED.** Fix 4
referred to "the 200-line placeholder in the warning example above" but no
200-line value appeared in the warning block. Fixed: restored explicit `200
lines` to the warning example's threshold display so the cross-reference is
valid. The rationale text updated to remove "200-line / 8KB" and replace with
"the 200-line placeholder figure" — making clear it is a placeholder, not a
derived value.

**NC3-3. Testing Methodology step 4 contradicted Finding 3 — RESOLVED.** Step 4
said "verified expected canonical file in top-3 results for each" — the
opposite of Finding 3's conclusion (0/7 canonical files in top-3). Fixed: step
4 now reads "recorded the top file returned for each query and compared against
the expected canonical file (Finding 3 — all 7 failed)." Step 5 retains
"verified" framing since RDR queries did return expected results.

### Gate Review 4 (2026-02-28) — BLOCKED

### Significant Issues — Resolved (Gate 4)

**NS4-1. Fix 1 SQL ordered namespaces alphabetically; Fix 2 requires recency
order — RESOLVED.** The original SQL `SELECT DISTINCT project ... ORDER BY
project` returns alphabetical order. Fix 2's cap algorithm favours the most
recently written namespace, which requires timestamp ordering. Fixed: SQL
replaced with `SELECT project, MAX(updated_at) ... GROUP BY project ORDER BY
MAX(updated_at) DESC`.

**NS4-2. Fix 1 lacked try/except error handling requirement — RESOLVED.** The
pseudocode showed raw T2Database calls with no exception handling. Both
`rdr_hook.py` (T2Database) and `session_start_hook.py` (`run_command`) use
silent fallback on failure. Fixed: Fix 1 now explicitly requires wrapping the
T2Database context manager in `try/except Exception` with empty-namespace
fallback and debug-mode stderr logging.

**NS4-3. Fix 2 described CLI subprocess path that Fix 1 eliminated — RESOLVED.**
Fix 2 said "After `nx memory list`, run `nx memory get` for the 5 most recent
entries." Fix 1's `db.get_all(project=ns)` already returns full content — no
subprocess calls needed. Fixed: Fix 2's implementation now reads "Fix 1's
`db.get_all()` already returns full entry content — extract snippet from
`content` field directly."

**NS4-4. Change B omitted that FTS5 does not index titles — RESOLVED.** `nx
memory search "RDR-007"` silently returns nothing even if that entry exists,
because FTS5 only indexes `content` and `tags`. Fixed: added explicit bullet to
Change B: "Title searches always return empty — use `nx memory get --title`
for title-based lookup."

### Gate Review 5 (2026-02-28) — BLOCKED

### Critical Issues — Resolved (Gate 5)

### Gate Review 6 (2026-02-28) — BLOCKED

### Critical Issues — Resolved (Gate 6)

**NC6-1. FTS5 uses AND semantics, not OR — RDR stated the opposite throughout —
RESOLVED.** Finding 4 body said "Multi-word queries are tokenized and OR-matched"
and Change B said "Multi-word queries are OR-matched." SQLite FTS5 default query
syntax uses implicit AND — all tokens must appear somewhere in the document for
a match. The empirical evidence in Finding 4 is only consistent with AND:
`"embedding startup failure"` returns nothing because no document contains all
three tokens simultaneously (under OR it would match any document containing
"embedding" alone, which would be most of the RDR store). Fixed: "OR-matched"
corrected to "AND-matched" in Finding 4 and Change B; debugging guidance updated
to "drop one term at a time" rather than "try different vocabulary."

### Significant Issues — Resolved (Gate 6)

**NS6-1. Risks section described latency from per-entry `nx memory get` calls —
eliminated by NS4-3 — RESOLVED.** The stale risk described subprocess-based
per-entry calls; NS4-3 switched to a single `db.get_all()` query. Fixed: risk
updated to describe the actual remaining risk (get_all loading more content than
the cap displays) with correct mitigation (LIMIT clause if needed).

**NS6-2. Fix 1's LIKE vs. GLOB note read as an open design question — RESOLVED.**
The note said "consider whether... Either works; the choice should be consistent."
The Open Questions Resolved section already settled this (LIKE), but Fix 1 in
isolation appeared undecided. Fixed: note updated to state the resolution directly
("Resolved: LIKE was chosen as standard SQL for prefix queries").

**NS6-3. Phase 3 implementation plan missing step to add `--chunk-size` CLI
option — RESOLVED.** Phase 3 step 8 conditioned the warning on `--chunk-size`
not being specified, and the warning example suggests `nx index repo . --chunk-size
80`, but `nx index repo` currently has no `--chunk-size` flag. Fixed: added Phase
3 step 8 to add `--chunk-size N` option (or confirm RDR-006 adds it); renumbered
subsequent steps.

**NS6-4. Open Questions "proposed answer" contradicted Fix 3 Change C — RESOLVED.**
The Open Questions section had a live "proposed answer" (make Grep guidance
permanent with two tiers) that contradicted Change C's temporary framing. Fixed:
both Open Questions marked resolved with explicit conclusions.

**NC5-1. Fix 1 SQL used `updated_at` column which does not exist in T2 schema —
RESOLVED.** The Gate 4 fix for NS4-1 introduced `MAX(updated_at) AS last_updated`
and `ORDER BY MAX(updated_at) DESC`. The T2 schema (`src/nexus/db/t2.py`) has no
`updated_at` column — the timestamp column is named `timestamp`. This would raise
`sqlite3.OperationalError: no such column: updated_at` at runtime, which the
`try/except Exception` block (introduced in NS4-2) would silently swallow,
returning an empty namespace list and reproducing Finding 1 exactly despite the
fix appearing correct on paper. Fixed: both occurrences of `updated_at` in the
Fix 1 SQL replaced with `timestamp`; confirmed against `idx_memory_timestamp` in
`t2.py`.
