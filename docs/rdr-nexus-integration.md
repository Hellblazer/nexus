**Reading order:** [Overview](rdr-overview.md) | [Workflow](rdr-workflow.md) | Nexus Integration (this page) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)

---

# RDR: Nexus Integration

RDR documents live in the repository as markdown, but Nexus makes them queryable through both structured metadata (T2) and semantic search (T3). This means agents and team members don't need to parse files or remember which RDR covered a topic — they search by meaning and get relevant decisions back.

## How agents use RDRs

The typical agent workflow touches the storage tiers at each stage:

1. **Before new work** — search T3 for prior RDRs: `nx search "topic" --corpus rdr`. New designs often build on or refine earlier decisions; the search surfaces that chain automatically.
2. **During research** — `/nx:rdr-research` can delegate to `deep-research-synthesizer` or `codebase-deep-analyzer` for investigation that goes beyond what a single agent session can cover.
3. **At gate time** — `substantive-critic` provides independent review of the RDR's logic, evidence, and completeness.
4. **At accept time** — `/nx:rdr-accept` updates T2 metadata, then optionally dispatches the planning chain (strategic-planner → plan-auditor → plan-enricher) to decompose the implementation into trackable beads.
5. **After close** — `/nx:rdr-close` archives the full RDR to T3 for permanent semantic retrieval.

Agents access all storage tiers via structured MCP tools rather than CLI commands, which works reliably in background agents and restricted permission contexts. Team members use the `nx` CLI directly. See [nx/README.md](../nx/README.md#mcp-servers) for MCP tool details.

## T2 — structured metadata

Each RDR has a T2 record in the `{repo}_rdr` project, providing structured access to status, type, priority, timestamps, and linked beads without parsing markdown.

```bash
nx memory search "caching" --project myrepo_rdr
nx memory search "status:draft" --project nexus_rdr
```

Key fields include `id`, `status` (Draft → Accepted → Implemented/Reverted/Abandoned/Superseded), `type`, `priority`, `accepted_date`, `epic_bead` (links to implementation tracking), and `file_path`. T2 is the authoritative source for RDR state — the markdown file is the human-readable persistence layer.

Timestamps (`created`, `gated`, `accepted_date`, `closed`) let you reconstruct which decisions were active at any point in time.

## T3 — semantic search

RDRs are indexed into `rdr__<repo>` collections using `voyage-context-3` embeddings. This happens two ways:

- **`nx index repo`** auto-discovers `docs/rdr/*.md` and indexes them during normal repo indexing. Draft RDRs are findable immediately — no need to wait for `/nx:rdr-close`.
- **`/nx:rdr-close`** indexes the RDR explicitly at close time as part of permanent archival.

```bash
nx search "caching strategy" --corpus rdr
nx search "chromadb quota" --corpus rdr --n 5
```

Cross-project search works automatically — decisions from one project surface when researching similar problems in another.

### What search returns

The highest-signal chunks are typically the **Problem Statement** (best for determining relevance) and the **Proposed Solution** (surfaces implementation approach and trade-offs). Evidence classification (Verified, Documented, Assumed) is visible in result metadata, so agents can distinguish validated constraints from working assumptions without loading the full document. The `file_path` metadata allows loading the complete RDR when more depth is needed.

## Beads integration

`/nx:rdr-accept` optionally decomposes the Implementation Plan into beads (epic + tasks) via the planning chain. The `epic_bead` T2 field links each accepted decision to its implementation work items. Session hooks inject T2 context and the active bead into spawned agents, so they pick up where the previous session left off.

## Catalog — document registry and link graph

When the [catalog](catalog.md) is initialized, RDR lifecycle skills create typed links that connect RDRs to each other and to the broader knowledge base:

| Lifecycle stage | What happens in the catalog |
|---|---|
| `nx index rdr` / `nx index repo` | RDR document registered with tumbler, title from frontmatter, content_type=rdr |
| `/nx:rdr-research add` | `cites` link from RDR to referenced paper (if indexed in catalog) |
| `/nx:rdr-gate` | Prior-art search uses `catalog_search` + `catalog_links` before falling back to T3 |
| `/nx:rdr-accept` | `relates` links to topically related RDRs found during planning |
| `/nx:rdr-show` | Displays inbound/outbound catalog links (implements-heuristic, cites, supersedes) |
| `/nx:rdr-close` (Superseded) | `supersedes` link between new and old RDR |
| `/nx:rdr-close` (Implemented) | `cites` links from RDR to referenced research papers |
| Indexer hook | `implements-heuristic` links from code files to RDRs (title substring match) |

This means `nx catalog links "RDR-051"` shows which code implements it, what research it cites, and what it supersedes — without parsing markdown.

All catalog steps are skipped silently if the catalog isn't initialized. T2 and the markdown file remain the authorities.

## Learning from post-mortems

`/nx:rdr-close` creates a post-mortem template for drift analysis. Findings from post-mortems feed directly into the next RDR:

- **Unvalidated assumption** → add a Spike task to the next RDR's research phase.
- **Missing failure mode** → add explicit failure mode entries to the risk section.
- **Framework API detail wrong** → search T3 and verify against source before writing the Proposed Solution.
- **Scope creep** → narrow the Problem Statement; if the problem has two parts, write two RDRs.
- **Implementation diverged from design** → flag the delta in the post-mortem and open a follow-up RDR.

---

**Reading order:** [Overview](rdr-overview.md) | [Workflow](rdr-workflow.md) | Nexus Integration (this page) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)
