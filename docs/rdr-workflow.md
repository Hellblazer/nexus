# RDR Workflow

## State Machine

```
/rdr-create
     │
  [Draft] ◄── /rdr-research (repeat as needed)
     │
     │ /rdr-gate (optional but recommended)
     │ ├─ BLOCKED → fix and re-gate
     │ └─ PASSED
     ▼
/rdr-accept
     │
  [Accepted]
     │
     │ /rdr-close --reason implemented
     ▼
[Implemented]

Terminal states: Reverted · Abandoned · Superseded
```

Only **Create** and **Research** are required. Gate, Accept, and Close add
formal validation and archival — use them when the decision is load-bearing.

---

## Full Journey in 5 Minutes

**Scenario**: fix a bug where AST chunks show wrong line ranges.

```
/rdr-create
# Title: "Fix: chunker emits full-file line range for every AST chunk"
# Type: Bug Fix  Priority: High
# → Creates docs/rdr/016-fix-chunker-line-range.md  (status: draft)
```

```
/rdr-research add 016
# Finding: CodeSplitter never populates line_start/line_end
# Classification: Verified — Source Search (chunker.py:210)
# Finding: start_char_idx/end_char_idx are populated; char→line conversion works
# Classification: Verified — Source Search (llama-index TextNode)
```

```
/rdr-gate 016
# Layer 1: all sections present ✓
# Layer 2: no Assumed findings ✓
# Layer 3: substantive-critic → no blockers ✓
# Outcome: PASSED
```

```
/rdr-accept 016
# Verifies gate result, updates status → accepted
```

```
/rdr-close 016 --reason implemented
# Creates post-mortem template
# Decomposes Implementation Plan → beads
# Indexes RDR into rdr__ collection
```

Total elapsed: ~10 minutes of tool calls. The RDR is now discoverable via
`nx search --corpus rdr` and its decisions are tracked in T2.

---

## Status Model

| Status | Meaning |
|--------|---------|
| **Draft** | In progress, not yet gated |
| **Accepted** | Gate passed; author/reviewer approved |
| **Implemented** | Code matches accepted design |
| **Reverted** | Implemented then rolled back |
| **Abandoned** | Stopped before implementation |
| **Superseded** | Replaced by a later RDR |

---

## Create (`/rdr-create`)

1. Prompts for **title**, **type** (Feature, Bug Fix, Technical Debt, Framework
   Workaround, Architecture), and **priority** (High, Medium, Low).
2. Assigns a **sequential ID** by scanning `docs/rdr/` for the highest existing
   number and incrementing. Zero-padded to three digits (e.g., `007`).
3. Derives the **project prefix** from the repository name.
4. Creates `docs/rdr/NNN-kebab-title.md` from the standard template with
   `## Metadata` pre-filled.
5. Writes a **T2 metadata record** to `{repo}_rdr`: id, prefix, title, status,
   type, priority, created, file_path.
6. Regenerates `docs/rdr/README.md`.
7. Stages new files via `git add`.

Status after creation: **Draft**. Sections exist but contain no research yet.

---

## Research (`/rdr-research`)

Adds structured findings to a Draft RDR.

Each finding records:
- **Summary** — one sentence describing what was learned
- **Classification** — Verified, Documented, or Assumed
- **Method** — how the finding was obtained
- **Source** — where the evidence came from

### Verification Methods

| Method | Description |
|--------|-------------|
| `Source Search` | API verified against dependency source code |
| `Spike` | Behavior verified by running code against a live service |
| `Docs Only` | Documentation reading only — insufficient for load-bearing assumptions |

### Agent Delegation

For complex research, `/rdr-research` can delegate to:
- **deep-research-synthesizer** — multi-source web and document research
- **codebase-deep-analyzer** — deep codebase exploration and pattern analysis

Findings are written to the markdown file and to T2 (machine-queryable for agents).
The RDR stays **Draft** throughout the research phase.

---

## Gate (`/rdr-gate`)

Three-layer validation before implementation.

### Layer 1: Structural

- All required sections filled (Problem Statement, Context, Research Findings,
  Proposed Solution, Alternatives Considered, Trade-offs, Implementation Plan,
  Finalization Gate)
- Metadata complete
- At least one research finding present

### Layer 2: Assumption Audit

- Every **Assumed** finding must have an explicit risk assessment
- Each Critical Assumption must acknowledge: what happens if it is wrong?
- Unacknowledged assumptions prompt the user before proceeding

### Layer 3: AI Critique

- Delegates to the **substantive-critic** agent
- Evaluates: logical coherence, missing alternatives, unstated assumptions, evidence gaps
- Critique findings are appended to the RDR

### Outcomes

| Outcome | Meaning |
|---------|---------|
| **BLOCKED** | Critical issues found — fix and re-gate |
| **PASSED** | No critical issues (may have observations) |

No "Conditional Accept" or other ad-hoc outcomes. The gate either blocks or
passes. Acceptance is a separate human decision after the gate passes.

Gate result is written to T2 as `{id}-gate-latest` for `/rdr-accept` to verify.

---

## Accept (`/rdr-accept`)

Author/reviewer decision point. The gate validates; acceptance is deliberate.

1. Verifies T2 gate result shows `outcome: "PASSED"`. Blocks if no gate result.
2. Updates **T2 first** (process authority): sets `status: "accepted"` and `accepted_date`.
3. Updates the **RDR file** frontmatter to match.
4. Regenerates `docs/rdr/README.md`.
5. Stages modified files via `git add`.

**Self-healing**: if T2 shows `accepted` but the file still shows `draft`,
`/rdr-accept` repairs the file to match T2.

---

## Close (`/rdr-close`)

Finalizes an Accepted RDR and sets up implementation tracking.

Requires status: **Accepted**. Blocked otherwise — use `--force` to override.

Close reasons: `implemented` · `reverted` · `abandoned` · `superseded`

Steps:
1. Creates `docs/rdr/post-mortem/NNN-kebab-title.md` for drift analysis.
2. Decomposes the Implementation Plan into beads: one epic, one task per step.
3. Indexes RDR content via `nx index rdr` into the `rdr__` collection.
4. Updates T2 with close date, close reason, and final status.
5. Regenerates `docs/rdr/README.md`.

After closing, use `nx search --corpus rdr` to find decisions across all projects.

---

## List (`/rdr-list`)

```
/rdr-list                      # all RDRs
/rdr-list --status Draft       # active research only
/rdr-list --type "Bug Fix"     # bug fixes only
/rdr-list --has-assumptions    # RDRs with unverified findings
```

Reads from T2 — no markdown parsing required.

---

## Show (`/rdr-show`)

```
/rdr-show 007
```

Displays: metadata summary, research findings with classifications, gate status,
linked beads (if closed), post-mortem status (if exists). Combines markdown and
T2 data into a single view.

---

## T2 Synchronization

T2 is the **process authority** for RDR status. Files are git-versioned,
human-editable persistence. Agents read and write T2.

### SessionStart Reconciliation

`rdr_hook.py` runs on every session start using the **monotonic-advance rule**:
status always advances, never regresses.

| Condition | Action |
|-----------|--------|
| File more advanced than T2 (e.g., human edited draft → accepted) | T2 updated to match file |
| T2 more advanced than file (e.g., file write failed) | File repaired to match T2 |
| Both sides have different terminal states | File wins, warning logged |

No file watchers or git hooks required.
