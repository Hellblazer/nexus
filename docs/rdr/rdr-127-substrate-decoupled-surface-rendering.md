---
title: "Substrate-Decoupled Surface Rendering: palinex as v1 Reference, a2ui v0.9 as IR, Bolt Points for the Eventual Cockpit"
id: RDR-127
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-22
accepted_date:
related_issues: []
related_rdrs: [RDR-053, RDR-110, RDR-111, RDR-118, RDR-119, RDR-120, RDR-122, RDR-123, RDR-124]
related_external: [a2ui-v0.9-spec, palinex-0.0.1, mcp-ui-resources]
related_tests: []
supersedes: [RDR-123, RDR-124]
---

# RDR-127: Substrate-Decoupled Surface Rendering

> palin- (Greek: "again") + nexus (Latin: "bond") — the surface gets rewritten in place.

## Problem Statement

Nexus producers (`nx_answer`, subagent results, plan inspectors, RDR dashboards) routinely emit structured intermediates — citations, findings, hypothesis chains, ranked results — and flatten them to markdown before returning. The structure is lost in the flattening; downstream consumers re-derive it imperfectly.

The previous attempt at fixing this (RDR-118 "surfaces as tuples" + RDR-119 "cockpit UI fabric") tied surface rendering to the cockpit substrate stack (RDR-110 semantic tuple space, RDR-111 ORB cockpit, RDR-112 storage-as-service, RDR-113 host-trust). That arc was scrapped 2026-05-19 when the substrate work was deferred to RDR-120 and the moratorium on consumer-stack RDRs went into effect. RDR-123 and RDR-124 (drafted 2026-05-19) inherited the same dependency and are likewise unbuildable today.

The framing that worked the last time was wrong. **Surface rendering does not need the cockpit substrate.** It needs:
1. An IR for the content. ← *adopted, not invented: a2ui v0.9*
2. A renderer. ← *can be a single HTML file rendered today by Claude Code*
3. A delivery channel between producer and renderer. ← *MCP UI resource for the host we already have*
4. A bolt point where the substrate plugs in later. ← *one Protocol interface*

This RDR codifies that shape with a working reference implementation already shipped to PyPI as `palinex 0.0.1` and to GitHub Pages as `https://hellblazer.github.io/palinex/`.

### Enumerated gaps to close

1. **No canonical IR decision.** Producers either emit ad-hoc JSON or fall back to markdown. Need a decision that a2ui v0.9 is the IR.
2. **No reference renderer for the host we use daily.** Claude Code can render MCP UI resources; nothing in nexus produces them.
3. **No substrate-decoupling seam.** Previous attempts coupled rendering to tuple-space subspaces (RDR-118). Need a Protocol interface that hides storage/transport, so the renderer and producers don't see them.
4. **Action handling without trust gates.** RDR-118 §6 punted; we need a v1 allowlist.
5. **Markdown sidecar discipline.** Producers must always emit markdown alongside surfaces so non-rendering hosts still get useful output.
6. **Outstanding supersession of RDR-123/124.** Both were merged 2026-05-19 but are tombstone-shaped — they depend on scrapped 118/119.

## Context

### Background

The path from the scrapped arc to this RDR ran through three observations (recorded in conversation 2026-05-20 through 2026-05-22):

1. **a2ui itself is the IR.** RDR-118 treated a2ui as one of several wire formats; I drafted a parallel `SurfaceIR` in early sketches. That was duplicative — a2ui v0.9 already ships per-version structural backward-compat (`v0_8/`, `v0_9/`), 18 Basic Catalog components, JSON Schema validation, and four-message lifecycle protocol. Adopt it whole, no parallel IR.
2. **AG-UI is perpendicular, not central.** I kept name-dropping AG-UI as a transport option in early diagrams; it earns its place only when we own both ends of an agent↔web-UI conversation. For Claude Code as host, it's not in the picture.
3. **A Claude Artifact is a Willison-style HTML tool, and so is the renderer.** The Willison `html-tools` patterns (catalogued in T3 `simonw-2025-12-10-html-tools-patterns` and `surface-renderer-html-tool-patterns-for-nexus`) describe exactly the discipline a v1 renderer needs: one file, no build step, CDN-pinned deps, small. The renderer is a single HTML file. It ships today.

