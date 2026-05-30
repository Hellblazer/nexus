---
title: "DEVONthink MCP Integration: Semantic Linking, Bibliographic Enrichment, Content Extraction, Bidirectional Sync, and Capture"
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

# RDR-139: DEVONthink MCP Integration

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-099 shipped first-class DEVONthink ingest (`nx dt index`, `nx dt open`)
over a synchronous osascript layer (`src/nexus/devonthink.py:104`
`_run_osascript`). Ingest is one-way and metadata-only: a DEVONthink record
is chunked, stamped with `source_uri = x-devonthink-item://<UUID>`, and left
an island — no semantic edges, no DT-side visibility, no use of DEVONthink's
AI, content-extraction, or bibliographic surfaces. DEVONthink 4 ships a
built-in MCP server (2026-05-26, macOS Sequoia+) exposing 59 tools over
localhost HTTP. The gjz52 evaluation
(T2: `nexus/gjz52-devonthink-mcp-eval-2026-05-28`) established that selectors
stay on osascript, but the AI / content / write-back surfaces are genuine new
capability. The 2026-05-29 spike (below) verified the server is reachable
from a nexus CLI process and the relevant tools behave as needed. This RDR
designs a comprehensive integration: a shared MCP-client substrate plus eight
capability layers, **every one optional with a tested fallback** — if
DEVONthink or its MCP server is absent, nexus behaves exactly as it does
today — phased so a tight first proof ships before the breadth.

### Enumerated gaps to close

#### Gap 0: The integration must be optional, with a tested fallback

DEVONthink is a per-user macOS app that may be closed, unlicensed for MCP, or
absent (CI, Linux, other users). Every capability here is therefore an
*enhancement*, never a dependency: if the DT MCP is unreachable, each layer
must degrade to nexus's existing behaviour (metadata-only index, Semantic-
Scholar-only enrichment, file-path-only extraction) with no error and no
partial corruption. This fallback is a first-class, separately-tested path —
not an incidental `try/except`. The fallback suite must pass with the DT MCP
forced unavailable.

#### Gap 1: Papers with no bibliographic match get zero graph edges

