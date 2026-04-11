---
name: rdr-close
description: Use when an RDR is done — close it with optional post-mortem, bead status gate, and T3 archival
effort: medium
---

# RDR Close Skill

Gates on open bead status before closing. Optionally delegates post-mortem archival to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "close this RDR", "RDR done", "finish RDR"
- User invokes `/nx:rdr-close`
- Implementation is complete and the RDR should be finalized

## Inputs

- **RDR ID** (required) — e.g., `003`
- **Reason** (required): Implemented | Reverted | Abandoned | Superseded

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Pre-Check

1. Read T2 record: mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="NNN"
2. If status is not "accepted" (or "final") and reason is "Implemented":
   - Warn: "RDR NNN status is '{current_status}' — expected 'accepted'. Close anyway?"
   - Require `--force` or explicit user confirmation to proceed
3. If T2 record not found, check filesystem for the markdown file

## Flow: Implemented

### Step 1: Divergence Notes

Ask: "Did implementation diverge from the plan? If so, describe the divergences."

If diverged:

### Step 1.5: Problem Statement Replay

Branch on what the preamble emitted:

**A. If the preamble emitted `PROBLEM STATEMENT REPLAY: validation passed` (Pass 2 success)**:
- Surface this to the user in the conversation
- Show the per-gap → file:line summary the preamble printed
- Continue to Step 2

**B. If the preamble emitted a Pass 1 gap enumeration** (no `--pointers` was supplied):
- Show the gap list to the user
- Conversationally collect closure pointers from the user, one gap at a time
- Format as: `Gap1=file.py:123,Gap2=other.py:45`
- Re-invoke: `/nx:rdr-close NNN --reason implemented --pointers 'Gap1=...,Gap2=...'`
- The re-invocation will run Pass 2 of the preamble
- On Pass 2 success, surface the validation-passed message and jump to branch A

**C. If the preamble emitted a legacy WARN** (`This RDR predates structured gaps; no action required`):
- Surface the warn to the user explicitly
- Note that grandfathering applied because RDR ID < 065
- Continue to Step 2

**D. If the preamble blocked** (`sys.exit(0)` with error — malformed new RDR or pointer failure):
- The skill body will not have run; the user sees the preamble error directly
- This case requires no SKILL.md guidance — the preamble error message is self-explanatory
- Resolve the error (fix gaps or fix pointers) then re-invoke

**Mandatory user-facing framing** (say this verbatim before continuing to Step 2):
> "The replay gate verifies you have committed to a specific file:line pointer per gap. It does NOT verify the pointer is semantically correct. Correctness is your responsibility — review each pointer manually before allowing the close to proceed."

Also clear the T1 scratch `rdr-close-active` marker after Step 4 (Update State) completes — add a note at Step 4 to run: `nx scratch delete <entry-id>` where the entry was set by the preamble.

### Step 1.75: Automatic Critique

Dispatch `/nx:substantive-critique <rdr-id>` via the Agent tool and parse the `## Verdict` block from the response. This is the authoritative gate signal (CA-1 verified n=4 for outcome-category determinism; finding-level variance is expected).

**Verdict extraction** — try in order, take the first hit:

1. **Canonical path** (preferred): find the literal line `- **outcome**:` (bullet-dash, bold "outcome", colon, space, value). The value is one of `justified`, `partial`, `not-justified`.
2. **Code-block path**: if the agent emitted the Verdict block inside a code fence, look for a line matching `outcome:\s*(\S+)` within a `## Verdict` section. Normalize the captured value case-insensitively: `FAILED`/`FAIL`/`BLOCKED`/`NOT-JUSTIFIED` → `not-justified`; `PARTIAL`/`PARTIALLY` → `partial`; `PASS`/`PASSED`/`APPROVED`/`JUSTIFIED` → `justified`.
3. **Fallback path**: if neither canonical nor code-block form is present, count `### Issue:` headers under `## Critical Issues` and `## Significant Issues`. Derive outcome mechanically: Critical > 0 → `not-justified`; Critical == 0 AND Significant > 0 → `partial`; all clear → `justified`. Surface the fallback path to the user explicitly: "The critic did not emit a canonical Verdict block. Falling back to section counting: <counts>."

All three paths map to the same 3-valued enum (`justified` / `partial` / `not-justified`) which is the gate signal branched on below.

**Short-circuit**: if the preamble surfaced a `Force Implemented (audit)` line, skip the dispatch entirely — the user has taken explicit responsibility. Write a T2 override audit entry with `critic_verdict: skipped` (see branch E below) and continue to Step 2.

**Relay framing** (load-bearing, do not vary): the dispatch relay MUST be fixed-shape and minimal. Pass only `{rdr_id}` and the standard input artifacts (T2 RDR record, catalog entry, RDR markdown file). NEVER pass session-generated summaries of what was built, what diverged, or what the user intends. Rationalization bias is the exact failure mode RDR-069 addresses — see RDR-069 §Risks "Dispatch isolation risk".

