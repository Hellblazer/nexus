# RDR: Nexus Integration

Nexus indexes every RDR the moment it is committed. Search, filter, and retrieve
past decisions without parsing markdown.

---

## T2 — RDR Metadata

Each RDR has a T2 record in the `{repo}_rdr` project. Query with:

```bash
nx memory search "caching" --project myrepo_rdr
nx memory search "status:draft" --project nexus_rdr
```

| Field | Description |
|---|---|
| `id` | Sequential number (e.g., `007`) |
| `prefix` | Project prefix derived from repo name |
| `title` | Human-readable title |
| `status` | Draft, Accepted, Implemented, Reverted, Abandoned, Superseded |
| `type` | Feature, Bug Fix, Technical Debt, Framework Workaround, Architecture |
| `priority` | High, Medium, Low |
| `created` | ISO timestamp |
| `gated` | ISO timestamp of gate pass (empty if not gated) |
| `accepted_date` | ISO date set by `/rdr-accept` (empty if not accepted) |
| `closed` | ISO timestamp of close (empty if open) |
| `close_reason` | Why the RDR was closed |
| `superseded_by` | ID of the replacing RDR (if superseded) |
| `supersedes` | ID of the RDR this one replaces |
| `epic_bead` | Bead ID of the implementation epic (set at accept, if planning chain runs) |
| `archived` | Whether content has been archived to T3 |
| `file_path` | Relative path to the markdown file |

T2 is the authoritative source. The markdown file is the human-readable
persistence layer. Timestamps let you reconstruct which decisions were active
at any point in time.

---

## T3 — Permanent Archival

`/rdr-close` indexes the RDR into the `rdr__` collection via VoyageAI embeddings.
RDRs committed to the repo are also indexed immediately by `nx index repo`.

**Search commands:**

```bash
nx search "caching strategy" --corpus rdr
nx search "chromadb quota" --corpus rdr --n 5
nx search "authentication" --corpus rdr --n 3
```

Cross-project search works automatically. Decisions from one project surface
when researching similar problems in another.

### What an agent sees

When an agent queries `--corpus rdr`, the highest-signal chunks returned are:

- **Problem Statement** — the most semantically dense chunk; best for
  determining relevance. A well-written Problem Statement is the single most
  valuable thing for agent context.
- **Proposed Solution** — surfaces implementation approach and trade-offs.

Evidence classification (`Verified`, `Documented`, `Assumed`) is visible in
search result metadata. Agents can distinguish validated constraints from
working assumptions without reading the full file.

The result metadata includes `file_path`, so an agent can load the full RDR
when it needs depth beyond the top chunks.

---

## Smart Repo Indexing

`nx index repo` auto-discovers `docs/rdr/*.md` files and routes them to
`rdr__<repo>` using the `voyage-context-3` model. Draft RDRs are indexed and
findable immediately — no need to wait for `/rdr-close`.

Index a single RDR or directory independently:

```bash
nx index rdr docs/rdr/rdr-015-indexing-pipeline-rethink.md
nx index rdr docs/rdr/
```

---

## Beads Integration

`/rdr-accept` optionally decomposes the Implementation Plan into beads
(epic + tasks) via the planning chain (strategic-planner → plan-auditor →
plan-enricher). The `epic_bead` T2 field links each decision to its work
items. The `SubagentStart` hook injects T2 memory context and the active
bead, so spawned agents pick up where the last session left off.

---

## Agent Workflow

1. **Before new work**: search T3 for prior RDRs — `nx search "topic" --corpus rdr`. A new RDR often refines an earlier one; the search surfaces that chain.
2. **During research**: `/rdr-research` can delegate to `deep-research-synthesizer` or `codebase-deep-analyzer` for heavy investigation.
3. **At gate time**: `substantive-critic` provides independent review.
4. **At accept time**: `/rdr-accept` updates T2 first, then repairs the file to match. Optionally dispatches the planning chain to create implementation beads.
5. **After close**: `/rdr-close` archives to T3 and displays bead status advisory.

**MCP access**: Agents access all storage tiers via structured MCP tools (`mcp__plugin_nx_nexus__search`, `mcp__plugin_nx_nexus__memory_search`, etc.) rather than CLI commands. This eliminates Bash dependency and works reliably in background agents and restricted permission contexts. Human users continue using the `nx` CLI. See [nx/README.md](../nx/README.md#mcp-servers) for tool details.

---

## From Post-Mortem to Next RDR

Post-mortem findings map directly to improvements in the next design:

- **Unvalidated assumption** → add a Spike task to the next RDR's research phase before writing the Proposed Solution.
- **Missing failure mode** → add an explicit failure mode entry to the next RDR's risk section.
- **Framework API detail wrong** → standard fix: run `nx search "<api>" --corpus rdr` and grep the source before writing the Proposed Solution.
- **Scope creep** → narrow the next RDR's Problem Statement; if the problem has two parts, write two RDRs.
- **Implementation diverged from design** → flag the delta in the post-mortem and open a follow-up RDR to record the actual solution.

---

**Reading order:** [Overview](rdr-overview.md) | [Workflow](rdr-workflow.md) | Nexus Integration (this page) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)
