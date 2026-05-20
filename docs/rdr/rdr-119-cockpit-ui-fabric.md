---
title: "Cockpit UI Fabric — Bakke Auto-Layout over A2UI Catalogs, per-Host Realization"
id: RDR-119
type: Architecture
status: scrapped
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-18
accepted_date:
scrapped_date: 2026-05-19
scrap_reason: "Draft never gated. UI fabric is a third-order consumer (sits atop RDR-118 surfaces-as-tuples which sits atop RDR-111 ORB which sits atop RDR-112 substrate). The whole stack is deferred until substrate work (RDR-120) ships. Postmortem: docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md."
related_issues: []
related_rdrs: [RDR-110, RDR-111, RDR-112, RDR-113, RDR-053, RDR-118, RDR-120]
related_external: [a2ui-v0.8-spec, a2ui-v0.9-draft, lumino, notcurses, tauri]
reactivates_beads: [nexus-kkh2, nexus-0rws]
related_tests: []
---

> **TOMBSTONE 2026-05-19.** This RDR is preserved as historical reference. Never gated; built atop a scrapped multi-layer consumer stack. See frontmatter `scrap_reason` and the [postmortem](../postmortem/2026-05-16-rdr110-113-remediation-chain.md). Active substrate work: [RDR-120](rdr-120-storage-substrate-split.md). Do not implement against this design.

---


# RDR-119: Cockpit UI Fabric

> Sketch. Companion to RDR-118. Defines the **per-host realization** of
> A2UI catalogs that RDR-118 adopts, the policy layer that drives them
> (Bakke auto-layout already shipped in RDR-111), and the cross-platform
> shell (Tauri 2).

## Problem Statement

RDR-118 adopts A2UI as the surface descriptor format and inherits the
RDR-110/111 substrate. What's still open is the **per-host fabric** —
the concrete realization that takes A2UI surface descriptors and renders
them as a usable, accessible, keyboard-navigable cockpit on each
medium.

The cockpit substrate (RDR-111) shipped a vertical-stack Bakke engine and
three reference panels rendered as tmux output. Phase 4 explicitly
deferred the *fulfillment adapters* that consume connection manifests
and render A2UI surfaces on real hosts:

- **`nexus-kkh2`** (deferred, P4.1) — tmux pane fulfillment adapter
- **`nexus-0rws`** (deferred, P4.2) — ext-apps iframe fulfillment adapter

RDR-119 is the reactivation of both. It also addresses the
*UI fabric* concerns RDR-118 names but doesn't settle: focus, navigation,
layout containers, modal stack, accessibility, keyboard discipline.

### Enumerated gaps to close

#### Gap 1: No per-host A2UI catalog implementations

A2UI ships as a protocol. Each host needs a concrete catalog
(`nexus.lumino.v1` for web/Tauri, `nexus.notcurses.v1` for tty) that
maps A2UI components to native rendering primitives. Without these, no
cell renders.

#### Gap 2: UI fabric concerns are unsettled

Tab order, focus traversal, modal stack, layout containers, accessibility
relationships, keyboard discipline, cross-surface navigation gestures —
all unaddressed by RDR-111 / RDR-118 alone. The cockpit's organizing
principle is Bakke auto-layout (already shipped), but the *fabric* that
gives it ergonomics is open.

#### Gap 3: No cross-platform shell decision

RDR-118 names Tauri as the recommended shell. RDR-119 commits the
decision and addresses the consequences (Tauri 2 supports web + mobile +
desktop; native webview per platform).

#### Gap 4: First end-to-end mission profile is dormant

RDR-111 `nexus-kgo4` (first mission profile) was left consumer-driven.
RDR-118 names "RDR audit cockpit" as the MVP profile. RDR-119 builds it
across both backends.

## Context

### What the source already nails (do not re-derive)

- **Layout policy**: Bakke auto-layout engine (RDR-111
  `src/nexus/cockpit/layout.py:79`). Vertical stack + demotion cascade.
  Three phases: Measure → Auto-Style → Layout. 250ms debounce.
