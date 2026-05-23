---
title: "nx_answer A2UI Surfaces: Render Composed-Retrieval Responses as Structured Surfaces"
id: RDR-123
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date:
related_issues: []
related_rdrs: [RDR-110, RDR-111, RDR-118, RDR-119]
related_tests: []
implementation_notes: ""
---

# RDR-123: nx_answer A2UI Surfaces — Render Composed-Retrieval Responses as Structured Surfaces

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`nx_answer` is the canonical entry point for analytical questions over nexus knowledge collections. Today it returns a markdown blob — citations inline as text, evidence interleaved with synthesis, follow-up questions buried at the end. Consumers (Claude Code orchestrators, future cockpit hosts, sub-agents) get a wall of prose and must re-parse it to act on individual citations or evidence items.

The shape of an `nx_answer` response is **already structured**: there's a synthesis, a set of cited chunks (each with chash, collection, score), a confidence signal, optional follow-up plan steps. Flattening it to markdown discards that structure. Downstream consumers re-grep their way back to it imperfectly.

A2UI v0.9 (adopted as nexus's wire-level surface descriptor in RDR-119) gives us a structured format for exactly this: a `Card` per citation, a `Text` for the synthesis, a `List` of follow-up actions, optional `Button` affordances. Rendering hosts already exist (terminal via notcurses per RDR-118, Tauri via Lumino, Claude Code via inline MCP App resources).

## Context

### Background

RDR-118 (surfaces as tuples) and RDR-119 (cockpit UI fabric) established A2UI as the descriptor format and Bakke auto-layout as the catalog-selection policy. The plumbing is in place. What's missing is producers — and `nx_answer` is the highest-traffic producer candidate in nexus because every analytical question routes through it (per `using-nx-skills` red-flag table).

Adjacent precedent: RDR-053 (Xanadu fidelity, closed) established `chash:` as the content-addressed citation primitive. `nx_answer` already emits chash references in its evidence list; rendering them as Card components with `BoundValue { kind: chash, path: "<chash>" }` makes the citation directly navigable in any A2UI host.

### Technical Environment

`nx_answer` today is implemented in `nexus/operators/answer.py` (entry via MCP tool `mcp__plugin_conexus_nexus__nx_answer`). Internally it:

1. Runs plan-match gate (RDR-080 plan library).
2. Composes search + traverse + rank operators.
3. Collates results into a synthesis prompt.
4. LLM generates final answer.
5. Returns markdown string.

The structured intermediates from steps 2-3 are discarded at step 5.

### Constraints

- **Backward compatible.** Existing callers consume the markdown string. Surface emission must be opt-in via a `response_format="surface" | "markdown" | "both"` parameter, default `"markdown"`.
- **Catalog discipline.** Only Basic Catalog components (per a2ui v0.9). No host-specific extensions — that's the renderer's job.
- **No new LLM call.** Surface construction is a structured transformation of intermediates already in hand; no extra inference.
- **Citation fidelity.** Every chash citation in the markdown variant must appear as an addressable Card in the surface variant. Lossless transformation.

## Decision

Add `response_format` parameter to `nx_answer`. When set to `"surface"` or `"both"`, emit an A2UI v0.9 `surfaceUpdate` payload alongside (or instead of) the markdown.

### Surface schema (per nx_answer response)

```
Surface
├── Text (id: synthesis) — the LLM-generated answer body
├── List (id: citations)
│   ├── Card (id: cite-{n}) — one per cited chunk
│   │   ├── Text (chunk title + collection + score)
│   │   ├── Text (chunk excerpt, BoundValue { kind: chash, path: <chash> })
│   │   └── Button (id: cite-{n}-open, action: open-chash, payload: { chash })
│   └── ...
├── Card (id: confidence) — optional, only if confidence signal present
│   └── Text (confidence summary + caveats)
└── List (id: followups) — optional, only if plan-match suggested follow-up steps
    └── Button (one per suggested next-step skill)
```

### Wiring

- `nexus/operators/answer.py` gains a `_to_surface(intermediates, synthesis) -> SurfacePayload` helper.
- MCP tool wrapper accepts `response_format` and dispatches accordingly.
- Output envelope: for `"both"`, return `{ "markdown": "...", "surface": {...} }`; for `"surface"`, return just the surface; for `"markdown"` (default), unchanged.
- Hosts that don't render A2UI (current Claude Code without MCP App support) keep using markdown.

### Action handlers

The `open-chash` action is host-side: the renderer resolves the chash via `nexus.catalog` (existing `mcp__plugin_conexus_nexus-catalog__resolve` tool) and opens the resulting document/chunk. No nexus-side handler change.

## Alternatives Considered

### Alt 1: Surfaces as the only format (no markdown)

Replace markdown entirely. Rejected because:
- Breaks every existing caller including the Claude Code orchestrator without MCP App support.
- Forces premature commitment to A2UI catalog stability for a producer-side change.
- One-way doors should be deferred until the cockpit substrate is more widely deployed.

### Alt 2: Markdown with embedded JSON sidecar

Ship a `<!-- nx_answer_data: {...} -->` JSON block in the markdown. Rejected because:
- Couples consumers to a nexus-specific sidecar format.
- A2UI v0.9 already exists as the standard for exactly this.
- Sidecar formats rot — A2UI evolves under spec governance.

### Alt 3: Wait for cockpit GA before producer work

Defer until RDR-118/119 ship and there's a host actually rendering surfaces. Rejected because:
- Producer and consumer work parallelize cleanly with the `response_format` opt-in.
- Without producers, the cockpit substrate has nothing to render — bootstrapping order matters.
- Risk of cockpit shipping with only synthetic test surfaces and no real producer is higher than risk of producer shipping ahead of cockpit.

## Consequences

### Positive

- First substantial A2UI producer in nexus — validates the wire format end-to-end on real content.
- Citations become addressable and interactive in hosts that render A2UI.
- Structured intermediates from `nx_answer` stop being thrown away.
- Pattern generalizes: `/conexus:query`, `/conexus:research`, `/conexus:analyze` are obvious next producers.

### Negative

- Two output paths to maintain (markdown + surface). Mitigated by single underlying intermediates representation — surface is a view, not a parallel pipeline.
- Surface schema drift risk if a2ui v0.9 → v0.10 changes the relevant primitives. Mitigated by versioned producer (matching RDR-119 catalog versioning).

### Neutral

- Telemetry: track `response_format` distribution to measure cockpit uptake.

## Success Criteria

- [ ] `response_format` parameter shipped on `nx_answer`.
- [ ] Surface emission produces a v0.9-valid `surfaceUpdate` payload for at least 5 representative nx_answer queries (test corpus in `tests/operators/nx_answer/`).
- [ ] Every chash citation in the markdown variant has a matching Card in the surface variant (lossless transformation test).
- [ ] One downstream consumer wired to render the surface (terminal renderer per RDR-118, or Claude Code MCP App resource).
- [ ] No regression in `nx_answer` latency (surface construction must be < 5ms — structured transform, not inference).

## Open Questions

1. **Follow-up plan steps as Buttons** — currently `nx_answer`'s plan-match gate suggests follow-up skills. Should those be `Button` components with `openUrl`-style actions, or just `Text`? Buttons require the host to know how to dispatch nexus skills — couples renderer to nexus skill catalog. Defer to implementation review.
2. **Confidence visualization** — A2UI Basic Catalog has no progress/gauge component. Use a plain `Text` for now, revisit if we need richer.
3. **Streaming surface updates** — for long nx_answer responses, could emit `surfaceUpdate` incrementally as the LLM streams. Worth it? Defer to follow-up RDR; v1 is whole-response.
4. **Catalog version pinning** — does the surface payload carry a `catalogVersion`? Required by a2ui v0.10, optional in v0.9. Pin to v0.9 for v1 of this RDR.

## References

- a2ui architecture analysis — T3 doc `architecture-a2ui-overview` (2026-05-19)
- RDR-118 — surfaces as tuples (surface_cell subspace)
- RDR-119 — cockpit UI fabric (catalog negotiation, Bakke auto-layout)
- RDR-053 — Xanadu fidelity (chash as citation primitive)
- A2UI v0.9 specification — `specification/v0_9/` in `/Users/hal.hildebrand/git/a2ui`
