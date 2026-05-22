---
title: "Surface Rendering Integration: nexus Adopts palinex, Defines Pilot Producers, Supersedes RDR-123/124"
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
related_external: [palinex-rdr-001, palinex-0.0.1, a2ui-v0.9-spec]
related_tests: []
supersedes: [RDR-123, RDR-124]
---

# RDR-127: Surface Rendering Integration — nexus Adopts palinex

> Architecture for the surface IR + renderer + host-bridge protocol lives in palinex [RDR-001](https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-001-architecture.md). This RDR is the **nexus-side integration** only.

## Problem Statement

Nexus producers (`nx_answer`, subagent results, plan inspectors, RDR dashboards) routinely emit structured intermediates and flatten them to markdown at the wire. The structure is lost; downstream consumers re-derive it imperfectly.

The previous attempts at fixing this (RDR-118 "surfaces as tuples" + RDR-119 "cockpit UI fabric") coupled rendering to the cockpit substrate (RDR-110/111/112/113). That arc was scrapped 2026-05-19 when substrate work was deferred to RDR-120. RDR-123 (nx_answer surfaces) and RDR-124 (subagent result surfaces) inherited the same dependency and are unbuildable today.

This RDR ships surface rendering today by depending on an external project — [**palinex**](https://github.com/Hellblazer/palinex) — that solves the architecture-level problem (a2ui v0.9 as IR, single-file renderer, postMessage host-bridge protocol, three delivery shapes, markdown sidecar discipline). palinex 0.0.1 is on PyPI; the renderer is hosted at <https://hellblazer.github.io/palinex/>. The substantive architecture lives in palinex's RDR-001.

This RDR is what nexus does with palinex.

### Enumerated gaps to close

1. **No nexus dependency on palinex.** Need it added as a runtime dep.
2. **No pilot producers.** `nx_answer` is the obvious first candidate; one subagent (codebase-deep-analyzer) is the second.
3. **No nexus-side integration of the postMessage host-bridge protocol.** When palinex's renderer is delivered as an MCP UI resource from a nexus tool, the host wrapper must implement palinex's `a2ui.request`/`a2ui.response` protocol and route requests (chash resolution, etc.) through nexus MCP tools.
4. **No `SurfaceBroker` impl in nexus.** Sub-agent surface emission needs an in-process broker mapped to surface_id keys so the orchestrator can fetch surfaces on demand.
5. **Outstanding supersession of RDR-123/124.** Both were merged 2026-05-19 but built atop scrapped 118/119.

## Context

### Background

palinex (palin- "again" + nexus "bond") is a separate project shipped 2026-05-22. It's not part of nexus; nexus depends on it. The full architecture rationale lives in palinex's RDR-001 — covering:

- a2ui v0.9 as IR
- Typed Python builders (`Surface`, `DataPath`, `FunctionCall`, `Event`)
- Single-file lit-html renderer covering 14 of 18 Basic Catalog components
- Three delivery shapes (MCP UI resource, embedded artifact, external URL)
- postMessage RPC host-bridge protocol
- Action allowlist (`openUrl`, `copyToClipboard`, `openChash`)
- Markdown sidecar discipline (always emit)

This RDR does not re-derive that. It addresses nexus's *consumption* of palinex.

### Technical environment

- palinex 0.0.1 on PyPI, Apache-2.0
- Renderer at `https://hellblazer.github.io/palinex/`
- nexus license is AGPL-3.0-or-later; Apache-2.0 dep is compatible
- Existing nexus MCP tools that emit text returns can opt in to also emit surface payloads without changing their signatures

### Constraints

- **palinex pinned as a runtime dep.** Semver range `palinex >= 0.0.1, < 0.1`. nexus tests run against the pinned version.
- **No nexus-side IR.** All payload construction goes through palinex builders.
- **Markdown sidecar always.** Inherited from palinex RDR-001 §5; producers emit both.
- **Action allowlist enforced producer-side.** v1 supports the three actions palinex RDR-001 §7 names. Adding nexus-specific actions (e.g., `runSkill`, `openBead`) requires extending the host bridge AND opting in per producer.

## Decision

Adopt **palinex** as nexus's surface-emission and rendering library. Define the integration shape (host-bridge wiring, pilot producers, broker location). Mark **RDR-123 and RDR-124 as superseded** by this RDR.

### Approach

**Item 1: Add palinex as a runtime dependency.**

`pyproject.toml`: `palinex>=0.0.1,<0.1`. Optional `palinex[validate]` for deep producer-side validation in CI. Lockfile (`uv.lock`) regenerated.

**Item 2: Define `nexus/surfaces/` module.**

Internal package re-exporting `palinex.Surface`, `palinex.DataPath`, etc., plus:
- `nexus.surfaces.broker.SurfaceBroker` — Protocol matching the shape palinex RDR-001 §3 hints at (`put_cell`, `take_cell`, `subscribe`, `post_action`)
- `nexus.surfaces.broker.InProcessBroker` — dict-backed v1 impl, ~150 LOC, session-scoped retention
- `nexus.surfaces.delivery.wrap_as_mcp_ui_resource(surface, action_handlers)` — helper that produces the MCP UI resource shape Claude Code consumes, with the host-bridge wrapper embedded
- `nexus.surfaces.host_bridge.route(method, params)` — server-side handler for postMessage requests routed through MCP

The broker port is the bolt point: when RDR-120 successor lands and the tuple-space substrate exists, swap `InProcessBroker` for `TupleSpaceBroker`. Producers and renderers don't change.

**Item 3: Pilot producer — `nx_answer response_format` parameter.**

Add `response_format: Literal["markdown", "surface", "both"]` to `nx_answer`. Default `"markdown"` (unchanged behavior). `"surface"` constructs a citation surface via palinex builders; `"both"` returns both. Round-trip CI gate: a 10-question test corpus where `to_markdown(surface_from(intermediates))` equals the markdown variant.

**Item 4: Pilot subagent — codebase-deep-analyzer surface emission.**

Highest-token subagent (typical return: 600–1500 tokens). Opt-in: subagent imports `palinex.Surface`, builds findings as a structured surface (module map as List, dependencies as Tabs, open questions as List of Card), emits to `InProcessBroker` keyed by a generated `surface_id`, returns to orchestrator: `surface_id` pointer + 2–3 sentence summary + top 3–5 findings as bullets. Orchestrator fetches the surface (via broker) only when detail is needed.

Target: ≥40% reduction in Agent-tool return-value tokens vs. prose baseline.

**Item 5: MCP UI resource delivery for `nx_answer` surface mode.**

When `response_format="surface"` (or `"both"`), the MCP tool returns:

```python
{
    "type": "resource",
    "resource": {
        "uri": f"ui://nexus/surface/{surface_id}",
        "mimeType": "text/html",
        "text": wrap_as_mcp_ui_resource(surface, action_handlers=...),
    },
}
```

The wrapper is an HTML document that:
1. Embeds palinex's renderer (either inline from CDN or hosted at `hellblazer.github.io/palinex/`)
2. Pre-loads the surface payload via `postMessage({type: "a2ui.load", payload})`
3. Implements the host side of palinex's `a2ui.request`/`a2ui.response` protocol — routing `openChash` requests through `mcp__plugin_nx_nexus__store_get_many`, future actions through their respective MCP tools

**Item 6: Action registry with nexus-specific extensions.**

palinex RDR-001 §7 names three v1 actions; nexus adds (initially):

| Action | Resolves via | Trust |
|---|---|---|
| `openChash` | `mcp__plugin_nx_nexus__store_get_many` then display chunk | nexus daemon trust gates (RDR-113 successor when available) |
| `openBead` | `bd show <id>` parsed for display | bead access via local CLI |
| `runSkill` | Deferred — requires per-skill allowlist | not in v1 |

Each new action gets a stanza in `nexus.surfaces.host_bridge.route` and a producer-side helper in `nexus.surfaces` (e.g., `nexus.surfaces.open_bead(bead_id)` returns a palinex `FunctionCall`).

**Item 7: Supersession bookkeeping.**

- RDR-123 status → `superseded`, `superseded_by: RDR-127`, `superseded_date: 2026-05-22`
- RDR-124 status → `superseded`, `superseded_by: RDR-127`, `superseded_date: 2026-05-22`
- Both files get a tombstone note at the top pointing here.
- T2 `nexus_rdr/RDR-123` and `RDR-124` entries updated.

**Item 8: Documentation.**

- `docs/surfaces.md` — nexus-side guide on how producers emit surfaces (uses palinex builders, registers via `nexus.surfaces`, returns via MCP UI resource pattern)
- README pointer to palinex
- `nexus.surfaces` module docstrings cross-reference palinex RDR-001

## Alternatives Considered

### Alt 1: Vendor palinex into nexus

Copy the code into `nexus/surfaces/_vendored/palinex/` rather than depending on the package. Rejected because:
- palinex is small and OSS — vendoring forks the maintenance burden
- semver dep gives a clear upgrade path
- bus-factor concern is real but addressed by `Apache-2.0` license (forkable if maintenance lapses)

### Alt 2: Reimplement palinex's architecture inside nexus

The original RDR-127 sketch (before this split). Rejected because:
- duplicates work
- couples nexus to the specific shape palinex chose
- prevents other projects (separate from nexus) from using the same library

### Alt 3: Skip palinex; wait for RDR-120 substrate

The path that scrapped RDR-118/119. Rejected because surface rendering doesn't depend on substrate work — producer + renderer + bridge close the loop today.

## Trade-offs

### Consequences

- **(+)** External dep is small, versioned, Apache-2.0, OIDC-published — easier to update than vendored code
- **(+)** palinex evolves independently; nexus pins versions
- **(+)** The interesting design lives in one place (palinex RDR-001), not duplicated across nexus
- **(+)** Supersedes two tombstone-shaped nexus RDRs cleanly
- **(−)** External dep adds one supply-chain edge; mitigated by version pin and OIDC publisher
- **(−)** Bus-factor on palinex (single maintainer); mitigated by license + small surface

### Risks and Mitigations

- **Risk:** palinex changes break nexus integration.
  **Mitigation:** semver range pin; CI runs against the pinned version; upgrade via deliberate PR.

- **Risk:** Sub-agent surface emission accumulates state in `InProcessBroker` and grows unbounded.
  **Mitigation:** session-scoped TTL; broker capacity limit with LRU eviction.

- **Risk:** Action registry grows organically without trust review.
  **Mitigation:** every new action requires an RDR (or a `## Approach` item in an existing RDR) before it lands.

### Failure modes

- *Visible:* `nx_answer response_format=surface` against a host that doesn't render → markdown sidecar displays (palinex discipline)
- *Visible:* `openChash` action with chash not in T3 → host-bridge returns error; renderer shows error modal
- *Silent:* `InProcessBroker` capacity exceeded — surfaces silently evicted (mitigation: log + warn at 75% capacity)

## Implementation Plan

### Prerequisites

- [x] palinex 0.0.1 on PyPI (shipped 2026-05-22)
- [x] palinex renderer at `hellblazer.github.io/palinex/` (shipped 2026-05-22)
- [x] palinex RDR-001 published (shipped 2026-05-22)
- [ ] RDR-127 accepted

### Phase 1: Supersession + dep landing

Items 1, 7, 8.

- Tombstone RDR-123 and RDR-124 (this PR)
- Add `palinex` to `pyproject.toml`, regenerate `uv.lock`
- Author `docs/surfaces.md` integration guide

### Phase 2: `nexus.surfaces` module

Items 2, 5, 6 (initial).

- `nexus/surfaces/__init__.py` re-exporting palinex symbols + nexus helpers
- `nexus/surfaces/broker.py` — Protocol + `InProcessBroker`
- `nexus/surfaces/delivery.py` — `wrap_as_mcp_ui_resource()`
- `nexus/surfaces/host_bridge.py` — server-side action routing
- Tests in `tests/test_surfaces_broker.py`, `tests/test_surfaces_delivery.py`

### Phase 3: `nx_answer` pilot

Item 3.

- Add `response_format` parameter
- Surface construction from existing intermediates
- Round-trip CI gate with 10-question corpus

### Phase 4: Subagent pilot

Item 4.

- codebase-deep-analyzer opts in
- Returns pointer + summary
- Measure token reduction

### Day 2 Operations

- palinex updates: deliberate PRs after evaluating CHANGELOG
- New actions: RDR or §Approach item before adding
- Broker swap to tuple-space: when RDR-120 successor lands

## Test Plan

- **Scenario:** `nx_answer` with `response_format="surface"` emits valid v0.9 envelope + lossless markdown sidecar.
- **Scenario:** `wrap_as_mcp_ui_resource` produces HTML that loads in a sandboxed iframe and renders the surface.
- **Scenario:** Renderer dispatches `openChash`; host-bridge resolves via nexus MCP; result returns to renderer.
- **Scenario:** Subagent emits surface; orchestrator return value contains pointer + summary; broker fetch returns the full surface.
- **Scenario:** Producer emits unknown action; host-bridge logs but does not execute.

## Validation

palinex RDR-001 covers the architectural validation. nexus-side validation focuses on the integration glue:

- `nexus.surfaces.broker.InProcessBroker` semantics (put/take/subscribe/post_action)
- MCP UI resource shape matches what Claude Code expects
- Round-trip markdown ↔ surface for nexus producers
- Token-reduction measurement for the subagent pilot

## Finalization Gate

(deferred — sketch only)

### Assumption verification

- [ ] **A1** — palinex 0.0.x is API-stable enough to pin without churn through Phase 4.
  **Method:** review palinex CHANGELOG; pin to a known-good version.
- [ ] **A2** — Claude Code's MCP UI resource consumes the wrapper shape we emit.
  **Method:** smoke test in Claude Code with a representative payload.
- [ ] **A3** — Token reduction target (≥40%) for the subagent pilot is achievable.
  **Method:** measure on representative codebase-deep-analyzer dispatches before extending to other subagents.

### Cross-cutting concerns

- **Versioning:** palinex pinned via semver range in `pyproject.toml`
- **Build tool compatibility:** standard `uv` workflow, no special handling
- **Licensing:** Apache-2.0 (palinex) compatible with AGPL-3.0-or-later (nexus)
- **Deployment model:** runtime Python dep; no service deployment
- **IDE compatibility:** unchanged; MCP UI resource rendering happens in the host
- **Incremental adoption:** opt-in per producer; default behavior unchanged
- **Secret/credential lifecycle:** unchanged — daemon URLs and tokens stay where they already live
- **Memory management:** `InProcessBroker` session-scoped TTL + LRU eviction

### Proportionality

Small RDR. The interesting work lives in palinex (separate project, separate RDR). This RDR is purely the nexus-side integration shape.

## References

- **palinex RDR-001** — the substantive architecture this RDR depends on: <https://github.com/Hellblazer/palinex/blob/main/docs/rdr/rdr-001-architecture.md>
- palinex repo — <https://github.com/Hellblazer/palinex>
- palinex on PyPI — <https://pypi.org/project/palinex/>
- a2ui v0.9 spec — <https://a2ui.org/specification/v0.9-a2ui/>
- T3 entries:
  - `architecture-a2ui-overview`
  - `a2ui-design-philosophy-stack-positioning`
  - `surface-renderer-html-tool-patterns-for-nexus`
- RDR-053 — Xanadu fidelity (chash addresses; informs `openChash` action)
- RDR-118 — Surfaces as Tuples (scrapped); Cell shape adopted into palinex IR
- RDR-119 — Cockpit UI Fabric (scrapped); per-host catalog discipline informs eventual bolt
- RDR-120 — Storage Substrate Split; eventual substrate replaces `InProcessBroker`
- RDR-122 — LLM-JSON Repair Pass (orthogonal, still valid)
- RDR-123, RDR-124 — superseded by this RDR

## Revision History

_2026-05-22 — initial sketch. Originally drafted as a single RDR carrying both architecture and integration; split out architecture into palinex RDR-001 on the same day. This RDR is the integration-only successor._