```markdown
## Relay: substantive-critic

**Task**: Critique RDR {rdr_id} against its Problem Statement for silent scope reduction, retcon, or unjustified-implemented closure.
**Bead**: none

### Input Artifacts
- nx memory: {repo}_rdr/{rdr_id} (status, research records, planning chain)
- Files: docs/rdr/rdr-{rdr_id}-*.md
- Catalog: mcp__plugin_nx_nexus-catalog__search query "RDR-{rdr_id}"

### Deliverable
Critique report with canonical `## Verdict` block (outcome, confidence, critical_count, significant_count, summary).

### Quality Criteria
- [ ] Verdict block uses 5-field canonical format
- [ ] Findings grounded in RDR text (file:line references)
- [ ] No session context consumed beyond the listed input artifacts
```

Branch on `Verdict.outcome`:

**A. `justified`** → surface the one-line summary to the user and continue to Step 2. No constraint on `close_reason`.

**B. `partial`** → surface all Critical and Significant findings to the user verbatim. Block `close_reason: implemented` unless `--force-implemented "<reason>"` was supplied. If no override, prompt the user: "The critic found significant issues. Address them and re-run, or pass `--force-implemented '<reason>'` to override with audit trail." Do not proceed until the user either resolves findings (recursive loop) or passes the override.

**C. `not-justified`** → surface ALL Critical findings verbatim. Block `close_reason: implemented` unless `--force-implemented "<reason>"` was supplied. Per RDR-069 §Proposed Solution (line 282) and §Technical Design (line 307), `close_reason: reverted` and `close_reason: partial` are legitimate non-override paths on `not-justified` — a user who genuinely wants to acknowledge failure with `reverted` should be able to do so without the override flag. User's options: (1) address findings and re-run with `--reason implemented` (recursive refinement loop), (2) re-run with `--reason reverted` or `--reason partial` (honest failure-acknowledgment, no override needed), or (3) pass `--force-implemented "<reason>"` with a substantive reason to force `implemented` despite the Critical findings. A one-word reason (e.g., `--force-implemented "wontfix"`) is insufficient — prompt the user to expand it before accepting. Only `close_reason: implemented` requires the override.

**D. Verdict extraction failure** → this case should be rare now that the verdict extractor above tries canonical bullet form, code-block key-value form, AND section-counting fallback. Only reach this branch if all three extraction paths fail (e.g. critic response is completely empty or wholly unparseable). Surface explicitly to the user: "Critic response could not be parsed at all. Proceed without critique? (y/N)". Do not silently block; do not silently proceed.

**E. Override audit entry** (runs for every `--force-implemented` invocation, regardless of critic outcome):

```
mcp__plugin_nx_nexus__memory_put(
    project="{repo}_rdr",
    title="{rdr_id}-close-override-{YYYY-MM-DD}",
    content="critic_verdict: {outcome|skipped}\nuser_reason: {force_implemented_reason}\nfinal_close_reason: {close_reason}\ntimestamp: {ISO8601}\nrdr_id: {rdr_id}",
    ttl="permanent",
    tags="rdr,close-override,rdr-{rdr_id}"
)
```

The audit entries are the measurement surface for CA-4: if `nexus_rdr/*-close-override-*` exceeds 20% of closes in any 30-day window, Phase 2 dispatch degrades to advisory mode (see RDR-069 Day 2 Operations).

**Scenario 4 — timeout or transport failure**: if the Agent tool dispatch fails, times out (>5 minutes without response), or returns an unparseable response (not just a missing Verdict block, but genuinely malformed output), surface the failure to the user explicitly and ask: "Critic dispatch failed. Proceed without critique? (y/N)". Do not silently block and do not silently proceed — the user must choose.

### Step 2: Create Post-Mortem

Create `$RDR_DIR/post-mortem/NNN-kebab-title.md` from the post-mortem template. Populate:

- **RDR Summary**: Extract from the RDR's Problem Statement
- **Implementation Status**: "Implemented"
- **What Diverged**: User's divergence notes
- **Drift Classification**: Prompt user to classify each divergence into categories:
  - Unvalidated assumption
  - Framework API detail
  - Missing failure mode
  - Missing Day 2 operation
  - Deferred critical constraint
  - Over-specified code
  - Under-specified architecture
  - Scope underestimation
  - Internal contradiction
  - Missing cross-cutting concern

### Step 3: Bead Status Gate

If T2 record has an `epic_bead` field (set during accept-time planning):
1. Read epic bead ID from T2: mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="NNN"
2. Run `/beads:show <epic-id>` to get child bead statuses
3. Display bead status table to user:
   - Bead ID, title, status (open/in_progress/closed)
   - Highlight any unclosed beads
4. Do NOT automatically mark beads complete — the human decides which beads to close.

If T2 record has no `epic_bead` field (user skipped planning at accept time):
- Check the command output for open beads listed by the pre-check script.

**HARD GATE — if ANY open or in-progress beads exist:**
- Display the open beads to the user
- Ask explicitly: "These beads are still open. Close this RDR anyway?"
- **Do NOT proceed until the user confirms.** This is not advisory — it is a gate.
- If the user says no, stop and let them resolve the beads first.

### Step 4: Update State

1. Update T2 record: mcp__plugin_nx_nexus__memory_put(content="... (same fields, status: Implemented, closed: YYYY-MM-DD, close_reason: Implemented, archived: true)", project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,{type},closed"
   If T3 archive fails, set `archived: false` — retryable by re-running `/nx:rdr-close`

2. Update status in RDR markdown metadata
3. Regenerate `docs/rdr/README.md` index
4. **Conditional reindex** — run `nx index rdr` only if the RDR body actually changed during close (e.g. divergence notes added, post-mortem link inserted into the RDR doc, or any text outside the frontmatter block modified). A frontmatter-only edit (status/closed_date/close_reason flipping) does NOT need a T3 reindex — the chunk text is unchanged, so embeddings would not shift. Check with:

   ```bash
   # If the diff is wholly inside the frontmatter block, skip the reindex.
   git diff HEAD -- docs/rdr/rdr-NNN-*.md | grep -v '^[+-]---' | grep -v '^[+-][a-z_]*: ' | grep -E '^[+-]' | head -1
   # If the command prints nothing, body was not modified — skip Step 4.4.
   # If it prints lines, body changed — run `nx index rdr` to refresh.
   ```

   The rdr indexer is hash-dedup aware, so a no-op reindex is cheap but not free — it still walks every RDR file. Skip when not warranted.

### Step 5: Catalog Links (if catalog initialized)

The RDR already has a catalog entry from the original accept-time indexing. Create links to capture implementation provenance (if catalog is initialized):

1. **Code→RDR links**: The indexer hook auto-generates `implements-heuristic` links via title substring matching. These are created automatically. Review with `nx catalog links <rdr-tumbler> --type implements-heuristic` — promote high-confidence ones to `implements` via `mcp__plugin_nx_nexus-catalog__link` for link-boost scoring benefit (heuristic links have zero search boost weight).

2. **RDR→prior-RDR links**: If the RDR's T2 record has a `supersedes` field, create the catalog link:
   ```
   mcp__plugin_nx_nexus-catalog__link(from_tumbler="<this-rdr-title>", to_tumbler="<superseded-rdr-title>", link_type="supersedes", created_by="rdr-close")
   ```

3. **RDR→research links**: If research findings reference indexed papers, create `cites` links:
   - Read T2 research findings for this RDR
   - For each finding with a URL or paper title as source, search catalog: `mcp__plugin_nx_nexus-catalog__search(query="<source>")`
   - If found, resolve the RDR tumbler (`mcp__plugin_nx_nexus-catalog__search(query="RDR-NNN")`), then: `mcp__plugin_nx_nexus-catalog__link(from_tumbler="<rdr-tumbler>", to_tumbler="<paper-tumbler>", link_type="cites", created_by="rdr-close")`

Skip all catalog steps silently if catalog is not initialized. The T2 record and markdown are the authorities — catalog links are supplementary graph enrichment.

### Step 6: T3 Archive (post-mortem only)

The main RDR was already semantically indexed at accept time (and refreshed in Step 4 only if the body changed during close). Do **not** duplicate it with store_put tool — that would create non-CCE blob entries in the same collection, degrading search quality.

If a post-mortem exists, archive it to a separate collection (using the exact file path from Step 2, not a glob): mcp__plugin_nx_nexus__store_put(content=(contents of $RDR_DIR/post-mortem/NNN-kebab-title.md), collection="knowledge__rdr_postmortem__{repo}", title="PREFIX-NNN Title (post-mortem)", tags="rdr,post-mortem,{drift-categories}"

Before dispatching the knowledge-tidier, seed link-context so the post-mortem auto-links to the RDR:
```
mcp__plugin_nx_nexus__scratch(action="put", content='{"targets": [{"tumbler": "<rdr-tumbler>", "link_type": "relates"}], "source_agent": "rdr-close"}', tags="link-context")
```

Dispatch `knowledge-tidier` agent for post-mortem archival if the post-mortem contains substantial divergence analysis that benefits from knowledge organization.

## Flow: Reverted or Abandoned

1. Prompt for reason (free text)
2. Offer post-mortem (useful for capturing what was learned, even from abandoned work)
3. Update T2 record with close reason
4. Update markdown metadata
5. **Conditional reindex** — run `nx index rdr` only if the RDR body actually changed (apply the same frontmatter-vs-body diff check from Step 4 of the Implemented flow). A frontmatter-only `status: reverted` flip does not warrant a reindex.
6. Archive post-mortem to `knowledge__rdr_postmortem__{repo}` (if created)
7. Regenerate README index

## Flow: Superseded

1. Prompt for superseding RDR ID
2. Cross-link both RDRs (bidirectional):
   - **Old RDR**: In T2, set `superseded_by: "NNN"`. In markdown, add "Superseded by RDR-NNN" note
   - **New RDR**: In T2, set `supersedes: "MMM"`. In markdown, add "Supersedes RDR-MMM" note
3. **Conditional reindex** — this flow typically DOES warrant a reindex because the markdown notes added in step 2 live in the RDR body (not the frontmatter), so chunk text shifts. Run `nx index rdr`. If a given cross-link note was only added to the frontmatter (unusual), apply the diff check from Implemented flow Step 4 instead.
4. **Catalog link** (if catalog initialized): Create `supersedes` link in the catalog so the graph reflects the relationship:
   ```
   # Find both RDRs by title in catalog
   mcp__plugin_nx_nexus-catalog__link(from_tumbler="<new-rdr-title>", to_tumbler="<old-rdr-title>", link_type="supersedes", created_by="rdr-close")
   ```
   If catalog is not initialized or either RDR is not found, skip silently — the T2 record is the authority.
5. Regenerate index

## Failure Handling

The close operation performs multiple state mutations. If any step fails:
- Each step emits clear status (e.g., "T2 updated ✓", "Bead gate ✓", "T3 archive ✗ FAILED")
- T2 `archived` flag tracks whether T3 archival succeeded
- Re-running `/nx:rdr-close` is idempotent: checks T2 state and skips completed steps

## Relay Template (Use This Format)

When dispatching the knowledge-tidier agent via Agent tool for post-mortem archival, use this exact structure:

```markdown
## Relay: knowledge-tidier

**Task**: Archive RDR NNN post-mortem to T3 with drift classification metadata.
**Bead**: none

### Input Artifacts
- nx store: [prior archived RDRs or "none"]
- nx memory: {repo}_rdr/NNN (status, research records, close metadata)
- nx scratch: [scratch IDs or "none"]
- Files: docs/rdr/post-mortem/NNN-kebab-title.md

### Deliverable
Post-mortem archived to `knowledge__rdr_postmortem__{repo}` with drift categories as tags.

### Quality Criteria
- [ ] Post-mortem content fully archived to T3
- [ ] Tags include divergence/drift categories
- [ ] Title includes RDR prefix and ID
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Success Criteria

- [ ] RDR directory resolved from `.nexus.yml` `indexing.rdr_paths[0]` (default `docs/rdr`)
- [ ] Pre-check completed (status verified, warnings issued for non-Final RDRs)
- [ ] Divergence notes captured from user (if implementation diverged)
- [ ] Post-mortem created with drift classification (if diverged or reverted/abandoned)
- [ ] Open beads displayed and user asked for explicit confirmation before proceeding
- [ ] Beads NOT auto-closed — human decides
- [ ] T2 record updated with close reason, date, epic bead ID, and archived flag
- [ ] T3 semantic index refreshed via `nx index rdr` **only if the RDR body changed during close** (divergence notes added, cross-link notes inserted, etc.) — skipped for frontmatter-only closes
- [ ] Post-mortem archived to `knowledge__rdr_postmortem__{repo}` (if exists)
- [ ] README index regenerated
- [ ] Idempotent: re-running skips completed steps

## Agent-Specific PRODUCE

Outputs produced by this skill directly:

- **Console output**: Bead status gate table (if epic_bead in T2)
- **T2 memory**: Close metadata via memory_put tool: project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,{type},closed"
- **T3 semantic index**: Conditionally refreshed via `nx index rdr` (CCE embeddings, section-level chunks) — only when the RDR body changed during close; frontmatter-only edits are skipped
- **Filesystem**: Post-mortem at `$RDR_DIR/post-mortem/NNN-kebab-title.md`, updated README

Outputs generated by the knowledge-tidier agent (post-mortem archival only):

- **T3 knowledge**: Post-mortem archive via store_put tool: content=(post-mortem contents), collection="knowledge__rdr_postmortem__{repo}", title="PREFIX-NNN Title (post-mortem)"
- **T1 scratch**: Working notes via scratch tool: action="put", content="RDR NNN close: archiving post-mortem", tags="rdr,close"

## Does NOT

- Force close if gate hasn't passed (warns, allows override)
- Delete the markdown file (it stays in the repo permanently)
- Auto-commit (user decides when to commit)
