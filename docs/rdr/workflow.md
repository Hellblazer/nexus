# RDR Workflow

The only required steps are **Create** and **Research** — write the document,
record what you know. Gate, Close, and the rest are available when you need
formal validation or long-term archival, but a quick RDR that captures the
decision and moves on is perfectly valid.

A typical project cycle looks like: write a few RDRs, implement them, discover
what you didn't know, write more RDRs that refine or pivot from the earlier
ones. Later RDRs naturally reference earlier ones — a performance optimization
RDR might fix assumptions from a foundation RDR, or a new capability RDR might
extend an architecture decision. The sequential numbering makes this chain of
reasoning visible.

All operations are invoked via Claude Code slash commands.

---

## Status Model

RDR statuses (what the document records):

| Status | Meaning |
|--------|---------|
| **Draft** | In progress, not yet gated |
| **Accepted** | Author/reviewer decision after gate passes |
| **Implemented** | Code matches accepted design |
| **Reverted** | Implemented but rolled back |
| **Abandoned** | Work stopped before implementation |
| **Superseded** | Replaced by a later RDR |

The first three are the primary lifecycle. The last three are terminal states
for RDRs that don't reach or stay at Implemented. The `/rdr-close` command's
close reasons (Implemented, Reverted, Abandoned, Superseded) map directly to
the terminal statuses.

---

## Create (`/rdr-create`)

Creates a new RDR document and registers it in Nexus.

1. Prompts for **title**, **type** (Feature, Bug Fix, Technical Debt, Framework
   Workaround, Architecture), and **priority** (High, Medium, Low).
2. Assigns a **sequential ID** by scanning `docs/rdr/` for the highest existing
   number and incrementing. The ID is zero-padded to three digits (e.g., `007`).
3. Derives the **project prefix** from the repository name.
4. Creates the markdown file at `docs/rdr/NNN-kebab-title.md` from the standard
   template with the `## Metadata` section pre-filled.
5. Writes a **T2 metadata record** to the `{repo}_rdr` project containing the
   RDR's structured fields (id, prefix, title, status, type, priority, created,
   file_path).
6. **Regenerates `docs/rdr/README.md`** — the index table listing all RDRs with
   status, type, and title.
7. Stages the new files via `git add`.

After creation the RDR has status **Draft**. The document contains section
headings but no research content yet. Run `/rdr-research add <id>` to record
findings before gating.

---

## Research (`/rdr-research`)

Adds structured research findings to an active RDR (status: Draft).

Each finding contains:
- **Summary**: one-sentence description of what was learned
- **Classification**: Verified, Documented, or Assumed
- **Method**: how the finding was obtained
- **Source**: where the evidence came from

### Verification Methods

| Method | Description |
|---|---|
| `Source Search` | API verified against dependency source code |
| `Spike` | behavior verified by running code against a live service |
| `Docs Only` | based on documentation reading alone (insufficient for load-bearing assumptions) |

### Agent Delegation

For complex research, `/rdr-research` can delegate to specialized agents:
- **deep-research-synthesizer**: multi-source web and document research
- **codebase-deep-analyzer**: deep codebase exploration and pattern analysis

Findings are written to both the **markdown file** (human-readable Research
Findings table) and as **T2 records** (machine-queryable for agents).

The RDR remains in **Draft** status throughout the research phase.

---

## Gate (`/rdr-gate`)

Three-layer validation that determines whether an RDR is ready for implementation.

### Layer 1: Structural Validation
- All required sections are filled (Problem Statement, Context, Research Findings,
  Proposed Solution, Alternatives Considered, Trade-offs, Implementation Plan,
  Finalization Gate)
- Metadata section is complete
- At least one research finding exists

### Layer 2: Assumption Audit
- Every finding classified as **Assumed** must have an explicit risk assessment
- Each Critical Assumption must be acknowledged: what happens if it is wrong?
- If unacknowledged assumptions exist, the user is prompted for acknowledgment

### Layer 3: AI Critique
- Delegates to the **substantive-critic** agent for independent review
- The critic evaluates: logical coherence, missing alternatives, unstated
  assumptions, evidence gaps
- Critique findings are appended to the RDR document

### Outcomes

Gate outcomes (what the gate returns):
- **BLOCKED** — critical issues found, must fix and re-gate
- **PASSED** — no critical issues (may have significant/observations)

Do not use "Conditional Accept" or other ad-hoc outcomes. The gate either
blocks or passes. Acceptance is a separate decision by the author/reviewer
after the gate passes.

The gate writes its result to T2 as `{id}-gate-latest` for verification by
`/rdr-accept`.

---

## Accept (`/rdr-accept`)

Accepts an RDR after the gate passes. This is the author/reviewer decision
point — the gate validates, but acceptance is a deliberate human action.

1. Verifies the **T2 gate result** shows `outcome: "PASSED"`. Blocks if the
   gate has not passed or no gate result exists.
2. Updates **T2 first** (process authority): sets `status: "accepted"` and
   `accepted_date`.
3. Updates the **RDR file** frontmatter to match.
4. Regenerates `docs/rdr/README.md`.
5. Stages modified files via `git add`.

Self-healing: if T2 already shows `accepted` but the file still shows `draft`,
`/rdr-accept` repairs the file to match T2.

---

## Close (`/rdr-close`)

Finalizes a gated RDR and sets up implementation tracking. Requires status:
Accepted or Final — close is hard-blocked otherwise. Use `--force` to override.
Close reasons: Implemented, Reverted, Abandoned, or Superseded.

1. Creates a **post-mortem template** at `docs/rdr/post-mortem/NNN-kebab-title.md`
   for future drift analysis.
2. **Decomposes** the RDR into beads: one epic bead for the overall effort, plus
   task beads for each implementation step identified in the Implementation Plan
   section.
3. **Indexes** the RDR content via `nx index rdr` for permanent semantic search
   into the `rdr__` collection.
4. Updates the **T2 metadata** record with close date, close reason, and status
   (e.g., Implemented).
5. Regenerates the `docs/rdr/README.md` index.

After closing, the RDR's decisions are discoverable via `nx search --corpus rdr`
across all projects.

---

## List (`/rdr-list`)

Displays the RDR index table with optional filters.

```
/rdr-list                          # all RDRs
/rdr-list --status Draft           # only active research
/rdr-list --type "Bug Fix"         # only bug fix RDRs
/rdr-list --has-assumptions        # RDRs with unverified findings
```

Reads from T2 metadata for fast response without parsing markdown files.

---

## Show (`/rdr-show`)

Displays a unified view of a single RDR.

```
/rdr-show 007
```

Includes: metadata summary, research findings table with classifications,
gate status, linked beads (if closed), and post-mortem status (if exists).
Combines data from the markdown file and T2 metadata into a single readable
output.

---

## T2 Synchronization

T2 is the **process authority** for RDR status. Agents read and write T2;
files are the git-versioned human-editable persistence layer.

### SessionStart Reconciliation

On every session start, the `rdr_hook.py` hook reconciles file and T2 status
using the **monotonic-advance rule**: status always advances, never regresses.

- If the file has a more advanced status than T2 (e.g., human edited
  `draft` → `accepted`), T2 is updated to match.
- If T2 has a more advanced status than the file (e.g., file write failed),
  the file is repaired to match T2.
- If both sides have different terminal states, the file wins with a warning.

This ensures T2 stays in sync without file watchers or git hooks.