### Technical environment

| Layer | Today | After RDR-120 substrate work lands |
|---|---|---|
| IR | a2ui v0.9 (palinex pins) | unchanged |
| Renderer | `palinex/index.html` (lit-html, ~710 LOC) | same file, possibly with notcurses/Lumino added as additional renderers |
| Producer-side | `palinex` PyPI package (Python builders, structural validation) | nexus depends on or vendors palinex; emits via `SurfaceBroker.put_cell()` |
| Delivery | MCP UI resource (Claude Code), embedded artifact, or external URL | + tuple-space `surface_cell` subspace (RDR-118 reactivation) |
| Action handling | `openUrl`, `copyToClipboard`, `openChash` (host-bridged via postMessage RPC) | + skill dispatch, file open, etc. as the action registry grows |
| Substrate | none — in-process, return-value-shaped | RDR-118 `surface_cell` subspace via RDR-120-shipped storage |

palinex is already real and tested: 19 passing tests, four-Python matrix CI (3.10-3.13), tag-triggered OIDC trusted-publisher release flow mirroring nexus's own. The renderer hosts at `https://hellblazer.github.io/palinex/` and the package at `https://pypi.org/project/palinex/`.

### Constraints

- **Substrate-independent.** Nothing in this RDR's v1 footprint requires RDR-118/119/120 to be alive. The same code stays correct when they ship.
- **No new IR.** a2ui v0.9 is the contract. Nexus-specific extensions (chash references, custom actions) live behind the `Action` shape and `BoundValue` literal strings, not as schema extensions.
- **Markdown fallback always.** Producers emit both a surface payload and a lossless markdown sidecar. The sidecar is what every host gets if surface rendering isn't available.
- **No long-running daemon required.** The reference renderer works from `file://`. Sidecar HTTP proxy is optional, postMessage-bridged host is the primary path.
- **Action allowlist.** Three actions in v1 (`openUrl`, `copyToClipboard`, `openChash`). Adding `runSkill`, `openFile`, etc. is incremental and gated.
- **palinex is the reference, not a hard dependency.** Other producers/renderers can target the same IR independently. palinex exists to validate the shape.

## Decision

Adopt **a2ui v0.9** as the canonical declarative UI IR for nexus producers. Adopt **palinex** (PyPI / GitHub) as the v1 producer-side helper and reference renderer. Codify a substrate-decoupled architecture with explicit bolt points so the eventual cockpit substrate (RDR-118/119/120 successors) plugs in without changing producers, renderers, or the IR.

Mark RDR-123 (`nx_answer surfaces`) and RDR-124 (`subagent result surfaces`) as **superseded by RDR-127**. The intent of both is preserved here, reframed to depend on palinex + the SurfaceBroker port instead of the scrapped surface_cell subspace.

### Approach

**Item 1: Pin a2ui v0.9 as the IR.** Producers emit `version: "v0.9"` message envelopes (`createSurface`, `updateComponents`, `updateDataModel`, `deleteSurface`) per `https://a2ui.org/specification/v0.9-a2ui/`. palinex provides typed builders; structural validation is the producer-side gate. Deep jsonschema validation is opt-in via `pip install palinex[validate]`.

**Item 2: Adopt palinex as the v1 reference.** Producers depend on `palinex >= 0.0.1` for payload construction. The single-file renderer `palinex/index.html` is hosted at `https://hellblazer.github.io/palinex/` and embedded in nexus deliveries via MCP UI resource wrapping.

