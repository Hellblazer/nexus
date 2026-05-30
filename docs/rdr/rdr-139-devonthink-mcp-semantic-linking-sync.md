---
title: "DEVONthink MCP Integration: Semantic Linking, Bibliographic Enrichment, Content Extraction, Bidirectional Sync, and Capture"
id: RDR-139
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-29
accepted_date: 2026-05-30
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

There is exactly **one intentional exception**: Layer G (`nx dt capture`) is
inherently DT-bound — capturing a URL/DOI *into* DEVONthink has no meaning
without DEVONthink. It does not degrade silently; it exits non-zero with a
clean "DEVONthink required" message. Every *other* layer (B–F, A′) degrades to
its pre-RDR-139 behaviour with exit 0. This exception is called out here so the
"every capability is optional" invariant is not read as "every capability
silently no-ops" — Layer G fails loud by design.

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

#### Gap 3: nexus has no MCP-client substrate, and DT cannot reach the agent surface safely

nexus has never consumed an external MCP server (a repo search for
`ClientSession` / `stdio_client` in `src/nexus/` returns nothing; the
`src/nexus/mcp_client/` module does not yet exist). Every capability layer here
needs an MCP client inside a synchronous CLI path.

**Cross-RDR shape decision (vs RDR-126).** Draft RDR-126 (Qwen-MCP) also
introduces `src/nexus/mcp_client/`, but as a *daemon-resident* `NexusMcpClient`
with a connection held open across calls (lazy connect, reconnect-with-backoff,
shutdown on daemon stop). RDR-139's need is the opposite lifecycle: a
*per-call, short-lived* HTTP session in a synchronous CLI process with no
daemon. These two lifecycles are not interchangeable, so "whichever lands first
establishes the pattern" is rejected as hand-waving. The explicit decision:
both live under `src/nexus/mcp_client/` and share only a thin transport-and-
fail-soft **core** (`mcp_client/core.py`: open a session over a configured
transport, `call_tool(name, args) -> dict | None` with the result-or-None /
structured-log contract, redaction-aware). Lifecycle wrappers are separate and
deliberately not shared: RDR-139 contributes `mcp_client/devonthink.py` (a
per-call HTTP wrapper, used by the sync CLI via `asyncio.run`); RDR-126
contributes the daemon-resident held-open wrapper. Neither RDR depends on the
other landing first; each adds its own wrapper over the shared core. If RDR-126
lands first, RDR-139 conforms its wrapper to the then-existing `core.py`; if
RDR-139 lands first, it authors `core.py` to this seam and RDR-126 builds on
it.

Separately, DT's MCP tools cannot be exposed to Claude Code / subagents (the
"agent surface") by declaring DT's *own* server binary in the conexus plugin
`.mcp.json`: conexus ships to every consumer, most without DEVONthink (Linux,
CI, non-DT Macs), and on those hosts there is no DT MCP endpoint to reach, so a
hard-wired DT server entry is dead or erroring. The agent-surface path
therefore needs a nexus-owned shim that gates on DT availability internally.
This is met by a nexus-owned MCP *server* (`nx-mcp-devonthink`) — a conexus
console-script that, like its siblings `nx-mcp` / `nx-mcp-catalog`, is **always
installed with the package and always spawns successfully** regardless of
whether DEVONthink is present. The optionality is internal: on startup the
wrapper probes DT and advertises the DT toolset only if DT is reachable,
otherwise zero tools. Spawn never fails on a DT-less consumer because the
process being spawned is nexus code, not DT's binary. (The `.mcp.json` entry
carries `alwaysLoad: false`, but only to defer tool-search registration until
first use — it is **not** the optionality mechanism and is **not** load-bearing
for it; the internal gate is. See Layer A′.) Both faces — CLI Python client and
agent-surface server — share one two-faced substrate (Layer A + A′).

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

The four *read-path* assumptions are verified by the 2026-05-29 spike. The
spike was deliberately read-only (no write tool executed against the user's
DB), so the two *write-path* assumptions below are **not yet behaviourally
verified** — they are presence-and-signature only and are scheduled for
behavioural verification at the phase that first exercises them.

- [x] **CA1 — DT AI tools reachable from a nexus CLI MCP client, returning
  UUID-bearing results.** — Verified — Spike.
- [x] **CA2 — DT UUIDs map to catalog entries via
  `source_uri = x-devonthink-item://<UUID>`** (un-indexed neighbours
  skipped). — Verified — Spike.