- **Placement-plan transport**: `layout_state/<profile>` subspace
  (RDR-111).
- **Surface descriptor format**: A2UI v0.8 (Stable). Per RDR-118.
- **Cell substrate**: `surface_cell/<surface_id>` subspace (per RDR-118).
- **Trust boundary**: RDR-113 daemon-level + per-cell `origin` label.
- **Ordered retrieval**: direct T2 SQL via RDR-112 daemon RPC.
- **Reference panels**: active-claims, recent-events, active-bindings
  (shipped in RDR-111).

### What we adopt (zero new framework code)

- **Lumino** (formerly Phosphor) — JupyterLab's TypeScript widget
  framework. `DockPanel`, `MenuBar`, `CommandRegistry`, `FocusTracker`,
  token-based service plugins. *Battle-tested* recursive composable web
  fabric.
- **notcurses** — z-ordered plane API for tty. Already cited in RDR-118.
- **Tauri 2** — Rust + native webview shell. ~10–20× smaller than
  Electron; supports web + iOS + Android + desktop.
- **A2UI catalog negotiation through MCP `initialize`** — per A2UI v0.9
  transport-level mode. Per RDR-118.

### The framing tension and its resolution

`agentic-cockpit.md` line 791 explicitly states:

> Not building, intentionally: A new component / widget framework.

Adopting Lumino is not building a framework; it is **choosing** an
existing one to fill the GUI adapter slot the source deferred. Two
honest framings, both correct:

(a) Lumino is *the GUI fulfillment adapter* RDR-111 deferred (`nexus-0rws`).
(b) Lumino is *the harness's rendering substrate* for GUI hosts, same role
    tmux/notcurses fills for terminal hosts.

We pick (b) as the official framing.

## Research Findings

### Investigation

Audit of `agentic-cockpit.md` (§What we build vs leverage, lines 739-797;
§Surfaces, lines 593-738), RDR-111 (§Auto-layout engine, post-mortem),
A2UI v0.8/v0.9 specs (per RDR-118 ingestion), survey of Lumino /
notcurses / Tauri / Adaptive Cards / Croquet / tldraw (per
brainstorm-research-synthesis 2026-05-17).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
|---|---|---|
| Lumino architecture | Yes (lumino.readthedocs.io, jupyterlab repo) | DockPanel, MenuBar, CommandRegistry, FocusTracker, token-based service plugins; production-grade across JupyterLab ecosystem |
| notcurses planes | Yes (github.com/dankamongmen/notcurses) | z-ordered rectangles, EGC-aware cells, OSC 4 palette query, multimedia support |
| Tauri 2 | Yes (PkgPulse 2026 comparison) | Native webview per platform; ~10–20× smaller than Electron; supports web + mobile + desktop; Rust core |
| Bakke shipped engine | Yes (src/nexus/cockpit/layout.py + post-mortem) | Vertical-stack + demotion cascade only at v1; simpler than spec described |
| Phase 4 deferred beads | Yes (RDR-111 post-mortem) | nexus-kkh2 (tmux), nexus-0rws (iframe) — both dormant under closed epic, explicit reactivation triggers |
| A2UI catalog model | Yes (per RDR-118 ingestion) | Pre-approved catalogs; orchestrator validates identity; framework-agnostic |

### Key Discoveries

- **Verified (RDR-111 shipped)**: Bakke engine is vertical-stack-only
  in v1. RDR-119 inherits this; extension to multi-column / dock-tree
  is future work.
- **Verified (A2UI)**: per-host catalogs (`nexus.lumino.v1`,
  `nexus.notcurses.v1`) are the established A2UI extension pattern.
- **Verified (Lumino architecture)**: DockPanel + CommandRegistry +
  FocusTracker collectively cover focus/nav/modal/accessibility
  concerns we'd been worried about — Lumino solves these natively for
  the web side.
- **Documented (Tauri 2)**: mobile support (iOS, Android) ships in
  Tauri 2; web/desktop/mobile via the same Lumino fabric.