**Item 3: Define the `SurfaceBroker` Protocol** as the substrate-decoupling seam:

```python
class SurfaceBroker(Protocol):
    def put_cell(self, surface_id: str, slot: str, cell: Cell) -> None: ...
    def take_cell(self, surface_id: str, slot: str) -> None: ...
    def subscribe(self, surface_id: str, callback) -> Subscription: ...
    def post_action(self, surface_id: str, slot: str, action: UserAction) -> None: ...
```

where `Cell` is the shape RDR-118 §1 specified (surface_id, slot, origin, child_subspace, a2ui_catalog_id, representation_type, data_ref, data_ref_kind, actions) — frozen as a dataclass, *not* parameterized over storage. v1 ships `InProcessBroker` (dict + asyncio pub/sub). vN swaps in `TupleSpaceBroker` when RDR-118 successor lands.

**Item 4: Define three delivery shapes** for producer → renderer:

| Shape | Host | Mechanism |
|---|---|---|
| **MCP UI resource** | Claude Code | Tool returns `{type: "resource", resource: {uri: "ui://nexus/surface/{id}", mimeType: "text/html"}}` |
| **Embedded artifact** | Any chat host | Tool returns HTML inline with a2ui payload as `<script type="application/json">` and the renderer pulled from `hellblazer.github.io/palinex/` |
| **External URL** | Any browser | Tool returns `https://hellblazer.github.io/palinex/?payload=<b64>` for shareable links |

Default delivery shape is determined by caller capability: MCP UI resource when the tool's host advertises support; embedded otherwise; URL as universal fallback.

**Item 5: Markdown sidecar always.** Every producer emits both. palinex's `Surface.to_markdown()` is the lossless reference; producers that emit ad-hoc surfaces must provide an equivalent. CI gate: round-trip from markdown → surface → markdown must equal the input on a representative corpus.

**Item 6: postMessage RPC for host-bridged actions.** Renderers do not call nexus MCP directly. When a chash needs resolving (or any other host-bridged operation), the renderer posts `{type: "a2ui.request", method, requestId, params}` to its parent window; the host (Claude Code, or a custom wrapper) calls the nexus MCP tool and posts back `{type: "a2ui.response", requestId, result|error}`. This is the protocol palinex's `host-bridge.html` implements as a reference.

**Item 7: Action allowlist for v1.** Three actions are first-class:
- `openUrl` — opens a URL in a new tab. Pure browser.
- `copyToClipboard` — pure browser.
- `openChash` — host-bridged. Renderer posts a request; host resolves via `mcp__plugin_nx_nexus__store_get_many` or chash resolver; host posts result back.

All other actions go through the generic `a2ui.request` path with `method` set to the action name. Trust gates (which actions a producer may emit, which the renderer dispatches) ride on the action registry, not the IR. v1 default registry is permissive for the three above; restrictive for everything else (logs but does not execute).

**Item 8: Pilot producer: `nx_answer` with `response_format` parameter.** Accepted values: `"markdown"` (default, unchanged behavior), `"surface"` (a2ui v0.9 + markdown sidecar), `"both"`. Surface variant constructs a List of citation Cards from the existing intermediates; the markdown sidecar matches today's output. Implementation depends on `palinex`.

**Item 9: Sub-agent surface emission, opt-in per subagent.** Reframed from RDR-124: subagents that opt in import `palinex.Surface`, build their findings as a surface, and return it via the SurfaceBroker (v1: in-process map) keyed by a `surface_id`. The Agent-tool return value carries a pointer (`surface_id`) plus the markdown summary. Orchestrator can fetch the surface (via the broker) when detail is needed. No new Agent-tool contract.

## Alternatives Considered

### Alt 1: Wait for RDR-120 substrate + RDR-118/119 successors

The previously-attempted path. Rejected because: substrate work has multi-phase scope and slippage is the norm in that arc (entire 110-113 chain was scrapped once). Producer-side and renderer-side work can ship today and pay off immediately; gating them on substrate is the failure mode that scrapped 118/119 in the first place.

