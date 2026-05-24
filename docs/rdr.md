# RDR: Research-Design-Review

An RDR is a specification document written *before* implementation. It captures the problem, the research journey, competing options, and the chosen approach, then locks that decision so implementation has a stable target.

The core insight: complex features are bigger than what fits in working memory, whether yours or an LLM's. Without a locked specification, purpose drift sets in as new problems emerge during coding, side-quests derail the original vision, and you end up coding your way out of corners instead of designing your way around them. An RDR front-loads the thinking so implementation can focus on execution.

**The rule is mostly about timing.** Iterate on the RDR freely during drafting; at acceptance it becomes the reference for what was decided. If implementation proves the design wrong, don't rewrite the accepted RDR to match what shipped; abandon it and draft a new one with what you learned. Iteration on a decision lives in the chain of RDRs, not in any single accepted document.

## Quick start

1. `/conexus:rdr-create`: creates a new file with metadata prefilled, status set to Draft
2. `/conexus:rdr-research add <id>`: appends a finding with an evidence classification tag
3. `/conexus:rdr-gate <id>`: runs 3-layer validation (structure check, assumption audit, AI critique). Optional but recommended for irreversible decisions.
4. `/conexus:rdr-accept <id>`: locks the decision, sets status to Accepted
5. `/conexus:rdr-close <id> --reason implemented`: archives the RDR, creates a post-mortem template, indexes to T3

Steps 3–5 add rigor for high-stakes decisions. For a straightforward bug fix, steps 1–2 plus writing the solution may be all you need.

## When to write one

Write an RDR *before implementing* when any of these apply:

- The problem has multiple viable solutions and you need to choose
- External constraints (API limits, vendor behavior, library quirks) will shape the design
- You're about to make a change that others depend on
- A bug requires root-cause analysis, not just a patch
- A previous decision turned out to be wrong and you're correcting course
- The feature is complex enough that you'd lose track of why you're making specific choices mid-implementation

Not every decision needs an RDR. If the rationale is self-evident from the code, skip it. But if you find yourself three hours into implementation wondering "why did I go this way?", that's the RDR you should have written first.

## Right-sizing

Match depth to the decision's complexity.

| Scenario | Sections needed | Example |
|---|---|---|
| **Minimal** (bug, single option) | Problem + Root Cause + Fix | AST line-range bug: splitter returns empty metadata |
| **Full** (architecture, multiple options) | All sections | Four-store T3 architecture with quota enforcement |

If you can state the problem, root cause, and fix in one paragraph, that IS the RDR. Don't add sections to look thorough.

## Evidence classification

Each research finding is tagged so readers (both human and agent) know what is solid and what needs further validation.

| Classification | Meaning |
|---|---|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external documentation only |
| **Assumed** | Unverified belief based on experience or inference |

Flag assumptions that your design depends on. Low-stakes assumptions need no verification; load-bearing ones should be explicitly visible so they can be challenged or validated later.

## The iterative pattern

RDRs are iterative across a project, not within a single document. Write one, lock it, build against it, learn from what you find. If the design turns out to be wrong, abandon the RDR and write a new one capturing what you learned. Each RDR builds on what earlier ones established, and the corpus grows into institutional memory.

Research may reveal that one RDR needs to split into several; that's normal. Cross-reference related RDRs to maintain conceptual integrity. Stack them by dependency so implementation order is clear.

The Nexus project has produced over 125 RDRs across its development. The corpus is searchable, so when starting a new design, prior decisions surface automatically, preventing contradictions and avoiding redundant investigation.

## Statuses and types

```
Draft --> Accepted --> Implemented
                           |
                       Reverted / Abandoned / Superseded
```

| Status | Meaning |
|---|---|
| **Draft** | Created, research in progress |
| **Accepted** | Gate passed, decision formally accepted |
| **Implemented** | Implementation complete, archived to T3 |
| **Reverted** | Implementation rolled back |
| **Abandoned** | Dropped before implementation |
| **Superseded** | Replaced by a newer RDR (linked via `superseded_by`) |

**Types**: Feature, Bug Fix, Technical Debt, Framework Workaround, Architecture.

## Using RDR in your project

