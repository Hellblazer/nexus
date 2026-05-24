# Workflow engine + ext-apps — document set

Four artifacts on disk, all gitignored, all indexed. Read them in the
order below; each builds on the previous.

## What this is

A design exploration of a Parmar-style MCP workflow engine extended with
ext-apps for human-in-the-loop, incremental cross-session durability,
and explicit positioning against the broader workflow-orchestration
landscape. The user has a working TypeScript first-cut of the engine;
these documents are inputs to the next round of synthesis on it, not a
greenfield design.

Source paper: [Parmar 2026](https://arxiv.org/abs/2605.00827) —
indexed locally as `knowledge__workflow-engine` (79 chunks).

Canonical MCP + ext-apps reference material: `docs__mcp-protocol`
(460 chunks). Includes the full SEP-1865 ext-apps spec
(2026-01-26), SEP-1686 Tasks proposal with community thread, and
MCP core spec for transports / lifecycle / resources / tools. Cite
from this corpus when verifying any ext-apps or MCP-protocol claim
in the design.

## Reading order

### 1. `workflow-engine-with-ext-apps.md` — the design doc

The substantive design. ~1990 lines. Cuts across:

- **Three combination patterns** — workflow-as-app, human-in-the-loop
  checkpoints, blueprint authoring as an app.
- **The blocking model and the yield alternative** — v1 ships blocking
  RPC; v2 defaults to yield semantics.
- **The 7 v1 shape constraints** — pure-data state, workflow IDs,
  execution log shape, await-annotation branch, stateless UI-MCPs,
  discriminated-union return schema, identity threading.
- **Split storage commitment** — `ExecutionStore` (CAS snapshot) +
  `AuditLog` (append-only). Atomic `park` / `resume` / `cancel`
  transitions, not raw CRUD.
- **Why isn't this just LangGraph?** — detailed comparison; the
  recommendation is Tier C (steal patterns, no dep) primarily on
  dependency-weight grounds.
- **Recommended TS-engine architecture** — client-hosted in-process
  by default; Mode B (HTTP+SSE + REST gateway) only when remote
  access is required.
- **ext-apps integration in detail** — protocol shape, lifecycle,
  blueprint declarations, parent-iframe strategy for
  `parallel { await_user; await_user }`.
- **Async surface** — 14 enumerated issues sorted by when they bite.
- **Adjacent workflow systems — hard-won lessons beyond LangGraph** —
  landscape map (4 quadrants), ASL versioning, conditional-logic
  escape, sub-workflow composition, saga gap, full HITL taxonomy
  (blocking / task-token / awakeable / waitForEvent / yield).
- **Substrate dependencies and pre-v1 experiments** — 4 substrate
  questions (MCP Tasks SEP-1686, nx plan library, effect annotations,
  ext-apps multi-iframe) + 5 bounded experiments.
- **Design space kept open** — 8 angles not committed (effect-shadow
  analysis, capability-aware blueprints, sub-workflows, OTel trees,
  record/replay, semantic diff, blueprint mining, beyond-iframe UI),
  plus the "no v2 durability ever" steelman.
- **Decision posture** — consolidated decisions with alternatives
  considered and reasons; v1 / v2 / substrate / experiments / deferred
  lists; open architectural questions.

Indexed: `docs__workflow-engine` (101 chunks).

### 2. `workflow-engine-synthesis.md` — landscape position

Cross-source synthesis (deep-research-synthesizer agent, May 2026).
Reads the design through the lens of the broader landscape:

- Positions the engine in the four-quadrant workflow-orchestration map.
- Distills lessons from AWS Step Functions, Temporal, Restate,
  Inngest, Argo, Tekton, LangGraph, Serverless Workflow Spec.
- Full human-in-the-loop taxonomy (5 mechanically distinct shapes).
- Convergent evidence where the design aligns with mature systems.
- Genuine novelties (defensible) and gaps (table-stakes elsewhere).
- 5 pre-commit experiments with bounded costs.
- Citations to external sources (paper, MCP roadmap, ext-apps spec,
  industry docs).

Most of this is now folded into the main doc's "Adjacent workflow
systems" section, but the synthesis remains the source-of-truth for
the citation trail.

Indexed: `docs__workflow-engine` (9 chunks).

### 3. `workflow-engine-brainstorm.md` — adjacent design space

Brainstorm pass (architect-planner agent, May 2026). 12 angles the
design doc does not develop:

1. nx plan library *is* the BlueprintStore
2. Catalog tumblers as workflow-scoped identifiers
3. Effect-shadow static analysis
4. Semantic blueprint diff
5. Workflow-as-test-fixture (record/replay)
6. Cross-blueprint composition (`engine.run_workflow` as a tool)
7. OpenTelemetry trace-tree by construction + cost roll-up
8. Agent-side `describe_workflow` tool
9. Capability-aware blueprints (workflow-as-privilege-bracket)
10. UI surfaces beyond ext-apps (voice / Slack / mobile / email)
11. Blueprint mining from observed agent traces
12. The "no v2 durability ever" steelman

Eight of the twelve are folded into the main doc's "Design space kept
open" section with status and rationale. The brainstorm document
retains the full sketches for reference.

Indexed: `docs__workflow-engine` (14 chunks).

### 4. (Reference) Parmar 2026 paper

Source artifact. Indexed locally as `knowledge__workflow-engine`
(79 chunks). Cite when verifying paper claims; do not paraphrase
without checking the chunk text.

## Provenance

This document set was produced through:

1. An initial design pass.
2. Two rounds of substantive critique (against the doc itself, then
   against the doc cross-referenced with the paper). Critiques
   surfaced one critical factual inversion (~54k token attribution),
   one silent extension (`collect` as a primitive), and structural
   gaps in interpreter architecture grounding, `branch_id` schema,
   identity threading, `WorkflowStore` interface, yield-vs-blocking
   framing, and LangGraph Tier-B rejection rationale. All addressed
   in the current main doc.
3. A landscape synthesis pass and a brainstorm pass.
4. A fold-up pass that integrated the synthesis and brainstorm
   findings into the main doc's structure, with the standalone
   companion documents retained as the source artifacts.
5. A spec-grounding pass: the canonical SEP-1865 ext-apps spec,
   SEP-1686 Tasks proposal (with community thread), and MCP core
   spec (transports / lifecycle / resources / tools) were fetched
   and indexed as `docs__mcp-protocol`. A deep-analyst verification
   against that corpus surfaced three refuted claims and three
   needing substantial correction in the doc's ext-apps section.
   All nine resulting patches were applied: the PostMessage
   protocol now uses actual SEP-1865 method names
   (`ui/initialize`, `ui/notifications/tool-input` etc.); the
   multi-iframe limitation is correctly attributed to host
   implementations not the spec; the Sandbox-proxy hop on web
   hosts is documented; the CSP defaults and four
   `_meta.ui.csp.*` fields are spec-derived; SEP-1686 is
   characterised as "accepted by Core maintainers, iterating to
   final" rather than "experimental"; the spec's
   `notifications/progress` lever for extending host tool-call
   timeouts is named; Streamable HTTP (with `Last-Event-ID`
   resumability) replaces "HTTP+SSE" throughout; the `ui/message`
   fallback channel is added to progressive enhancement.

## Reference material to (re-)index on the receiving instance

The companion corpora cited throughout the design (`knowledge__workflow-engine`,
`docs__mcp-protocol`) are indexed on the **originating** instance. On a
different machine those collections won't exist; their tumbler-shaped
collection names (e.g. `knowledge__workflow-engine__voyage-context-3__v1`,
`docs__mcp-protocol__voyage-context-3__v1`) are project-local artifacts
of the catalog/T3 layout per RDR-101/RDR-108 and may also be organized
differently on the receiving instance (different corpus name, different
tumbler scheme, different catalog conventions). Re-index from source,
don't try to import-by-name.

Fetch and index the following before relying on any spec-citation in the
main doc. All URLs verified as of May 2026.

### The source paper

- **Parmar 2026** — "Separating Intelligence from Execution":
  `https://arxiv.org/pdf/2605.00827v1` (PDF; 12 pages; the doc was indexed
  with 16 pages via MinerU due to detected formulas).
  Suggested local target: `knowledge__workflow-engine` (or the receiving
  project's external-PDF convention).

### MCP ext-apps spec (SEP-1865)

GitHub repo: `modelcontextprotocol/ext-apps`. Fetch raw via
`gh api repos/modelcontextprotocol/ext-apps/contents/<path>` and base64-decode.

Files to grab:

- `README.md` — entry-point overview
- `specification/2026-01-26/apps.mdx` — the normative spec (1768 lines).
  The load-bearing reference. If you only fetch one ext-apps file, this
  is it.
- `docs/overview.md` — high-level rationale
- `docs/quickstart.md` — minimal working example
- `docs/patterns.md` — interaction patterns, large-payload handling,
  `ui/update-model-context` recipes
- `docs/csp-cors.md` — security model details (short)
- `docs/authorization.md` — auth scenarios
- `docs/testing-mcp-apps.md` — testing harness
- `docs/migrate_from_openai_apps.md` — useful as a reverse map: what
  ChatGPT widgets do vs SEP-1865
- `docs/agent-skills.md` — agent-skill integration

Suggested local target: `docs__mcp-protocol` (or your equivalent).

### MCP core spec — the load-bearing subset

GitHub repo: `modelcontextprotocol/modelcontextprotocol`. Paths under
`docs/specification/draft/`.

- `basic/transports.mdx` — stdio, Streamable HTTP, resumability,
  `Last-Event-ID`. Needed to ground the v1 transport claims.
- `basic/lifecycle.mdx` — initialization handshake, timeouts (the
  SHOULD-set-timeouts and MAY-reset-on-progress clauses are the
  spec-sanctioned levers for long-running calls).
- `server/resources.mdx` — `resources/list`, `resources/read`,
  `notifications/resources/updated`.
- `server/tools.mdx` — tool definition shape; how `_meta` fields are
  carried.

For a deeper dive, the same directory has `basic/authorization.mdx`,
`server/prompts.mdx`, `server/utilities/`, `client/utilities/`,
`changelog.mdx`. Not load-bearing for the design as written but useful
when extending it.

### MCP Tasks proposal (SEP-1686)

Not a file — a GitHub issue with an extensive community thread.
Fetch via `gh issue view 1686 --repo modelcontextprotocol/modelcontextprotocol
--json title,body,state,comments`. Include the comments; the open issues
(idempotency 2452/2451, deadlines 1956, polling 1955) are the gaps the
design cites.

Related open PRs worth checking the state of when revisiting:

- SEP-2694: Resumable Task Event Streams —
  `https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2694`
- SEP-2679: Task streaming partial results —
  `https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2679`
- SEP-1905: Task Result Streaming and Immediate Result Acceptance —
  `https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1905`
- SEP-2575: Make MCP Stateless —
  `https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2575`

### Bulk-index recipe (originating instance used this shape)

```bash
mkdir -p /tmp/mcp-refs && cd /tmp/mcp-refs

# 1. ext-apps repo files
for f in README.md docs/overview.md docs/quickstart.md docs/patterns.md \
         docs/csp-cors.md docs/authorization.md docs/testing-mcp-apps.md \
         docs/migrate_from_openai_apps.md docs/agent-skills.md \
         specification/2026-01-26/apps.mdx; do
  out="ext-apps-$(echo "$f" | tr '/' '-')"
  gh api "repos/modelcontextprotocol/ext-apps/contents/$f" --jq '.content' \
    | base64 -d > "${out%.mdx}.md"
done

# 2. MCP core spec
for f in docs/specification/draft/basic/transports.mdx \
         docs/specification/draft/basic/lifecycle.mdx \
         docs/specification/draft/server/resources.mdx \
         docs/specification/draft/server/tools.mdx; do
  out="mcp-spec-$(basename "$f" .mdx).md"
  gh api "repos/modelcontextprotocol/modelcontextprotocol/contents/$f" --jq '.content' \
    | base64 -d > "$out"
done

# 3. SEP-1686 Tasks (issue + thread)
gh issue view 1686 --repo modelcontextprotocol/modelcontextprotocol \
  --json title,body,state,comments \
  --jq '"# \(.title)\n\nState: \(.state)\n\n## Body\n\n\(.body)\n\n## Comments\n\n" +
        ([.comments[] | "### @\(.author.login)\n\(.body)\n"] | join("\n---\n"))' \
  > mcp-sep-1686-tasks.md

# 4. Index into your local convention (this example uses nexus's `nx index md`)
for f in *.md; do nx index md "$PWD/$f" --corpus mcp-protocol; done

# 5. Parmar paper
curl -sL https://arxiv.org/pdf/2605.00827v1 -o parmar-2026.pdf
nx index pdf "$PWD/parmar-2026.pdf" --collection knowledge__workflow-engine
```

If your receiving environment uses a different indexing tool, the corpus
names and tumbler shapes will differ — but the *content* to fetch is the
same. The design doc cites these sources by name and quoted spec
language, not by tumbler ID.

## How to use this set

For deeper reading: the main doc is self-contained. Synthesis and
brainstorm are useful when verifying a claim's provenance or when
considering an adjacent angle.

For further synthesis: the four substrate questions and five pre-v1
experiments in the main doc's Decision Posture are the highest-leverage
unblocked surface. Experiments #1 (measure host stdio timeout) and #2
(read the existing TS interpreter) are both bounded enough to run
immediately and gate large pieces of the design.

For implementation: the v1 shape constraints (7) + the split
`ExecutionStore` / `AuditLog` interface + the parent-iframe ext-apps
strategy are the load-bearing v1 commitments. Everything else is
either deferred to v2 (additive) or kept open in the design-space
section (genuinely undecided).