- [x] **CA3 — "Exclude from AI & MCP" enforced server-side** so nexus need not
  re-filter. — Verified — Docs.
- [x] **CA4 — One built-in server covers every layer's *tool surface*;
  community server unnecessary.** — Verified (presence) — Spike + Config.
  *Note:* "covers" here means the tools exist on one server; the write tools'
  *behaviour* is CA5 below, not part of CA4's verification.
- [ ] **CA5 (Phase 1, write-path) — `set_record_tags` in add-mode appends
  without clobbering the user's existing tags, and write tools refuse a
  record flagged "Exclude from AI & MCP" with a clean error (not a silent
  partial write).** — Presence + signature only (spike read-only). Behavioural
  verification is a Phase-1 obligation before Layer F ships, against a
  throwaway DT record.
- [ ] **CA6 (Phase 3, capture-path) — `capture_web_page(url)` returns a
  record UUID that immediately resolves through the same
  `x-devonthink-item://<UUID>` → catalog join, so a captured page is
  indexable end-to-end.** — Presence only. Behavioural verification is a
  Phase-3 obligation before Layer G ships.

**Method definitions**: Source Search = verified against dependency source;
Spike = behaviour verified against the live service; Presence = tool exists
with the expected signature but its I/O behaviour was not executed; Docs Only =
insufficient for load-bearing assumptions.

## Proposed Solution

### Approach

A shared MCP-client substrate plus eight capability layers. Selectors/CRUD
(Layer 1) stay on osascript per gjz52.

**Optionality invariant (Gap 0).** Every layer is gated and fail-soft: a
missing, closed, or MCP-disabled DEVONthink degrades to nexus's existing
behaviour with no error and no partial write. "DT enhances; it is never
required." This is enforced by a single capability gate
(`mcp_client.devonthink.available()`, a cached `is_running` + reachable probe) that
every layer consults before any call, and it is verified by a dedicated
fallback suite that runs every `nx dt` / enrich path with the DT MCP forced
unavailable and asserts the legacy result is byte-identical to pre-RDR-139.

- **Layer A — MCP-client substrate (Gap 3).** `nexus/mcp_client/devonthink.py`
  (per-call HTTP wrapper) over the shared `nexus/mcp_client/core.py`: an HTTP
  MCP client (`mcp.client.streamable_http`) to `http://localhost:8420/mcp`
  (config-overridable `devonthink.mcp.url`), bridged into the sync CLI via
  `asyncio.run` per call. `available()` gate (`is_running` + reachability,
  cached per-invocation). Result-or-None contract: any failure → log + skip,
  never abort. **Async-context guard:** `dt_call` first checks for a running
  event loop (`asyncio.get_running_loop()` in a `try`); if one is found (e.g.
  an aspect-worker or future daemon path called a Layer B/C/D helper from
  inside async code), it does **not** call `asyncio.run` (which would raise
  `RuntimeError: asyncio.run() cannot be called from a running event loop`,
  the hazard documented at `taxonomy_cmd.py:1163-1169`). Instead it logs a
  *distinct* `dt_asyncio_context_error` event and returns `None` — so the
  failure is visible as a misuse signal, never silently conflated with
  "DT unavailable". Layer A is **CLI-path-only by contract**; any async caller
  must use the Layer A′ server face (which owns its own loop), not the sync
  wrapper. The shared `core.py` is the seam with RDR-126 (above). This is the
  Python-API face used by every CLI layer below.
