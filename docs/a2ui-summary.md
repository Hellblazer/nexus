# A2UI Summary — Reader's Brief for Nexus Adoption

> **Purpose**: a self-contained reader-friendly summary of A2UI as it
> applies to the nexus cockpit substrate. Companion to RDR-118 and
> RDR-119. Locally reviewable before committing to the adoption path.

---

## 1. What A2UI Is

**A2UI (Agent-to-UI)** is an open protocol — Apache 2.0 — for letting
**LLM agents describe user interfaces declaratively**, with the *client*
rendering using its own native, pre-approved components.

- **Released**: 2026, Google-led, with active contributions from
  CopilotKit, Oracle, Flutter team.
- **Stable version**: v0.8 (production).
- **Draft version**: v0.9 (transport-level catalog negotiation).
- **Spec home**: <https://a2ui.org/specification/v0.8-a2ui/>
- **Repo**: <https://github.com/google/a2ui>
- **License**: Apache 2.0.

The core move: agents emit JSON describing **intent** (this is a button
labelled "Submit" with this action). Clients render it with their own
trusted UI primitives (a native Flutter widget, a Lumino DOM node, a
SwiftUI view, …). No HTML eval, no remote scripts, no iframe sandboxing.

## 2. Where A2UI Sits in the 2026 Agent Stack

```
┌───────────────────────────────────────────┐
│  A2UI       — agent ↔ user (UI intent)    │   ← Adoption target
├───────────────────────────────────────────┤
│  AG-UI      — agent ↔ runtime (events)    │   ← Compatible scaffold
├───────────────────────────────────────────┤
│  A2A        — agent ↔ agent (messaging)   │   ← Compatible (some overlap)
├───────────────────────────────────────────┤
│  MCP        — agent ↔ tool                │   ← Already in nexus
└───────────────────────────────────────────┘
```

Complementary, not overlapping. A2UI deliberately leaves transport to
A2A/AG-UI/MCP; nexus's MCP plumbing carries A2UI without modification.

## 3. The Surface Concept

> A **Surface** is "a contiguous portion of screen real estate into
> which an A2UI UI can be rendered."

Each surface has:

- A unique `surfaceId`.
- A separate root component hierarchy.
- An independent data model (`Map<String, dynamic>`).

The `surfaceId` appears in messages: `beginRendering`, `surfaceUpdate`,
`dataModelUpdate`, `deleteSurface`.

**Why this matters for nexus**: the vocabulary is *literally* the same
word nexus has been using since `agentic-cockpit.md`. RDR-111 surface
levels (status-line / notification / panel / full-screen) are
realizable as A2UI surfaces.

## 4. The Five Message Types

### 4.1 `beginRendering`

Server tells client to start rendering a surface. Names the
`surfaceId`, the `catalogId`, and the `root` component id.

```json
{
  "beginRendering": {
    "surfaceId": "main_content_area",
    "catalogId": "https://nexus.dev/a2ui/catalogs/lumino-v1.json",
    "root": "outer-rdr-list"
  }
}
```

### 4.2 `surfaceUpdate`

Server sends a list of components for a surface. **Flat adjacency list,
not nested tree** (LLM-friendly + incremental).

```json
{
  "surfaceUpdate": {
    "surfaceId": "main_content_area",
    "components": [
      { "id": "outer-rdr-list",
        "component": {
          "Column": {
            "children": { "explicitList": ["rdr-card-118", "rdr-card-119"] }
          }
        }
      },
      { "id": "rdr-card-118",
        "component": {
          "Card": {
            "child": "rdr-card-118-text"
          }
        }
      },
      { "id": "rdr-card-118-text",
        "component": {
          "Text": { "text": { "literalString": "RDR-118: Surfaces as Tuples" } }
        }
      }
    ]
  }
}
```

Children reference siblings by `id`. The client reconstructs the tree
at render time.

### 4.3 `dataModelUpdate`

Patches to the surface's data model, keyed by JSON-pointer paths.

```json
{
  "dataModelUpdate": {
    "surfaceId": "main_content_area",
    "path": "/rdrs/118",
    "contents": [
      { "key": "title", "valueString": "Surfaces as Tuples" },
      { "key": "status", "valueString": "draft" }
    ]
  }
}
```

### 4.4 `userAction` (client → server)

