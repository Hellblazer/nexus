---
title: "Surfaces as Tuples — The ORB is the Portal Broker (A2UI Adoption + Xanadu Inheritance)"
id: RDR-118
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-17
revised: 2026-05-18
accepted_date:
related_issues: []
related_rdrs: [RDR-053, RDR-108, RDR-110, RDR-111, RDR-112, RDR-113]
related_external: [a2ui-v0.8-spec, a2ui-v0.9-draft, ag-ui, a2a-protocol, mcp]
reactivates_beads: [nexus-kkh2, nexus-0rws, nexus-kgo4]
related_tests: []
---

# RDR-118: Surfaces as Tuples — The ORB is the Portal Broker

> Third iteration. The first two sketches re-derived layers that already
> exist. This one names the inheritance honestly and limits itself to the
> delta nexus actually adds.

## Problem Statement

RDR-111 ships the cockpit substrate (surface levels, bindings primitive,
auto-layout engine, layout_state subspace, three reference panels). RDR-110
ships the semantic tuple space — the broker. RDR-053 ships Xanadu fidelity
(`chash://` content-addressed spans, tumbler comparison, link survivability)
at the catalog layer. A2UI v0.8 (Google, 2026, Apache 2.0) ships the
canonical surface descriptor protocol — explicitly anticipated by
`agentic-cockpit.md` lines 813–815 as the mature target format.

What's missing is the **small discipline that wires these layers together**
so a cell can be (a) heterogeneous content from an arbitrary producer,
(b) recursively composable, (c) capability-negotiated with the host,
(d) referenced by stable content address, and (e) trust-scoped under the
existing daemon boundary. That wiring is this RDR.

This RDR adopts A2UI v0.8 as the descriptor format, inherits RDR-053's
`chash://` transclusion stack, reuses RDR-111's `layout_state` placement
protocol, and adds four small things: a `surface_cell` subspace, a
sibling-surface recursion convention, `chash://` as a BoundValue path,
and producer discipline for HATEOAS-style action affordances.

### Enumerated gaps to close

#### Gap 1: No published cell-as-tuple schema bridging `bindings/<profile>` to A2UI

RDR-111 ships `bindings/<profile>` with an opaque `surface_payload` field.
A2UI ships a structured `surfaceUpdate` event. There is no documented
mapping between them. Without it, producers and hosts coordinate ad-hoc,
which forks.

#### Gap 2: No recursion convention (A2UI doesn't cover nested surfaces)

A2UI supports multiple `surfaceId`s but no nested-surface composition. A
cell that hosts another surface is undocumented. Sibling-surface
coordination via tuple is the smallest extension that preserves the
recursive-surfaces ambition.

#### Gap 3: No first-class transclusion path

RDR-053 ships `chash://<hash>[#span]` as the catalog-layer content
address. Cells should be able to render content by referencing a chash —
without inventing a new URI scheme or new resolution path. A2UI's
`BoundValue.path` field is the natural attachment point.

#### Gap 4: No per-cell origin refinement of RDR-113

