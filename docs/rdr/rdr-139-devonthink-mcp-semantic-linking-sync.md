---
title: "DEVONthink MCP Semantic-Linking and Bidirectional Sync: Consume DT AI Tools for Graph Edges and Write-Back"
id: RDR-139
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-29
accepted_date:
related_issues: [nexus-qtbuh, nexus-lxy5n]
related_rdrs: [RDR-099, RDR-126, RDR-049, RDR-051, RDR-089]
---

# RDR-139: DEVONthink MCP Semantic-Linking and Bidirectional Sync

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-099 shipped first-class DEVONthink ingest (`nx dt index`, `nx dt open`)
over a synchronous osascript layer (`src/nexus/devonthink.py:104`
`_run_osascript`). Ingest is one-way and metadata-only: a DEVONthink record
is chunked, stamped with `source_uri = x-devonthink-item://<UUID>`, and
left as an island. Two capabilities that only DEVONthink's AI surface can
provide are missing, and both require nexus to consume a DEVONthink MCP
server (official built-in, shipped 2026-05-26 for macOS Sequoia+, or the
community `dvcrn/mcp-server-devonthink`). The gjz52 evaluation
(T2: `nexus/gjz52-devonthink-mcp-eval-2026-05-28`) adjudicated that the
selector/CRUD layer must stay on osascript (migrating it to MCP is
net-negative); only the AI tools (`compare`/`classify`) and write-back
justify MCP-client plumbing. This RDR designs that plumbing and the two
layers it enables.

### Enumerated gaps to close

#### Gap 1: Papers with no bibliographic match get zero graph edges

The catalog auto-linker is metadata-only. `generate_citation_links`
(`src/nexus/catalog/link_generator.py:24`) keys off
`bib_semantic_scholar_id` / `bib_openalex_id`; `auto_link`
(`src/nexus/catalog/auto_linker.py:83`) consumes seed `relates` contexts.
A document with no Semantic Scholar / OpenAlex match has no
semantic-neighbour edge source at all. Observed during the 2026-05-27
incorporation of the MemForest paper (`x-devonthink-item://886082AB-…`): it
produced **zero** links and sat unconnected to its obvious agent-memory
peers in `knowledge__dt-papers`. DEVONthink's `compare` (hybrid similarity)
and `classify` (DT AI group proposals) are an edge source nexus cannot get
from osascript — they are DT-AI MCP tools, not plain AppleScript. The fix
delivers `relates` candidate edges for any indexed DT record, bib-matched
or not.

#### Gap 2: Incorporation is invisible on the DEVONthink side

After `nx dt index` runs, DEVONthink has no record that nexus indexed,
enriched, linked, or assigned a tumbler to the item. There is no write-back
of `nx-indexed` / tumbler / aspect-keyword tags or annotations, and no
capture flow (`create_from_url` → DT record → nexus index). A user working
inside DEVONthink cannot see which items are in the knowledge graph or
navigate to their nexus identity. The fix makes incorporation
bidirectional: nexus stamps DT-side metadata after a successful
index+enrich, under an explicit authoritative-source contract, respecting
the per-item "Exclude from Chat & MCP" privacy flag.

#### Gap 3: nexus has no MCP-client substrate

nexus has never consumed an external MCP server. A repository search for
`ClientSession` / `stdio_client` / `StdioServerParameters` in `src/nexus/`
returns nothing — every existing integration (Voyage, ChromaDB,
Semantic Scholar, osascript) is a direct synchronous client. Layers 2 and 3
both require an MCP client running inside a synchronous CLI path
(`nx dt index`), which introduces async/event-loop coupling, a server
lifecycle (spawn/connect/teardown), and a transport choice (the official
built-in server vs a community stdio server). This is a cross-cutting
substrate gap that the draft RDR-126 (Qwen-MCP figure augmentation) shares;
whichever lands first should establish the reusable pattern.

## Context

### Background