- **Assumed**: notcurses input + plane APIs cover focus/nav for the
  tty side with comparable ergonomics to Lumino. Spike needed.

### Critical Assumptions

- [ ] **A1** — Lumino's DockPanel and CommandRegistry compose under
  A2UI catalog rendering without architectural conflict. **Status**:
  Unverified. **Method**: Spike — render a `nexus.lumino.v1` catalog
  example with three components in a DockPanel.
- [ ] **A2** — notcurses planes + input handling give comparable
  focus/nav ergonomics to Lumino on tty. **Status**: Unverified.
  **Method**: Spike — render the same three-component example via
  `nexus.notcurses.v1`; cross-test keyboard discipline.
- [ ] **A3** — Bakke vertical-stack output maps cleanly onto Lumino
  DockPanel rows/columns. **Status**: Unverified. **Method**: Spike —
  drive Lumino layout from a `layout_state` tuple stream.
- [ ] **A4** — Tauri 2 native webview hosts Lumino reliably across
  macOS WebKit, Windows WebView2, Linux WebKitGTK. **Status**:
  Unverified. **Method**: Spike — Tauri app shells with Lumino DockPanel
  + 10-tab smoke.
- [ ] **A5** — Per-host catalogs can advertise capabilities via MCP
  `initialize` per A2UI v0.9 transport-level mode without forking the
  protocol. **Status**: Carries from RDR-118 A1. Same spike.
- [ ] **A6** — The "RDR audit cockpit" MVP mission profile (RDR-118
  §MVP) exercises all four fabric concerns (focus, nav, container,
  modal) sufficiently to validate the design. **Status**: Unverified.
  **Method**: Build it; measure.
- [ ] **A7** — Mission-context profiles couple to permission modes
  (WoW combat-lockdown analog) explicitly, not implicitly. **Status**:
  Unverified. **Method**: Paper design + RDR-111 author confirm.
- [ ] **A8** — Per-cell Yjs as opt-in cell type (for editable surfaces)
  composes with A2UI catalog rendering. **Status**: Unverified.
  **Method**: Defer until a surface explicitly needs it.

## Proposed Solution

### Approach

Bakke auto-layout is the policy layer (already shipped). Lumino and
notcurses are passive rendering primitives that consume placement plans
(`layout_state` tuples). Tauri 2 hosts Lumino. Each host publishes an
A2UI catalog (`nexus.lumino.v1`, `nexus.notcurses.v1`) that maps A2UI
components to native primitives. UI fabric concerns (focus, nav, modal,
accessibility) are owned by the rendering primitive — Lumino on web,
notcurses on tty.

### Technical Design

#### 1. Three-layer fabric

```
┌──────────────────────────────────────────────────────────┐
│ Policy:  Bakke auto-layout (RDR-111, shipped)             │
│          Measure → Auto-Style → Layout, demotion cascade  │
│          Writes: layout_state/<profile> subspace          │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ (subscribers read)
┌──────────────────────────────────────────────────────────┐
│ Substrate:  RDR-110 tuple space subspaces (RDR-118)       │
│             surface_cell/<id>, cell_layout, cell_input,   │
│             cell_focus, layout_state                      │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ (renderers consume)
┌──────────────────────────────────────────────────────────┐
│ Primitives:  Lumino (web/Tauri) | notcurses (tty)         │
│              A2UI catalog: nexus.lumino.v1 |              │
│              nexus.notcurses.v1                           │
│              Owns: focus, nav, container, modal, a11y     │
└──────────────────────────────────────────────────────────┘
```

#### 2. `nexus.lumino.v1` A2UI catalog

Maps standard A2UI components to Lumino widgets:

| A2UI component | Lumino widget |
|---|---|
| `Column` / `Row` | `BoxPanel` (vertical/horizontal) |
| `Card` | `Widget` with title bar |
| `Text` | DOM `<div>` with text content |
| `Image` | DOM `<img>` |
| `List` | `Widget` with template-driven children |
| `Button` | DOM `<button>` with `userAction` dispatch |
| `Container.children.template` | `Widget` rebuilt on data binding |
| `Modal` (v0.9) | `Dialog` widget with focus trap |
| `Slider` (v0.9) | DOM input range |
| Nexus extension: `NestedSurface` | Child `DockPanel` bound to subspace |