RDR-113 sets trust at the daemon boundary. A2UI v0.9 carries identity via
`iconUrl` and `agentDisplayName`. We need a per-cell `origin` dimension on
`surface_cell` that refines RDR-113 (the daemon decides who can attach;
`origin` labels who's currently rendering inside that boundary). Not new
trust — finer labeling within existing trust.

## Context

### Animating principle (preserved across iterations)

The nexus thesis is that producers and consumers should bind by **meaning**
— RDR-110 semantic tuple space, `match_text` fields, NLP-parsed bindings.
The ORB is the broker; the substrate already does capability negotiation.
A2UI's *catalog* abstraction is the protocol-level realization of the same
discipline: clients hold pre-approved component catalogs; agents request
from them; the negotiation IS the capability handshake.

We do **not** invent a parallel decider, MIME registry, or capability
bundle. The tuple space + A2UI catalogs together cover this.

### Inheritance map (zero new substrate code)

| Concern | Inherited from | Notes |
|---|---|---|
| Surface descriptor schema | A2UI v0.8 (Stable) | `surfaceId`, `surfaceUpdate`, `dataModelUpdate`, `userAction`, `BoundValue`. |
| Component catalogs | A2UI + RDR-110 dimension registry | Catalogs register as subspaces; per-host catalogs publish capabilities. |
| Catalog negotiation | A2UI v0.8 two-step (v0.9 transport-level) | Server advertises supported catalogs; client declares its set; server picks. |
| Coordination substrate | RDR-110 tuple space | Log-structured, offset-tracking subscribers, semantic match. |
| Placement-plan transport | RDR-111 `layout_state/<profile>` subspace | Bakke auto-layout writes; surfaces read; 250ms debounce. |
| Auto-layout engine | RDR-111 `src/nexus/cockpit/layout.py` | Vertical-stack + demotion cascade (shipped). |
| Static surface render | RDR-111 binding payload | One tuple → one render, cheap, low-latency. |
| Streaming surface render | RDR-111 connection_manifest (Phase 4, deferred) | Manifest tuple declares pipe; peers connect direct. |
| Trust boundary | RDR-113 | Daemon-level. Per-cell `origin` is a label refinement. |
| Daemon SQL RPC | RDR-112 | Canonical pattern for ordered/temporal retrieval. |
| Content-addressed transclusion | RDR-053 + RDR-108 | `chash://<hex>` resolves via catalog manifest → ChromaDB chunk. |
| Tumbler arithmetic / span overlap | RDR-053 D6 | Comparison-only (no ADD/SUBTRACT). Sufficient for our use. |
| Identity attribution | A2UI v0.9 (`iconUrl`, `agentDisplayName`) | Maps to RDR-110 `actor` dimension. Orchestrator validates per A2UI. |

### What the source documents explicitly anticipated

`agentic-cockpit.md` lines 813–815, in §Open Questions:

> Surface descriptor format. Need to settle on a small, stable shape (level
> + payload + optional layout hints). Earliest version can be a JSON
> Schema; **mature version maps to A2UI or similar declarative component
> intent.**

A2UI shipped in 2026. This RDR is the "mature version" landing.

### Phase 4 reactivation

RDR-111 deferred two beads explicitly:

- **`nexus-kkh2`** (P4.1) — tmux pane fulfillment adapter (connection_manifest consumer)
- **`nexus-0rws`** (P4.2) — ext-apps iframe fulfillment adapter (connection_manifest consumer)

And one consumer-driven bead:

- **`nexus-kgo4`** (P3.5) — first end-to-end mission profile

RDR-118 + RDR-119 are the reactivation work for all three. The MVP mission
profile (§Implementation Plan) is an RDR audit cockpit.

### Enumerated gaps closed (sources)

- Gap 1 closure: §"Surface_cell subspace schema" + §"A2UI mapping"
- Gap 2 closure: §"Sibling-surface recursion convention"
- Gap 3 closure: §"chash:// as BoundValue path"
- Gap 4 closure: §"Per-cell origin discipline"

## Research Findings

### Investigation

Read in full: `agentic-cockpit.md` (889 lines), RDR-110, RDR-111 (1198
lines) + post-mortem, RDR-053 (Xanadu fidelity, closed), RDR-108 (T3
chunk soft-delete), RDR-112 + RDR-113 (trust + daemon). Fetched A2UI v0.8
specification, A2UI v0.9 draft, Google Developers introduction blog.
Ingested four T3 entries (`a2ui-v08-surface-descriptor-schema`,
`a2ui-v09-draft-changes`, `a2ui-design-philosophy-stack-positioning`,
`a2ui-nexus-synthesis-cockpit-mapping`) with cross-references.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
|---|---|---|
| A2UI v0.8 schema | Yes (a2ui.org/specification/v0.8-a2ui) | `surfaceUpdate`, `dataModelUpdate`, `userAction`, `BoundValue` (literal\|path), two-step catalog negotiation, Container.children = explicitList \| template |
| A2UI v0.9 draft | Yes (a2ui.org/specification/v0.9-a2ui) | Transport-level catalog negotiation, JSON-pointer dataModel updates, two-way input binding, no nested-surface recursion documented |
| A2UI Google intro | Yes (developers.googleblog.com) | Apache 2.0, complementary to A2A/AG-UI/MCP, security-first declarative-only |
| RDR-053 Xanadu fidelity | Yes (full read) | `chash:<sha256>` canonical, `_SPAN_PATTERN` accepts `chash:[0-9a-f]{64}`, `link_audit(t3=...)` detects stale spans, tumbler comparison via -1 sentinel padding |
| RDR-108 T3 chunk soft-delete | Partial | T3 chunk natural ID is `chunk_text_hash[:32]`; catalog `document_chunks` manifest gives `(doc_id, position) → chash` |
| RDR-111 shipped surfaces | Yes (post-mortem + src/nexus/cockpit/) | hook_bridge.py:239 (emit), bindings.py:137 (Binding/BindingProfile), layout.py:79 (render_text), three panels under panels/ |
| RDR-111 deferred work | Yes (post-mortem) | nexus-kkh2 (tmux pane fulfillment), nexus-0rws (ext-apps iframe fulfillment), nexus-kgo4 (first mission profile) all dormant |

### Key Discoveries

- **Verified (RDR-111 §Open Questions line 813-815)**: A2UI was explicitly
  named as the mature surface descriptor format. This RDR is that landing.
- **Verified (A2UI spec)**: surface vocabulary is *literally* the same word
  and concept. Near-1:1 mapping to existing nexus concepts.
- **Verified (A2UI repo + blog)**: pre-approved component catalogs make
  A2UI *capability-based UI* by construction. This is the trust-under-
  recursion property we wanted, off the shelf.
- **Verified (RDR-053)**: `chash://` resolution path runs through the
  existing catalog manifest and ChromaDB. `link_audit(t3=...)` extends
  cleanly to detect stale transclusions.
- **Documented (A2UI v0.9 draft)**: catalog negotiation moved to
  transport-level metadata (A2A Agent Cards, MCP `initialize`). Better fit
  for our MCP transport.
- **Verified (A2UI v0.9 draft)**: no nested-surface recursion model. The
  single nexus-specific extension we ship.
- **Verified (RDR-111 post-mortem)**: phase-4 work was deferred with
  explicit reactivation triggers; this RDR is that reactivation.

### Critical Assumptions

- [ ] **A1** — A2UI catalog negotiation through MCP `initialize`
  (v0.9 transport-level mode) is reachable without forking the protocol.
  **Status**: Unverified. **Method**: Paper design + small spike against
  the A2UI reference implementation.
- [ ] **A2** — Sibling-surface coordination via tuple gives correct
  recursion semantics through three nesting levels (no name collision,
  no infinite-loop risk, deterministic teardown).
  **Status**: Unverified. **Method**: Spike — three-level nested surface
  with mixed content; verify under resize, focus, dispose.
- [ ] **A3** — `chash://` is a valid `BoundValue.path` value in A2UI
  v0.8/v0.9 (or can be rendered via a small renderer-side resolver).
  **Status**: Unverified. **Method**: Spike — render a cell whose path
  is a chash; verify catalog → ChromaDB → A2UI fragment pipeline.
- [ ] **A4** — Per-cell `origin` is a strict refinement of RDR-113.
  **Status**: Unverified. **Method**: Source search + RDR-113 author
  confirmation.
- [ ] **A5** — Per-host A2UI catalogs (`nexus.lumino.v1`,
  `nexus.notcurses.v1`) can each cover the cockpit's reference panels
  without compromise.
  **Status**: Unverified. **Method**: Build both catalogs minimally for
  the MVP mission profile.

## Proposed Solution

### Approach

Adopt A2UI v0.8 (Stable) as the surface descriptor format. Track v0.9.
Add four small nexus-specific extensions (cell tuple schema, sibling
recursion convention, `chash://` path, origin discipline). Reuse
RDR-111's layout_state for placement plans. Inherit RDR-053's
transclusion stack. Reactivate three dormant beads as deliverables.

### Technical Design

#### 1. `surface_cell/<surface_id>` subspace

A new subspace whose tuples *declare* what should be in each slot of a
surface. Each tuple is the **materialized rendering state** for one
slot — distinct from `layout_state` (which carries Bakke's per-binding-
type stylesheet) and from individual binding events (which carry
content instances).

```yaml
surface_cell:
  dimensions:
    - name: surface_id      # composition: which parent surface (A2UI surfaceId)
    - name: slot            # composition: named layout slot
    - name: origin          # trust label: URL origin | peer-cred+container
                            # | binding profile name (RDR-113 refinement)
    - name: child_subspace  # recursion: name of nested surface's subspace
                            # (null for leaf cells)
    - name: a2ui_catalog_id # which A2UI catalog this cell is shaped against
  match_text_field: description  # natural-language capability description
  content_fields:
    - representation_type   # short discriminator: a2ui-component | chash |
                            # connection-manifest-ref | lumino-widget |
                            # notcurses-plane | nexus-surface
    - data_ref              # inline | URI | subspace-name | chash:// |
                            # manifest-id
    - data_ref_kind         # inline | uri | subspace | transclusion |
                            # manifest | a2ui-component-inline
    - actions               # optional: A2UI userAction descriptors
                            # (HATEOAS-style per-cell affordances)
```

A *cell* is one tuple in this subspace. A *surface* is the set of cells
with the same `surface_id`. A *sub-surface* is a cell whose
`child_subspace` is non-null — the host instantiates a nested layout
engine bound to that subspace.

**Default representation**: `a2ui-component` with `data_ref_kind: inline`
carrying an A2UI component JSON fragment. This is the cheap common case.

#### 2. A2UI mapping

| A2UI concept | Nexus realization |
|---|---|
| `surfaceId` | `surface_cell.surface_id` |
| `surfaceUpdate` | binding action writes `surface_cell` tuple |
| `component` | `surface_cell.data_ref` when `data_ref_kind=a2ui-component-inline` |
| `BoundValue.literalString` | inline payload |
| `BoundValue.path` | `chash://<hex>[#span]` (transclusion) or `tuple://<subspace>/<id>` (subspace ref) |
| `dataModelUpdate` | tuple `out` to `surface_cell` (incremental) |
| `userAction` | tuple `out` to `cell_input/<surface_id>` |
| `deleteSurface` | tuple `take` on all `surface_cell` for the surface_id |
| Container.children (explicitList) | named cells in the same `surface_id` |
| Container.children (template) | binding fires per matching event in a trigger subspace |
| Catalog id | `a2ui_catalog_id` dimension |
| Catalog negotiation | MCP `initialize` (per A2UI v0.9 transport-level) |

#### 3. Sibling-surface recursion convention

A2UI has no nested-surface model. Nexus adds:

- A cell with `child_subspace = <sub_id>` declares "render a sub-surface
  here." The host treats this as an A2UI Container whose body is the set
  of `surface_cell` tuples in `surface_cell/<sub_id>`.
- Sub-surface composition is by **sibling surfaceId** with a parent-child
  reference dimension, not by literal A2UI nesting. The renderer
  instantiates a nested layout engine bound to the child subspace.
- Trust re-evaluation at every nesting level: the child surface inherits
  no capability from its parent. `origin` is per-cell.
- Cycle detection: host tracks the subspace-reference path; refuses to
  recurse into a subspace already on the path. Depth cap (default 8) as
  belt-and-braces.

If A2UI later defines a nested-surface model, contribute upstream and
deprecate this convention.

#### 4. `chash://` as A2UI `BoundValue.path`

A cell can render content by reference:

```json
{
  "Text": {
    "text": { "path": "chash://b5e2a8c9...3f1" }
  }
}
```

The renderer-side resolver:

1. Recognizes `chash://` scheme.
2. Resolves the chunk via the catalog (`Catalog.resolve_chunk()` from
   RDR-053).
3. Substitutes the chunk text (or image bytes, for image components) as
   the rendered value.

`link_audit(t3=...)` from RDR-053 already detects stale chash spans.
Reuse.

For document-level transclusion: `chash://<doc_hash>` resolves via the
`document_chunks` manifest's `(doc_id, position) → chash` mapping
(RDR-108).

#### 5. Per-cell `origin` discipline

`origin` is a *label*, not a *capability*. RDR-113 daemon-level trust
gates daemon access. `origin` filters within that boundary:

| Producer kind | `origin` value |
|---|---|
| Local binding (default) | binding profile name (e.g., `cockpit.local`) |
| Foreign iframe content | URL origin (e.g., `https://docs.example.com`) |
| PTY content | `pty:peer-cred+container-id` (when daemonized) |
| Transcluded content | `chash:` (label propagates from the chash source's origin) |
| A2UI agent | `a2ui:<agent_did>` (when A2UI v0.9 identity validated) |

Orchestrator (the binding watcher) validates `iconUrl` /
`agentDisplayName` against `origin` per A2UI v0.9 anti-impersonation
discipline. No new identity layer.

#### 6. Layout / input / focus subspaces

Already shipped in RDR-111 (`hook_events/*`, bindings, `layout_state`).
This RDR names two new conventional subspaces:

- `cell_layout/<surface_id>` — resize, visibility, damage events.
  Damage clipped at cell rectangle (recursion-safe).
- `cell_input/<surface_id>` — pointer/key events in cell-local
  coordinates. Carries A2UI `userAction` shape.
- `cell_focus/<root_surface_id>` — single focus-path tuple. DFS by
  default; per-container overrides allowed via cell `actions` field.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Surface descriptor schema | A2UI v0.8 (external) | **Adopt entire** |
| `surface_cell` subspace | none | **New** (this RDR) — registers via RDR-112 admin RPC |
| `cell_layout` / `cell_input` / `cell_focus` | RDR-110 subspace registry | **New schemas** (this RDR) |
| Sibling-surface recursion | none | **New convention** (this RDR) |
| `chash://` BoundValue path | RDR-053 catalog + RDR-108 manifest | **Extend** — add renderer-side resolver (~50 LoC) |
| Per-cell `origin` | RDR-113 | **Refine** — label dimension; no trust expansion |
| Capability negotiation | A2UI catalog negotiation + RDR-110 registry | **Reuse entire** |
| Placement-plan transport | RDR-111 `layout_state/<profile>` subspace | **Reuse entire** |
| Auto-layout engine | RDR-111 `src/nexus/cockpit/layout.py` | **Reuse entire** |
| Direct SQL RPC for ordered retrieval | RDR-112 | **Reuse entire** |
| HATEOAS action affordances | A2UI `userAction` already supports this | **Producer discipline only** |
| Per-host catalog implementation | none | **Deferred to RDR-119** — `nexus.lumino.v1`, `nexus.notcurses.v1` |

### Decision Rationale

Three forces:

1. **The substrate already does the work.** RDR-110/111 are the broker;
   A2UI is the descriptor protocol. Re-inventing either is duplication.
   RDR-053 already shipped Xanadu fidelity at the catalog layer; we
   inherit.
2. **Cells-as-tuples is symmetric across nesting** under sibling-surface
   recursion. Composition is automatic.
3. **No new vocabulary** beyond the four nexus-specific extensions.
   Producers describe themselves once in `match_text` + `description`;
   hosts negotiate via A2UI catalogs; nothing else to invent.

## Alternatives Considered

### Alternative 1: Invent a parallel MIME-bundle + decider (abandoned)

The first sketch of this RDR. Rejected because RDR-110 is already the
broker; A2UI is already the catalog protocol. Adding a parallel decider
would have duplicated both.

### Alternative 2: Adapt MCP `ui://` resource model

MCP treats UI as a sandboxed HTML resource. **Pros**: existing MCP
plumbing. **Cons**: HTML is executable; loses A2UI's capability-based
discipline; doesn't compose cleanly with the cockpit's surface-level
discipline. **Reason for rejection**: A2UI explicitly positions itself
as the *native-first* alternative to MCP `ui://` for exactly this
case.

### Alternative 3: Extend A2UI upstream with nested-surface recursion

Add a `NestedSurface` component to the standard A2UI catalog and
contribute the spec change upstream. **Pros**: standardized recursion.
**Cons**: blocks our v1 on upstream acceptance; slow.

**Reason for deferral**: ship sibling-surface coordination first; if it
proves load-bearing, contribute upstream as v2.

### Briefly Rejected

- **Invent a tuple:// URI scheme for transclusion**: RDR-053's
  `chash://` already exists; using a parallel scheme would fork the
  address space.
- **New transport for cell payloads**: tuple bodies + A2UI BoundValue
  already work; no new mechanism needed.
- **NestedSurface as our own catalog component, not a convention**:
  defer to v2 after sibling-surface convention proves out.

## Trade-offs

### Consequences

- (+) Contract is tiny: one new subspace schema + three convention
  subspaces + a renderer-side chash resolver.
- (+) Recursion is genuinely composable (sub-surfaces are first-class
  values, addressable by subspace name).
- (+) Capability evolution is free — A2UI catalogs are the existing
  protocol mechanism.
- (+) Forward-compatible: A2UI v0.9 transport-level negotiation fits
  MCP naturally.
- (+) Cross-host portability is a protocol-level property; the same
  cell can render on Lumino, notcurses, or any A2UI client.
- (+) Reactivates three previously-dormant beads (`nexus-kkh2`,
  `nexus-0rws`, `nexus-kgo4`).
- (−) v1 commits to A2UI v0.8 (Stable) and tracks v0.9 (Draft) —
  protocol may evolve under us.
- (−) Sibling-surface recursion is a nexus-specific extension to A2UI;
  may have to migrate if/when A2UI ships a nested-surface model.
- (−) Match-quality at the broker becomes a correctness concern for
  cockpit rendering, not just retrieval. Threshold tuning matters.

### Risks and Mitigations

- **Risk**: A2UI v0.8 deprecated faster than expected.
  **Mitigation**: track v0.9 in parallel; the descriptor shape is
  stable across versions (catalog negotiation changed transport, not
  semantics).

- **Risk**: Sibling-surface coordination cycles.
  **Mitigation**: subspace-reference path tracking + depth cap;
  refuse to recurse into a subspace already on the path.

- **Risk**: `chash://` resolution latency on render path.
  **Mitigation**: catalog cache + chunk metadata pre-fetch; resolution
  is at binding-activation cadence (Auto-Style), not per event.

- **Risk**: `origin` misused as backdoor to RDR-113 trust.
  **Mitigation**: spec discipline — `origin` is label-only; RDR-113
  daemon trust still gates access.

- **Risk**: Per-host catalog drift (`nexus.lumino.v1` and
  `nexus.notcurses.v1` diverge in semantics).
  **Mitigation**: shared catalog spec (RDR-119); cross-host
  conformance tests.

### Failure Modes

- *Visible*: cell renders wrong representation (catalog negotiation
  bug); cell renders wrong size (`layout_state` mismatch); transclusion
  resolves to stale content (RDR-053 `link_audit` flags).
- *Silent*: focus desync under deep nesting; sub-surface buffer leak on
  dispose; recursive cycle without depth cap.
- *Recovery*: portal-inspector cell (built using the contract itself)
  shows for any selected cell — A2UI catalog id, capability match
  trace, origin, recent layout_state entries, top-k semantic matches
  with scores.

## Implementation Plan

### Prerequisites

- [ ] A1–A5 verified via spikes.
- [ ] A2UI v0.8 reference renderer evaluated (Lit on web, GenUI SDK on
      Flutter).
- [ ] RDR-111 layout_state subspace confirmed read-accessible from
      Phase 4 fulfillment adapter.
- [ ] RDR-053 `Catalog.resolve_chunk()` exposed via daemon RPC for
      renderer-side chash resolution.

### Minimum Viable Validation

**MVP mission profile: RDR audit cockpit.** A three-level surface that:

1. Outer surface = list of all open RDRs (top-level panel).
2. Each RDR item is a sub-surface containing problem-statement,
   research-findings, design, and gate sub-cells.
3. Each sub-cell can transclude (via `chash://`) from another RDR or
   the catalog.

Renders on both backends from RDR-119 (Lumino-in-Tauri and notcurses).
Inner surfaces use only `surface_cell` subspace operations — no
medium-specific API. Reactivates `nexus-kgo4`.

### Phase 1: Schemas and conventions

#### Step 1: Land subspace YAML schemas

- `nx/tuplespace/builtin/surface_cell.yml`
- `nx/tuplespace/builtin/cell_layout.yml`
- `nx/tuplespace/builtin/cell_input.yml`
- `nx/tuplespace/builtin/cell_focus.yml`

Register via RDR-112 admin RPC. Document the sibling-surface recursion
convention in the `surface_cell.yml` header.

#### Step 2: `chash://` BoundValue resolver

Renderer-side library (~50 LoC): recognizes `chash://` scheme, calls
`Catalog.resolve_chunk()` via daemon RPC, returns chunk text/bytes for
A2UI substitution. Cache resolved values at the cell's
`a2ui_catalog_id` scope.

#### Step 3: A2UI catalog negotiation through MCP

Document the handshake: cockpit hosts advertise supported A2UI catalog
IDs via MCP `initialize` (per A2UI v0.9 transport-level mode);
producers query via the existing MCP capability discovery.

### Phase 2: Reactivation of fulfillment adapters

Hand off to RDR-119 for the per-host catalog implementations:

- **Reactivate `nexus-kkh2`** — tmux pane fulfillment adapter using
  `nexus.notcurses.v1` catalog.
- **Reactivate `nexus-0rws`** — ext-apps iframe fulfillment adapter
  using `nexus.lumino.v1` catalog (in Tauri shell).

### Phase 3: First end-to-end mission profile

- **Reactivate `nexus-kgo4`** — RDR audit cockpit MVP (above).
- Author bindings, layout disposition, permission mode.
- Run on both backends.
- Measure what's awkward; iterate.

### Day 2 Operations

- New subspaces inherit RDR-110 dimension-registry operations and
  RDR-112 daemon admin RPCs (`list-subspaces`, `show-schema`, `stats`,
  `out`, `read`, `take`, `ack`, `nack`).
- `chash://` validation extends `link_audit` (already in RDR-053).

### New Dependencies

- A2UI v0.8 reference renderer (Lit / GenUI SDK / CopilotKit) — adoption,
  not in-tree.
- No new Python/Rust dependencies for the substrate side.

## Test Plan

- **Scenario**: A2UI catalog negotiation through MCP initialize.
  **Verify**: cockpit host's `nexus.lumino.v1` advertised in
  `MCP initialize` response; producer can request rendering against it.
- **Scenario**: Three-level nested surface renders identically on
  Lumino and notcurses backends.
  **Verify**: same `surface_cell` tuples; rendered output differs only
  in medium-specific component realization.
- **Scenario**: `chash://` BoundValue path resolves correctly.
  **Verify**: cell with `BoundValue.path = chash://<hex>` renders the
  chunk text; `link_audit` flags stale spans.
- **Scenario**: Sibling-surface recursion cycle prevention.
  **Verify**: a `child_subspace` referencing an ancestor surface is
  refused with a clear error; depth cap (8) terminates pathological
  recursion.
- **Scenario**: Origin filtering.
  **Verify**: a cell with `origin=foreign-iframe` cannot post to its
  parent's `cell_input` subspace; RDR-113 daemon trust unchanged.
- **Scenario**: HATEOAS-style action affordances render and dispatch.
  **Verify**: a cell carrying A2UI `userAction` triggers a binding fire
  with correct `surfaceId`/`sourceComponentId`.
- **Scenario**: Match-quality threshold floor.
  **Verify**: when no producer-tuple matches a host's catalog query
  above threshold, host renders a documented placeholder with the
  query for diagnosis.

## Validation

### Testing Strategy

1. **Scenario**: Reference RDR audit cockpit (three levels, mixed
   content per level) renders identically on Lumino-in-Tauri and
   notcurses backends.
   **Expected**: pass with the same `surface_cell` tuples; chosen
   A2UI components differ per host catalog; no medium-specific code
   in the producer.

### Performance Expectations

Negotiation cost = A2UI catalog handshake at MCP initialize (once per
session). Capability match cost = one RDR-110 semantic-match query per
cell per Auto-Style tick. Cached at the binding watcher. Per-event
Layout cost unchanged. `chash://` resolution = one catalog →
ChromaDB lookup per cell mount, cached.

## Finalization Gate

(deferred — sketch only)

### Contradiction Check

(deferred)

### Assumption Verification

(deferred — A1–A5 unverified; spikes required)

### Scope Verification

(deferred)

### Cross-Cutting Concerns

- **Versioning**: A2UI is versioned externally (v0.8 Stable, v0.9
  Draft); per-host catalog versions namespaced (`nexus.lumino.v1`,
  `nexus.notcurses.v1`).
- **Build tool compatibility**: N/A at spec stage.
- **Licensing**: A2UI is Apache 2.0; compatible with nexus.
- **Deployment model**: per-host catalogs ship independently; sibling-
  surface recursion convention documented in `surface_cell.yml`.
- **IDE compatibility**: VS Code webview ships as single-cell host in
  v1 (no nested webviews — explicit deferral, same as RDR-119).
- **Incremental adoption**: legacy RDR-111 `surface_payload` strings
  continue to work — they are one form of tuple body. Migration to
  `surface_cell` subspace is opt-in per binding.
- **Secret/credential lifecycle**: `origin` labels cells; RDR-113
  daemon trust gates access. Secrets never cross cell boundaries by
  default.
- **Memory management**: dispose is `take`; RDR-110 retention sweeper
  releases bodies; sub-surface teardown closes any held resources.

### Proportionality

Small RDR. The substrate is RDR-110/111; the descriptor protocol is
A2UI; the Xanadu inheritance is RDR-053. This RDR is the discipline
that wires them. Resist the urge to grow it.

## References

- A2UI v0.8 spec — https://a2ui.org/specification/v0.8-a2ui/
- A2UI v0.9 draft — https://a2ui.org/specification/v0.9-a2ui/
- A2UI Google introduction —
  https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/
- A2UI repo (Apache 2.0) — https://github.com/google/a2ui
- T3 entries: `a2ui-v08-surface-descriptor-schema`,
  `a2ui-v09-draft-changes`,
  `a2ui-design-philosophy-stack-positioning`,
  `a2ui-nexus-synthesis-cockpit-mapping`
- `docs/agentic-cockpit.md` (especially §Surfaces lines 593–738, §Open
  Questions lines 813–815)
- RDR-053: Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed
  Spans
- RDR-108: T3 Chunk Soft-Delete (chunk_text_hash as natural ID)
- RDR-110: Semantic Tuple Space
- RDR-111: ORB Cockpit Substrate + post-mortem
- RDR-112: Storage-as-Service Container Boundary
- RDR-113: Host-Trust Model
- `docs/a2ui-summary.md` (this worktree) — reader-friendly A2UI
  summary for review

## Revision History

_2026-05-17 — initial sketch under wrong framing (MIME-bundle + decider
indirection). Built a parallel decider over the existing semantic
broker._

_2026-05-17 — abandoned and rewritten. The ORB is the broker; cells
are tuples; recursion is a subspace reference. Contract collapsed to
four subspace schemas + query template._

_2026-05-18 — third iteration after thorough audit: read full
`agentic-cockpit.md`, RDR-111 + post-mortem, RDR-053, RDR-108; fetched
and ingested A2UI v0.8/v0.9 specs and Google announcement. Adopted
A2UI v0.8 as the descriptor format (explicitly anticipated by
cockpit.md lines 813-815); inherited RDR-053 `chash://` transclusion;
named sibling-surface coordination as the one nexus-specific
extension; reactivates dormant beads `nexus-kkh2`, `nexus-0rws`,
`nexus-kgo4`. This is the current document._