`nx dt` shipped under RDR-099 (accepted; `src/nexus/commands/dt.py`,
`src/nexus/devonthink.py`). The incorporation pain that motivated this RDR
is captured in nexus-lxy5n: indexing the MemForest paper required a manual
five-step dance (index → stamp → enrich bib → enrich aspects → link), and
the final linking step produced nothing because the paper had no bib match.
nexus-gjz52 evaluated DT-MCP-vs-osascript per layer and recommended: keep
osascript for selectors (Layer 1), promote `compare`/`classify` linking
(Layer 2) and bidirectional sync (Layer 3) to this RDR.

### Technical Environment

- **DEVONthink**: built-in MCP server (DEVONtechnologies, 2026-05-26;
  macOS Sequoia+, Settings → AI → MCP). Community alternative:
  `dvcrn/mcp-server-devonthink` (16 JXA tools incl. `search`, `lookup`,
  `create_record`, `classify`, `compare`, `add_tags`, `get_record_content`).
- **nexus catalog**: `Catalog.link` (`catalog.py:1846`),
  `Catalog.link_if_absent` (`catalog.py:1865`, idempotent, `created_by`
  attribution). Documents addressed by tumbler; DT records carry
  `source_uri = x-devonthink-item://<UUID>`.
- **Linking today**: `generate_citation_links` (bib-keyed `cites`),
  `auto_link` (seed-context `relates`). Both idempotent via
  `link_if_absent`.
- **Aspect extraction**: RDR-089 structured aspects; the reliable path is
  the synchronous `nx enrich aspects` (the ingest-hook queue is
  WAL-contended — see nexus-lxy5n).
- **Python MCP client**: the official `mcp` SDK (`mcp.client.stdio`,
  `ClientSession`) — not currently a nexus dependency.

## Research Findings

### Investigation

Grounded in current `develop`:
- `src/nexus/devonthink.py:104` — `_run_osascript(script, timeout)`,
  synchronous `subprocess.run`. The entire selector/CRUD surface routes
  through this; no async, no long-lived process.
- `src/nexus/catalog/link_generator.py:24-65` — `generate_citation_links`
  iterates `bib_semantic_scholar_id` / `bib_openalex_id` and calls
  `cat.link_if_absent(from_tumbler, to_tumbler, "cites",
  created_by="bib_enricher")`. Confirms the metadata-only edge source and
  the exact idempotent link API Layer 2 will reuse.
- `src/nexus/catalog/auto_linker.py:53-83` — `read_link_contexts` →
  `auto_link`, default `link_type="relates"`. The seed-context path Layer 2
  parallels.
- `src/nexus/commands/dt.py:69-142` — `_index_record` and
  `_stamp_dt_uri_on_entry` establish the UUID↔tumbler join Layer 2 needs to
  map DT neighbours onto catalog nodes.
- No `ClientSession` / `stdio_client` anywhere under `src/nexus/` —
  confirming Gap 3.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| DEVONthink built-in MCP | No (opaque service) | Tool surface, transport, and reachability-from-CLI unverified — **spike required** |
| `dvcrn/mcp-server-devonthink` | No (not yet cloned) | Advertised 16 JXA tools incl. `compare`/`classify`/`add_tags`; needs source clone to confirm I/O shapes |
| `mcp` Python SDK | No | `ClientSession` + stdio transport assumed standard; verify against SDK source before locking |
| nexus `Catalog.link_if_absent` | Yes | `catalog.py:1865`, idempotent, `created_by` attribution — reusable as-is for Layer 2 |

### Key Discoveries

- **Documented** — the catalog has a single idempotent edge primitive
  (`link_if_absent`) already used by two generators; Layer 2 is a third
  generator, not new link machinery.
- **Documented** — nexus has no MCP-client substrate; this RDR (or RDR-126)
  must introduce it.
- **Assumed** — DT `compare` returns neighbour records with UUIDs that map
  1:1 onto `x-devonthink-item://<UUID>` `source_uri` values already in the
  catalog. Needs a spike.
- **Assumed** — the official built-in DT MCP server is reachable from a
  nexus CLI subprocess (not only from Claude Desktop / the DT app's own AI
  client). This is the highest-risk unknown.

### Critical Assumptions

All four verified by the 2026-05-29 spike against the live DEVONthink 4
built-in MCP server (see Spike Results below).