Catalog id: `nexus.lumino.v1` (semver). Hosted at
`https://nexus.dev/a2ui/catalogs/lumino-v1.json` (eventually).

**Layout containers map onto Lumino DockPanel**: each named slot in a
panel is a Lumino dock area. The Bakke engine writes
`layout_state/<profile>` tuples; the Lumino adapter reads them and calls
`DockPanel.addWidget` / `moveWidget` minimally.

**Focus, keyboard discipline, modal stack** are owned by Lumino's
`CommandRegistry` + `FocusTracker`. The A2UI Button's `userAction` field
provides the action handler; Lumino dispatches it via tuple `out` to
`cell_input/<surface_id>`.

#### 3. `nexus.notcurses.v1` A2UI catalog

Symmetric for tty:

| A2UI component | notcurses primitive |
|---|---|
| `Column` / `Row` | Stacked / side-by-side planes |
| `Card` | Plane with border |
| `Text` | Plane with text |
| `Image` | Plane with half-block / sextant pixel rendering |
| `List` | Plane with row iteration |
| `Button` | Highlighted plane with mouse/key bind |
| `Container.children.template` | Plane rebuilt per data binding |
| `Modal` (v0.9) | Top z-order plane with focus trap |
| Nexus extension: `NestedSurface` | Nested plane bound to child subspace |

Catalog id: `nexus.notcurses.v1`. Same advertisement path via MCP
`initialize`.

#### 4. Tauri 2 shell

The web/desktop/mobile host. Tauri 2 wraps the platform's native webview
(WebKit on macOS/iOS, WebView2 on Windows, WebKitGTK on Linux,
WebView on Android). Lumino runs inside the webview.

**Build target**: a small Tauri app that:
1. Connects to the local nexus daemon (RDR-112) via MCP.
2. Performs A2UI catalog negotiation on `initialize`.
3. Hosts a Lumino `DockPanel` as the cockpit root.
4. Subscribes to `layout_state/<profile>` and `surface_cell/*` subspaces.
5. Renders A2UI components per `nexus.lumino.v1` mappings.
6. Posts `userAction` events back via `cell_input/<surface_id>`.

#### 5. Catalog negotiation via MCP `initialize`

Per A2UI v0.9 transport-level mode. The MCP server initialization
response includes:

```json
{
  "a2uiClientCapabilities": {
    "supportedCatalogIds": [
      "https://nexus.dev/a2ui/catalogs/lumino-v1.json",
      "https://nexus.dev/a2ui/catalogs/notcurses-v1.json",
      "https://a2ui.org/specification/v0_8/standard_catalog_definition.json"
    ],
    "acceptsInlineCatalogs": false
  }
}
```

Producers query supported catalogs through the existing MCP capability
discovery. No new transport.

#### 6. UI fabric concerns ownership

| Concern | Owned by | Notes |
|---|---|---|
| Tab order / focus traversal | Lumino `FocusTracker` (web) / notcurses input (tty) | DFS default; override via cell's `focus_priority` registry field. |
| Keyboard shortcuts | Lumino `CommandRegistry` (web) / notcurses keybind (tty) | A2UI Button `userAction` is the dispatch target. |
| Modal stack | Lumino `Dialog` widget (web) / top z-plane (tty) | Bakke `surface_level=full-screen` with `preempts=[*]`. |
| Layout containers | Lumino `DockPanel/BoxPanel` (web) / notcurses planes (tty) | Driven by Bakke `layout_state` tuples. |
| Accessibility (ARIA/labels) | Lumino's DOM (web) / notcurses' tags (tty) | A2UI components carry semantic role; renderer translates. |
| Cross-surface navigation | A2UI `userAction` → binding fires `surface-swap` action | Action types: `surface-swap`, `drill-in`, `surface-back`. |
| Drag-and-drop | Lumino DockPanel built-in (web) / N/A (tty) | Manual pin via dragging; auto-layout respects. |
| Pointer / mouse | Lumino DOM events (web) / notcurses mouse (tty) | Translated to `cell_input` tuples. |