### Alt 2: Invent a parallel SurfaceIR over a2ui

Initial sketch. Rejected because a2ui v0.9 *is* the IR — per-version structural backward-compat, schema validation, 18 Basic Catalog components. Inventing a wrapper layer would duplicate every concept and force translation at the wire.

### Alt 3: MCP UI resource as the only delivery shape

Tighter scope but locks v1 to Claude Code as host. Rejected because the markdown sidecar + external URL combination preserves the discipline that hosts without MCP UI resource support (terminal nexus CLI, custom web hosts, future Tauri shell) still get useful output.

### Alt 4: Adaptive Cards instead of a2ui

A real peer; Microsoft-maintained. Rejected per RDR-119 §Alternative 3: Adaptive Cards is *card-native* (single-card rendering); a2ui is *surface-native* (multi-component coordinated). Nexus producers emit surfaces.

### Briefly rejected

- **In-house DSL** — duplicates a2ui, no upstream support.
- **HTML strings as the IR** — loses structure, no validation, no per-host catalog story.
- **JSON Schema "Form"** standard — narrower scope than UI rendering.

## Trade-offs

### Consequences

- **(+)** Producer→renderer loop closes today, in Claude Code, with no substrate work.
- **(+)** palinex is a clean dependency boundary — versioned, tested, hosted on PyPI, OIDC-published.
- **(+)** Bolt points are minimal and named (Items 3, 4, 6, 7). Cockpit substrate when it ships replaces `InProcessBroker` and adds host renderers; everything above is unchanged.
- **(+)** Supersedes two tombstone-shaped RDRs (123, 124) without leaving them as scope debt.
- **(+)** Markdown sidecar discipline means we never ship a surface that some host can't display.
- **(−)** Two implementations to maintain — `palinex` (independent) and any nexus integration of it. Mitigated by palinex being a thin typed builder, not a framework.
- **(−)** External dependency on a2ui v0.9 spec. Spec evolves; per-version subdirs at upstream insulate, but we pin a version.
- **(−)** Action allowlist is producer-side discipline, not technically enforced. Trust gates land later when the action registry is real.

### Risks and Mitigations

- **Risk:** a2ui v0.9 → v0.10 introduces breaking changes in component shapes.
  **Mitigation:** palinex pins v0.9; v0.10 lands as a new palinex major when ready; nexus depends on a palinex range that maps to whichever a2ui versions we support.

- **Risk:** Claude Code MCP UI resource rendering changes upstream.
  **Mitigation:** delivery is one of three shapes; embedded artifact and external URL are mutually independent fallbacks.

- **Risk:** Sub-agent surface emission becomes a context-budget mirage (each surface is small, but accumulating many is worse than the original prose).
  **Mitigation:** in-process SurfaceBroker has session-scoped retention; orchestrator decides when to fetch detail.

- **Risk:** palinex bus-factor (single maintainer).
  **Mitigation:** Apache 2.0 + small surface (typed builders + one HTML file). Forkable.

### Failure modes

