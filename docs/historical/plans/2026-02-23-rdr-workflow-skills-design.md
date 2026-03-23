# RDR Workflow Skills — Design Document

**Date:** 2026-02-23
**Status:** Approved
**Type:** Feature
**Priority:** High
**Revision:** 3 (incorporates deep-critic post-implementation audit findings)

## Problem Statement

The RDR (Recommendation Decisioning Records) process is a rigorous planning methodology for capturing technical decisions before implementation. It enforces structured research, alternative evaluation, and finalization gates — but today it is entirely manual. Creating an RDR means copying a template, manually assigning an ID, maintaining a README index by hand, and tracking research findings as unstructured prose.

Nexus provides the data infrastructure (T2 memory, T3 semantic search, agent orchestration) to automate the RDR lifecycle while preserving its core value: disciplined thinking before code.

## Design Decisions

### Separation of Concerns

- **`nx`** = data infrastructure (store, search, index, memory)
- **`/nx:rdr-*`** = workflow orchestration as Claude Code slash command skills (live in the `nx/` plugin)
- **`bd`** = execution tracking (beads for implementation work)

RDR skills invoke `nx` under the hood for persistence but present a workflow-oriented interface to the user. This keeps `nx` focused on data and avoids coupling planning methodology into the CLI.

### Identity

Sequential numeric IDs, zero-padded to 3 digits: `001`, `002`, `003`. Project prefix derived from repo name (e.g., `NX-001`, `ART-002`).

This matches the convention established in the arcaneum project's 19 existing RDRs.

### Status Vocabulary

This design follows the canonical rdr repo terminology:

```
Draft → Final → Implemented | Reverted | Abandoned | Superseded
```

**Note:** The arcaneum project historically used "Recommendation" instead of "Draft" and "In Progress" instead of "Final". This design standardizes on the canonical rdr repo vocabulary. Existing arcaneum RDRs retain their original terminology; new RDRs use "Draft".

### Type Vocabulary

From the canonical rdr template:

```
Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
```

### Storage — Hybrid Model

| Layer | What | Why |
|-------|------|-----|
| **Filesystem** | `docs/rdr/NNN-title.md` | Source of truth for content. Version-controlled, PR-reviewable. |
| **T2** | `{repo}_rdr` namespace | Structured metadata: status, type, priority, research findings, linked beads, timestamps. Queryable. All records written with `--ttl permanent`. |
| **T3** | `docs__rdr__{repo}` collection | Semantic index of RDR content (via `nx index rdr`) and permanent archive on close. Uses `voyage-context-3` CCE embeddings. Searchable cross-project via `--corpus docs__rdr` prefix fan-out. |

Active RDRs live in the filesystem (for collaboration) with structured metadata in T2 (for querying). RDR content is semantically indexed to T3 via `nx index rdr` for prior-art search. Closed RDRs are archived to T3 with divergence metadata for institutional memory.

**Why `docs__rdr__` not `knowledge__rdr__`:** The `docs__` namespace routes to `voyage-context-3` (Contextualized Contextual Embedding) which embeds each chunk with awareness of surrounding chunks — critical for RDRs where individual sections derive meaning from the broader document context. The `knowledge__` namespace uses the same model but `docs__` is semantically correct: RDRs are structured documents, not atomic knowledge entries.

**T2 TTL policy:** All RDR records use `--ttl permanent`. RDRs can span months from creation to close. The default 30-day TTL would silently expire active RDR metadata, causing `/nx:rdr-gate` to report zero assumptions and `/nx:rdr-list` to omit active RDRs.

**T3 title uniqueness:** T3 document IDs are derived from `sha256(collection:title)`. The T3 title must include the project prefix (e.g., `NX-003 Semantic Search Pipeline`) to guarantee uniqueness across projects that share a `docs__rdr__` collection prefix.

### Lifecycle