#### 7. Mission-context × permission-mode coupling

A mission-context profile is the tuple `mission_context/<name>` carrying:

```yaml
mission_context:
  dimensions:
    - profile_name
    - permission_mode  # plan | acceptEdits | auto | default | bypassPermissions
  content_fields:
    - active_bindings    # list of binding ids
    - active_catalogs    # list of A2UI catalog ids
    - active_layout      # layout_state profile id
    - recent_events_filter
```

Switching mission contexts is `out`ting a new `current_mission_context`
tuple; subscribed surfaces react (per RDR-111). Permission mode is part
of the bundle; the harness honors it (WoW combat-lockdown analog).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Bakke auto-layout policy | RDR-111 `src/nexus/cockpit/layout.py` | **Reuse entire** |
| Placement-plan transport | RDR-111 `layout_state/<profile>` subspace | **Reuse entire** |
| Cell substrate | RDR-118 `surface_cell` + cell_layout/input/focus subspaces | **Reuse entire** |
| Reference panels (claims, events, bindings) | RDR-111 panels | **Reuse**; rewrap as `nexus.lumino.v1` / `nexus.notcurses.v1` catalog components |
| Lumino | external (Apache-2.0 / BSD) | **Adopt whole** |
| notcurses | external (Apache-2.0) | **Adopt whole** |
| Tauri 2 | external (Apache-2.0 / MIT) | **Adopt whole** |
| A2UI v0.8 protocol | external (Apache-2.0) | **Adopt entire** |
| `nexus.lumino.v1` catalog | none | **New** (this RDR) |
| `nexus.notcurses.v1` catalog | none | **New** (this RDR) |
| Tauri shell binary | none | **New** (this RDR) |
| MCP `initialize` catalog advertisement | RDR-110/112 MCP path | **Extend** — add `a2uiClientCapabilities` field |
| `mission_context` subspace | none | **New** (this RDR) |
| Per-cell Yjs | external (MIT) | **Deferred** — adopt selectively per cell type that needs it |

### Decision Rationale

Three forces:

1. **The substrate is shipped.** RDR-110/111 + RDR-118 cover everything
   below the per-host realization. Building anything else would
   duplicate.
2. **Lumino + notcurses cover the UI fabric.** Focus/nav/modal/a11y
   are decades-old problems with mature solutions. Adopt.
3. **A2UI's catalog model is the integration seam.** Per-host catalogs
   are A2UI's *intended extension pattern* — we extend along the
   protocol's grain, not against it.

## Alternatives Considered

### Alternative 1: Build a bespoke widget framework (rejected)

`agentic-cockpit.md` line 791 explicitly says "Not building." Reasons
remain valid. Lumino + notcurses are the better answer.

### Alternative 2: Electron instead of Tauri

**Pros**: most mature, JS-everywhere ecosystem, identical Chromium
across platforms.
**Cons**: 10–20× larger bundle, higher RAM, larger attack surface, no
native mobile.
**Reason for deferral**: Tauri 2 wins for nexus's profile (developer
tool, security-sensitive substrate, eventual mobile). Reconsider only
if Tauri's per-platform webview drift bites.

### Alternative 3: Adaptive Cards as the catalog format

A real option — Adaptive Cards is a peer to A2UI for semantic
component-intent rendering. **Pros**: even more mature, Microsoft-
maintained, large renderer ecosystem.
**Cons**: not surface-shaped (single-card rendering), not
multi-component-coordinated.
**Reason for rejection**: A2UI is *surface-native*; Adaptive Cards is
*card-native*. The cockpit needs surfaces.

### Alternative 4: Croquet OS for fabric collab

Bit-identical shared VM with reflector. **Pros**: zero merge logic,
deterministic.
**Cons**: paradigm shift (no side effects in Model code), overlaps
RDR-110 at layer 1, narrower ecosystem than Yjs.
**Reason for rejection**: covered in research synthesis 2026-05-17.
Tuple space already does coarse-grained collab; Yjs adopted per cell
type that needs fine-grained collab.