- *Visible:* surface fails to render (host doesn't support MCP UI resources) → markdown sidecar displays, user proceeds.
- *Visible:* host-bridge times out (10s default) → modal shows "host did not respond" with the expected protocol shape.
- *Silent:* producer emits markdown that doesn't match the surface (lossy transformation). CI gate (Item 5) catches this on a corpus.
- *Recovery:* renderer's debug pane shows the raw JSON + data model; surface_id + markdown sidecar always allow human reconstruction.

## Implementation Plan

### Prerequisites

- [x] palinex 0.0.1 on PyPI (shipped 2026-05-22)
- [x] palinex renderer hosted at hellblazer.github.io/palinex (shipped 2026-05-22)
- [x] a2ui v0.9 spec indexed in T3 knowledge
- [ ] RDR-127 accepted (gate pending)

### Phase 1: Schema-level decision + supersession bookkeeping

Item 1, Item 2.

- Land RDR-127.
- Mark RDR-123 and RDR-124 status `superseded`, add tombstone notes pointing at RDR-127.
- Add T2 memory entry `nexus_rdr/RDR-127` (the rdr-create skill handles this automatically).

### Phase 2: SurfaceBroker port + InProcessBroker impl

Items 3, 4 (partial), 5.

- Add `nexus/surfaces/broker.py` with the `SurfaceBroker` Protocol, `Cell` dataclass (mirrors RDR-118 §1 shape), and `InProcessBroker` impl (~150 LOC).
- Add `palinex` as a nexus runtime dep.
- Add `nexus/surfaces/__init__.py` re-exporting `palinex.Surface`, `DataPath`, and the broker.

### Phase 3: First producer — `nx_answer response_format=surface`

Items 4 (full), 5, 8.

- Add `response_format: Literal["markdown", "surface", "both"]` parameter to `nx_answer`.
- Surface construction: existing intermediates → palinex builders → a2ui v0.9 envelope.
- Round-trip CI gate on a 10-question test corpus.

### Phase 4: MCP UI resource delivery

Item 4 (MCP shape), Item 6 (host-bridge protocol).

- nexus MCP tools that emit surfaces wrap the rendered HTML as `ui://nexus/surface/{id}` resource.
- Host-bridge logic lives in the MCP tool's response wrapper: it includes the renderer iframe + the postMessage listener that calls back into nexus tools.
- The reference `host-bridge.html` in palinex is the protocol spec.

### Phase 5: Subagent surface emission

Item 9.

- One pilot subagent (codebase-deep-analyzer is the highest-token candidate) opts in.
- Emits surface to in-process broker, returns pointer + markdown summary in Agent-tool result.
- Measure orchestrator token-per-dispatch delta.

### Day 2 Operations

- palinex updates land as new PyPI releases (independent repo, OIDC-published).
- nexus tracks palinex semver range.
- Action allowlist additions go through RDR-create.
- Sub-agent opt-ins are per-subagent decisions.

### New dependencies

- `palinex >= 0.0.1` (PyPI, Apache-2.0)
- Optional: `palinex[validate]` (adds jsonschema) for producer-side deep validation in CI

## Test Plan

- **Scenario:** `nx_answer` with `response_format="surface"` returns a v0.9-valid envelope.
  **Verify:** payload validates structurally; markdown sidecar contains all chash citations from the surface.

- **Scenario:** Markdown ↔ surface round-trip on a 10-question corpus.
  **Verify:** `to_markdown(surface_from(markdown))` equals input on a curated representative set.

- **Scenario:** MCP UI resource delivery in Claude Code.
  **Verify:** rendered surface displays, openChash button triggers host bridge, response renders inline.

- **Scenario:** Sub-agent emits surface; orchestrator return-value is pointer + summary.
  **Verify:** Agent-tool return tokens reduced ≥40% vs prose baseline (target from former RDR-124).

- **Scenario:** Producer emits a non-allowlist action.
  **Verify:** renderer logs but does not execute; host-bridge receives the `a2ui.request` but the action registry returns `error: not_in_allowlist`.

- **Scenario:** Renderer in standalone mode (no host bridge, no daemon).
  **Verify:** openChash shows the documented "needs host or sidecar" message, not a dead-end prompt.

## Validation

### Testing strategy

The reference renderer + producer ship with their own test suites (palinex: 19 tests, palinex CI matrix on Python 3.10-3.13). Nexus-side integration tests live with the producer (`tests/test_nx_answer_surface.py`) and the broker (`tests/test_surface_broker.py`).

### Performance expectations

- Surface construction (palinex Python builders): <5ms for typical `nx_answer` shape.
- Surface → MCP UI resource wrapping: <10ms.
- Renderer cold load (incl. lit-html CDN fetch): <500ms on a warm CDN.
- Markdown sidecar generation: <5ms (tree walk, no I/O).
- Host-bridge round-trip for openChash: <100ms when daemon is in-process MCP, <250ms via fetch.

## Finalization Gate

(deferred — sketch only)

### Contradiction check

(deferred)

### Assumption verification

- [ ] **A1** — Claude Code MCP UI resource support is stable for the rendering paths we use. **Method:** smoke test in Phase 4.
- [ ] **A2** — palinex's structural validation catches the producer-side errors that matter (no false negatives on a corpus of broken producers). **Method:** mutation-test the demo payload.
- [ ] **A3** — Sub-agent return-value token reduction is real (≥40% target). **Method:** measure on codebase-deep-analyzer dispatches.
- [ ] **A4** — `SurfaceBroker` Protocol is forward-compatible with a tuple-space implementation (no missing operations, no semantic mismatch). **Method:** paper-design the `TupleSpaceBroker` against RDR-118 §1 cell shape.

### Scope verification

(deferred — this RDR explicitly does not implement the cockpit substrate; that's RDR-120 + successors)

### Cross-cutting concerns

- **Versioning:** a2ui external (v0.9 stable, v0.10 draft); palinex follows semver; nexus pins palinex range.
- **Build tool compatibility:** palinex builds via hatchling; nexus integration is plain Python imports.
- **Licensing:** palinex Apache-2.0, compatible with nexus AGPLv3+.
- **Deployment model:** palinex package vendored via PyPI; renderer hosted on GitHub Pages; nexus serves MCP UI resources from its tools.
- **IDE compatibility:** VS Code webview can host the renderer; tested via host-bridge.html.
- **Incremental adoption:** opt-in per producer; existing markdown-returning tools unchanged.
- **Secret/credential lifecycle:** localStorage for daemon URL (sidecar mode); never in URL params or HTML inline.
- **Memory management:** in-process broker has session-scoped TTL; surfaces are immutable on emit.

### Proportionality

Small RDR. The IR is a2ui (external, adopted). The renderer is one HTML file (shipped). The producer is one PyPI package (shipped). This RDR's deliverable is the *decision* to adopt them, plus the SurfaceBroker port and the pilot producer. Resist the urge to grow this into the cockpit RDR.

## References

- a2ui v0.9 specification — https://a2ui.org/specification/v0.9-a2ui/
- palinex — https://github.com/Hellblazer/palinex, https://pypi.org/project/palinex/
- palinex renderer — https://hellblazer.github.io/palinex/
- T3 entries:
  - `simonw-2025-12-10-html-tools-patterns` — Willison HTML tool patterns
  - `surface-renderer-html-tool-patterns-for-nexus` — nexus-side application of those patterns
  - `architecture-a2ui-overview` — a2ui repo deep analysis (2026-05-19)
  - `a2ui-design-philosophy-stack-positioning` — a2ui vs MCP UI vs AG-UI
- RDR-053: Xanadu Fidelity (chash content-addressed spans — informs Item 7 openChash)
- RDR-118: Surfaces as Tuples (scrapped) — Cell shape adopted verbatim as the IR shape
- RDR-119: Cockpit UI Fabric (scrapped) — per-host catalog discipline informs eventual bolt
- RDR-120: Storage Substrate Split — eventual substrate that will replace `InProcessBroker`
- RDR-122: LLM-JSON Repair Pass — orthogonal, still valid
- RDR-123, RDR-124: superseded by this RDR
- Conversation 2026-05-20 through 2026-05-22 — design discussion that converged on this shape

## Revision History

_2026-05-22 — initial sketch. Direct successor to scrapped RDR-118/119 work; explicit supersession of merged-but-stale RDR-123/124. Reference implementation already shipped as palinex 0.0.1 before the RDR was authored — the RDR codifies a working architecture rather than proposing one._