```
/nx:rdr-create ──author──> /nx:rdr-research ──refine──> /nx:rdr-gate ──pass──> /nx:rdr-close
                             ^                        |
                             └────fail, iterate────────┘

/nx:rdr-show    (inspect one)
/nx:rdr-list    (inspect all)
```

Status transitions: `Draft → Final → Implemented | Reverted | Abandoned | Superseded`

## Nexus Infrastructure Additions

### `nx index rdr [path]`

New CLI command (~15 lines) that reuses the existing `doc_indexer.batch_index_markdowns()` pipeline:

```
nx index rdr [repo_path]   # discover docs/rdr/*.md, index to docs__rdr__{repo}
```

**Pipeline:** Discovers `docs/rdr/*.md` → parses YAML frontmatter (status, type, priority) → `SemanticMarkdownChunker` splits by heading hierarchy → `voyage-context-3` CCE embeddings → upserts to `docs__rdr__{repo}` collection.

This gives RDRs proper semantic chunking (each section like "Problem Statement", "Research Findings", "Alternatives" becomes a distinct searchable chunk) instead of the 150-line code-oriented chunks they would get from `nx index code`.

### SessionStart Hook Enhancement

New hook script in `nx/hooks/scripts/rdr_hook.py`:

1. Detects `docs/rdr/` in the current repo
2. Checks whether `docs__rdr__{repo}` collection exists in T3
3. If not indexed: prints "RDRs found but not indexed — run `nx index rdr`"
4. If indexed: prints collection stats so agents know prior-art search is available

### Server Auto-Reindex (v2, deferred)

Future enhancement: after `check_and_reindex()`, inspect `git diff --name-only` for `docs/rdr/` changes and dispatch to `batch_index_markdowns()`. Not in scope for initial implementation.

## Skill Specifications

### `/nx:rdr-create`

**Trigger:** User says "create an RDR", "new RDR", or `/nx:rdr-create`

**Agent:** None — mechanical scaffolding.

**Inputs:**
- Title (required) — e.g., "Bulk PDF Indexing with OCR Support"
- Type (optional, default: Feature) — Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
- Priority (optional, default: Medium) — High | Medium | Low
- Related issues (optional) — bead IDs or URLs

**Behavior:**
1. **Bootstrap** (first use only): create `docs/rdr/` directory if absent, copy `TEMPLATE.md` from the rdr repo, initialize `docs/rdr/README.md` with index table header
2. Scan `docs/rdr/` for highest existing number → next ID is N+1, zero-padded to 3 digits
3. Derive project prefix from repo name, stripping non-alphanumeric characters (e.g., `nexus` → `NEX`, `nx-tools` → `NXT`)
4. Create `docs/rdr/NNN-kebab-title.md` from the RDR template with metadata pre-filled (date, status: Draft, type, priority)
5. Write T2 record (see T2 invocation pattern below)
6. Regenerate `docs/rdr/README.md` index table from T2 records (handles empty T2 on first create by also scanning filesystem)
7. `git add` the new files

**Does NOT:** Create beads, run validation, or commit.

**Output:** Prints the new RDR path and ID.

---

### `/nx:rdr-research`

**Trigger:** User says "add research finding", "update RDR research", or `/nx:rdr-research`

**Agent:** `deep-research-synthesizer` for gathering evidence. `codebase-deep-analyzer` for code-specific questions.

**Inputs:**
- RDR ID (required) — e.g., `003`
- Subcommand: `add | status | verify`

**`add <id>`:**
1. Prompts for finding text (what was discovered)
2. Prompts for classification: Verified (✅) | Documented (⚠️) | Assumed (❓)
3. Prompts for verification method: Source Search | Spike | Docs Only
4. Prompts for source — code path, URL, experiment description
5. Determines next sequence number by listing T2 entries for `{repo}_rdr` and filtering titles matching `NNN-research-*`, parsing seq numbers, incrementing
6. Writes structured record to T2 (see T2 invocation pattern below)
7. Appends a formatted entry to the RDR's Research Findings section in the markdown file