### Briefly Rejected

- **Pure DOM (no Lumino)**: re-invents DockPanel, FocusTracker,
  CommandRegistry. Not worth the cost.
- **Web Components only, no Lumino**: viable but no DockPanel
  equivalent matches Lumino's ergonomics; same rejection reason.
- **Visual binding-authoring UX**: explicitly rejected by RDR-111
  (Alternative D). Form-based + LLM-assisted only.

## Trade-offs

### Consequences

- (+) v1 build budget is small — per-host catalog implementations only.
- (+) Lumino's ecosystem (extensions, themes, layouts) becomes
  available essentially for free.
- (+) Tauri 2 gives web + desktop + mobile from one codebase.
- (+) A2UI catalog negotiation is the existing protocol mechanism;
  no new vocabulary.
- (+) Reactivates two deferred beads with clear MVP gating.
- (−) Lumino is a non-trivial dependency to vendor; upstream churn
  becomes a concern.
- (−) Tauri 2's native webview drift across platforms is a real risk.
- (−) Bakke's vertical-stack-only v1 limits initial layouts; multi-
  column / nested-dock is future work.
- (−) notcurses + Lumino catalogs must stay semantically aligned;
  drift is the failure mode.

### Risks and Mitigations

- **Risk**: per-platform Tauri webview drift breaks Lumino rendering.
  **Mitigation**: cross-platform smoke suite at every release; pin
  Tauri 2.x.

- **Risk**: notcurses + Lumino catalogs drift in semantics.
  **Mitigation**: shared catalog spec doc; conformance tests run on
  every catalog change.

- **Risk**: A2UI v0.8 → v0.9 migration disrupts catalog format.
  **Mitigation**: v0.8 schema is stable; v0.9 is largely a transport-
  level change. Per-host catalogs versioned (`v1` first).

- **Risk**: Lumino vendor lock-in.
  **Mitigation**: catalog mapping is one of N possible web realizations
  of A2UI; alternative renderers (Lit, React) can replace Lumino if
  needed.

- **Risk**: Bakke vertical-stack-only is too limited for real
  cockpits.
  **Mitigation**: extend to multi-column / dock-tree under a separate
  RDR; v1 ships with the shipped engine.

### Failure Modes

- *Visible*: cell renders wrong widget (catalog mapping bug);
  layout doesn't fit on screen (Bakke demotion cascade).
- *Silent*: focus desync between Lumino and notcurses backends; modal
  trap leaks; mission-context switch doesn't persist.
- *Recovery*: portal-inspector cell (from RDR-118) shows the catalog
  id, the requested A2UI component, the mapped Lumino/notcurses
  widget, and the focus path. Diagnosis is one click.

## Implementation Plan

### Prerequisites

- [ ] RDR-118 schemas landed (`surface_cell`, `cell_layout`,
      `cell_input`, `cell_focus`).
- [ ] `chash://` BoundValue resolver shipped per RDR-118 Phase 1 Step 2.
- [ ] A2UI v0.8 reference implementation chosen for each host
      (`@a2ui/lit` for Lumino web, custom for notcurses).
- [ ] A1–A8 verified via spikes.

### Minimum Viable Validation

**MVP mission profile: RDR audit cockpit** (shared with RDR-118).

Three-level recursive surface, rendered on both backends:

1. Outer surface: list of all RDRs (Lumino `DockPanel` rows / notcurses
   stacked planes).
2. Each RDR is a sub-surface with problem / research / design / gate
   sub-cells.
3. Each sub-cell can transclude (`chash://`) into another RDR or the
   catalog.

Validates: A2UI catalog negotiation, Bakke layout, focus/nav,
recursion, transclusion, cross-host rendering. Reactivates `nexus-kgo4`.

### Phase 1: `nexus.notcurses.v1` catalog

Reactivates `nexus-kkh2` (tmux pane fulfillment adapter).