Triggered when the user interacts with a component carrying an `action`
property. Client resolves the action's context values against the data
model, sends:

```json
{
  "userAction": {
    "name": "open_rdr",
    "surfaceId": "main_content_area",
    "sourceComponentId": "rdr-card-118",
    "timestamp": "2026-05-18T10:30:00Z",
    "context": { "rdrId": "RDR-118" }
  }
}
```

Server responds with a fresh `surfaceUpdate` / `dataModelUpdate`.

### 4.5 `deleteSurface`

Removes a surface entirely.

## 5. The Component Catalog

Components are **catalog-defined and extensible**. Each component wraps
exactly one typed property:

```json
{
  "Text": {
    "text": { "literalString": "Hello" },
    "usageHint": "h3"
  }
}
```

### 5.1 Standard catalog (v0.8 base)

`Column`, `Row` — containers with alignment.
`Card` — wraps a child with styling.
`Text` — text display with `usageHint` (h1/h2/h3/body).
`Image` — image display.
`List` — iterable list.
`Button` — interactive with `action` property.
`Container` — children via `explicitList` or `template`.

### 5.2 v0.9 additions

`AudioPlayer`, `DateTimeInput`, `Modal`, `Slider`.

### 5.3 Custom catalogs

Apps publish their own catalogs (e.g., `nexus.lumino.v1`,
`nexus.notcurses.v1`). Catalog IDs are URIs; each catalog defines its
component set as JSON Schema.

## 6. The Catalog Negotiation Handshake

This is where A2UI's **capability-based security** lives.

### 6.1 v0.8 (Message-based, two-step)

**Step 1 — Server advertises** via A2A Agent Card extension:

```json
{
  "capabilities": {
    "extensions": [{
      "uri": "https://a2ui.org/a2a-extension/a2ui/v0.8",
      "params": {
        "supportedCatalogIds": [
          "https://a2ui.org/specification/v0_8/standard_catalog_definition.json"
        ],
        "acceptsInlineCatalogs": true
      }
    }]
  }
}
```

**Step 2 — Client declares; server chooses**: client includes
`a2uiClientCapabilities.supportedCatalogIds` in every message; server
picks via `beginRendering.catalogId`.

### 6.2 v0.9 (Transport-level metadata)

The negotiation moves out of protocol messages and into the **transport
metadata** layer:

- `a2uiClientCapabilities` — advertises supported catalogs.
- `a2uiClientDataModel` — transmits surface data model via transport
  metadata.
- Exchange happens via transport-specific handshakes: A2A metadata,
  Agent Cards, MCP `initialize`.

**Why this matters for nexus**: nexus already uses MCP. The v0.9
handshake fits naturally — cockpit hosts advertise their supported
catalogs on MCP `initialize`. Producers cache.

### 6.3 The capability discipline

Clients hold a **pre-approved catalog** of trusted components. Agents
can request only what the client offers. This is *capability-based UI
by construction* — no agent can request a component the client hasn't
approved. The whole "trust under recursion" question we wrestled with
in RDR-118 is *built into the protocol*.

## 7. Data Binding — `BoundValue`

Any place a value can appear, A2UI accepts a `BoundValue`:

```json
{ "literalString": "static text" }
```

```json
{ "path": "/user/name" }
```

```json
{ "literalString": "Bob",   // both forms = static init that
  "path": "/user/name" }    // also seeds the data model
```

- `literalString` / `literalNumber` / `literalBoolean` / `literalImage`:
  static values.
- `path`: JSON-pointer into the data model. The renderer subscribes to
  the path; updates re-render.

**v0.9 addition: relative paths**. Inside a template iterating over
`/users`, a relative `firstName` resolves to `/users/0/firstName`,
`/users/1/firstName`, etc.

**Two-way input binding (v0.9)**: input components implement read/write
contracts. User interactions immediately update local data model;
server sync only on explicit `userAction`.

### 7.1 Nexus extension: `chash://` as a path

RDR-118 adopts `chash://<hex>[#span]` as a valid `BoundValue.path`.
The renderer-side resolver:

1. Recognizes `chash://`.
2. Calls `Catalog.resolve_chunk()` via the daemon (RDR-053).
3. Substitutes the chunk content into the component.

