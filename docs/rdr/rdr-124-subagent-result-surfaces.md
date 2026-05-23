---
title: "Subagent Result Surfaces: Return A2UI Cards from Subagents to Relieve Orchestrator Context"
id: RDR-124
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date:
related_issues: []
related_rdrs: [RDR-118, RDR-119, RDR-123]
related_tests: []
implementation_notes: ""
---

# RDR-124: Subagent Result Surfaces — Return A2UI Cards from Subagents to Relieve Orchestrator Context

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus subagents (codebase-deep-analyzer, deep-research-synthesizer, code-review-expert, debugger, etc.) return their findings as prose to the orchestrator. A typical run produces 600–1500 tokens of structured analysis flattened into markdown. The orchestrator then:

1. Reads the whole thing into context (orchestrator-context cost).
2. Decides what to relay to the user.
3. Re-writes a summary, often dropping structure (filenames, line numbers, evidence chains) the subagent produced.

Two distinct costs: **context-budget pressure** on the orchestrator, and **information loss** in the relay step. A 1200-word subagent report becomes a 200-word orchestrator summary; 80% of the structured detail is discarded or compressed lossily.

Subagent outputs are **already structured** — codebase-deep-analyzer produces module maps, dependency graphs, open-questions lists; code-review-expert produces issue lists with file/line/severity; debugger produces hypothesis chains. Each maps cleanly onto A2UI Basic Catalog primitives (Card per finding, List for grouping, Button for follow-up actions).

## Context

### Background

RDR-123 (concurrent draft) introduces `response_format="surface"` for `nx_answer`. This RDR extends the pattern to subagent results: a subagent can return a surface payload alongside (or instead of) prose, the orchestrator embeds the surface reference rather than the full text, and the user's host renders the surface directly when supported.

Orchestrator-context relief is the load-bearing benefit. Today every subagent dispatch costs ~1500 orchestrator tokens. Surface emission lets the orchestrator hold a pointer (surface_id + summary) and let the host pull the surface from the surface_cell subspace (RDR-118) on demand.

### Technical Environment

Subagent dispatch flow:

- Orchestrator calls `Agent` tool with subagent type + prompt.
- Subagent runs in isolated context, produces a result.
- Result is returned as the Agent tool's return value to the orchestrator.
- Orchestrator must read the entire return value into context.

The Agent tool's return value is a string today. Extending it to optionally carry structured surface payloads requires either:

(a) wrapping the existing string return in a JSON envelope (breaking change), or
(b) emitting the surface to a side channel (T1 scratch or surface_cell subspace per RDR-118) and returning a short pointer + summary in the string.

Option (b) is reversible and doesn't break existing subagent contracts.

### Constraints

- **No Agent-tool contract changes.** Subagents continue to return strings. Surface side-channel is the mechanism.
- **Pointer + summary in the return string.** The orchestrator needs *something* in context — a 2-3 sentence summary plus a surface_id is the right granularity.
- **Producer-opt-in.** Each subagent decides whether to emit a surface. Some (code-explorer doing a one-shot lookup) don't need to; some (codebase-deep-analyzer, code-review-expert) benefit substantially.
- **Hosts without A2UI rendering** must still get useful output. The pointer+summary is human-readable and the surface payload is fetchable via `tuplespace_read` for fallback prose rendering.

## Decision

Subagents that opt in emit their structured result as an A2UI surface to the `surface_cell` subspace (RDR-118) with a generated `surface_id`. The return value to the orchestrator becomes:

```
## {subagent_type} — {short title}

**Surface:** `surface_cell:{surface_id}` ({N} findings, view via `/conexus:surface-show {surface_id}`)

{2-3 sentence summary highlighting top finding and any blocker}

{Optional: 3-5 most critical findings as bullets — orchestrator can read these without fetching}
```

The full surface in `surface_cell` holds the complete structured payload. The orchestrator can choose to fetch it (`tuplespace_read`) if it needs detail, or pass the surface_id through to the user's host for direct rendering.

### Subagent-side wiring

A shared helper `nexus.subagents.emit_surface(findings: FindingsSchema) -> SurfaceId`:

1. Builds A2UI v0.9 surface from a subagent-specific `FindingsSchema` (one per subagent type — codebase-analysis findings, code-review findings, debug findings, research findings).
2. Writes surface to `surface_cell` subspace with TTL matching session.
3. Returns the `surface_id` for embedding in the return string.

### FindingsSchema per subagent (v1)

