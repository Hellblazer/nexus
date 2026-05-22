---
title: "Surface Rendering: Palinex is Downstream (nexus has no integration story to ship)"
id: RDR-127
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-22
revised: 2026-05-22
accepted_date:
related_issues: []
related_rdrs: [RDR-053, RDR-118, RDR-119, RDR-120, RDR-122, RDR-123, RDR-124]
related_external: [palinex-rdr-001, palinex-rdr-003, a2ui-v0.9-spec]
related_tests: []
supersedes: [RDR-123, RDR-124]
---

# RDR-127: Surface Rendering — Palinex is Downstream

> Architecture for a2ui v0.9 surface emission, the SurfaceBroker port,
> the postMessage RPC, and the Claude Code MCP UI plugin lives in
> palinex's own RDRs ([RDR-001](https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-001-architecture.md),
> [RDR-002](https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-002-pyodide-as-runtime-augmentation.md),
> RDR-003 — plugin + HTTP sidecar packaging). This RDR records nexus's
> *non-decision*: nexus ships no surface-rendering code.

## Problem Statement

The previous attempts at putting surface rendering inside nexus (RDR-118
"surfaces as tuples" + RDR-119 "cockpit UI fabric") tied rendering to the
scrapped cockpit substrate stack (RDR-110/111/112/113). RDR-123 and RDR-124
(drafted 2026-05-19) inherited that dependency.

The successor work first attempted nexus-side integration with palinex (RDR-127
v1, drafted earlier this session, then reversed): a `render_surface` MCP tool
in nexus that called palinex's `wrap_as_mcp_ui_resource`. Useful in isolation;
unnecessary in context. The cleaner shape is the **opposite dependency direction**:
palinex depends on nexus (optional `[nexus]` extra), ships its own Claude Code
plugin + HTTP sidecar, and owns the integration story from end to end. Nexus
needs nothing surface-specific.

This RDR codifies that decision. Its operative content is one paragraph plus
the supersession bookkeeping for RDR-123/124.

## Decision

Nexus ships no surface-rendering code. Users who want to render a2ui v0.9
surfaces in Claude Code install the palinex Claude Code plugin (a separate
project, downstream of nexus). The plugin starts the palinex MCP server, which
exposes `render_surface(payload)` and resolves chash references against
nexus's existing T3 / catalog APIs via `import nexus`. Nexus does not need to
know about palinex; the import flows the other way.

The HTTP sidecar (`palinex serve`) is the corresponding deployment for
non-Claude-Code use cases (browser-served renderer with a working backend).
Same direction of dependency.

## Supersession bookkeeping

RDR-123 (nx_answer surfaces) and RDR-124 (subagent surfaces) are superseded
by this decision. Their intents — `nx_answer` returning surfaces, subagents
emitting surfaces — are preserved at the palinex layer:

- For `nx_answer`: callers wanting a surface render compose two tools — call
  `nx_answer` for the synthesis + chash list, then pass to the palinex plugin's
  `render_surface` tool. Or: a future `nx_answer` could emit a surface payload
  directly as part of its response when called with an appropriate flag, with
  the host then rendering via palinex's plugin. Either way, no nexus-side
  rendering code.
- For subagents: same composition pattern. A subagent's structured output
  becomes a palinex `Surface`, then gets handed to the plugin's `render_surface`
  for emission as an MCP UI resource. Nexus subagents stay rendering-agnostic.

T2 `nexus_rdr/RDR-127` entry will be updated to reflect this slimmed scope.

## Alternatives Considered

### Alt 1: nexus ships a `render_surface` MCP tool (this RDR's v1, withdrawn)

Was: nexus imports palinex, registers an MCP tool wrapping it, ships a chash
resolver in nexus code. Rejected because the dependency direction was backward:
palinex was downstream, but nexus was importing it. Inversion is cleaner —
palinex owns the integration and depends on nexus when it needs to.

### Alt 2: Embed surface rendering in cockpit substrate (RDR-118/119)

Tried; scrapped 2026-05-19. Surface rendering does not need the cockpit
substrate, so coupling them again would re-introduce the same scope debt.

### Alt 3: Wait for cockpit substrate, then revisit

Open-ended slippage. Surface rendering can ship today via palinex without
any nexus-side substrate work.

## Consequences

- (+) Nexus stays clean of surface-rendering code; the integration story lives
  one repo over where it belongs.
- (+) Palinex can iterate independently — plugin updates, sidecar evolution,
  renderer changes — without churning nexus's release cycle.
- (+) Users compose tools through Claude (the agent) rather than via a baked-in
  resolver inside an MCP tool. Composition stays general.
- (−) "Integration" is now spread across two repos — palinex's documentation
  has to clearly describe the nexus integration. Mitigated by palinex RDR-003.
- (−) Two install commands instead of one (`pip install conexus`, then install
  palinex plugin in Claude Code). Acceptable cost for the cleaner boundary.

## References

- **palinex RDR-001** — architecture (a2ui v0.9 as IR, SurfaceBroker, three
  delivery shapes, postMessage RPC, markdown sidecar):
  <https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-001-architecture.md>
- **palinex RDR-002** — Pyodide as preferred runtime augmentation:
  <https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-002-pyodide-as-runtime-augmentation.md>
- **palinex RDR-003** — Claude Code plugin + HTTP sidecar packaging (in progress)
- palinex repo — <https://github.com/Hellblazer/palinex>
- palinex on PyPI — <https://pypi.org/project/palinex/>
- RDR-053 — Xanadu fidelity (chash addresses; nexus exposes via existing
  catalog API for palinex to use)
- RDR-118, RDR-119 — Scrapped; lineage preserved as tombstones
- RDR-122 — LLM-JSON Repair Pass (orthogonal, still valid)
- RDR-123, RDR-124 — Superseded by this RDR (intent preserved at palinex layer)

## Revision History

_2026-05-22 (v1) — drafted as a substantial integration RDR proposing nexus-side
`render_surface` MCP tool importing palinex. Implementation landed alongside,
then immediately reversed when the dependency direction was reconsidered._

_2026-05-22 (v2, current) — slimmed to the operative decision: nexus ships no
surface-rendering code; palinex (a downstream project) owns the integration
via its own Claude Code plugin and HTTP sidecar, with nexus as a `[nexus]`
extra dependency. Implementation files removed; supersession of RDR-123/124
preserved._