- [x] **DT similarity/classify are reachable from a nexus CLI MCP client
  and return UUID-bearing neighbour lists.** — **Status**: Verified —
  **Method**: Spike. The nexus venv connected to `http://localhost:8420/mcp`
  from a CLI subprocess; `find_similar_records` returned ranked
  `{score, uuid, name}` neighbours (0.52–0.60), `classify_record` returned
  uuid-keyed group proposals. Tool names corrected: `find_similar_records`
  (not `compare`), `classify_record`.
- [x] **DT neighbour UUIDs map to catalog entries via
  `source_uri = x-devonthink-item://<UUID>`** (un-indexed records skipped,
  not errors). — **Status**: Verified — **Method**: Spike. Every DT record
  (`search_records`, `find_similar_records`, `classify_record`) is
  uuid-addressed; the join is `x-devonthink-item://<uuid>`. Catalog-side
  match is a `source_uri` lookup; non-catalog neighbours are simply absent
  from the result map.
- [x] **The per-item "Exclude from AI & MCP" flag removes a record
  server-side**, so nexus need not re-filter. — **Status**: Verified —
  **Method**: Docs (DT MCP appendix): "items excluded from AI & MCP access
  are fully ignored"; sensitive data (credit cards, passwords) is stripped
  before the LLM (server `redaction` categories: credit_card, auth_tokens,
  labeled_secrets, url, email).
- [x] **A single DT MCP server covers both layers.** — **Status**: Verified
  — **Method**: Spike + Config. The built-in server exposes 59 tools
  including `find_similar_records` + `classify_record` (Layer 2) and
  `set_record_tags` + `set_record_annotation` (Layer 3). The community
  `dvcrn` server is unnecessary.

**Method definitions**: Source Search = API verified against dependency
source; Spike = behaviour verified against the live service (DT is opaque,
so spikes are mandatory here); Docs Only = insufficient for load-bearing
assumptions.

### Spike Results (2026-05-29)

Verified against DEVONthink 4 (`com.devon-technologies.think`, built-in MCP
LoginItem `DEVONthink MCP.app`):

- **Transport is HTTP, not stdio** — the built-in server is a persistent
  localhost HTTP MCP endpoint at `http://localhost:8420/mcp`
  (`mcp-config-default.json`: `port 8420`, `access localhost`,
  `tlsIdentity ""` → plain HTTP, `auth.required false`,
  `devonthink.launchIfNeeded true`). nexus connects as an HTTP MCP client
  (`mcp.client.streamable_http`) to an always-on server — **no process
  spawn, no stdio, no teardown lifecycle**. This invalidates the original
  stdio design (Gap 3 / `devonthink_mcp.py`) and simplifies it.
- **`mcp` Python SDK** is importable in the nexus venv and drove the live
  handshake — the only new dependency.