**`status <id>`:**
1. Lists T2 entries for `{repo}_rdr` project, filters titles matching `NNN-research-*`
2. Displays summary: N verified, N documented, N assumed
3. Lists all assumed findings with their verification method — findings marked "Docs Only" on load-bearing assumptions are the highest risk
4. Shows verification method breakdown: N source search, N spike, N docs only

**`verify <id> <finding-seq>`:**
1. Promotes an assumed finding to verified or documented
2. Updates verification method if changed (e.g., docs only → source search)
3. Updates both T2 record and the emoji marker in the markdown file

**Design rationale:** Structured T2 data enables programmatic queries ("show all unverified assumptions across open RDRs", "show all docs-only findings on load-bearing assumptions") without parsing markdown prose. The markdown document remains the authoritative narrative; T2 is the queryable index. The `verification_method` field preserves the rdr process's core taxonomy (Source Search / Spike / Docs Only) — the most load-bearing distinction in the methodology.

---

### `/nx:rdr-gate`

**Trigger:** User says "gate this RDR", "finalization check", or `/nx:rdr-gate`

**Agent:** `substantive-critic` for Layer 3 AI critique.

**Input:** RDR ID (required)

**Three validation layers, run in sequence:**

**Layer 1 — Structural Validation (instant, no AI):**
- Required sections present and non-empty: Problem Statement, Context, Research Findings, Proposed Solution, Alternatives Considered, Trade-offs, Implementation Plan
- At least one alternative evaluated
- Implementation Plan has numbered steps
- Fails hard if any missing — no point running AI on an incomplete document

**Layer 2 — Assumption Audit (from T2, no AI):**
- List T2 entries for `{repo}_rdr`, filter titles matching `NNN-research-*`
- Warn on any remaining Assumed (❓) findings
- Highlight findings with verification method "Docs Only" on load-bearing assumptions
- Display count: "3 verified, 1 documented, 2 assumed (1 docs-only)"
- User can accept risk: "proceed with unverified assumptions" (recorded in T2 as acknowledged)

