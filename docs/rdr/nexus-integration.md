# RDR: Nexus Integration

The iterative nature of RDRs creates an information management problem. A
real project produces dozens of design documents recording decisions, pivots,
refinements, and failures. Without tooling, it becomes difficult to figure
out the current state — what's active, what was superseded, how things
changed, and why. This is the problem Nexus solves.

Nexus search, indexing, and metadata tracking are integrated into the RDR
process at the foundation, not bolted on as an afterthought. Every RDR is
semantically searchable the moment it is committed. Metadata is queryable
without parsing markdown. Agents receive prior-art context automatically.
The result is that the RDR corpus stays navigable as it grows, rather than
becoming the kind of documentation graveyard that teams learn to ignore.

---

## T2 -- RDR Metadata

Each RDR has a T2 record in the `{repo}_rdr` project. Fields:

| Field | Description |
|---|---|
| `id` | Sequential number (e.g., `007`) |
| `prefix` | Project prefix derived from repo name |
| `title` | Human-readable title |
| `status` | Draft, Final, Implemented, Reverted, Abandoned, Superseded |
| `type` | Feature, Bug Fix, Technical Debt, Framework Workaround, Architecture |
| `priority` | High, Medium, Low |
| `created` | ISO timestamp |
| `gated` | ISO timestamp of gate pass (empty if not gated) |
| `closed` | ISO timestamp of close (empty if open) |
| `close_reason` | Why the RDR was closed |
| `superseded_by` | ID of the replacing RDR (if superseded) |
| `supersedes` | ID of the RDR this one replaces |
| `epic_bead` | Bead ID of the implementation epic (set at close) |
| `archived` | Whether content has been archived to T3 |
| `file_path` | Relative path to the markdown file |

T2 enables fast listing, filtering, and status tracking without parsing
markdown. FTS5 search works across all RDR records:

```bash
nx memory search "caching" --project myrepo_rdr
```

---

## T3 -- Permanent Archival

When `/rdr-close` runs, it indexes the RDR content via `nx index rdr` into the
`rdr__` collection with semantic embeddings via VoyageAI. This enables:

- **Semantic search** across all past decisions:
  ```bash
  nx search "authentication strategy" --corpus rdr
  ```
- **Cross-project discovery**: decisions from one project surface when
  researching similar problems in another project.
- **Agent context**: spawned agents can query T3 to find relevant prior art
  before proposing new solutions.

T3 records are tagged with the RDR's type, priority, and extracted key terms
for filtered retrieval.

---

## Smart Repo Indexing -- RDR Discovery

The `nx index repo` pipeline auto-discovers `docs/rdr/*.md` files and routes
them to a dedicated `rdr__<repo>` collection using the `voyage-context-3`
embedding model. This means RDR content is semantically searchable as soon as
it is committed, even before formal archival via `/rdr-close`.

You can also index RDR documents independently: `nx index rdr [PATH]`.

This is particularly useful during active research: an RDR in Draft status
is already indexed and findable by agents working on related problems.

---

## Project Management Integration

- `nx pm` tracks RDR-driven project phases. An RDR can represent a phase gate
  or a prerequisite for entering the next phase.
- `/rdr-close` creates beads (epic + tasks) that integrate with `bd` for
  implementation tracking.
- `nx pm reference "topic"` searches across all archived project syntheses,
  including RDR decisions, surfacing relevant context for planning.

---

## Agent Workflow

RDR integration with agents supports the iterative cycle — build, discover,
write another RDR:

1. **Before new work**: agents search T3 for relevant prior RDRs. A new RDR
   often refines or extends an earlier one, and the search surfaces that chain
   of reasoning.
2. **During research**: `/rdr-research` can delegate to `deep-research-synthesizer`
   or `codebase-deep-analyzer` for heavy investigation.
3. **At gate time**: the `substantive-critic` agent provides independent review.
4. **After close**: implementation agents receive RDR context via the
   `SubagentStart` hook, which injects T2 metadata so spawned agents know
   about active and recently-closed RDRs.
5. **Post-implementation**: the post-mortem template captures what was learned,
   which often feeds into the next RDR.