This is how transclusion lands. See RDR-118 §"`chash://` as A2UI
`BoundValue.path`".

## 8. Container Children — `explicitList` vs `template`

Containers (`Column`, `Row`, `Card`, etc.) declare children one of two
ways:

### 8.1 explicitList (named children)

```json
{
  "children": {
    "explicitList": ["child-id-1", "child-id-2", "child-id-3"]
  }
}
```

### 8.2 template (data-driven iteration)

```json
{
  "children": {
    "template": {
      "dataBinding": "/items",
      "componentId": "item-template"
    }
  }
}
```

Renders `item-template` once per item in `/items`. With v0.9 relative
paths, the template can reference per-item fields without absolute
paths.

## 9. Security Model

### 9.1 Declarative-only

A2UI is **data, not code**. No script execution, no eval, no remote
loading. Clients can only do what their pre-approved catalog allows.

### 9.2 Identity attribution (v0.9)

Identity carried via theme properties:

- `iconUrl` — agent's icon.
- `agentDisplayName` — agent's display name.

In **multi-agent systems, the orchestrator validates** these fields to
prevent impersonation. No explicit sandboxing model beyond identity
spoofing prevention.

### 9.3 Nexus mapping

| A2UI concept | Nexus mapping |
|---|---|
| Orchestrator validates identity | RDR-111 binding watcher validates `iconUrl` / `agentDisplayName` against per-cell `origin` |
| Pre-approved catalog | RDR-110 dimension registry + per-host catalog files (`nexus.lumino.v1`, `nexus.notcurses.v1`) |
| Capability scope | RDR-113 daemon trust + per-cell `origin` label |

The capability discipline composes cleanly with RDR-113 single-user
v1; multi-user (future) lands on top with no schema change.

## 10. Renderer Ecosystem (May 2026)

### 10.1 Web

- **Lit** — Google's lightweight web components library. Reference
  renderer.
- **React** — via AG-UI / CopilotKit integration.
- **Angular** — community renderer.

### 10.2 Mobile

- **Flutter** — via GenUI SDK.
- **iOS (SwiftUI)** — planned for v1.0+.
- **Android (Jetpack Compose)** — planned for v1.0+.

### 10.3 Nexus-specific (RDR-119)

- **`nexus.lumino.v1`** — Lumino DockPanel + widgets in a Tauri shell.
  Covers web + desktop + (Tauri 2) mobile.
- **`nexus.notcurses.v1`** — notcurses planes for tty hosts.

Both ship as part of RDR-119 deliverables. Same A2UI catalog
negotiation; same `surface_cell` tuple substrate from RDR-118.

## 11. What Nexus Inherits from A2UI

| Inherit | Notes |
|---|---|
| Surface descriptor schema | `surfaceId` / `surfaceUpdate` / `dataModelUpdate` / `userAction` / `deleteSurface`. Verbatim. |
| Component catalog model | RDR-118 `representation_type` = A2UI catalog id. |
| Catalog negotiation | RDR-118 capability handshake = A2UI catalog negotiation. |
| BoundValue (literal | path) | RDR-118 `data_ref_kind: inline | uri` ↔ A2UI. |
| Container templates | Bakke iteration over event types maps onto A2UI templates. |
| Security model | Capability-based UI = the trust-under-recursion property we wanted. |
| Identity (v0.9) | `iconUrl` / `agentDisplayName` ↔ RDR-110 `actor` dimension. |

## 12. What Nexus Adds on Top (the Honest Delta)

| Addition | RDR | Rationale |
|---|---|---|
| `surface_cell` subspace (cells-as-tuples) | RDR-118 | Materialized per-slot rendering state; substrate for declarative cells. |
| Sibling-surface recursion convention | RDR-118 | A2UI has no nested-surface model. Sub-surfaces as sibling-surfaceIds coordinated by tuple. |
| `chash://` as BoundValue path | RDR-118 | Transclusion via RDR-053 inheritance. |
| Per-cell `origin` discipline | RDR-118 | RDR-113 refinement; labels who's rendering inside daemon trust boundary. |
| HATEOAS-style action affordances | RDR-118 | A2UI `userAction` already supports this; producer discipline only. |
| `nexus.lumino.v1` catalog | RDR-119 | Per-host A2UI catalog for web/desktop/mobile. |
| `nexus.notcurses.v1` catalog | RDR-119 | Per-host A2UI catalog for tty. |
| Tauri 2 shell | RDR-119 | Cross-platform native webview host for Lumino. |
| Bakke-driven `layout_state` | RDR-111 (shipped) | Already there. The fabric reads it; A2UI components fill the slots. |
| `mission_context` subspace | RDR-119 | Profile bundle (active bindings, catalogs, layout, permission mode). |