- **Bonus tools that reshape scope** (in the same 59-tool server):
  - `get_record_links` — DT's own record link graph (linked to/from): a
    second, higher-precision edge source than similarity alone.
  - `resolve_doi_metadata` / `search_crossref` / `download_pdf_from_doi` /
    `resolve_google_books_metadata` — overlap nexus's Semantic-Scholar
    bib-enrich (relevant to nexus-lxy5n; potential alternative/secondary
    enrichment source via DT's CrossRef path).
  - `capture_web_page` — the `create_from_url` capture flow Layer 3
    deferred; it is one tool call, not bespoke orchestration.
  - `research_topic`, `chat_response`, `extract_record_content` (AI-
    optimised text), `get_record_custom_metadata` (write-back target richer
    than tags alone).

## Proposed Solution

### Approach

Introduce a minimal, lazily-constructed MCP-client helper and two
incorporation stages that consume it. Selectors stay on osascript.

1. **MCP-client substrate (Gap 3).** A small `nexus/devonthink_mcp.py`
   module wrapping the `mcp` SDK **HTTP** client
   (`mcp.client.streamable_http`) against the always-on built-in server at
   `http://localhost:8420/mcp` (spike-verified; endpoint + port
   config-overridable via `devonthink.mcp.url`). Each call: connect → one
   tool call → close, bridged into the synchronous CLI via `asyncio.run` at
   the call boundary. No process spawn or teardown lifecycle — the server
   is a persistent DEVONthink LoginItem, and `launchIfNeeded` starts
   DEVONthink itself if closed. `auth.required` is false on localhost.
   Fail-soft: if the server is unreachable, Layer 2/3 log and skip — never
   abort an index.

2. **Layer 2 — semantic linking (Gap 1).** On `nx dt index` (opt-in
   `--link-semantic`, later default once precision is trusted), after the
   record is indexed and has a tumbler: call DT `compare` for the record,
   take the top-k neighbours above a similarity floor, map each neighbour
   UUID → catalog tumbler via `source_uri`, and create `relates` edges with
   `cat.link_if_absent(from, to, "relates",
   created_by="dt_similar")`. Neighbours not in the catalog are skipped (or
   optionally surfaced as "index candidates"). This is the semantic-linking
   sub-step nexus-lxy5n's auto-link phase calls.

3. **Layer 3 — bidirectional sync (Gap 2).** After a successful
   index+enrich (opt-in `--writeback`): stamp DT-side metadata via
   `add_tags` (`nx-indexed`, `nx-tumbler:<t>`, top aspect keywords) and an
   annotation linking back to the tumbler. Governed by an authoritative-
   source contract: nexus owns the knowledge-graph metadata it writes; it
   never edits DT user content. Respects "Exclude from Chat & MCP"
   (server-enforced per CA-3). A `create_from_url` capture flow (URL → DT
   record → `nx dt index`) is **deferred** to a follow-up phase to keep the
   MVV tight.

### Technical Design

Tool I/O verified by spike (DT MCP `find_similar_records` returns
`{count, results:[{score, uuid, name, doi, ...}]}`; `classify_record`
returns uuid-keyed group records):

```text
// devonthink_mcp.py — HTTP MCP client to http://localhost:8420/mcp
def dt_find_similar(uuid, *, limit, floor) -> list[Neighbour]   # asyncio.run bridge
//   Neighbour = {uuid: str, score: float, name: str}
def dt_set_tags(uuid: str, tags: list[str], *, mode="add") -> bool
def dt_set_annotation(uuid: str, text: str) -> bool

// linking (Layer 2), reuses existing catalog primitive
for n in dt_find_similar(uuid, limit=K, floor=F):
    to_tumbler = catalog.tumbler_for_source_uri(f"x-devonthink-item://{n['uuid']}")
    if to_tumbler:
        cat.link_if_absent(this_tumbler, to_tumbler, "relates", created_by="dt_similar")
```

Error contract: every DT-MCP call returns a result-or-None; callers treat
None as "skip this enhancement," log structured, and continue. No DT-MCP
failure may fail an index or an enrich.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| MCP-client helper | (none) | New — `nexus/devonthink_mcp.py`; coordinate shape with RDR-126 |
| `relates` edge writer | `catalog.py:link_if_absent` | Reuse as-is (`created_by="dt_similar"`) |
| Semantic-link generator | `link_generator.py` / `auto_linker.py` | Extend pattern (third generator), do not modify existing ones |
| UUID↔tumbler join | `dt.py:_select_dt_uri_from_entry`, `_resolve_dt_uri_from_tumbler` | Reuse; add the inverse `source_uri → tumbler` lookup if absent |
| Selector/CRUD | `devonthink.py:_run_osascript` | Keep (gjz52: do not migrate) |

### Decision Rationale

Reusing `link_if_absent` makes Layer 2 a thin generator over a proven
idempotent primitive — the only genuinely new code is the MCP client and
the compare→tumbler mapping. Opt-in flags gate precision-sensitive edges
behind explicit user intent until `find_similar_records` precision is measured.
Fail-soft keeps the DT-AI dependency strictly additive: a missing/asleep DT
app degrades to today's metadata-only behaviour, never a broken index.

## Alternatives Considered

### Alternative 1: Migrate the whole DT layer (selectors + AI) to MCP

**Description**: Replace osascript with the DT MCP server for everything.
**Pros**: one transport. **Cons**: gjz52 showed selectors gain nothing and
lose a synchronous, lifecycle-free path; adds async coupling to every
`nx dt` call. **Reason for rejection**: net-negative for Layer 1; settled
in gjz52.

### Alternative 2: Approximate semantic neighbours with nexus's own vectors

**Description**: Use T3 cosine similarity instead of DT `compare`.
**Pros**: no MCP dependency. **Cons**: nexus already has this and it is not
the gap — DT `compare` is hybrid (DT's own AI + classification) over the
user's whole database including non-indexed items, surfacing neighbours
nexus has never seen. **Reason for rejection**: solves a different, smaller
problem; loses DT's cross-database reach.

### Briefly Rejected

- **Ship `create_from_url` capture in v1**: expands scope and lifecycle
  surface; deferred to a follow-up phase.

## Trade-offs

### Consequences

- (+) Any DT-indexed paper gets `relates` edges regardless of bib match.
- (+) DEVONthink becomes navigable into the knowledge graph (write-back).
- (+) Establishes the reusable MCP-client substrate (shared with RDR-126).
- (−) New runtime dependency on a running DEVONthink app + MCP server for
  the enhanced path (mitigated by fail-soft).
- (−) Async/event-loop bridging in a sync CLI (contained to one module).

### Risks and Mitigations

- **Risk**: the built-in DT MCP is only exposable to Claude Desktop, not a
  CLI subprocess. **Mitigation**: spike both servers; the community stdio
  server is the fallback transport.
- **Risk**: `find_similar_records` precision is low → noisy `relates` edges.
  **Mitigation**: opt-in flag, similarity floor, `created_by="dt_similar"`
  so edges are filterable/revocable; measure precision before defaulting on.
- **Risk**: privacy leak via `get_record_content` on excluded items.
  **Mitigation**: CA-3 spike verifies server-side enforcement before any
  content read.

### Failure Modes

DT app closed / MCP unreachable → Layer 2/3 skipped, structured log,
index succeeds (visible: no `dt_similar` edges, a one-line warning).
Wrong-server config → connect fails fast at first call, skipped same as
above. Silent risk: a UUID→tumbler mapping bug could create `relates` edges
to the wrong node — mitigated by `created_by` attribution enabling a bulk
audit/revoke.

## Implementation Plan

### Prerequisites

- [ ] All four Critical Assumptions verified via spike against a live
  DEVONthink install.
- [ ] Server choice (built-in vs community) locked from the spike.

### Minimum Viable Validation

`nx dt index --uuid <MemForest-UUID> --link-semantic --writeback` on the
real `knowledge__dt-papers` database: the MemForest paper gains ≥1
`relates` edge to an agent-memory peer (the edge it could not get from bib
matching), and the DT record shows an `nx-indexed` + `nx-tumbler:<t>` tag.
This is in scope, not deferred.

### Phase 1: Code Implementation

#### Step 1: MCP-client substrate (`devonthink_mcp.py`) + config + spike harness
#### Step 2: Layer 2 `find_similar_records` → `relates` generator, wired into `nx dt index --link-semantic`
#### Step 3: Layer 3 `add_tags`/annotation write-back, wired into `nx dt index --writeback`

### Phase 2: Operational Activation

Config keys documented; opt-in flags default off until precision measured.
`create_from_url` capture flow deferred to a Phase 3 follow-up RDR section.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `relates` edges (`created_by=dt_similar`) | `catalog_link_query` | `catalog_links` | `link delete` by creator | doctor link census | catalog JSONL |
| DT-side `nx-*` tags | DT search | DT record | `add_tags` removal verb (deferred) | spot-check | DT's own backup |

### New Dependencies

`mcp` Python SDK (MIT) — first MCP-client dependency for nexus. License
benign; no legal review needed.

## Test Plan

- **Scenario**: `find_similar_records` returns neighbours, 2 of 3 are catalog-known —
  **Verify**: exactly 2 `relates` edges created, 1 skipped, idempotent on
  re-run.
- **Scenario**: DT MCP unreachable — **Verify**: index succeeds, zero
  edges, one structured warning, exit 0.
- **Scenario**: neighbour UUID not in catalog — **Verify**: skipped, no
  error.
- **Scenario**: excluded-from-MCP record — **Verify**: never appears as a
  neighbour and `get_record_content` refused (CA-3).
- **Scenario**: `--writeback` on a real record — **Verify**: `nx-indexed` +
  `nx-tumbler:<t>` tags present, user content untouched, idempotent.

## Validation

### Testing Strategy

Unit: UUID→tumbler mapping, edge idempotency, fail-soft on None, flag
gating (all mockable against a fake DT-MCP client). Integration/spike: the
MVV against a live DEVONthink install (mandatory — DT is opaque). "Done" =
MVV passes and all five Test Plan scenarios are green.

### Performance Expectations

One `compare` call per indexed record; negligible against index/enrich
cost. No throughput target — measure `find_similar_records` precision empirically
before defaulting `--link-semantic` on (default off until precision measured).

## Finalization Gate

### Contradiction Check

To be completed at gate.

### Assumption Verification

All four Critical Assumptions are Unverified pending the spike; none may be
Docs-Only at accept time (DT is opaque). Verification plan: a single spike
session against a live DEVONthink Sequoia install exercising `compare`,
`classify`, `add_tags`, and an excluded-item probe.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `compare` / `classify` | DT MCP | Spike (pending) |
| `add_tags` / annotate | DT MCP | Spike (pending) |
| `ClientSession` stdio | `mcp` SDK | Source Search (pending) |
| `link_if_absent` | nexus catalog | Source Search (done) |

### Scope Verification

MVV (MemForest gains a semantic edge + a write-back tag) is in scope and
executed during implementation, not deferred.

### Cross-Cutting Concerns

- **Versioning**: new opt-in flags; no migration. N/A schema.
- **Build tool compatibility**: adds `mcp` SDK to `pyproject.toml`.
- **Licensing**: `mcp` SDK MIT — benign.
- **Deployment model**: enhanced path needs a running DEVONthink + MCP
  server; fail-soft preserves the base path.
- **IDE compatibility**: N/A.
- **Incremental adoption**: opt-in flags, default off.
- **Secret/credential lifecycle**: none (local app MCP, no keys).
- **Memory management**: one short-lived MCP session per call; explicit
  teardown.

### Proportionality

Right-sized: the only new substrate is one MCP-client module; both layers
reuse existing catalog primitives. Trim if the spike shows the built-in
server is CLI-unreachable (then the RDR narrows to the community server).

## References

- nexus-qtbuh (this RDR's source), nexus-lxy5n (incorporation pipeline)
- T2: `nexus/gjz52-devonthink-mcp-eval-2026-05-28` (per-layer decision)
- RDR-099 (DT integration substrate), RDR-126 (Qwen-MCP, shared MCP-client
  substrate), RDR-049/051 (catalog + link lifecycle), RDR-089 (aspects)
- `src/nexus/devonthink.py`, `src/nexus/commands/dt.py`,
  `src/nexus/catalog/link_generator.py`, `src/nexus/catalog/auto_linker.py`,
  `src/nexus/catalog/catalog.py`
- `dvcrn/mcp-server-devonthink`; DEVONthink built-in MCP (Settings → AI → MCP)

## Revision History

### 2026-05-29 — Critical Assumption spike (draft)

Spiked all four Critical Assumptions against the live DEVONthink 4 built-in
MCP server. All verified. Material corrections folded into the design:

- **Transport**: HTTP `http://localhost:8420/mcp` (persistent LoginItem),
  not a nexus-spawned stdio server. `devonthink_mcp.py` becomes an HTTP MCP
  client with no process lifecycle — simpler than the draft.
- **Tool names**: `find_similar_records` (was "compare"), `classify_record`.
  Edge `created_by` → `dt_similar`.
- **Server choice**: built-in only; community `dvcrn` server dropped.
- **Scope inputs surfaced**: `get_record_links` (DT's own link graph, a
  second edge source), `capture_web_page` (the deferred capture flow is one
  call), and DT's CrossRef/DOI tools (overlap nexus bib-enrich — note for
  nexus-lxy5n). To be triaged into phases at the gate.