RDR works in any repository; it doesn't require the Nexus CLI or plugin. The tooling amplifies RDRs with search, validation, and agent context, but the core value is the document itself.

**Minimal setup (no tooling):**

1. Create `docs/rdr/` in your repo
2. Copy the [template](#rdr-template) into `docs/rdr/TEMPLATE.md`
3. Write your first RDR: Problem Statement + Research Findings + Proposed Solution is enough

**With Nexus CLI + plugin:**

1. `/conexus:rdr-create` bootstraps the directory, templates, and README automatically on first use
2. `/conexus:rdr-research`, `/conexus:rdr-gate`, `/conexus:rdr-accept`, `/conexus:rdr-close` manage the full lifecycle
3. RDRs are auto-indexed by `nx index repo` and searchable via `nx search --corpus rdr`

---

# Workflow

The operational details of each lifecycle step.

## Lifecycle

```
/conexus:rdr-create
     │
  [Draft] ◄── /conexus:rdr-research (repeat as needed)
     │
     │ /conexus:rdr-gate (optional but recommended)
     │ ├─ BLOCKED → fix and re-gate
     │ └─ PASSED
     ▼
/conexus:rdr-accept
     │
  [Accepted]
     │
     │ optional: planning chain → implementation beads
     │
     │ /conexus:rdr-close --reason implemented
     ▼
[Implemented]

Terminal states: Reverted · Abandoned · Superseded
```

Only **Create** and **Research** are required. Gate, Accept, and Close add formal validation and archival; use them when the decision is load-bearing.

## Worked example

A bug fix RDR from create to close:

```
/conexus:rdr-create
  Title: "Fix: chunker emits full-file line range for every AST chunk"
  Type: Bug Fix   Priority: High
  → Creates docs/rdr/rdr-016-fix-chunker-line-range.md (status: draft)

/conexus:rdr-research add 016
  Finding: CodeSplitter never populates line_start/line_end
  Classification: Verified (Source Search, chunker.py:210)

/conexus:rdr-gate 016
  Structure ✓ · Assumptions ✓ · AI critique ✓ → PASSED

/conexus:rdr-accept 016
  Verifies gate, updates status → accepted

/conexus:rdr-close 016 --reason implemented
  Creates post-mortem template, indexes to T3
```

The RDR is now searchable via `nx search --corpus rdr` and tracked in T2.

## Create (`/conexus:rdr-create`)

Prompts for title, type, and priority. Creates `docs/rdr/NNN-kebab-title.md` from the standard template with metadata prefilled, writes a T2 record, and regenerates the RDR index. Status: **Draft**.

On first use in a repository, `/conexus:rdr-create` bootstraps the `docs/rdr/` directory and copies the template automatically.

## Research (`/conexus:rdr-research`)

Adds structured findings to a Draft RDR. Each finding records a summary, evidence classification (Verified, Documented, or Assumed), verification method, and source reference.

Verification methods:

- **Source Search**: API or behavior verified against dependency source code
- **Spike**: behavior verified by running code against a live service
- **Docs Only**: documentation reading only; insufficient for load-bearing assumptions

For complex investigations, `/conexus:rdr-research` can delegate to specialized agents (`deep-research-synthesizer` for web/document research, `codebase-deep-analyzer` for codebase exploration). Findings are written to both the markdown file and T2. The RDR stays Draft throughout.

## Gate (`/conexus:rdr-gate`)

Three-layer validation. Optional but recommended before committing to irreversible decisions.

**Layer 1, Structural**: Required sections filled, metadata complete, at least one research finding present.

**Layer 2, Assumption audit**: Every Assumed finding must have a risk assessment. Each critical assumption must acknowledge what happens if it's wrong.

**Layer 3, AI critique**: Delegates to the `substantive-critic` agent, which evaluates logical coherence, missing alternatives, unstated assumptions, and evidence gaps. Findings are appended to the RDR.

The gate either **BLOCKS** (critical issues; fix and re-gate) or **PASSES** (no critical issues, may have observations). No conditional outcomes. The result is stored in T2 for `/conexus:rdr-accept` to verify.

## Accept (`/conexus:rdr-accept`)

The decision point. The gate validates; acceptance is a deliberate human choice.

Verifies that the gate passed, updates T2 status to Accepted, updates the file frontmatter to match, and regenerates the index. For multi-phase implementation plans, `/conexus:rdr-accept` optionally dispatches the planning chain: `strategic-planner` agent → `nx_plan_audit` MCP tool → `nx_enrich_beads` MCP tool, decomposing the work into trackable beads.

If T2 and the file disagree on status, `/conexus:rdr-accept` self-heals by repairing the file to match T2.

## Close (`/conexus:rdr-close`)

Finalizes an Accepted RDR. Requires status Accepted (use `--force` to override).

Close reasons: `implemented` · `reverted` · `abandoned` · `superseded`

Closing creates a post-mortem template for drift analysis, indexes the RDR into the `rdr__` collection for permanent semantic retrieval, and updates T2 with the close date and reason. If beads were created during accept, their status is displayed as an advisory. If the [catalog](catalog.md) is initialized, closing also creates typed links: `supersedes` for superseded RDRs, `cites` for referenced research papers.

## Querying RDRs

```bash
/conexus:rdr-list                      # all RDRs
/conexus:rdr-list --status Draft       # active research only
/conexus:rdr-list --type "Bug Fix"     # bug fixes only

/conexus:rdr-show 007                  # full detail: metadata, findings, gate status, linked beads
```

Both commands read from T2; no markdown parsing required.

## T2 synchronization

T2 is the process authority for RDR status; the markdown file is the human-readable persistence layer. On session start, a reconciliation hook ensures they agree using a monotonic-advance rule: status only moves forward, never regresses. If a human edits the file ahead of T2, T2 catches up. If T2 is ahead (e.g., a file write failed), the file is repaired.

---

# Nexus Integration

RDR documents live in the repository as markdown, but Nexus makes them queryable through both structured metadata (T2) and semantic search (T3). Agents and team members don't parse files or remember which RDR covered a topic; they search by meaning and get relevant decisions back.

## How agents use RDRs

The typical agent workflow touches the storage tiers at each stage:

1. **Before new work**: search T3 for prior RDRs with `nx search "topic" --corpus rdr`. New designs often build on or refine earlier decisions; the search surfaces that chain automatically.
2. **During research**: `/conexus:rdr-research` can delegate to `deep-research-synthesizer` or `codebase-deep-analyzer` for investigation that goes beyond what a single agent session can cover.
3. **At gate time**: `substantive-critic` provides independent review of the RDR's logic, evidence, and completeness.
4. **At accept time**: `/conexus:rdr-accept` updates T2 metadata, then optionally dispatches the planning chain: `strategic-planner` agent → `nx_plan_audit` MCP tool → `nx_enrich_beads` MCP tool.
5. **After close**: `/conexus:rdr-close` archives the full RDR to T3 for permanent semantic retrieval.

## T2: structured metadata

Each RDR has a T2 record in the `{repo}_rdr` project, providing structured access to status, type, priority, timestamps, and linked beads without parsing markdown.

```bash
nx memory search "caching" --project myrepo_rdr
nx memory search "status:draft" --project nexus_rdr
```

Key fields include `id`, `status`, `type`, `priority`, `accepted_date`, `epic_bead` (links to implementation tracking), and `file_path`. T2 is the authoritative source for RDR state; the markdown file is the human-readable persistence layer.

Timestamps (`created`, `gated`, `accepted_date`, `closed`) let you reconstruct which decisions were active at any point in time.

## T3: semantic search

RDRs are indexed into `rdr__<repo>` collections using `voyage-context-3` embeddings. This happens two ways:

- **`nx index repo`** auto-discovers `docs/rdr/*.md` and indexes them during normal repo indexing. Draft RDRs are findable immediately; no need to wait for `/conexus:rdr-close`.
- **`/conexus:rdr-close`** indexes the RDR explicitly at close time as part of permanent archival.

```bash
nx search "caching strategy" --corpus rdr
nx search "chromadb quota" --corpus rdr --n 5
```

Cross-project search works automatically: decisions from one project surface when researching similar problems in another.

The highest-signal chunks are typically the **Problem Statement** (best for determining relevance) and the **Proposed Solution** (surfaces implementation approach and trade-offs). Evidence classification is visible in result metadata, so agents can distinguish validated constraints from working assumptions without loading the full document. The `file_path` metadata allows loading the complete RDR when more depth is needed.

## Beads integration

`/conexus:rdr-accept` optionally decomposes the Implementation Plan into beads (epic + tasks) via the planning chain. The `epic_bead` T2 field links each accepted decision to its implementation work items. Session hooks inject T2 context and the active bead into spawned agents, so they pick up where the previous session left off.

## Catalog: document registry and link graph

When the [catalog](catalog.md) is initialized, RDR lifecycle skills create typed links that connect RDRs to each other and to the broader knowledge base:

| Lifecycle stage | What happens in the catalog |
|---|---|
| `nx index rdr` / `nx index repo` | RDR document registered with tumbler, title from frontmatter, content_type=rdr |
| `/conexus:rdr-research add` | `cites` link from RDR to referenced paper (if indexed in catalog) |
| `/conexus:rdr-gate` | Prior-art search uses `catalog_search` + `catalog_links` before falling back to T3 |
| `/conexus:rdr-accept` | `relates` links to topically related RDRs found during planning |
| `/conexus:rdr-show` | Displays inbound/outbound catalog links (implements-heuristic, cites, supersedes) |
| `/conexus:rdr-close` (Superseded) | `supersedes` link between new and old RDR |
| `/conexus:rdr-close` (Implemented) | `cites` links from RDR to referenced research papers |
| Indexer hook | `implements-heuristic` links from code files to RDRs (title substring match) |

`nx catalog links "RDR-051"` shows which code implements it, what research it cites, and what it supersedes, without parsing markdown.

All catalog steps are skipped silently if the catalog isn't initialized. T2 and the markdown file remain the authorities.

## Learning from post-mortems

`/conexus:rdr-close` creates a post-mortem template for drift analysis. Findings from post-mortems feed directly into the next RDR:

- **Unvalidated assumption** → add a Spike task to the next RDR's research phase.
- **Missing failure mode** → add explicit failure mode entries to the risk section.
- **Framework API detail wrong** → search T3 and verify against source before writing the Proposed Solution.
- **Scope creep** → narrow the Problem Statement; if the problem has two parts, write two RDRs.
- **Implementation diverged from design** → flag the delta in the post-mortem and open a follow-up RDR.

---

# RDR Template

**Right-size to the decision.** Fill what helps; skip what doesn't. A quick bug-fix RDR may need only Problem Statement, Research Findings, and Proposed Solution. A high-stakes architecture decision warrants everything.

## Minimal RDR Example

A complete bug-fix RDR at its leanest:

````markdown
---
title: "Fix: chunker emits full-file line range for every AST chunk"
id: RDR-016
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
created: 2026-03-03
related_issues: []
related_tests: [test_indexer_chunk_flow.py]
---

# RDR-016: Fix: chunker emits full-file line range for every AST chunk

## Problem Statement

Every code chunk shows `Lines: 1-780` regardless of actual position.
`CodeSplitter` returns empty metadata; the fallback assigns full-file extent.
Result: context prefixes are useless; all chunks from a file look identical.

## Research Findings

**Root cause** (Verified, source search `chunker.py:210`):
`setdefault("line_start", 1)` / `setdefault("line_end", len(lines))` fires
for every node. `CodeSplitter` never populates `line_start`/`line_end`.

**Fix available** (Verified, llama-index source):
`TextNode.start_char_idx` / `end_char_idx` are populated.
Convert: `content[:start_char_idx].count('\n') + 1` → exact 1-indexed line.

## Proposed Solution

Replace the `setdefault` fallback at `chunker.py:210–212` with character-to-line
conversion using `start_char_idx`/`end_char_idx`. No dependency changes needed.

## Implementation Plan

1. Fix `chunker.py:210–212`
2. Add `test_ast_chunk_line_range_populated` in `test_chunker.py`
3. Re-index affected collections (`code__*`)
````

## Full Template

Location: `docs/rdr/TEMPLATE.md` (copied from `conexus/resources/rdr/TEMPLATE.md` on first use).

### Metadata

```yaml
---
title: "RDR Title"
id: RDR-NNN
type: Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
status: draft | accepted | implemented | reverted | abandoned | superseded
priority: high | medium | low
author: Author Name
reviewed-by: self | reviewer name(s)
created: YYYY-MM-DD
accepted_date: # YYYY-MM-DD, set by /conexus:rdr-accept
related_issues: []
related_tests: []
implementation_notes: ""
---
```

`reviewed-by: self` is acceptable for solo projects. Collaborative projects require at least one reviewer other than the author. `related_tests` lists test files or test names that validate this RDR's implementation, populated at close time. `implementation_notes` captures deviations from the plan, filled in at close time.

### Sections

| Section | Purpose |
|---------|---------|
| **Problem Statement** | The specific challenge or requirement |
| **Context** | Background and technical environment |
| **Research Findings** | Investigation, source verification, key discoveries, critical assumptions |
| **Proposed Solution** | Approach, technical design, existing infrastructure audit, decision rationale |
| **Alternatives Considered** | Full analysis for serious alternatives; one-sentence rejection for trivial ones |
| **Trade-offs** | Consequences, risks and mitigations, failure modes |
| **Implementation Plan** | Prerequisites, minimum viable validation, phased steps, Day 2 operations, new dependencies |
| **Test Plan** | Specific scenarios covering edge cases and failure modes |
| **Validation** | Testing strategy and performance expectations |
| **Finalization Gate** | Contradiction check, assumption verification, scope check, cross-cutting concerns, proportionality |
| **References** | Requirements, dependency docs, related issues |
| **Revision History** | Gate findings appendix; keeps design sections clean |

### Critical Assumptions

Each load-bearing assumption is a checkbox with verification status:

```markdown
- [ ] [Assumption 1]
  - **Status**: Verified | Unverified
  - **Method**: Source Search | Spike | Docs Only
```

| Method | Description |
|--------|-------------|
| **Source Search** | API verified against dependency source code |
| **Spike** | Behavior verified by running code against a live service |
| **Docs Only** | Documentation reading only; insufficient for load-bearing assumptions |

## Post-Mortem Template

Location: `docs/rdr/post-mortem/TEMPLATE.md` (copied from `conexus/resources/rdr/post-mortem/TEMPLATE.md`).

Created automatically by `/conexus:rdr-close`. Fill it after implementation to analyze drift between what was decided and what was built.

### Sections

| Section | Purpose |
|---------|---------|
| **RDR Summary** | 2–3 sentence summary of the proposal |
| **Implementation Status** | Implemented / Partially Implemented / Not Implemented / Reverted |
| **Implementation vs. Plan** | What matched, diverged, was reused, added, or skipped |
| **Drift Classification** | Categorized divergences for cross-RDR pattern analysis |
| **RDR Quality Assessment** | What the RDR got right, missed, or over-specified |
| **Key Takeaways** | Actionable improvements to the RDR process |

### Drift Classification

| Category | Description |
|----------|-------------|
| Unvalidated assumption | A claim presented as fact but never verified |
| Framework API detail | Method signatures, interface contracts, or config syntax wrong |
| Missing failure mode | What breaks or fails silently was not considered |
| Missing Day 2 operation | Bootstrap, CI/CD, removal, rollback, or migration not planned |
| Deferred critical constraint | Downstream use case that validates the approach was out of scope |
| Over-specified code | Implementation code substantially rewritten |
| Under-specified architecture | Architectural decision that should have been made but wasn't |
| Scope underestimation | Sub-feature that grew into its own major effort |
| Internal contradiction | Research findings conflicting with the proposal |
| Missing cross-cutting concern | Versioning, licensing, config, deployment model, etc. |

Each category has a **Count**, **Examples**, and **Preventable?** column (values: `Yes -- source search`, `Yes -- spike`, or `No`).

### RDR Quality Assessment

Three dimensions:
- **What the RDR got right**: research and decisions worth repeating
- **What the RDR missed**: wrong assumptions, overlooked constraints
- **What the RDR over-specified**: code rewritten, features deferred, config never used

---

See [the RDR index](rdr/README.md) for the full catalog of accepted decisions.