## 13. The Honest Gap (Nexus-Specific Extension)

A2UI v0.9 supports multiple surfaces via distinct `surfaceId`s but has
**no nested-surface recursion model**. RDR-118 ships:

> **Sibling-surface recursion convention**: a cell with
> `child_subspace = <sub_id>` declares "render a sub-surface here." The
> host treats this as an A2UI Container whose body is the set of
> `surface_cell` tuples in `surface_cell/<sub_id>`. The renderer
> instantiates a nested layout engine bound to the child subspace.

If A2UI later ships a nested-surface model, we contribute the spec
change upstream and deprecate our convention.

## 14. Recommended Adoption Path

### 14.1 v1 — Adopt A2UI v0.8 (Stable)

- Target the Stable schema.
- Use two-step catalog negotiation initially; migrate to v0.9
  transport-level when v0.9 stabilizes.
- Ship `nexus.lumino.v1` and `nexus.notcurses.v1` catalogs.
- Add `chash://` BoundValue path and sibling-surface recursion as
  nexus-specific extensions documented in `surface_cell.yml`.

### 14.2 v1.5 — Track A2UI v0.9

- Migrate catalog negotiation to MCP `initialize` (v0.9 transport-level).
- Adopt v0.9 components: `Modal`, `DateTimeInput`, `Slider`,
  `AudioPlayer`.
- Adopt relative-path resolution in templates.

### 14.3 v2 — Contribute upstream

- If sibling-surface recursion proves load-bearing, propose
  `NestedSurface` as an A2UI standard component.
- Contribute `chash://` BoundValue path as a content-addressed
  reference extension.

## 15. Quick-Reference Checklist for Reviewers

- [ ] Read §1–§6 for the protocol overview.
- [ ] Read §11–§13 for the nexus mapping and the one honest gap.
- [ ] Cross-reference RDR-118 §Technical Design for the cell substrate.
- [ ] Cross-reference RDR-119 §Technical Design for the per-host
      realization.
- [ ] Validate adoption rationale (§14) against your priorities.
- [ ] Flag anything that needs spike before commitment (A2UI
      catalog-through-MCP negotiation, `chash://` path, sibling-
      surface recursion).

## 16. References

### Primary sources

- A2UI v0.8 spec — <https://a2ui.org/specification/v0.8-a2ui/>
- A2UI v0.9 draft — <https://a2ui.org/specification/v0.9-a2ui/>
- A2UI repo — <https://github.com/google/a2ui>
- Google Developers introduction —
  <https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/>

### Nexus context

- `docs/agentic-cockpit.md` (especially §Surfaces lines 593–738, §Open
  Questions lines 813–815 — explicitly anticipates A2UI)
- RDR-053: Xanadu Fidelity (`chash://` content-addressed spans)
- RDR-108: T3 Chunk Soft-Delete (chunk_text_hash as natural ID)
- RDR-110: Semantic Tuple Space
- RDR-111: ORB Cockpit Substrate (+ post-mortem)
- RDR-112: Storage-as-Service Container Boundary
- RDR-113: Host-Trust Model
- RDR-118: Surfaces as Tuples (companion)
- RDR-119: Cockpit UI Fabric (companion)

### Nexus T3 knowledge entries

- `a2ui-v08-surface-descriptor-schema` — full schema details
- `a2ui-v09-draft-changes` — v0.9 diffs
- `a2ui-design-philosophy-stack-positioning` — Google's design rationale
- `a2ui-nexus-synthesis-cockpit-mapping` — the synthesis behind RDR-118 + RDR-119

Recover via `nx_answer "What is A2UI and how does it map onto nexus?"`
or `nx search a2ui`.

---

**Status of this document**: companion to RDR-118 + RDR-119 draft.
Reader-friendly; intended for local review before commitment to the
adoption path. Not normative — the RDRs are.

**Date**: 2026-05-18.
