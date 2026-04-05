**Reading order:** [Overview](rdr-overview.md) | Workflow (this page) | [Nexus Integration](rdr-nexus-integration.md) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)

---

# RDR Workflow

This document covers the operational details of each lifecycle step. For background on what RDRs are and when to write them, see the [Overview](rdr-overview.md).

## Lifecycle

```
/nx:rdr-create
     │
  [Draft] ◄── /nx:rdr-research (repeat as needed)
     │
     │ /nx:rdr-gate (optional but recommended)
     │ ├─ BLOCKED → fix and re-gate
     │ └─ PASSED
     ▼
/nx:rdr-accept
     │
  [Accepted]
     │
     │ optional: planning chain → implementation beads
     │
     │ /nx:rdr-close --reason implemented
     ▼
[Implemented]

Terminal states: Reverted · Abandoned · Superseded
```

Only **Create** and **Research** are required. Gate, Accept, and Close add formal validation and archival — use them when the decision is load-bearing.

## Worked example

A bug fix RDR from create to close:

```
/nx:rdr-create
  Title: "Fix: chunker emits full-file line range for every AST chunk"
  Type: Bug Fix   Priority: High
  → Creates docs/rdr/rdr-016-fix-chunker-line-range.md (status: draft)

/nx:rdr-research add 016
  Finding: CodeSplitter never populates line_start/line_end
  Classification: Verified — Source Search (chunker.py:210)

/nx:rdr-gate 016
  Structure ✓ · Assumptions ✓ · AI critique ✓ → PASSED

/nx:rdr-accept 016
  Verifies gate, updates status → accepted

/nx:rdr-close 016 --reason implemented
  Creates post-mortem template, indexes to T3
```

The RDR is now searchable via `nx search --corpus rdr` and tracked in T2.

## Create (`/nx:rdr-create`)

Prompts for title, type, and priority. Creates `docs/rdr/NNN-kebab-title.md` from the standard template with metadata prefilled, writes a T2 record, and regenerates the RDR index. Status: **Draft**.

On first use in a repository, `/nx:rdr-create` bootstraps the `docs/rdr/` directory and copies the template automatically.

## Research (`/nx:rdr-research`)

Adds structured findings to a Draft RDR. Each finding records a summary, evidence classification (Verified, Documented, or Assumed), verification method, and source reference.

Verification methods:

- **Source Search** — API or behavior verified against dependency source code
- **Spike** — behavior verified by running code against a live service
- **Docs Only** — documentation reading only; insufficient for load-bearing assumptions

For complex investigations, `/nx:rdr-research` can delegate to specialized agents (`deep-research-synthesizer` for web/document research, `codebase-deep-analyzer` for codebase exploration). Findings are written to both the markdown file and T2. The RDR stays Draft throughout.

## Gate (`/nx:rdr-gate`)

Three-layer validation. Optional but recommended before committing to irreversible decisions.

**Layer 1 — Structural**: Required sections filled, metadata complete, at least one research finding present.

**Layer 2 — Assumption audit**: Every Assumed finding must have a risk assessment. Each critical assumption must acknowledge what happens if it's wrong.

**Layer 3 — AI critique**: Delegates to the `substantive-critic` agent, which evaluates logical coherence, missing alternatives, unstated assumptions, and evidence gaps. Findings are appended to the RDR.

The gate either **BLOCKS** (critical issues — fix and re-gate) or **PASSES** (no critical issues, may have observations). No conditional outcomes. The result is stored in T2 for `/nx:rdr-accept` to verify.

## Accept (`/nx:rdr-accept`)

The decision point. The gate validates; acceptance is a deliberate human choice.

Verifies that the gate passed, updates T2 status to Accepted, updates the file frontmatter to match, and regenerates the index. For multi-phase implementation plans, `/nx:rdr-accept` optionally dispatches the planning chain (strategic-planner → plan-auditor → plan-enricher) to decompose the work into trackable beads.

If T2 and the file disagree on status, `/nx:rdr-accept` self-heals by repairing the file to match T2.

## Close (`/nx:rdr-close`)

Finalizes an Accepted RDR. Requires status Accepted (use `--force` to override).

Close reasons: `implemented` · `reverted` · `abandoned` · `superseded`

Closing creates a post-mortem template for drift analysis, indexes the RDR into the `rdr__` collection for permanent semantic retrieval, and updates T2 with the close date and reason. If beads were created during accept, their status is displayed as an advisory. If the [catalog](catalog.md) is initialized, closing also creates typed links — `supersedes` for superseded RDRs, `cites` for referenced research papers.

## Querying RDRs

```bash
/nx:rdr-list                      # all RDRs
/nx:rdr-list --status Draft       # active research only
/nx:rdr-list --type "Bug Fix"     # bug fixes only

/nx:rdr-show 007                  # full detail: metadata, findings, gate status, linked beads
```

Both commands read from T2 — no markdown parsing required.

## T2 synchronization

T2 is the process authority for RDR status; the markdown file is the human-readable persistence layer. On session start, a reconciliation hook ensures they agree using a monotonic-advance rule: status only moves forward, never regresses. If a human edits the file ahead of T2, T2 catches up. If T2 is ahead (e.g., a file write failed), the file is repaired.

---

**Reading order:** [Overview](rdr-overview.md) | Workflow (this page) | [Nexus Integration](rdr-nexus-integration.md) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)