**Layer 3 — AI Critique (`substantive-critic` agent):**
- Reads the full RDR markdown
- Queries T3 for related prior RDRs: `nx search "query" --corpus docs__rdr` (prefix fan-out across all repos' RDR collections)
- If no prior RDRs found in T3 (cold-start), produces: "No prior RDRs archived yet. Cross-project prior-art search will improve as RDRs are closed and indexed."
- Evaluates the finalization gate checklist:
  - Internal contradictions between sections
  - Missing failure modes in trade-offs
  - Scope creep (implementation plan exceeds problem statement)
  - Cross-cutting concerns not addressed
  - Proportionality (solution complexity vs. problem severity)
- Produces a structured critique with pass/warn/fail per criterion
- Surfaces relevant prior decisions when found: "RDR-009 in arcaneum addressed a similar problem — consider its trade-offs"

**Gate aggregation rule:** Any fail blocks the gate. Warns count as pass — surfaced to the user but do not block. This matches the assumption audit pattern where the user can accept risk. The author must still complete the Finalization Gate section in the markdown with written responses — the AI critique supplements but does not replace the author's gate responses.

**On pass:** Status updated to Final in T2 and markdown metadata. Index regenerated. Runs `nx index rdr` to update T3 semantic index.

**On fail:** Critique displayed with specific sections to address. Status remains Draft. User iterates and re-runs.

---

### `/nx:rdr-close`

**Trigger:** User says "close this RDR", "RDR done", or `/nx:rdr-close`

**Agents:**
- `knowledge-tidier` (Haiku) — archives post-mortem to T3 (post-mortem only; bead creation done directly by skill)
- `substantive-critic` (if divergence reported) — classifies divergence patterns

**Inputs:**
- RDR ID (required)
- Reason: Implemented | Reverted | Abandoned | Superseded (required)

**If Implemented:**
1. Prompt for divergence notes — "Did implementation diverge from the plan?"
2. If diverged: create `docs/rdr/post-mortem/NNN-title.md` from the canonical post-mortem template. Populate the Drift Classification table with user-provided divergence categories: Unvalidated assumption | Framework API detail | Missing failure mode | Missing Day 2 operation | Deferred critical constraint | Over-specified code | Under-specified architecture | Scope underestimation | Internal contradiction | Missing cross-cutting concern
3. **Decompose into beads** (directly by skill — structured text extraction from a known schema):
   - Parse the Implementation Plan section for phases/steps
   - Create an epic bead titled after the RDR
   - Create child beads for each phase/major step
   - Wire dependencies: `bd dep add <child> <epic>`
   - Display the bead tree for user confirmation before committing
4. Update status to Implemented in T2 + markdown metadata
5. Run `nx index rdr` to update T3 semantic index (CCE embeddings, section-level chunks)
6. **Archive post-mortem** (if any) via `knowledge-tidier` to `knowledge__rdr_postmortem__{repo}` with drift category tags (making drift patterns searchable cross-project). The main RDR is **not** duplicated via `nx store put` — the `nx index rdr` CCE index is the authoritative T3 representation.
7. Regenerate index

**Failure handling:** The close operation performs multiple state mutations (markdown, beads, T2, T3, index). If any step fails:
- T2 tracks an `archived: false` flag that can be retried independently
- Each step emits clear status so a failure is diagnosable
- The skill reports which steps completed and which failed
- Failed steps can be retried by re-running `/nx:rdr-close` (idempotent: checks T2 state before repeating completed steps)

**If Reverted or Abandoned:**
1. Prompt for reason (free text)
2. Create `docs/rdr/post-mortem/NNN-title.md` from the post-mortem template (the rdr process requires post-mortems for reverted and abandoned RDRs, not just implemented ones)
3. Record in T2 + markdown metadata
4. Archive to T3 (research findings are valuable even for failed RDRs)
5. No beads created
6. Regenerate index

**If Superseded:**
1. Prompt for superseding RDR ID
2. Cross-link both RDRs bidirectionally:
   - Old RDR: set `superseded_by: "NNN"` in T2 and markdown
   - New RDR: set `supersedes: "MMM"` in T2 and markdown
3. Run `nx index rdr` to update T3 semantic index
4. Regenerate index

**Does NOT:** Force close if gate hasn't passed (warns, allows `--force`). Delete the markdown file. Auto-commit.

---

### `/nx:rdr-show`

**Trigger:** User says "show RDR", or `/nx:rdr-show`

**Agent:** None — read-only display.

**Input:** RDR ID (optional — defaults to most recently modified)

**Displays:**
- Status, type, priority, dates (created, gated, closed)
- Research summary: N verified / N documented / N assumed
- Verification method breakdown: N source search / N spike / N docs only
- Linked beads (epic + children with their statuses)
- Superseded-by / supersedes links
- Post-mortem drift categories if closed with divergence

---

### `/nx:rdr-list`

**Trigger:** User says "list RDRs", or `/nx:rdr-list`

**Agent:** None — read-only query.

**Displays:** Index table: ID | Title | Status | Type | Priority

**Filters:** `--status=draft`, `--type=feature`, `--has-assumptions` (any unverified)

**Default:** All open (non-closed) RDRs.

---

## Agent Mapping

| Skill | Agent | Role |
|-------|-------|------|
| `/nx:rdr-create` | — | Mechanical scaffolding |
| `/nx:rdr-research add` | `deep-research-synthesizer` | Fan out across web, codebase, T3, DEVONthink to gather and classify evidence |
| `/nx:rdr-research add` (code) | `codebase-deep-analyzer` | Deep code exploration for code-specific research questions |
| `/nx:rdr-gate` (Layer 3) | `substantive-critic` | Structural flaws, contradictions, unvalidated assumptions, proportionality |
| `/nx:rdr-gate` (prior art) | `nx search --corpus docs__rdr` | Prefix fan-out across all repos' RDR collections |
| `/nx:rdr-close` (decompose) | — (direct) | Parse Implementation Plan into beads via `bd create` + `bd dep add` |
| `/nx:rdr-close` (archive) | `knowledge-tidier` | Archive post-mortem to `knowledge__rdr_postmortem__{repo}` |
| `/nx:rdr-close` (divergence) | `substantive-critic` | Classify divergence patterns if post-mortem created |
| `/nx:rdr-show` | — | Read-only display |
| `/nx:rdr-list` | — | Read-only query |

**Agent selection rationale:** The original design used `strategic-planner` (Opus) for bead decomposition. Rev 2 delegated to `knowledge-tidier` (Haiku). Rev 3 moved bead creation back into the skill itself — the Implementation Plan section has a defined structure (Phase N / Step N headings), the skill already has `Bash` access, and splitting bead creation from archival simplifies failure handling. `knowledge-tidier` is now only used for post-mortem archival to T3 (its core strength: knowledge organization).

## Filesystem Layout

```
docs/
└── rdr/
    ├── README.md                    # Auto-generated index table
    ├── TEMPLATE.md                  # RDR template (copied from rdr repo on first use)
    ├── 001-project-structure.md     # Individual RDR documents
    ├── 002-semantic-search.md
    ├── ...
    └── post-mortem/
        ├── TEMPLATE.md              # Post-mortem template (copied from rdr repo)
        ├── 001-project-structure.md # Per-RDR post-mortem
        └── ...
```

**Bootstrapping:** `/nx:rdr-create` handles first-use initialization: creates `docs/rdr/` and `docs/rdr/post-mortem/` directories, copies templates, initializes the README index. No separate bootstrap command needed.

**README index merge conflicts:** The auto-generated `docs/rdr/README.md` will conflict in multi-contributor workflows. This is acceptable — the file is trivially regenerable from T2 state and filesystem contents. Consider adding a `.gitattributes` entry marking it as auto-generated.

## T2 Schema

Project namespace: `{repo}_rdr`

**T2 invocation pattern:** The `nx memory put` command requires a positional content argument. All RDR T2 writes use heredoc piping:

```bash
nx memory put - --project {repo}_rdr --title {title} --ttl permanent --tags rdr <<'EOF'
{yaml content}
EOF
```

**Per RDR (title: `NNN`):**
```yaml
id: "003"
prefix: "NX"
title: "Semantic Search Pipeline"
status: "Draft"           # Draft | Final | Implemented | Reverted | Abandoned | Superseded
type: "Feature"           # Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
priority: "Medium"        # High | Medium | Low
created: "2026-02-23"
gated: ""                 # date when gate passed
closed: ""                # date when closed
close_reason: ""          # Implemented | Reverted | Abandoned | Superseded
superseded_by: ""         # RDR ID if this RDR was superseded
supersedes: ""            # RDR ID if this RDR supersedes another
epic_bead: ""             # bead ID of the epic created on close
archived: false           # false at creation; true after successful T3 archive; retryable
file_path: "docs/rdr/003-semantic-search.md"
```

**Per research finding (title: `NNN-research-{seq}`):**
```yaml
rdr_id: "003"
seq: 1
finding: "PyMuPDF is 95x faster than pdfplumber for text extraction"
classification: "verified"           # verified | documented | assumed
verification_method: "source_search" # source_search | spike | docs_only
source: "Benchmark: tests/bench_pdf.py, 1000 pages, M2 MacBook Pro"
acknowledged: false                  # true if assumed but accepted at gate
```

**Research finding retrieval:** T2 has no title-prefix filter. The skills retrieve all research findings by calling `nx memory list --project {repo}_rdr` and filtering entries where the title matches the pattern `NNN-research-*` client-side. This is O(N) where N is the total number of T2 records in the project — acceptable for typical projects but may be slow for projects with 30+ RDRs. Skills should validate that parsed records have `rdr_id` and `seq` fields (not just matching title pattern) before using them. Sequence numbers are determined by parsing existing titles and incrementing.

**Alternative considered (timestamp keys):** Using `NNN-research-20260223T143022Z` instead of sequential numbers would avoid the enumeration problem but produces less human-readable output. Sequential numbers are preferred for consistency with the RDR ID convention.

## T3 Schema

Collection: `docs__rdr__{repo}`

**Embedding model:** `voyage-context-3` (CCE — Contextualized Contextual Embedding). Selected automatically by the `docs__` prefix in `index_model_for_collection()`.

**Semantic index (via `nx index rdr`):**
```yaml
# Metadata per chunk (from batch_index_markdowns pipeline):
source_path: "docs/rdr/003-semantic-search.md"
source_title: "Semantic Search Pipeline"    # from frontmatter
section_title: "Research Findings > Key Discoveries"  # heading breadcrumb
store_type: "markdown"
corpus: "rdr__nexus"
embedding_model: "voyage-context-3"
content_hash: "..."
indexed_at: "2026-02-23T..."
```

**Post-mortem archive** (on close, if post-mortem exists):

Collection: `knowledge__rdr_postmortem__{repo}` (voyage-4, separate from CCE-indexed RDR content)

```yaml
title: "NX-003 Semantic Search Pipeline (post-mortem)"
category: "rdr-post-mortem"
tags: "rdr,post-mortem,diverged:unvalidated-assumption,diverged:scope-underestimation"
source_agent: "rdr-close"
ttl_days: 0                                 # permanent
```

**Note:** The main RDR is **not** archived via `nx store put`. The `nx index rdr` CCE pipeline (Step 4 of close) produces section-level chunks with `voyage-context-3` embeddings — these are the authoritative T3 representation. Storing a duplicate blob via `nx store put` would create voyage-4 entries in the same collection, degrading semantic search quality.

**Cross-project search:** `resolve_corpus()` treats arguments containing `__` as exact collection names, not prefixes. Therefore `--corpus docs__rdr` does **not** fan out. The skills use collection enumeration for cross-project search:

```bash
# In /nx:rdr-gate Layer 3:
collections=$(nx collection list | grep '^docs__rdr__')
for col in $collections; do
  nx search "query" --corpus "$col" --n 5
done
```

**Repo name length constraint:** ChromaDB enforces 63-character collection names. `docs__rdr__` is 11 characters, leaving 52 for the repo name. Repo names exceeding 52 characters are truncated, with the full name stored in document metadata.

**Cold-start behavior:** On first use (no `docs__rdr__*` collections exist), `/nx:rdr-gate` Layer 3 gracefully reports "No prior RDRs indexed. Cross-project prior-art search will improve as RDRs are indexed and closed." T3 search handles non-existent collections via `_ChromaNotFoundError` → `continue`.

## Integration Points

```
nx index rdr ──────────────────────> docs__rdr__{repo} (T3, semantic index)
                                     SemanticMarkdownChunker + voyage-context-3

/nx:rdr-create ─────────────────────> docs/rdr/NNN.md (filesystem)
                                    nx memory put - ... --ttl permanent (T2)

/nx:rdr-research ──agent──> findings > docs/rdr/NNN.md (append to Research Findings)
                                    nx memory put - ... --ttl permanent (T2, structured)

/nx:rdr-gate ──────agent──> critique > nx search --corpus docs__rdr (T3, prior art)
             substantive-critic     nx memory put - ... (T2, status=Final)
                                    nx index rdr (T3, re-index updated content)

/nx:rdr-close ─────direct──> beads ──> bd create (epic + children)
                                    bd dep add (wiring)
                                    docs/rdr/post-mortem/NNN.md (if diverged)
           ─────direct──> state ──> nx memory put - ... (T2, status=closed)
                                    nx index rdr (T3, re-index final state)
           ─────agent───> archive > nx store put (post-mortem only, to
             knowledge-tidier        knowledge__rdr_postmortem__{repo})
```

## Bootstrap: Seeding Existing RDRs

For repos with existing RDRs (e.g., arcaneum with 19 RDRs) that predate this system:

```bash
# Index existing RDRs for semantic search:
nx index rdr /path/to/repo

# Optionally seed T2 metadata for /nx:rdr-list and /nx:rdr-show:
# A one-time /nx:rdr-import skill could scan docs/rdr/*.md,
# parse frontmatter, and create T2 records. Not in initial scope
# but a natural follow-up.
```

The `nx index rdr` command works immediately on any repo with `docs/rdr/*.md` — no migration needed for semantic search. T2 metadata is only needed for the structured query features (`/nx:rdr-list --has-assumptions`, etc.) and can be populated incrementally.

## What This Does NOT Cover

- **RDR content authoring** — The skills manage lifecycle, not prose. The author writes the RDR.
- **Automated research execution** — `/nx:rdr-research add` records findings; the agent helps gather them but the human classifies.
- **PR integration** — RDR files are version-controlled and naturally participate in PRs, but there's no GitHub-specific automation.
- **Cross-repo RDR dependencies** — T3 archive enables cross-project search, but there's no formal dependency graph between RDRs in different repos.
- **Server auto-reindex of RDRs** — Deferred to v2. Currently requires manual `nx index rdr` after changes.
- **Filesystem-T2 sync** — If a user edits an RDR's markdown metadata directly (e.g., changes status), T2 is not automatically updated. The skills are the intended interface; direct edits may cause T2/filesystem drift. A future `/nx:rdr-sync` command could reconcile.

## Revision History

**Rev 2 (2026-02-23):** Incorporated findings from plan-auditor (6 defects, 4 gaps), substantive-critic (3 critical, 5 significant, 4 observations), and codebase-deep-analyzer (indexing pipeline analysis). Key changes:
- Status vocabulary: "Recommendation" → "Draft" (canonical rdr repo terminology)
- Type field: dropped "Enhancement", added "Framework Workaround" (canonical template)
- T2 writes: added explicit `--ttl permanent`, documented heredoc invocation pattern
- T3 collection: `knowledge__rdr__` → `docs__rdr__` (correct embedding model + chunking)
- T3 search: documented `resolve_corpus()` limitations, specified enumeration approach
- Research schema: added `verification_method` field (Source Search / Spike / Docs Only)
- Post-mortem: adopted canonical `post-mortem/` directory with separate template (not inline)
- Bead decomposition: `strategic-planner` → `knowledge-tidier` (proportionate agent selection)
- Added: `nx index rdr` command, SessionStart hook, bootstrapping, failure handling, gate aggregation rule, cold-start behavior, research seq enumeration strategy, T3 title uniqueness constraint, repo name length constraint

**Rev 3 (2026-02-24):** Incorporated findings from deep-critic post-implementation audit (3 critical, 5 significant). Key changes:
- C1: Fixed `archived: true` → `false` at creation (was breaking close idempotency)
- C2: Replaced glob patterns with exact file paths in archive commands
- C3: Removed `nx store put` for main RDR (redundant with CCE index); post-mortem archived to separate `knowledge__rdr_postmortem__{repo}` collection
- S4: Fixed contradictory cross-project search explanation
- S5: Added `tr -cd '[:alnum:]'` to prefix derivation (hyphenated repo names)
- S6: Moved bead creation from knowledge-tidier relay to skill-direct execution
- S7: Documented O(N) T2 retrieval limitation with validation guidance
- S8: Added bidirectional supersession backlinks (`supersedes` field)