#### Step 1: Define catalog mapping

Author `nexus.notcurses.v1.json` mapping A2UI standard catalog
components to notcurses primitives. Reuse RDR-111's tmux adapter as
starting code; rebuild around notcurses planes per A2UI catalog.

#### Step 2: notcurses fulfillment adapter

In `src/nexus/cockpit/notcurses_adapter.py`:
- Subscribe to `surface_cell/*` + `cell_layout/*` + `layout_state/*`.
- Resolve each cell's `representation_type` → A2UI component →
  notcurses primitive per catalog mapping.
- Translate notcurses input events → `cell_input` tuples.
- Honor Bakke demotion cascade.

Estimated ~600 LoC (3× RDR-111's estimate per shipped-code calibration).

#### Step 3: Smoke test against RDR-111 reference panels

The three reference panels (active-claims, recent-events,
active-bindings) must render via the new catalog with feature parity.

### Phase 2: `nexus.lumino.v1` catalog + Tauri shell

Reactivates `nexus-0rws` (ext-apps iframe fulfillment adapter).

#### Step 4: Define catalog mapping

Author `nexus.lumino.v1.json` mapping A2UI components to Lumino
widgets. Use `@lumino/widgets` + `@lumino/commands` packages.

#### Step 5: Tauri shell scaffold

In `crates/nexus-cockpit-tauri/` (new):
- Tauri 2 app with Rust main.
- Embeds `dist/lumino-app/` (TypeScript Lumino frontend).
- Connects to local nexus daemon (RDR-112) via MCP over UDS.
- Performs A2UI catalog negotiation on `initialize`.

Estimated ~1000 LoC Rust + ~1500 LoC TypeScript.

#### Step 6: Lumino fulfillment adapter

In `dist/lumino-app/src/cockpit-adapter.ts`:
- Same shape as notcurses adapter but for Lumino DockPanel.
- Subscribes to subspaces via daemon WebSocket or MCP.
- Renders A2UI components → Lumino widgets per catalog.
- Translates Lumino events → `cell_input` tuples.

### Phase 3: MVP mission profile

Reactivates `nexus-kgo4`.

#### Step 7: RDR audit cockpit bindings

Author the bindings that produce the RDR audit surfaces. Use existing
RDR-111 bindings primitive.

#### Step 8: Cross-host parity tests

Same `surface_cell` tuples must render meaningfully on both backends.
Differences are in medium-specific catalog component mappings, never
in cell content or composition.

#### Step 9: Measure and iterate

Per RDR-111's discipline: "Run it. Measure what's awkward. Iterate."

### Day 2 Operations

- Per-host catalog JSON files versioned (`v1`, `v2` ...) per A2UI
  catalog conventions.
- Tauri shell auto-update via Tauri's built-in updater.
- notcurses adapter ships as part of `nx` CLI.
- MCP `initialize` advertises catalogs; producers cache.

### New Dependencies

- `@lumino/widgets`, `@lumino/commands`, `@lumino/coreutils` (web)
- `notcurses` (Apache-2.0; C with Python bindings)
- `tauri@2.x` (Apache-2.0 / MIT; Rust)
- `@a2ui/lit` or comparable A2UI renderer (Apache-2.0)

## Test Plan

- **Scenario**: Tauri shell launches, advertises catalogs via
  `initialize`.
  **Verify**: MCP response includes `a2uiClientCapabilities` with
  `nexus.lumino.v1`.
- **Scenario**: Three-level recursive surface renders on Lumino.
  **Verify**: nested DockPanels with correct focus path.
- **Scenario**: Same three-level surface renders on notcurses.
  **Verify**: nested planes; equivalent focus path; cross-host
  semantic parity.
- **Scenario**: A2UI Button `userAction` dispatches.
  **Verify**: Lumino click → `cell_input` tuple; binding fires.
- **Scenario**: Bakke demotion cascade under narrow terminal.
  **Verify**: panel → status-line → notification → suppressed as width
  shrinks.
- **Scenario**: Mission-context switch updates layout + permission
  mode.
  **Verify**: new `current_mission_context` tuple; surfaces re-render;
  harness permission mode changes.
- **Scenario**: `chash://` transclusion resolves on both backends.
  **Verify**: same chunk text rendered.
- **Scenario**: Modal stack works (RDR audit gate dialog).
  **Verify**: focus trap on Lumino `Dialog`; equivalent on notcurses
  top-z plane.

## Validation

### Testing Strategy

1. **Scenario**: RDR audit cockpit MVP runs end-to-end on both
   backends.
   **Expected**: full mission profile completes; reviewer can navigate,
   inspect, transclude, and act on RDRs without medium-specific
   awareness.

### Performance Expectations

- Tauri shell cold start: < 1 s on M-series Mac.
- Lumino render of 50-cell surface: < 100 ms.
- notcurses render of 50-cell surface: < 50 ms.
- A2UI catalog negotiation: < 10 ms (cached after first session).
- `chash://` resolve cache hit: < 1 ms.

## Finalization Gate

(deferred — sketch only)

### Contradiction Check

(deferred)

### Assumption Verification

(deferred — A1–A8 unverified; spikes required)

### Scope Verification

(deferred — RDR audit cockpit is MVP; multi-column Bakke, mobile
Tauri, per-cell Yjs are post-MVP)

### Cross-Cutting Concerns

- **Versioning**: A2UI external; Lumino external; per-host catalogs
  versioned (`v1`). Tauri shell follows semver.
- **Build tool compatibility**: TypeScript via npm/pnpm; Rust via
  cargo; Python (notcurses adapter) via existing nexus build.
- **Licensing**: Lumino (BSD-3), notcurses (Apache-2.0), Tauri (MIT/
  Apache-2.0), A2UI (Apache-2.0). All compatible.
- **Deployment model**: Tauri shell distributed as native binaries per
  platform; notcurses adapter ships with `nx` CLI.
- **IDE compatibility**: VS Code webview hosts the Lumino frontend
  without modification; nested-webview prohibition means VS Code is a
  single-cell host (explicit limitation).
- **Incremental adoption**: legacy RDR-111 surfaces (rendered via
  the shipped tmux output path) continue to work; migration to
  `nexus.notcurses.v1` is opt-in per binding.
- **Secret/credential lifecycle**: Tauri shell connects to daemon via
  UDS (RDR-112); no credentials in the webview.
- **Memory management**: Lumino widget dispose on cell `take`;
  notcurses plane teardown likewise.

### Proportionality

Right-sized. The substrate is RDR-110/111/118; A2UI is the protocol;
Lumino/notcurses/Tauri are external. This RDR ships ~3000 LoC of
adapter + shell code total — large but bounded.

## References

- A2UI v0.8 spec — https://a2ui.org/specification/v0.8-a2ui/
- A2UI v0.9 draft — https://a2ui.org/specification/v0.9-a2ui/
- Lumino — https://github.com/jupyterlab/lumino
- notcurses — https://github.com/dankamongmen/notcurses
- Tauri 2 — https://tauri.app/
- RDR-111: ORB Cockpit Substrate (especially §Auto-layout engine)
- RDR-118: Surfaces as Tuples (cell substrate this RDR realizes)
- `docs/agentic-cockpit.md` §What we build vs leverage (lines 739–797),
  §Surfaces (lines 593–738)
- `docs/a2ui-summary.md` (this worktree)
- T3 knowledge entries: `a2ui-v08-surface-descriptor-schema`,
  `a2ui-v09-draft-changes`, `a2ui-design-philosophy-stack-positioning`,
  `a2ui-nexus-synthesis-cockpit-mapping`
- RDR-111 post-mortem (deferred beads, reactivation triggers)

## Revision History

_2026-05-18 — initial sketch as the per-host realization companion to
RDR-118. Reactivates deferred beads `nexus-kkh2` (tmux pane
fulfillment) and `nexus-0rws` (ext-apps iframe fulfillment). MVP
mission profile shared with RDR-118 (RDR audit cockpit, reactivates
`nexus-kgo4`)._