The catalog auto-linker is metadata-only. `generate_citation_links`
(`src/nexus/catalog/link_generator.py:24`) keys off `bib_semantic_scholar_id`
/ `bib_openalex_id`; `auto_link` (`src/nexus/catalog/auto_linker.py:83`)
consumes seed `relates` contexts. A document with no Semantic Scholar /
OpenAlex match has no semantic-neighbour edge source. Observed 2026-05-27
incorporating the MemForest paper (`x-devonthink-item://886082AB-…`): zero
links, unconnected to its agent-memory peers in `knowledge__dt-papers`. DT's
`find_similar_records` (hybrid similarity), `classify_record` (AI group
proposals), and `get_record_links` (DT's own link graph) are edge sources
nexus cannot get from osascript.

#### Gap 2: Incorporation is invisible on the DEVONthink side

After `nx dt index`, DEVONthink has no record that nexus indexed, enriched,
linked, or assigned a tumbler. No write-back of `nx-indexed` / tumbler /
aspect tags, annotations, or structured custom metadata. A user inside
DEVONthink cannot see what is in the knowledge graph or navigate to its
nexus identity.

#### Gap 3: nexus has no MCP-client substrate

nexus has never consumed an external MCP server (a repo search for
`ClientSession` / `stdio_client` in `src/nexus/` returns nothing). Every
capability layer here needs an MCP client inside a synchronous CLI path. This
substrate is shared with draft RDR-126 (Qwen-MCP); whichever lands first
establishes the reusable pattern.

#### Gap 4: Non-file-backed DT records are unreachably or poorly indexed

`nx dt index` routes through `nx index pdf|md` on the record's on-disk path.
Web archives, RTF, and other non-file-backed records have no clean file path
(or live under `Files.noindex`), so their text is lost or degraded. DT's
`extract_record_content` (AI-optimised text), `get_record_text`, `ocr_record`
(scanned PDFs/images), and `transcribe_record` (audio/video) provide the
content these records otherwise can't surface.

#### Gap 5: Bibliographic enrichment is single-sourced (Semantic Scholar only)

`nx enrich bib` resolves metadata via Semantic Scholar / OpenAlex only. A
DOI-bearing record with no S2/OpenAlex hit gets nothing. DT exposes
`resolve_doi_metadata` (CrossRef), `search_crossref`, and
`resolve_google_books_metadata` — a complementary enrichment source,
directly relevant to nexus-lxy5n's enrich stage.

#### Gap 6: DT annotations / highlights are not captured as knowledge

A user's PDF highlights and annotations in DEVONthink are first-class
scholarly signal. `extract_record_highlights` / `summarize_record_highlights`
and the `*_mentions` tools expose them; nexus ingests none of it today.

#### Gap 7: No capture-into-graph flow

There is no path from a URL or loose file to an indexed, linked knowledge-
graph node. `capture_web_page` (URL → DT record), `import_file`, and
`download_pdf_from_doi` (DOI → OA PDF → DT) are the missing front door.

## Context

### Background

`nx dt` shipped under RDR-099 (accepted). The incorporation pain is captured
in nexus-lxy5n: indexing the MemForest paper took a manual five-step dance
and still produced zero links (no bib match). gjz52 evaluated
DT-MCP-vs-osascript per layer: keep osascript for selectors; promote the AI /
write-back surface to this RDR.

### Technical Environment

- **DEVONthink 4** (`com.devon-technologies.think`), built-in MCP server as a
  LoginItem (`DEVONthink MCP.app`), HTTP at `http://localhost:8420/mcp`,
  `auth.required=false`, 59 tools. Spike-verified (below).
- **nexus catalog**: `Catalog.link_if_absent` (`catalog.py:1865`, idempotent,
  `created_by`). DT records carry `source_uri = x-devonthink-item://<UUID>`.
- **Linking today**: `generate_citation_links` (bib `cites`), `auto_link`
  (seed `relates`).
- **Aspects**: RDR-089; reliable path is synchronous `nx enrich aspects`.
- **`mcp` Python SDK** (`mcp.client.streamable_http`, `ClientSession`) — new
  dependency; spike-confirmed importable in the nexus venv.

## Research Findings

### Investigation

Grounded in current `develop`: `devonthink.py:104` (osascript, synchronous);
`link_generator.py:24-65` (bib-keyed `link_if_absent`); `auto_linker.py:53-83`
(seed `relates`); `commands/dt.py:69-142` (UUID↔tumbler join); no MCP client
anywhere in `src/nexus/`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| DEVONthink 4 built-in MCP | Yes (live spike + config + tools manifest) | HTTP localhost:8420, 59 tools, behaviour verified — see Spike Results |
| `mcp` Python SDK | Yes (live) | `streamable_http` HTTP client drove the handshake; importable in nexus venv |
| nexus `Catalog.link_if_absent` | Yes | `catalog.py:1865`, idempotent, `created_by` — reused as-is |

### Key Discoveries

- **Verified** — the DT built-in MCP is an always-on localhost HTTP server,
  CLI-reachable; no spawn/teardown. Corrects the original stdio assumption.
- **Verified** — `find_similar_records` returns ranked `{score, uuid, name}`;
  `classify_record` returns uuid-keyed group proposals; all DT records are
  uuid-addressed → catalog `source_uri` join is mechanical.
- **Verified** — one server covers every layer here (linking, enrichment,
  content, write-back, capture); the community `dvcrn` server is unnecessary.
- **Documented** — the catalog has one idempotent edge primitive
  (`link_if_absent`); every linking layer is a thin generator over it.

### Spike Results (2026-05-29)

Verified live against DEVONthink 4 built-in MCP (LoginItem
`DEVONthink MCP.app`, `mcp-config-default.json`):

- **Transport**: HTTP `http://localhost:8420/mcp` (`port 8420`,
  `access localhost`, `tlsIdentity ""` → plain HTTP, `auth.required false`,
  `launchIfNeeded true`). nexus connects as an HTTP MCP client — no process
  lifecycle.
- **Reachability**: the nexus venv connected from a CLI subprocess via
  `mcp.client.streamable_http`, `initialize` + `list_tools` = 59 tools.
- **Layer B tools**: `find_similar_records(uuid, limit)` →
  `{count, results:[{score, uuid, name, doi, …}]}`, live scores 0.52–0.60
  ranked; `classify_record(uuid)` → uuid-keyed group records;
  `get_record_links` present (DT's native link graph).
- **Privacy (CA3)**: DT MCP appendix doc — "items excluded from AI & MCP
  access are fully ignored"; server redaction strips credit_card,
  auth_tokens, labeled_secrets, url, email before the LLM.
- **Full surface confirmed** for enrichment (`resolve_doi_metadata`,
  `search_crossref`, `download_pdf_from_doi`, `resolve_google_books_metadata`),
  content (`extract_record_content`, `get_record_text`, `ocr_record`,
  `transcribe_record`, `extract_record_visuals`), highlights
  (`extract_record_highlights`, `summarize_record_highlights`,
  `*_mentions`), write-back (`set_record_tags`, `set_record_annotation`,
  `set_record_custom_metadata`), and capture (`capture_web_page`,
  `import_file`, `create_record`).
- **Read-only spike**: no write-back tool was executed against the user's
  database; write tools verified by presence + signature only.

### Critical Assumptions

All four verified by the 2026-05-29 spike.

- [x] **DT AI tools reachable from a nexus CLI MCP client, returning
  UUID-bearing results.** — Verified — Spike.
- [x] **DT UUIDs map to catalog entries via
  `source_uri = x-devonthink-item://<UUID>`** (un-indexed neighbours
  skipped). — Verified — Spike.
- [x] **"Exclude from AI & MCP" enforced server-side** so nexus need not
  re-filter. — Verified — Docs.
- [x] **One built-in server covers every layer**; community server
  unnecessary. — Verified — Spike + Config.

**Method definitions**: Source Search = verified against dependency source;
Spike = behaviour verified against the live service; Docs Only = insufficient
for load-bearing assumptions.

## Proposed Solution

### Approach

A shared MCP-client substrate plus eight capability layers. Selectors/CRUD
(Layer 1) stay on osascript per gjz52.

**Optionality invariant (Gap 0).** Every layer is gated and fail-soft: a
missing, closed, or MCP-disabled DEVONthink degrades to nexus's existing
behaviour with no error and no partial write. "DT enhances; it is never
required." This is enforced by a single capability gate
(`devonthink_mcp.available()`, a cached `is_running` + reachable probe) that
every layer consults before any call, and it is verified by a dedicated
fallback suite that runs every `nx dt` / enrich path with the DT MCP forced
unavailable and asserts the legacy result is byte-identical to pre-RDR-139.

- **Layer A — MCP-client substrate (Gap 3).** `nexus/devonthink_mcp.py`: an
  HTTP MCP client (`mcp.client.streamable_http`) to
  `http://localhost:8420/mcp` (config-overridable `devonthink.mcp.url`),
  bridged into the sync CLI via `asyncio.run` per call. `available()` gate
  (`is_running` + reachability, cached per-invocation). Result-or-None
  contract: any failure → log + skip, never abort. Shared with RDR-126.
- **Layer B — Semantic & structural linking (Gap 1).** On
  `nx dt index --link-semantic`: `find_similar_records` (above a similarity
  floor) + `get_record_links` (DT's explicit links, higher precision) +
  optionally `classify_record` (group → topic hint). Map each neighbour UUID
  → catalog tumbler → `cat.link_if_absent(this, to, "relates",
  created_by="dt_similar")` (DT-link mirror uses `created_by="dt_link"`).
  Fallback: no DT → existing metadata-only linking (zero semantic edges).
- **Layer C — Bibliographic enrichment (Gap 5).** `nx dt index --enrich`
  (and `nx enrich bib --source dt`): for a DOI-bearing record with no
  Semantic-Scholar hit, fall back to DT `resolve_doi_metadata` /
  `search_crossref` (and `resolve_google_books_metadata` for books). Stamps
  the same `bib_*` catalog fields the existing enricher writes. Fallback: no
  DT → Semantic-Scholar-only enrichment (today's behaviour).
- **Layer D — Content extraction (Gap 4).** For non-file-backed or
  poorly-extracted records, source text via `extract_record_content` (AI-
  optimised) / `get_record_text`, `ocr_record` for scanned PDFs/images,
  `transcribe_record` for A/V, feeding nexus's existing chunking pipeline.
  Fallback: no DT → file-path extraction only (today's behaviour; non-file-
  backed records skipped as today).
- **Layer E — Annotations & highlights (Gap 6).** `extract_record_highlights`
  / `summarize_record_highlights` and `*_mentions` → ingested as
  highlight-aspects / notes attached to the document's tumbler. Fallback: no
  DT → no highlight ingest (today's behaviour).
- **Layer F — Bidirectional write-back (Gap 2).** After a successful
  index+enrich (`--writeback`): `set_record_tags` (`nx-indexed`,
  `nx-tumbler:<t>`, top aspect keywords), `set_record_annotation` (backlink
  to tumbler), and `set_record_custom_metadata` (structured tumbler / aspect
  fields). Authoritative-source contract: nexus owns only the metadata it
  writes; never edits user content; respects "Exclude from AI & MCP".
  Fallback: no DT → no write-back (index still succeeds).
- **Layer G — Capture into graph (Gap 7).** `nx dt capture <url>`:
  `capture_web_page` → DT record → `nx dt index` in one verb;
  `download_pdf_from_doi` for DOI capture; `import_file` for loose files.
  Fallback: no DT → `nx dt capture` reports DT-required and exits non-zero
  (capture is inherently DT-bound; this is the one verb that *needs* DT, and
  it says so cleanly rather than silently doing nothing).
- **Layer H — AI delegation (experimental, later).** `research_topic` and
  `chat_response` as optional augmentation of nexus's own retrieval. Gated
  behind explicit opt-in; precision/utility unproven — last phase or deferred.

**Explicitly out of scope** (bounding the expansion): selectors/CRUD stay on
osascript (`search_records`, `lookup_records`, `get_record_properties`,
`get_selected_records`, `get_current_record`, `open_record`, group/parent
walks, versions); DT file-management verbs nexus has no reason to drive
(`move_record`, `trash_record`, `duplicate_record`, `replicate_record`,
`merge_records`, `convert_record`, `update_record(_content)`, `export_record`,
reminders).

### Optionality and Fallback Contract

| Layer | DT present | DT absent (tested fallback) |
| --- | --- | --- |
| B linking | similarity / DT-link / classify edges | metadata-only linking, zero semantic edges |
| C enrich | DT CrossRef fills `bib_*` gaps | Semantic-Scholar only |
| D content | DT-extracted text for non-file records | file-path extraction only |
| E highlights | highlight-aspects ingested | none |
| F write-back | `nx-*` tags / metadata stamped | none; index still succeeds |
| G capture | URL/DOI/file → DT → indexed | `nx dt capture` exits non-zero, DT-required |

The fallback column is the pre-RDR-139 behaviour. The fallback test suite
(`tests/test_dt_mcp_fallback.py`) forces `devonthink_mcp.available()` False
and asserts each path equals that column — exact, not "no crash."

### Technical Design

Tool I/O verified by spike (`find_similar_records` →
`{count, results:[{score, uuid, name, …}]}`).

```text
// devonthink_mcp.py — HTTP MCP client to http://localhost:8420/mcp
def available() -> bool                                  # cached is_running + reachable
def dt_call(tool: str, args: dict) -> dict | None        # asyncio.run bridge, fail-soft
def dt_find_similar(uuid, *, limit, floor) -> list[Neighbour]   # {uuid,score,name}
def dt_record_links(uuid) -> list[Neighbour]
def dt_resolve_doi(doi) -> BibFields | None
def dt_extract_content(uuid) -> str | None
def dt_set_tags(uuid, tags, *, mode="add") -> bool
def dt_set_custom_metadata(uuid, fields: dict) -> bool

// every layer guards on the gate first
if not devonthink_mcp.available():
    return legacy_path(...)            # the tested fallback
for n in dt_find_similar(uuid, limit=K, floor=F):
    to = catalog.tumbler_for_source_uri(f"x-devonthink-item://{n['uuid']}")
    if to: cat.link_if_absent(this, to, "relates", created_by="dt_similar")
```

Error contract: every DT call returns result-or-None; None → structured log +
skip. No DT-MCP failure may fail an index, enrich, or capture (except `nx dt
capture`, which is DT-bound by definition and exits cleanly).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| MCP-client helper + `available()` gate | (none) | New `devonthink_mcp.py`; coordinate shape with RDR-126 |
| `relates`/`cites` edge writer | `catalog.py:link_if_absent` | Reuse (`created_by` = `dt_similar`/`dt_link`) |
| Semantic-link generator | `link_generator.py` / `auto_linker.py` | Extend pattern (new generator), don't modify existing |
| Bib enrichment | `nx enrich bib` (Semantic Scholar) | Extend: DT CrossRef as fallback `--source dt` |
| Content extraction | `nx index pdf|md` chunking | Extend: DT-sourced text for non-file-backed records |
| Aspect/highlight ingest | RDR-089 aspects | Extend: highlight-aspects from DT annotations |
| UUID↔tumbler join | `dt.py:_select_dt_uri_from_entry` | Reuse; add inverse `source_uri → tumbler` lookup |
| Selectors/CRUD | `devonthink.py:_run_osascript` | Keep (gjz52) |

### Decision Rationale

Every linking/enrichment layer reuses a proven idempotent primitive; the only
genuinely new code is the MCP client + `available()` gate and per-layer
mappers. Phasing keeps a tight MVV first (Layers A+B+F) and sequences breadth
by dependency and value. The single capability gate makes optionality
uniform and the fallback suite makes it provable.

## Alternatives Considered

### Alternative 1: Migrate the whole DT layer (selectors + AI) to MCP

**Description**: replace osascript entirely. **Pros**: one transport.
**Cons**: gjz52 — selectors gain nothing; churn on a shipped, tested path.
The spike weakens (but does not overturn) gjz52's "extra process" argument
since the server is always-on HTTP, not a spawn. **Reason for rejection**:
no capability gain for selectors; revisit only if osascript bit-rots.

### Alternative 2: Approximate semantic neighbours with nexus's own vectors

**Description**: T3 cosine instead of DT `find_similar_records`. **Cons**:
nexus already has this; DT's similarity spans the user's whole database
including non-indexed items. **Reason for rejection**: solves a smaller
problem, loses DT's reach. (Note: nexus's own vectors remain the *fallback*
edge source consideration only if Gap 1 later demands edges without DT — out
of scope here.)

### Briefly Rejected

- **Drive DT file-management from nexus** (move/trash/merge): nexus is a
  knowledge-graph consumer, not a DT file manager.
- **Make any layer hard-require DT**: violates Gap 0.

## Trade-offs

### Consequences

- (+) Any DT-indexed paper gets edges regardless of bib match.
- (+) DEVONthink becomes navigable into the knowledge graph.
- (+) Non-file-backed records become indexable; second bib source; user
  highlights become knowledge; URL→graph capture.
- (+) Establishes the reusable MCP-client substrate (shared with RDR-126).
- (+) Zero new hard dependency — every path has a tested fallback (Gap 0).
- (−) Async bridging in a sync CLI (contained to one module).
- (−) Breadth risks scope creep — mitigated by phasing + the explicit
  out-of-scope list.

### Risks and Mitigations

- **Risk**: `find_similar_records` precision low → noisy edges.
  **Mitigation**: opt-in, similarity floor, `created_by` attribution for bulk
  audit/revoke; measure before defaulting on.
- **Risk**: DT-sourced content diverges from file-based extraction.
  **Mitigation**: prefer file path when present; DT content only for
  non-file-backed records; stamp extraction provenance.
- **Risk**: a layer silently does nothing when DT is absent and the user
  thinks it ran. **Mitigation**: the `available()` gate logs a single
  "DT unavailable, layer skipped" line; `nx dt index` summary reports which
  enhancements ran vs were skipped.
- **Risk**: write-back pollutes the user's DB. **Mitigation**: `--writeback`
  opt-in, nexus-owned namespace only (`nx-*`), never touch user content;
  honour exclusion flag.

### Failure Modes

DT closed / MCP unreachable → enhanced layers skipped, structured log, base
path succeeds (Gap 0). `nx dt capture` with no DT → clean non-zero exit with a
DT-required message. Wrong UUID→tumbler mapping → wrong edge, mitigated by
`created_by` audit. Silent risk: DT content extraction quietly worse than file
extraction — mitigated by provenance stamping + file-path preference.

## Implementation Plan

### Prerequisites

- [x] All four Critical Assumptions verified (spike).
- [ ] Phase boundaries + per-phase MVV agreed at gate.

### Minimum Viable Validation

Two-sided, both in scope:

1. **Enhanced path**: `nx dt index --uuid <MemForest-UUID> --link-semantic
   --writeback` on the real `knowledge__dt-papers` → the MemForest paper gains
   ≥1 `relates` edge to an agent-memory peer (the edge bib matching could not
   give) **and** the DT record shows `nx-indexed` + `nx-tumbler:<t>`.
2. **Fallback path**: the same command with the DT MCP forced unavailable →
   the index completes with the pre-RDR-139 result (metadata-only, no edges,
   no write-back), zero errors. Asserted exactly in
   `tests/test_dt_mcp_fallback.py`.

### Phase 1 — Substrate + core linking + write-back + fallback suite (MVV)

Layer A (`devonthink_mcp.py` + `available()` gate), Layer B
(`find_similar_records` + `get_record_links` → `relates`), Layer F
(tag/annotation write-back), and the Gap-0 fallback suite. Ships both MVV
sides.

### Phase 2 — Enrichment + content

Layer C (DT CrossRef bib fallback), Layer D (content extraction for
non-file-backed records). Folds into `nx dt index --enrich` and the lxy5n
pipeline. Each ships with its fallback-suite case.

### Phase 3 — Highlights + capture

Layer E (annotations/highlights as aspects), Layer G (`nx dt capture <url>`,
`download_pdf_from_doi`, `import_file`).

### Phase 4 — AI delegation (experimental)

Layer H (`research_topic`, `chat_response`), opt-in, evaluated against
nexus's own retrieval. May be deferred out entirely.

### Day 2 Operations

| Resource | List | Info | Delete | Verify |
| --- | --- | --- | --- | --- |
| `relates`/`cites` edges (`created_by=dt_*`) | `catalog_link_query` | `catalog_links` | `link delete` by creator | doctor link census |
| DT-side `nx-*` tags / custom metadata | DT search | DT record | tag/metadata removal verb | spot-check |
| Captured records | catalog | catalog show | trash in DT + de-index | round-trip test |

### New Dependencies

`mcp` Python SDK (MIT) — first MCP-client dependency. Benign; no legal review.

## Test Plan

- **Scenario**: `find_similar_records` returns neighbours, 2/3 catalog-known —
  **Verify**: exactly 2 `relates` edges, idempotent on re-run.
- **Scenario**: `get_record_links` mirrors a DT link — **Verify**: one
  `relates` edge `created_by=dt_link`, deduped against similarity edges.
- **Scenario (fallback, Gap 0)**: every layer with `available()` False —
  **Verify**: result byte-identical to the pre-RDR-139 path; zero errors;
  exit 0 (except `nx dt capture` → clean non-zero, DT-required).
- **Scenario**: DOI record, no S2 hit, DT present — **Verify**: DT CrossRef
  fills `bib_*`. DT absent — **Verify**: fields stay empty as today.
- **Scenario**: web-archive record, DT present — **Verify**:
  `extract_record_content` text indexed with provenance stamp. DT absent —
  **Verify**: record skipped as today.
- **Scenario**: excluded-from-MCP record — **Verify**: never a neighbour;
  content read refused (CA3).
- **Scenario**: `--writeback`, DT present — **Verify**: `nx-indexed` +
  `nx-tumbler` tags, user content untouched, idempotent.
- **Scenario**: `nx dt capture <url>`, DT present — **Verify**: record created
  + indexed + linked end-to-end.

## Validation

### Testing Strategy

Unit: `available()` gate, UUID↔tumbler mapping, edge idempotency/dedup,
fail-soft on None, flag gating, bib-merge precedence, content provenance — all
mockable against a fake DT-MCP client. **Fallback suite**
(`tests/test_dt_mcp_fallback.py`): every path with the gate forced False,
asserting exact legacy results (Gap 0). Integration/spike: the two-sided MVV
against a live DEVONthink (enhanced) and with the MCP down (fallback). "Done"
per phase = that phase's scenarios green, fallback case green, and no
regression in the `nx dt index` base path.

### Performance Expectations

One-to-few MCP calls per indexed record; negligible against index/enrich cost.
The `available()` probe is cached per invocation (one `is_running` round-trip).
Measure `find_similar_records` precision empirically before defaulting
`--link-semantic` on.

## Finalization Gate

### Contradiction Check

To complete at gate. Note: the spike weakened gjz52's "extra process" argument
for selectors (always-on HTTP, not spawn) but the keep-osascript decision
stands on the no-capability-gain + churn-avoidance grounds; no contradiction.

### Assumption Verification

All four Critical Assumptions Verified (spike). No Docs-Only load-bearing
assumptions remain.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `find_similar_records` / `classify_record` / `get_record_links` | DT MCP | Spike (done) |
| `resolve_doi_metadata` / `search_crossref` | DT MCP | Spike presence; I/O at impl |
| `extract_record_content` / `ocr_record` | DT MCP | Spike presence; I/O at impl |
| `set_record_tags` / `set_record_custom_metadata` | DT MCP | Presence only (not executed) |
| `capture_web_page` | DT MCP | Presence only |
| `is_running` (gate) | DT MCP | Spike (done) |
| `streamable_http` `ClientSession` | `mcp` SDK | Spike (done) |
| `link_if_absent` | nexus catalog | Source Search (done) |

### Scope Verification

MVV is two-sided (enhanced edge + write-back; **and** the tested fallback),
both Phase 1, in scope, executed during implementation. Breadth is phased;
out-of-scope list bounds the expansion; Gap 0 makes the whole integration
optional.

### Cross-Cutting Concerns

- **Versioning**: opt-in flags, no migration.
- **Build tool compatibility**: adds `mcp` SDK to `pyproject.toml`.
- **Licensing**: `mcp` SDK MIT — benign.
- **Deployment model**: enhanced paths need a running DEVONthink + MCP;
  fallback (Gap 0) preserves every base path on Linux / CI / no-DT.
- **IDE compatibility**: N/A.
- **Incremental adoption**: per-layer opt-in flags, default off; the whole
  feature is inert without DT.
- **Secret/credential lifecycle**: none (localhost, `auth.required=false`);
  DT redacts secrets server-side before any content reaches an LLM.
- **Memory management**: one short-lived MCP call per tool invocation.

### Proportionality

Right-sized for an explicitly maximal-but-optional integration: one new
substrate module + gate, eight thin layers over existing primitives, phased
with a two-sided MVV, a hard out-of-scope boundary, and a tested fallback for
every layer. Trim Layer H (AI delegation) if it does not earn its keep at the
Phase 4 review.

## References

- nexus-qtbuh (source), nexus-lxy5n (incorporation pipeline; Gap 4/5 overlap)
- T2: `nexus/gjz52-devonthink-mcp-eval-2026-05-28`, `nexus_rdr/139-research-1`
- RDR-099 (DT substrate), RDR-126 (shared MCP-client substrate), RDR-049/051
  (catalog + link lifecycle), RDR-089 (aspects)
- `src/nexus/devonthink.py`, `commands/dt.py`, `catalog/link_generator.py`,
  `catalog/auto_linker.py`, `catalog/catalog.py`
- DEVONthink 4 built-in MCP (`DEVONthink MCP.app`, `http://localhost:8420/mcp`,
  59 tools), `mcp-tools.json`, `mcp-config-default.json`, `appendix-mcp.html`

## Revision History

### 2026-05-29 — CA spike (draft)

Spiked all four Critical Assumptions against the live DT4 built-in MCP. All
verified. Corrected transport to HTTP `localhost:8420`; tool names
(`find_similar_records`, `classify_record`); dropped the community server.

### 2026-05-29 — Scope expansion + optionality (draft)

Per direction to encompass the full useful DT MCP surface, expanded from two
layers to eight (A–H) across linking, bibliographic enrichment, content
extraction, highlights, write-back, capture, and AI delegation. Added Gaps
1–7 and, per the "optional with a tested fallback" requirement, **Gap 0**: a
single `available()` capability gate plus a dedicated fallback suite
(`tests/test_dt_mcp_fallback.py`) asserting every path degrades to exact
pre-RDR-139 behaviour when DT is absent. MVV made two-sided (enhanced +
fallback). Bounded with an explicit out-of-scope list; four-phase plan keeps
the MemForest edge + write-back as the Phase-1 MVV. Layer H flagged
experimental / possibly deferred.