| Subagent | Schema |
|---|---|
| codebase-deep-analyzer | `{ modules: [...], patterns: [...], data_flow: [...], open_questions: [...] }` |
| code-review-expert | `{ issues: [{file, line, severity, message, suggestion}] }` |
| debugger | `{ hypotheses: [...], evidence: [...], conclusion, fix_attempts: [...] }` |
| deep-research-synthesizer | `{ findings: [{claim, evidence_chash, confidence}], gaps: [...] }` |

Each schema serializes to a surface with the structure laid out in RDR-123 (Card per item, List for grouping, Buttons for follow-up actions like "open this file" or "run this skill").

### Orchestrator-side wiring

No required changes. Orchestrators that want to relay surfaces directly to user-side rendering can pass the surface_id through; orchestrators that don't can keep ignoring it and operate on the summary alone.

## Alternatives Considered

### Alt 1: Wrap Agent tool return value in JSON envelope

`{ summary: "...", surface: {...}, raw: "..." }` instead of bare string. Rejected because:
- Breaks every existing subagent contract.
- Requires Agent tool changes that nexus doesn't own (Claude Code platform).
- Side-channel via surface_cell is reversible and platform-agnostic.

### Alt 2: Subagents write findings directly to T2 memory

Skip A2UI; persist findings as structured memory entries. Rejected because:
- Loses host-rendering benefit (markdown-on-fetch instead of native widgets).
- Conflates "subagent intermediate output" with "promoted T2 finding" — those have different lifecycles.
- A2UI surfaces are already the descriptor format per RDR-119; introducing a parallel format is unjustified.

### Alt 3: Keep markdown returns, just structure the markdown better

Convention-based: every subagent emits H2 headers, bullet lists with specific patterns. Rejected because:
- Markdown structure is unparseable by hosts — they'd have to re-derive structure with heuristics.
- Doesn't relieve orchestrator-context pressure; the full text is still in the return value.
- Doesn't enable interactive affordances (Buttons for follow-up actions).

## Consequences

### Positive

- Orchestrator-context pressure reduced for any subagent dispatch with structured findings — measurable in tokens-per-dispatch.
- Findings become interactive in hosts that render A2UI (click a code-review issue → opens file at line).
- Surface payloads live in `surface_cell` and can outlive the immediate dispatch — useful for "show me what that subagent found earlier."
- Compounds with RDR-123: same surface infrastructure, two producer categories.

### Negative

- Subagent-side complexity: each opt-in subagent needs a FindingsSchema and serialization. Mitigated by shared helper and per-subagent schema (small, declarative).
- TTL discipline: surfaces in `surface_cell` need to expire or accumulate. Use session-scoped TTL by default.
- Debugging the relay: when a subagent emits a surface that doesn't render correctly, the failure is across two subsystems (subagent producer + host renderer). Mitigate via subagent-side surface validation against the a2ui v0.9 schema before emit.

### Neutral

- Telemetry: track surface-emit rate per subagent type and orchestrator-fetch rate.

## Success Criteria

- [ ] Shared `emit_surface` helper shipped with one FindingsSchema (codebase-deep-analyzer chosen as the pilot — highest-token subagent).
- [ ] codebase-deep-analyzer emits a surface on dispatch; return value to orchestrator includes pointer + ≤ 200-word summary.
- [ ] Surface payload validates against a2ui v0.9 schema in subagent-side check.
- [ ] Measurable token-per-dispatch reduction on codebase-deep-analyzer (target: ≥40% reduction in orchestrator return-value size).
- [ ] One downstream consumer reads the surface and renders it — terminal renderer per RDR-118, or `/conexus:surface-show` skill (new).
- [ ] If token reduction target not met or surfaces aren't being consumed, RDR is wrong — iterate before extending to other subagents.

## Open Questions

1. **`/conexus:surface-show` skill** — a new skill that fetches a surface by id and renders it (markdown fallback for non-A2UI hosts). Required for v1, or follow-up RDR? Required for v1 — without a consumer, the surface is write-only.
2. **Surface TTL policy** — session-scoped default, but some surfaces (a definitive architecture analysis) should be promoted to T2/T3. Use existing promote mechanism with surface-as-memory mapping? Defer to implementation.
3. **Should the orchestrator be allowed to *edit* a surface?** — e.g., orchestrator marks issues as triaged. Out of v1 scope; surfaces are immutable on emit.
4. **Failure mode: subagent emits invalid surface** — fall back to prose return? Or fail loudly? v1: fall back to prose, log validation error. Reconsider after rollout.

## References

- a2ui architecture analysis — T3 doc `architecture-a2ui-overview` (2026-05-19)
- RDR-118 — surface_cell subspace and surfaces-as-tuples
- RDR-119 — cockpit UI fabric and catalog negotiation
- RDR-123 — nx_answer A2UI surfaces (sibling RDR, same surface infrastructure)