- **Layer A′ — `nx-mcp-devonthink` agent-surface wrapper (Gap 3).** A
  nexus-owned MCP *server* (a third sibling to `nx-mcp` / `nx-mcp-catalog`,
  a conexus console-script declared in conexus `.mcp.json`) that is
  simultaneously an MCP *client* to DT via the Layer A core. It exposes DT to
  Claude Code and subagents, solving the agent-surface gap that declaring DT's
  own binary cannot: because the wrapper is nexus code that ships with the
  package, it **always spawns successfully** on every consumer (DT-present or
  not) and gates internally. On startup it probes `available()`; **DT present →
  advertise the curated toolset; DT absent → advertise zero tools (or a single
  `devonthink_status` stub)** — a harmless always-present server, never a spawn
  error on a DT-less consumer. The `.mcp.json` entry carries `alwaysLoad: false`
  purely to defer tool-search registration until first use; this is a
  startup-cost optimisation, **not** the optionality mechanism — the internal
  `available()` gate is, and the wrapper would be equally optionality-correct
  with `alwaysLoad: true`. (This must be validated at implementation: a Phase-3
  test asserts the wrapper process exits 0 / lists zero tools with DT absent,
  independent of the `alwaysLoad` value.) It also (a) curates the surface to
  ~20 relevant tools (dropping the
  out-of-scope file-management verbs and shrinking the ~28.6k full-schema
  footprint to roughly a third), and (b) adds nexus-aware *composite* tools
  that run the layers below server-side — e.g. `dt_incorporate(uuid)` =
  Layer B + F (find similar → map UUIDs → tumblers → `relates` links →
  write-back) as one agent call. Tools appear as
  `mcp__plugin_conexus_devonthink__*`. Layers A and A′ share one DT-client core
  (gate, redaction handling, UUID↔tumbler mapping); the CLI uses the Python
  face, the agent uses the server face.
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
  the same `bib_*` catalog fields the existing enricher writes.
  **Merge precedence (explicit):** the existing enricher
  (`commands/enrich.py:255-285`) writes `bib_*` *unconditionally* per backend.
  DT-CrossRef is strictly a **gap-filler**: it writes a `bib_*` field **only
  when that field is currently empty/zero**, and **never overwrites** a value
  already set by Semantic Scholar or OpenAlex. Concretely, DT is a *lowest-
  precedence* source (S2 > OpenAlex > DT-CrossRef); a partial S2 match keeps
  its `bib_doi` even if DT-CrossRef would resolve a different DOI form. This is
  enforced by a per-field `if not merged.get(k):` guard in the DT enrich path,
  not by call ordering. Fallback: no DT → Semantic-Scholar-only enrichment
  (today's behaviour).
- **Layer D — Content extraction (Gap 4).** For non-file-backed or
  poorly-extracted records, source text via `extract_record_content` (AI-
  optimised) / `get_record_text`, `ocr_record` for scanned PDFs/images,
  `transcribe_record` for A/V, feeding nexus's existing chunking pipeline.
  **Provenance:** every chunk sourced via DT (rather than the on-disk file)
  is stamped with a `extraction_source` metadata field (values: `file` |
  `dt_content` | `dt_ocr` | `dt_transcribe`); file-path extraction stays the
  default and is stamped `file`. This makes "DT content quietly worse than
  file extraction" auditable rather than invisible. Fallback: no DT →
  file-path extraction only, all chunks `extraction_source=file` (today's
  behaviour; non-file-backed records skipped as today).
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
| A′ agent-surface wrapper | curated DT toolset + composites advertised | wrapper process spawns + exits 0, advertises zero tools (or `devonthink_status` stub); no spawn error (wrapper is nexus code, always installed) |
| B linking | similarity / DT-link / classify edges | metadata-only linking, zero semantic edges |
| C enrich | DT CrossRef fills `bib_*` gaps | Semantic-Scholar only |
| D content | DT-extracted text for non-file records | file-path extraction only |
| E highlights | highlight-aspects ingested | none |
| F write-back | `nx-*` tags / metadata stamped | none; index still succeeds |
| G capture | URL/DOI/file → DT → indexed | `nx dt capture` exits non-zero, DT-required |

The fallback column is the pre-RDR-139 behaviour. The fallback test suite
(`tests/test_dt_mcp_fallback.py`) forces `available()` False and asserts each
path equals that column — exact, not "no crash."

**What "exact" means is per-layer, not a single byte-for-byte rule** (the
assertion is written to the layer's own contract):

- **Layer B (linking)**: *zero new edges* — the catalog edge set after the run
  equals the edge set before (no `created_by=dt_*` rows added).
- **Layer C (enrich)**: the `bib_*` fields written equal what the
  Semantic-Scholar-only path writes — *same fields, same values*. This is
  field-level equality, **not** byte-identity of any API response.
- **Layer D (content)**: the chunk set equals the file-path-only chunk set;
  every chunk `extraction_source=file`; non-file-backed records skipped.
- **Layer E (highlights)**: no highlight-aspects ingested.
- **Layer F (write-back)**: no DT-side mutation; the index/enrich result is
  unchanged and exits 0.
- **Layer A′ (wrapper)**: process spawns and exits 0, lists zero tools.
- **Layer G (capture)**: the deliberate exception — non-zero exit with a
  DT-required message (see Gap 0).

### Technical Design

Tool I/O verified by spike (`find_similar_records` →
`{count, results:[{score, uuid, name, …}]}`).

```text
// mcp_client/devonthink.py — per-call HTTP MCP client to http://localhost:8420/mcp
// (over shared mcp_client/core.py; CLI-path-only)
def available() -> bool                                  # cached is_running + reachable
def dt_call(tool: str, args: dict) -> dict | None        # asyncio.run bridge, fail-soft
    # guard: if asyncio.get_running_loop() succeeds -> log dt_asyncio_context_error,
    #        return None (do NOT call asyncio.run from a running loop)
def dt_find_similar(uuid, *, limit, floor) -> list[Neighbour]   # {uuid,score,name}
def dt_record_links(uuid) -> list[Neighbour]
def dt_resolve_doi(doi) -> BibFields | None
def dt_extract_content(uuid) -> str | None
def dt_set_tags(uuid, tags, *, mode="add") -> bool
def dt_set_custom_metadata(uuid, fields: dict) -> bool

// every layer guards on the gate first
if not available():                    # mcp_client.devonthink.available()
    return legacy_path(...)            # the tested fallback
for n in dt_find_similar(uuid, limit=K, floor=F):
    entry = catalog.by_source_uri(f"x-devonthink-item://{n['uuid']}")
    if entry:                                   # un-indexed neighbour → skip
        cat.link_if_absent(this, entry.tumbler, "relates", created_by="dt_similar")
```

Error contract: every DT call returns result-or-None; None → structured log +
skip. No DT-MCP failure may fail an index, enrich, or capture (except `nx dt
capture`, which is DT-bound by definition and exits cleanly).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| MCP-client core (transport + fail-soft `call_tool`) | (none; `src/nexus/mcp_client/` absent) | New `mcp_client/core.py`; **shared seam with RDR-126** (RDR-126 adds daemon held-open wrapper, RDR-139 adds per-call wrapper — see Gap 3) |
| DT per-call client + `available()` gate + async guard (Layer A) | (none) | New `mcp_client/devonthink.py` over `core.py`; CLI-path-only |
| Agent-surface wrapper server (Layer A′) | `nx-mcp`, `nx-mcp-catalog` (siblings) | New `nx-mcp-devonthink`; reuses Layer A core; declared in conexus `.mcp.json`, `alwaysLoad:false` |
| `relates`/`cites` edge writer | `catalog.py:link_if_absent` | Reuse (`created_by` = `dt_similar`/`dt_link`) |
| Semantic-link generator | `link_generator.py` / `auto_linker.py` | Extend pattern (new generator), don't modify existing |
| Bib enrichment | `nx enrich bib` (Semantic Scholar) | Extend: DT CrossRef as fallback `--source dt` |
| Content extraction | `nx index pdf|md` chunking | Extend: DT-sourced text for non-file-backed records |
| Aspect/highlight ingest | RDR-089 aspects | Extend: highlight-aspects from DT annotations |
| UUID↔tumbler join (forward) | `dt.py:_select_dt_uri_from_entry` | Reuse |
| UUID↔tumbler join (inverse) | (none — `Catalog` has `by_file_path` / `by_doc_id` but no `source_uri` lookup) | **New** `Catalog.by_source_uri(uri: str) -> CatalogEntry \| None` (SQL `SELECT … FROM documents WHERE source_uri = ?`); assigned to **Phase 1**. Returns `None` for un-indexed neighbours → caller skips |
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
- **Risk**: a Layer B/C/D helper is called from inside a running event loop
  (aspect worker, future daemon path) → `asyncio.run` raises `RuntimeError`,
  which the fail-soft contract would otherwise swallow as a `None`/"DT
  unavailable" result, masking a real misuse. **Mitigation**: `dt_call`'s
  running-loop guard logs a distinct `dt_asyncio_context_error` and returns
  `None`; Layer A is contractually CLI-path-only (async callers use the Layer
  A′ server face). A unit test asserts the guard fires and logs distinctly
  when invoked under a running loop.

### Failure Modes

DT closed / MCP unreachable → enhanced layers skipped, structured log, base
path succeeds (Gap 0). `nx dt capture` with no DT → clean non-zero exit with a
DT-required message. Wrong UUID→tumbler mapping → wrong edge, mitigated by
`created_by` audit. Silent risk: DT content extraction quietly worse than file
extraction — mitigated by provenance stamping + file-path preference.

## Implementation Plan

### Prerequisites

- [x] The four read-path Critical Assumptions (CA1–CA4) verified (spike).
- [ ] CA5 (write-path no-clobber + exclusion error) verified behaviourally in
  Phase 1 before Layer F ships.
- [ ] CA6 (`capture_web_page` returns an indexable UUID) verified
  behaviourally in Phase 3 before Layer G ships.
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

Layer A (`mcp_client/devonthink.py` + `available()` gate), the new
`Catalog.by_source_uri(uri) -> CatalogEntry | None` inverse lookup, Layer B
(`find_similar_records` + `get_record_links` → `relates`), Layer F
(tag/annotation write-back), and the Gap-0 fallback suite. Ships both MVV
sides.

### Phase 2 — Enrichment + content

Layer C (DT CrossRef bib fallback), Layer D (content extraction for
non-file-backed records). Folds into `nx dt index --enrich` and the lxy5n
pipeline. Each ships with its fallback-suite case.
**Phase 2 MVV (two-sided):** (enhanced) a DOI-bearing record with no S2 hit
gains `bib_*` from DT-CrossRef *as gap-fill only* (partial-S2 precedence test
green), **and** a web-archive record with no clean file path is indexed via
`extract_record_content` with `extraction_source=dt_content`; (fallback) both
with DT forced absent equal the Layer C/D fallback rows exactly (S2-only
fields; record skipped, all chunks `extraction_source=file`).

### Phase 3 — Highlights + capture + agent-surface wrapper

Layer E (annotations/highlights as aspects), Layer G (`nx dt capture <url>`,
`download_pdf_from_doi`, `import_file`), and Layer A′ (`nx-mcp-devonthink`
wrapper): the internal-gate + curated passthrough ship here, and the composite
`dt_incorporate` tool wraps the Phase-1 Layer B+F pipeline (hence after it
exists). The wrapper's own fallback case — DT absent → server loads with zero
tools, no error — joins the Gap-0 suite. **CA6 is verified behaviourally here**
before Layer G ships.
**Phase 3 MVV (two-sided):** (enhanced) `nx dt capture <url>` creates a DT
record, the returned UUID resolves through the catalog join (CA6), and the page
is indexed + linked end-to-end; the `nx-mcp-devonthink` wrapper, started with
DT present, advertises the curated toolset and `dt_incorporate` runs the B+F
pipeline as one agent call; (fallback) the wrapper started with DT absent
spawns, exits 0, and lists zero tools — asserted independent of the `alwaysLoad`
value (CRITICAL-1 resolution).

### Phase 4 — AI delegation (experimental)

Layer H (`research_topic`, `chat_response`), opt-in, evaluated against
nexus's own retrieval. May be deferred out entirely.
**Phase 4 MVV:** on a held-out question set, `research_topic`/`chat_response`
augmentation produces a *measurable* retrieval-quality delta over nexus's own
retrieval on the same questions (precision/recall or a rubric score), recorded
in the Phase-4 review. If the delta is not positive and material, Layer H is
dropped rather than shipped — this MVV is explicitly a go/no-go, not a
ship-gate.

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
- **Scenario**: `find_similar_records` returns a neighbour whose
  `x-devonthink-item://<uuid>` is **not** in the catalog — **Verify**:
  `Catalog.by_source_uri` returns `None`, that neighbour is skipped, no edge,
  no error.
- **Scenario**: `get_record_links` mirrors a DT link — **Verify**: one
  `relates` edge `created_by=dt_link`, deduped against similarity edges.
- **Scenario (fallback, Gap 0)**: every layer with `available()` False —
  **Verify**: result byte-identical to the pre-RDR-139 path; zero errors;
  exit 0 (except `nx dt capture` → clean non-zero, DT-required).
- **Scenario**: DOI record, no S2 hit, DT present — **Verify**: DT CrossRef
  fills `bib_*`. DT absent — **Verify**: fields stay empty as today.
- **Scenario (merge precedence)**: record with a *partial* S2 match
  (`bib_doi` set, `bib_year` empty), DT present — **Verify**: DT-CrossRef fills
  only `bib_year`; the S2 `bib_doi` is **unchanged** even when DT resolves a
  different DOI form.
- **Scenario**: web-archive record, DT present — **Verify**:
  `extract_record_content` text indexed with `extraction_source=dt_content`.
  DT absent — **Verify**: record skipped as today.
- **Scenario**: excluded-from-MCP record — **Verify**: never a neighbour;
  content read refused (CA3); and a `--writeback` attempt against it returns a
  clean error, not a silent partial write (CA5).
- **Scenario (CA5 no-clobber)**: `--writeback` against a record that already
  has user tags — **Verify**: `set_record_tags` add-mode appends `nx-indexed`
  / `nx-tumbler` and leaves the user's existing tags intact; re-run idempotent.
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

The four read-path Critical Assumptions (CA1–CA4) are Verified (spike). Two
write-path assumptions remain Presence-only and are explicitly deferred to the
phase that first exercises them: **CA5** (write-back no-clobber + exclusion
error) gates Layer F in Phase 1; **CA6** (`capture_web_page` returns an
indexable UUID) gates Layer G in Phase 3. No Docs-Only load-bearing assumption
is relied on for a path shipping before its behavioural verification.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `find_similar_records` / `classify_record` / `get_record_links` | DT MCP | Spike (done) |
| `resolve_doi_metadata` / `search_crossref` | DT MCP | Spike presence; I/O at impl |
| `extract_record_content` / `ocr_record` | DT MCP | Spike presence; I/O at impl |
| `set_record_tags` / `set_record_custom_metadata` | DT MCP | Presence only (not executed) — behavioural verification is **CA5**, Phase 1 |
| `capture_web_page` | DT MCP | Presence only — behavioural verification is **CA6**, Phase 3 |
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

### 2026-05-30 — Gate remediation (draft)

Cleared the 2026-05-29 substantive-critic gate (BLOCKED: 2 criticals, 4
significants, 5 observations). **CRITICAL-1**: reframed Layer A′ optionality —
the wrapper is a nexus console-script that always installs and always spawns;
the internal `available()` gate (not `.mcp.json` `alwaysLoad: false`, which
only defers tool-search) is the load-bearing optionality mechanism; added a
Phase-3 test asserting zero-tools/exit-0 independent of `alwaysLoad`.
**CRITICAL-2**: named and specced the missing inverse lookup as
`Catalog.by_source_uri(uri) -> CatalogEntry | None` (new `SELECT … WHERE
source_uri = ?`), assigned to Phase 1, fixed the pseudocode, added a
UUID-not-in-catalog skip test. **SIG-1**: made the RDR-126 substrate decision
explicit — shared `mcp_client/core.py` seam, separate per-call (139) vs
daemon-held-open (126) wrappers; dropped "whichever lands first." **SIG-2**:
split the CA list into verified read-path (CA1–CA4) and deferred write-path
(CA5 Phase-1 no-clobber + exclusion error, CA6 Phase-3 capture-UUID); removed
the "all four verified" overclaim. **SIG-3**: specified Layer C bib-merge as
gap-fill-only (S2 > OpenAlex > DT-CrossRef, per-field `if not set` guard) with
a partial-match precedence test. **SIG-4**: added the `dt_call` running-loop
guard logging a distinct `dt_asyncio_context_error`, Layer-A CLI-path-only
contract. **Observations**: per-phase MVVs for Phases 2–4; Layer G's
non-optional capture exception called out in Gap-0 text; Layer D provenance
field named `extraction_source`; the per-layer meaning of "exact" fallback
spelled out (not a single byte-identity rule).

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

### 2026-05-29 — Layer A′ agent-surface wrapper (draft)

Added **Layer A′** (`nx-mcp-devonthink`): a nexus-owned, two-faced MCP server
(client to DT via the Layer A core, server to Claude Code / subagents). It is
the answer to "how does the agent surface get DT" without breaking DT-less
consumers: declaring DT's *own* binary in conexus `.mcp.json` is dead/erroring
wherever DEVONthink is absent, whereas the wrapper is a nexus console-script
that ships with the package, always spawns successfully, and gates internally —
DT present → curated toolset; DT absent → zero tools / status stub, no spawn
error. (Gate-correction 2026-05-30: the optionality comes from the internal
`available()` gate, NOT from `.mcp.json` `alwaysLoad: false` — `alwaysLoad`
only defers tool-search and is not load-bearing for optionality. A Phase-3 test
asserts the wrapper lists zero tools / exits 0 with DT absent independent of the
`alwaysLoad` value.) It also curates the surface to ~20
tools (shrinking the ~28.6k full-schema footprint by ~2/3) and adds composite
nexus-aware tools (`dt_incorporate` = Layer B+F server-side). Extended Gap 3,
the Optionality contract, the Existing-Infrastructure audit, and Phase 3 (the
wrapper shell gates early; the composite tool follows the Phase-1 B+F pipeline
it wraps). Layers A and A′ share one DT-client core.
