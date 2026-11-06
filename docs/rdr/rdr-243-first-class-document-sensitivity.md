---
title: "First-Class Document Sensitivity: Route, Stamp and Exempt Sensitive Documents from Every LLM and External Surface"
id: RDR-243
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-03
accepted_date: 2026-11-05
related_issues: ["nexus-rfvyy"]
related_rdrs: [RDR-089, RDR-108, RDR-236, RDR-180, RDR-201, RDR-080, RDR-210]
---

# RDR-243: First-Class Document Sensitivity: Route, Stamp and Exempt Sensitive Documents from Every LLM and External Surface

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Bead nexus-rfvyy, filed 2026-07-28 after Sam indexed a
sensitive business PDF and found that keeping it away from language models
took operator knowledge, not a product feature. Drafted from the design
proposal in T2 `nexus/design-rfvyy-sensitivity-2026-11-03`, read against
develop 482f22563, and checked again against the code at 7e2146baf. Where
the code disagreed with the proposal, this RDR follows the code and says so
in Key Discoveries.

## Problem Statement

Nexus cannot mark a document as "keep this away from language models and
third parties". Today the only protection is to put the document in a
collection that happens to have no LLM pass over it. That protection lives
in the operator's head, nothing records it, and any reorganisation can
remove it without a word.

Terms used throughout. A **document** is a catalog row (the catalog is the
engine's PostgreSQL index of every indexed file or note), addressed by a
**tumbler** (a hierarchical id such as `1.1.42`). A document's text is split
into **chunks** stored in T3 (the permanent vector store). Each chunk is
content-addressed by its **chash** (the SHA-256 of its text), so two
documents with an identical paragraph share one chunk row. An **LLM
surface** is any code path that sends text derived from a document to a
language model or to another party off the machine. **Aspect extraction**
(RDR-089) is the ingest-time pass that asks an LLM to pull structured fields
(method, datasets, results) out of a document.

DEVONthink's `excludeFromChat` flag is the reference model. It protects
inputs (a tool refuses to act on an excluded record) and outputs (result
lists drop excluded records), and the user sets it on the record itself.

### Enumerated gaps to close

#### Gap 1: No sensitivity flag exists anywhere

The catalog row, the chunk row and the search result carry nothing that
says a document is sensitive. An agent that retrieves the chunks has no
signal that quoting them into a bead, a commit or a T3 note is wrong, and
no code path can test for the property because it is not stored.

#### Gap 2: Protection rides on collection routing, a side channel

The workaround in use is to index the document into a `docs__` collection,
because `docs__` has no registered aspect extractor (nexus-z70w,
`aspect_extractor._REGISTRY` at aspect_extractor.py:400). That protection
belongs to the collection, not the document, and it is weaker than it
looks. A `docs__` collection can be opted in to extraction by a config edit
(`aspects.docs_collections`, nexus-kk4ut) or by any machine on the same
tenant running `nx collection aspects <name> --enable` (the engine's
`catalog_collections.aspects_enabled`, nexus-l46pu). An export and import,
a re-index into `knowledge__`, or a catalog merge removes it silently. The
operator gets no message in any of these cases.

#### Gap 3: The dispatch surfaces do not know the property exists

The census in Research Findings counts every LLM surface: aspect
extraction and its validation sample, bibliographic enrichment to Semantic
Scholar, OpenAlex and CrossRef, taxonomy labelling, the `nx_answer`
operators, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, Voyage embedding
and reranking, a remote MinerU server, and the managed cloud tenant. None
checks anything. Two of them (`nx_enrich_beads`, `nx_plan_audit`) start a
child `claude -p` process that has the nx MCP tools, so the child can pull
corpus text through `search` that no check in the parent ever sees.

#### Gap 4: Nothing protects the output side

The MCP read tools (`search`, `query`, `store_get`, `store_get_many`, the
scoped searches, `traverse`) return chunk text to the session model, and
the session model is itself an LLM endpoint. `nx search` run through Claude
Code's Bash tool does the same through stdout. There is no withholding and
no flag on a hit.

#### Gap 5: Which verbs send what to which endpoints is not written down

`docs/privacy-policy.md` §2 lists the cloud tenant, Voyage, Semantic
Scholar, Anthropic and an OpenAI-compatible endpoint. It omits OpenAlex,
CrossRef through DEVONthink, and a MinerU server at a non-loopback URL, and
it has no per-verb table. Indexing a sensitive document in cloud mode
uploads its chunk text and embeddings to the managed tenant without saying
so at the moment of indexing (Sam, 2026-11-04: this gap covers the
sensitive case only, not every cloud-mode index).

## Relationship to Prior RDRs

Searched `docs/rdr/` titles and bodies for "sensitiv", "privacy",
"exclude", "aspect", "LLM backend" and "chunk metadata". No prior RDR
addresses document sensitivity. The overlapping records:

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-089 (closed) | Origin | It created ingest-time aspect extraction, the surface whose collection-level registry became the side channel in Gap 2. Its rationale (extraction is per collection prefix because the schema fits paper-shaped content) still holds; it was never meant as a privacy control. |
| RDR-108 (closed) | Precedent | Document facts live on the catalog document, chunks are content-addressed blobs. This RDR puts the flag on the document for the same reason. |
| RDR-236 (accepted) | Precedent | Moves document fields off chunk rows. Storing `sensitive` in chunk metadata would reverse it, which is one reason Alternative 2 is rejected. |
| RDR-180 (closed) | Precedent | One chunk row per distinct text. A shared chunk is the case that forces "sensitive if any live owner is sensitive". |
| RDR-201 (closed) | Precedent | Closed vocabularies are checked tables. The LLM-surface registry follows that rule (a TOML table with a typed loader). |
| RDR-080 (closed) | Adjacent | Built `nx_answer` and its operators, the largest group of surfaces in the census. Its `trace=false` opt-out (PQ-1) is the only privacy control in that path today, and it covers the run record only. |
| RDR-210 (closed) | Adjacent | One local engine serves bge-768 (on the machine) and Voyage (off it) collections side by side, which is why "sensitive into a Voyage collection" is a choice in local mode too. |

## Context

### Background

Bead nexus-rfvyy (2026-07-28): everything needed to protect the PDF was
possible, but all of it was knowledge rather than product. The operator had
to know that `docs__default` skips extraction, override `--collection` with
the four-segment name, and remember not to run `nx enrich bib` over it.
The bead asks for a `--sensitive` flag that stamps the document, filtering
by default at every LLM surface with loud skip lines, a flag on search
results, a doctor check for sensitive documents in extraction-eligible
collections, and a plain statement of which verbs send content where.

### Technical Environment

- Engine: the Java nexus-service over PostgreSQL 17 with pgvector. Schema
  changes ship as Liquibase changesets (versioned XML migrations) under
  `service/src/main/resources/db/changelog/`; the newest are catalog-046
  and vectors-046. Tables carry row-level security (RLS, a PostgreSQL
  feature that filters rows by tenant inside the database).
- Catalog writes: `CatalogRepository.java` (13,228 lines) and the HTTP
  routes in `CatalogHandler.java`; the Python side is
  `catalog/http_catalog_client.py` behind the `catalog_protocol.py` write
  surface (`CATALOG_WRITE_OPS`, catalog_protocol.py:334).
- Vector search: SQL functions in the `nexus` schema
  (`plain_search_*`, `hybrid_search_*`, `manifest_search_*`,
  `text_gated_search_*`, `search_metadata_scoped_*`,
  `search_aspect_scoped_*`, `search_topic_scoped_*`, `search_graph_hop_*`)
  plus direct jOOQ reads in `PgVectorRepository.java` for get-by-id.
- LLM dispatch: `operators/dispatch.py` and `operators/llm_backend.py`;
  backends are the `claude` CLI, the Anthropic API, and an `openai_compat`
  endpoint (`operators/llm_api.py`).
- Closed tables: `src/nexus/tables/` holds TOML tables with typed loaders
  (for example `chunk-metadata-keys.toml` and `chunk_metadata_keys.py`).

## Research Findings

### Investigation

Read the design record, the bead, and every file:line the design cites, at
7e2146baf. Grepped the package for every caller of `claude_dispatch` and
`run_text_sync`, every `"claude", "-p"` argv, every outbound `https://`
literal, and every module that constructs an `httpx` client. Read the three
document upserts and the register path in `CatalogRepository.java`, the
collection rename, the search-function list in the changelogs, and the
privacy policy.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine `CatalogRepository` | Yes | Document upserts replace `metadata` wholesale at lines 842, 12049 and 12102. `registerDocument` returns the existing tumbler without writing fields when a live row has the same source URI (around line 1131), and a tombstoned source URI gets a new tumbler. `UPDATABLE_DOC_COLUMNS` at line 1894 is a closed whitelist and unknown keys fail with 400. The update route merges `metadata` (`jsonb_concat`, nexus-ke45f). `renameCollection` (line 9697) rewrites `physical_collection` in place. |
| Engine search functions | Yes | Eight function families return chunk text. The HNSW searches set `hnsw.iterative_scan = strict_order` (vectors-030), so a filter inside the predicate keeps scanning until k rows pass, up to the scan limit. |
| Python dispatch | Yes | 21 call sites reach `claude_dispatch` or `run_text_sync` (16 in mcp/core.py, 2 taxonomy, 1 bundle, 1 memory rollup, 1 aspect API leg). Two argv sites build `claude -p`: `CliBackend` (dispatch.py:1301) and `_run_claude_isolated` (aspect_extractor.py:2232). |
| Python `contextvars` | Documented | `asyncio` tasks and `asyncio.to_thread` copy the context; `loop.run_in_executor` and `threading.Thread` do not. The runner's executor path copies it (runner.py:3342), but the search fan-out pools in `search_cross_corpus` do not (`nexus_rdr/243-research-14`), so reads take the posture as an explicit argument (Technical Design item 6). |
| DEVONthink AppleScript | No | The property names for record-level and database-level exclusion in DEVONthink 4 are not confirmed. Assumption A6. |

### Key Discoveries

**Documented (source reading): the census.** Every surface that sends
document-derived text off the machine or to a model, with the behaviour
this RDR proposes. "Loud" means a structured log line and a count in the
command's summary, never a silent drop. The table has 25 rows; "the census
count" below refers to it.

| # | Surface | file:line | Sends what, where | Proposed behaviour |
| --- | --- | --- | --- | --- |
| 1 | Aspect enqueue hook | aspect_worker.py:1868 | full content (MCP path) into the T2 queue for later extraction | skip loudly (`aspect_enqueue_skipped_sensitive`); persist no content |
| 2 | Aspect worker drain | aspect_worker.py:1140 `_process_row` | queued or on-disk content to the LLM | re-check at drain (the flag may be set after enqueue); drop the queue row and log the tumbler and the surface |
| 3 | Aspect extraction, CLI leg | aspect_extractor.py:2163 `_run_claude_isolated` | document text to `claude -p` (Anthropic) | `extract_aspects` (1517) refuses the document; the root asserts the scope |
| 4 | Aspect extraction, API leg | aspect_extractor.py:2377 `_api_inner_text` | document text to the Anthropic API or an `openai_compat` endpoint | as row 3 |
| 5 | `nx enrich aspects` | commands/enrich.py:1670; selection `_select_entries` 1875 | drives rows 3 and 4 | skip at selection; summary reports `N sensitive skipped` |
| 6 | `--validate-sample` | enrich.py:2427 → 3116 → mcp/core.py:9438 `operator_verify` | extracted aspect plus source evidence to the LLM | sensitive documents leave the sample frame; sample size is drawn from eligible documents |
| 7 | `nx enrich bib` | enrich.py:703; bib_enricher.py:48 (Semantic Scholar); bib_enricher_openalex.py:287, 381 | title, plus arXiv ids and DOIs scanned from chunks (`_extract_identifiers_for_title`, enrich.py:996) | skip loudly; a title is content too |
| 8 | DEVONthink CrossRef gap-fill | dt.py:972 (`dt index --enrich`), dt.py:2048 (`dt capture --enrich`); enrich.py:1052; mcp_client/devonthink.py:572 | DOI to CrossRef through DEVONthink | skip loudly |
| 9 | Taxonomy label | commands/taxonomy_cmd.py:1712 (dispatch 1774) | topic terms (c-TF-IDF, a per-topic term weighting) plus sample document names | sensitive documents never appear as sample names; a topic with a sensitive member keeps its terms label and skips the LLM label (phase 1), loudly |
| 10 | Taxonomy review `--auto` | taxonomy_cmd.py:1824 (dispatch 1904) | label, terms, document names | as row 9 |
| 11 | Knowledge Map in SubagentStart | hook output | topic labels into every agent's context | phase 3: sensitive members excluded from term computation |
| 12 | `nx_answer` operators (extract, rank, compare, summarize, generate, filter, check, verify, groupby, aggregate) | mcp/core.py:9191, 9212, 9236, 9268, 9289, 9314, 9388, 9438, 9490, 9565 | hydrated chunk text to the LLM | run enters the scope; retrieval excludes at the engine; hydration (runner.py:2697, 2112, 3890) refuses sensitive ids with a skip line in the run record |
| 13 | `nx_answer` bundle | plans/bundle.py:883 | bundled step inputs | as row 12 (shared hydration) |
| 14 | Plan miss, request coverage, grown-match description | mcp/core.py:12184, 11030, 10529 | question and plan text; coverage may carry hits | scope required; reads exclude |
| 15 | `nx_tidy` | mcp/core.py:15496 (dispatch 15586) | prefetched T3 entries, inlined | scope; prefetch excludes; summary states how many were withheld |
| 16 | `nx_enrich_beads` | mcp/core.py:15615 (dispatch 15654, with `mcp_servers`) | bead text, plus nx MCP tools in the child | posture exported to the child; the child's reads exclude |
| 17 | `nx_plan_audit` | mcp/core.py:15682 (dispatch 15761, with `mcp_servers`) | plan text, plus MCP tools in the child | as row 16 |
| 18 | Memory rollup / `memory_consolidate` | memory_rollup.py:152 | T2 notes to the LLM | `n/a-not-a-document`; recorded as an unprotected gap (a note quoting a sensitive document) |
| 19 | Voyage embedding | engine `VoyageEmbedder.java`; `CceEmbedder.java` (`/v1/contextualizedembeddings`, CceEmbedder.java:39) | chunk text to `api.voyageai.com` at index time | Q3, decided: an acknowledgement flag; local mode routes a sensitive document to a bge collection |
| 20 | Voyage rerank | engine `VoyageReranker.java` | query plus candidate chunk text to Voyage | Q4, decided: sensitive candidates are kept from the reranker, engine-side |
| 21 | MinerU remote | pdf_extractor.py:2787; config.py:623 | PDF bytes to `pdf.mineru_server_url`, possibly off the machine | `--sensitive` never sends the PDF to `pdf.mineru_server_url`, loopback or not, since a loopback URL can be a tunnel (Q6's reasoning, Sam 2026-11-04); it uses the local subprocess ladder, or refuses if none |
| 22 | Cloud tenant storage | `HttpVectorClient` upserts in cloud mode | chunk text and embeddings to the managed tenant | `--sensitive` prints a one-line disclosure naming tenant and embedder; Q3, decided: an acknowledgement flag is required |
| 23 | MCP read tools (output side) | mcp/core.py:3779 `search`, 5028 `query`, 6097 `store_get`, 6723 `store_get_many`; scoped searches; `traverse`; catalog `show`/`list` | content and titles to the session model | Q1, decided: withheld by default; the `sensitivity.mcp_output` config key can switch to `flag` |
| 24 | CLI `nx search` / `query` | commands/search_cmd.py:217 | stdout, often read by Claude Code's Bash tool | Q2, decided: withheld under Claude Code, badged in a terminal |
| 25 | Install ping | install_ping.py:191 | install id, version, mode; no content | `n/a`; listed so the census is complete |

**Documented: three dispatch roots.** Every LLM call in the package passes
through `claude_dispatch` (dispatch.py:893), `run_text_sync`
(llm_backend.py:516), or `_run_claude_isolated` (aspect_extractor.py:2163),
which runs `claude -p` itself and bypasses the other two. A check placed at
these three points sees every call; a check placed at the 21 call sites
misses the 22nd.

**Documented: the engine replaces document metadata wholesale on upsert.**
`upsertDocument` and both import paths set `metadata = EXCLUDED.metadata`.
Merging happens only in Python wrappers (`metadata_merging_catalog.py`,
`indexed_meta.fill_only_meta`, indexed_meta.py:121) and in the engine's
update route. A key one writer stamps and another omits is lost without an
error. A flag kept in `metadata` would fail the way Gap 2 describes.

**Documented, corrects the design: re-registration does not reach the
upsert.** `registerDocument` returns the existing tumbler when a live row
has the same source URI and writes none of the request's fields. A
`--sensitive` re-index of an already-indexed document would therefore never
set the flag through an `OR` in the upsert. A tombstoned (soft-deleted)
source URI is re-registered under a new tumbler, so delete-then-re-index
would also drop the flag. The design below handles both while a tombstone exists.

**Documented, corrects the design: rename keeps tumblers.**
`renameCollection` rewrites `physical_collection` in place, so a column on
the document row follows a rename with no extra code. The paths that write
a document row from outside data are import (`importDocumentsBatch`,
`doImportDocument`, fed by `.nxexp` import and the recovery bundle),
catalog merge (which aliases one document to another), and re-registration
after a tombstone. Whether `migrate-fallback` keeps tumblers is assumption
A1.

**Documented, corrects the design: `docs__` is no longer simply
unregistered.** Since nexus-kk4ut and nexus-l46pu a `docs__` collection is
extracted when config or the tenant-wide engine attribute opts it in, and
then only its prose files (`.md`, `.txt` and similar). Gap 2 is stated with
that in mind.

**Documented, corrects the design: the privacy policy already names the
`openai_compat` endpoint.** The missing rows are OpenAlex, CrossRef through
DEVONthink, and a non-loopback MinerU URL. The Anthropic bullet sits under
the managed-cloud heading although it applies in every mode.

**Documented, corrects the design: a lint on literal hosts misses most
outbound calls.** The bibliographic enrichers use module constants
(`_BASE_URL`), MinerU and `openai_compat` read their URL from config, and
`aspect_readers.py` fetches a document's own `https` source URI. None is a
literal at the call site. The registry lint below inverts the rule: every
`httpx` request outside the engine-client modules must sit in a function
the registry names. The lint reads Python only. Engine egress (rows 19 and
20, through `EgressProxy`) is already guarded by the engine's own source
census, `EgressGuardCensus` and `EgressGuardCensusTest`, which pin every
outbound construct per file (`nexus_rdr/243-research-24`); a new engine
path fails that test. Row 22 is client egress from `HttpVectorClient`, but
the lint exempts the engine-client modules (`HttpVectorClient` and the catalog
clients) and the canary covers the `refuse` and `skip` rows only, so neither
covers row 22; the cloud tenant disclosure test (Test Plan) does. The local cross-encoder reranker sends
nothing off the machine (CrossEncoderReranker.java uses ONNX only,
`nexus_rdr/243-research-15`); Q4 concerns the Voyage reranker only.

**Documented, corrects the design: a `pg_proc` parity check misses reads
that are not SQL functions.** Get-by-id in `PgVectorRepository.java` is a
jOOQ query. The parity check below enumerates engine routes that return
chunk text, then the functions behind them.

**Documented: the search path already resolves owners.**
`_attach_from_chash_positions` (search_engine.py:179) resolves every owning
document of each hit through the engine's chash-positions route, best
effort (a failure logs at debug and leaves no owners). Owner facts are
capped at `OWNER_RESOLVE_CAP = 10` (context_annotations.py:217). This is
the right place to carry a per-owner flag, but it cannot be the place that
enforces one, because it is best effort.

**Documented: a version-floor guard pattern exists.**
`db/serving_engine_version.py` probes the serving engine's version and
refuses note-URI writes on an engine below a floor
(`require_note_uri_engine`). On an unknown version it logs
`note_uri_engine_version_unknown` and allows the write
(serving_engine_version.py:99-110, `nexus_rdr/243-research-16`), so it is
not fail-closed. The capability checks in this RDR use the `GET /version`
key probes instead (Technical Design item 3), where an absent answer reads
as unsupported.

**Documented: DEVONthink is read through AppleScript.** `nx dt index`
enumerates records with `_run_osascript` (devonthink.py:105), not through
DEVONthink's MCP server, so DEVONthink's own exclusion does not stop nexus
from indexing an excluded record.

### Critical Assumptions

- **A1:** `nx catalog migrate-fallback` moves each document by rewriting
  `physical_collection` on the existing row and does not mint new tumblers.
  If it mints, it joins the carry list in Technical Design item 2.
  **Status**: Verified 2026-11-05 (`nexus_rdr/243-research-9`): the
  proposals post `/update`, and `CatalogRepository.updateDocument` updates the
  existing row by tumbler (CatalogRepository.java:2181, 2277-2279); no
  tumbler is minted. **Method**: Source Search.
- **A2:** The only engine statements that insert or wholesale-replace a
  `catalog_documents` row are `registerDocument` (single and batch),
  `upsertDocument`, `importDocumentsBatch` and `doImportDocument`.
  **Status**: Verified for the four named; the census test in the Test Plan
  re-derives the set from the source so a fifth cannot be missed.
  **Method**: Source Search.
- **A3:** A `NOT EXISTS` over live sensitive owners inside each search
  function keeps `k` results under `hnsw.iterative_scan = strict_order` for
  a corpus where sensitive chunks are a small fraction, and adds latency
  small enough not to matter. **Status**: Verified 2026-11-05
  (`nexus_rdr/243-research-10`): pgvector 0.8.7, 80k chunks at 768
  dimensions, 200 queries per cell; every cell returned `k` under both
  `strict_order` and `relaxed_order` at 0%, 5% and 50% sensitive (random and
  clustered); added p50 under 0.5 ms at k=10 and about 3.7 ms at k=100 with
  half the corpus sensitive. **Method**: Spike on a scratch database.
- **A4:** Every LLM call in the package reaches one of the three dispatch
  roots. **Status**: Verified at 7e2146baf (21 call sites, 2 argv sites);
  the registry lint keeps it true. **Method**: Source Search.
- **A5:** A child `claude -p` started with the environment from
  `_build_dispatch_env` passes that environment to the stdio nx MCP server
  it launches, so the child's server can read `NX_SENSITIVITY_POSTURE` at
  startup. **Status**: Verified 2026-11-05 on Claude Code 2.1.318
  (`nexus_rdr/243-research-11`), with and without an env block in the server
  spec; Step 2.5 keeps a test because a Claude Code upgrade could change it.
  **Method**: Spike (a marker variable reported by a probe MCP server).
- **A6:** DEVONthink 4 exposes record-level and database-level AI exclusion
  as readable AppleScript properties. **Status**: Verified 2026-11-05 on
  DEVONthink 4.4.2 (`nexus_rdr/243-research-12`): boolean `exclude from
  chat` on both `record` and `database`; a database read true and a record
  inherited from an excluded group were not observed. **Method**: Spike
  (`get properties of record`, read-only). Phase 3 only;
  phase 1 does not depend on it.
- **A7:** No code path that reads chunk text for an LLM surface runs on a
  thread that lacks the caller's context. **Status**: Refuted at 4cf8e820c
  (`nexus_rdr/243-research-14`): `search_cross_corpus` fans out on plain
  thread pools that copy no context (search_engine.py:1854, 1941, 2188,
  2257, 2293), so a posture read from a ContextVar in a worker would be the
  default. The design no longer depends on A7: the posture travels as an
  explicit argument (Technical Design item 6). **Method**: Source Search.

## Proposed Solution

### Approach

Three layers, each closing a different hole.

1. **A column on the document, set one way.** `catalog_documents.sensitive`
   is a real column, not a metadata key, so no wholesale metadata write can
   clear it. Registering can only raise it. Lowering it takes a separate,
   audited route that the operator calls from the CLI with a reason.
2. **A scope that every dispatch root asserts.** Code that sends anything
   to an LLM must run inside `nexus.sensitivity.llm_bound(...)`. A dispatch
   outside the scope raises. Inside the scope, every corpus read passes
   `sensitivity=exclude` to the engine as an explicit argument (never as
   thread state), and document-keyed pipelines check
   the document before reading it. A surface someone forgets fails its
   first test run instead of leaking.
3. **A closed registry with a lint and a canary.** A checked table lists
   every surface. A lint fails when a dispatch call or an outbound request
   sits outside a registered function. A canary test runs every registered
   surface over a corpus that holds a sensitive document carrying a unique
   string and asserts the string never appears in any outbound payload.

The output side (what the session model sees), rerank exclusion and the
DEVONthink import come in phase 3, under Sam's decisions Q1, Q2, Q4 and
Q7. The input-side external rules (MinerU, the Voyage acknowledgement, the
cloud tenant disclosure) ship with phases 1 and 2.

The flag protects against accidental exposure by tools and agents that
behave as designed. It does not stop a model with shell access that sets
out to bypass it, for example by changing `sensitivity.mcp_output` or
running `nx search` with `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` unset
(session.py:96 detects Claude Code by those variables; the bypass is
inferred, not read).

### Technical Design

**1. Schema (new changeset `catalog-0NN-document-sensitivity`, the next free number; 047 at drafting).**

- `nexus.catalog_documents.sensitive BOOLEAN NOT NULL DEFAULT false`, with a
  partial index on `(tenant_id, physical_collection) WHERE sensitive AND
  deleted_at IS NULL`.
- An audit table `nexus.catalog_sensitivity_changes` (tenant_id, tumbler,
  old, new, reason, actor, changed_at), RLS by tenant like its neighbours.
- `nexus.chunk_sensitive(tenant, collection, chash)` and a batched form
  over arrays: true when any live owner of the chunk (joined through
  `catalog_document_chunks`) is sensitive. `LANGUAGE sql`, `STABLE`,
  `SECURITY INVOKER`, so RLS applies.
- `sensitive` is not added to `UPDATABLE_DOC_COLUMNS`. The general update
  route therefore refuses it with 400, and only the audited route moves it.

**2. Write rules.**

- `upsertDocument` and both import paths set
  `sensitive = catalog_documents.sensitive OR EXCLUDED.sensitive`.
- `registerDocument`, when it finds a live row with the same source URI
  and the request carries `sensitive = true`, raises the flag on that row
  (`UPDATE ... SET sensitive = true WHERE NOT sensitive`) and records the
  change in the audit table. It never lowers it. A fresh registration that
  carries `sensitive = true` writes a `register_set` audit row too (actor
  `engine`, since the repository call has no caller principal). Every
  flagged document gets a first audit row: an upsert or import that inserts
  a fresh row carrying `sensitive = true` writes `upsert_set` or
  `import_set`. A fresh row that is flagged by its request and also
  inherits from a sensitive tombstone gets one audit row, the inheritance.
- A new registration, and an upsert or import that inserts a fresh row, for
  a source URI whose most recent tombstoned row was sensitive inherits the
  flag, with an audit row naming the predecessor.
  Delete-then-re-index keeps the protection while the tombstone exists
  (`nx catalog purge-trash` removes it); the audited route can still clear
  it.
- `nx catalog merge` raises both the canonical and the duplicate when
  either side was sensitive, with an audit row for each row that changes.
- `.nxexp` export and the recovery bundle write the column; import reads
  it.
- Clearing: `POST /v1/catalog/sensitivity {tumblers, sensitive, reason}`.
  Setting needs no reason; clearing refuses without one. Every change
  writes an audit row.

**3. Wire.** The document JSON carries `sensitive`. The chash-positions
route returns `sensitive` per (chash, doc_id) row. The vector search
routes accept `sensitivity=exclude` and return `sensitive` per hit,
computed in one batched query over the result chashes after the search, so
the `RETURNS TABLE` shapes of the search functions do not change. The
get-by-id and store-get routes return `sensitive` per row the same way.
The engine advertises two capabilities as boolean keys on `GET /version`,
the mechanism already in use (`nexus_rdr/243-research-22`):
`catalog_sensitivity_supported`, added with the catalog changeset in
phase 1, and `vector_sensitivity_exclude_supported`, added with the
vectors changeset in phase 2. The client reads them through cached probes
like `supports_chash_allow`, where a failed or absent answer reads as
unsupported. A `--sensitive` write (`index pdf`, `index md`, `dt index`,
`dt capture`, `store put`, MCP `store_put`) refuses before the register
call, and `nx catalog sensitive` before its route call, unless
`catalog_sensitivity_supported` is true, because an engine without the
column would drop the flag without an error (CatalogRepository.java reads
register fields by key). The public conexus edge trims `/version` to an
allowlist, so adding both keys there is paired conexus work that lands
before the client release; without it a cloud client refuses `--sensitive`
writes and `exclude` reads.

**4. Read-side filter (phase 2).** Each search function family takes the
parameter and, when it is `exclude`, adds `NOT EXISTS` over live sensitive
owners inside the search predicate, so the HNSW iterative scan fills `k`
from non-sensitive rows (A3). Get-by-id and get-many routes apply the same
filter.

**5. Python scope.** New module `src/nexus/sensitivity.py`:

```text
// Illustrative — verify during implementation
llm_bound(surface: str) -> ContextManager[None]
    sets a ContextVar posture = "llm_bound" and the surface name
current_posture() -> Literal["default", "llm_bound"]
    ContextVar first (visible on the thread that entered the scope and on
    threads that copy its context), then NX_SENSITIVITY_POSTURE in the
    environment (process-wide; set only in children)
assert_llm_bound(root: str) -> None
    raises DispatchOutsideSensitivityScope(root, surface=None)
class SensitiveDocumentRefused(Exception): tumbler, surface
```

`claude_dispatch`, `run_text_sync` and `_run_claude_isolated` call
`assert_llm_bound`. `_build_dispatch_env` always exports
`NX_SENSITIVITY_POSTURE=llm_bound` to the child. The nx MCP server reads
the variable at startup and runs every tool inside the scope (A5). The
ContextVar decides the dispatch asserts and the value an entry point passes
to its reads; no read consults it, because worker threads in the search
fan-out do not inherit it (A7, refuted). Three dispatch paths run on a
thread that copies no context (`nexus_rdr/243-research-20`):
`run_text_sync`'s helper thread (llm_backend.py:541), the taxonomy
labelling pool (taxonomy_cmd.py:2571) and the aspect worker thread
(aspect_worker.py:499). `run_text_sync` asserts before it hands work to
its helper thread; the taxonomy pool submits each batch through
`contextvars.copy_context().run`; the aspect worker enters `llm_bound` on
its own thread for each row it drains.

**6. Reads under the scope.** The posture reaches a read as an explicit
argument. The client calls that return chunk text (`HttpVectorClient`
`search`, `hybrid_search`, `search_per_collection`,
`search_metadata_scoped`, `search_topic_scoped`, `search_graph_hop`,
`search_aspect_scoped`, `get_by_id` and `chunk_rows`, and
`_ServiceCollectionStub.get`, which `get_collection` returns and
`store_get_many` reaches through `_batched_get_by_ids`;
`nexus_rdr/243-research-19`, `nexus_rdr/243-research-23`) take a keyword-only
`sensitivity: Literal["exclude", "include"]` with no default, and so does
every layer a posture flows through: `search_cross_corpus`, the
`T3Database` facade, `InMemoryVectorClient`, `_batched_get_by_ids` and the
runner's `_hydrate_tumbler_ids`, which calls the internal helper behind
`store_get_many` instead of the tool function it calls today
(runner.py:2224). A call site that omits it fails at call time.
`llm_bound` maps to `exclude` and `default` to `include`. Each entry point
reads `current_posture()` once on its own thread and passes the value
down; `search_cross_corpus` hands it to every pool task as an argument.
MCP tools (`search`, `query`, `store_get`, `store_get_many`, the scoped
searches) carry no `sensitivity` parameter in their schema: the tool body
derives the value from the posture and calls an internal helper that takes
it, so a model cannot pass `include`. `exclude` sends `sensitivity=exclude`;
`include` sends nothing, which is the request made today. Every existing
caller changes in the same release; non-LLM callers (indexing, dedupe,
export) pass `include`. If `vector_sensitivity_exclude_supported` is not
true, an `exclude` read raises; it never falls back to an unfiltered read. Document-keyed pipelines check the catalog row
before they read: aspect enqueue, the worker drain, `extract_aspects`, the
bib and CrossRef selection, the validation sample frame, taxonomy sample
names, and `aspect_readers` source reads (disk, DEVONthink, https) under
the scope, because those reads bypass T3 and the engine filter cannot see
them.

**7. Registry.** `src/nexus/tables/llm-surfaces.toml` with a typed loader
`llm_surfaces.py`, per RDR-201. One row per surface: name,
`module:function`, endpoint class (`anthropic`, `openai-compat`, `voyage`,
`s2`, `openalex`, `crossref-via-dt`, `mineru-url`, `cloud-tenant`,
`session-model`, `none`), and behaviour (`refuse`, `skip`, `exclude`,
`withhold`, `disclose`, `n/a-not-a-document`, `n/a-no-content`). The table
is the source for the privacy-policy rows and for each `--sensitive` help
text. Each row also names the phase that enforces it; generated text lists
only rows enforced in the release being built.

**8. Setting the flag.** `--sensitive` on `nx index pdf`, `nx index md`,
`nx dt index`, `nx dt capture` and `nx store put`; `sensitive: bool =
False` on MCP `store_put` (Q5, decided; not through catalog `update`). `nx catalog sensitive
<tumbler | --collection C | --path GLOB> [--clear --reason TEXT]` sets or
clears after the fact. `nx catalog list --sensitive` lists. `nx index repo
--sensitive-glob PATTERN` is phase 3 (Q8, decided). Under Q3,
`--sensitive` into a Voyage-embedded collection needs
`--ack-external-embedder`; in local mode a sensitive document indexed
without it goes to the bge collection of the same subject instead. MCP
`store_put(sensitive=True)` has no way to acknowledge: in local mode it
stores into the bge collection of the subject, and in cloud mode, where
every collection embeds through Voyage, it refuses and says why (Sam,
2026-11-04). On a local install whose engine embeds with Voyage (no bge
collection exists to route to) it refuses too, and the CLI with
`--ack-external-embedder` is the way in (Sam, 2026-11-05). The bge collection of a subject is
`<prefix>__<subject>__bge-base-en-v15-768__v1` for each of `knowledge__`,
`docs__`, `rdr__` and `code__`. A `--sensitive` index of a document already
registered in a Voyage collection is not re-routed, because registration
writes none of a matching row's fields: without the acknowledgement it
refuses, naming the tumbler and the collection, and with it the document is
indexed in place. In cloud mode every collection embeds through Voyage, so
`--sensitive` without `--ack-external-embedder` refuses on every verb; with
it, the verb prints a one-line disclosure naming the endpoint, the managed
tenant and Voyage (census row 22).

*Implementation notes for item 8 (what Phase 1 shipped; not new design).*

- The index paths key on the catalog flag as well as on `--sensitive`. `nx index pdf`, `nx index md`, `nx dt
  index` and the PDF leg of `nx index repo` read the document's catalog row on every run and treat a flagged
  document, or one whose flag cannot be read, as sensitive: MinerU runs as a local subprocess, `--enrich` is
  refused, and a write into a Voyage collection is refused without `--ack-external-embedder`.
- A repo run or a batch has no acknowledgement to ask for, so it skips a flagged document bound for a Voyage
  collection, with a logged warning, where the single-document verbs refuse. A bge target is indexed as usual.
- `nx collection re-embed` holds back the chunks of a flagged document in a Voyage collection.
- A note put onto a note the catalog flags, into a Voyage collection, refuses before any write, so a plain
  `nx store put` or `nx memory promote` cannot send a flagged note to Voyage. `nx store put --sensitive
  --ack-external-embedder` and `nx catalog import --ack-external-embedder` accept Voyage; MCP `store_put` and
  `nx memory promote` cannot.
- `nx store import` and the recovery-bundle restore follow the import ruling: a flagged document into a
  Voyage collection refuses without `--ack-external-embedder`. A document the catalog already flags (matched by
  `source_uri`) counts as flagged though the file or bundle does not say so, and an unreadable catalog reads as
  flagged.
- A dry run reads the flag without writing. `nx index pdf --dry-run` looks the document up read-only (source
  URI, the repo owner's row, collection and path); a flagged or unreadable result uses the local extractor and
  refuses `--enrich`. It registers nothing.
- `nx collection reindex` reads the flags before it deletes anything. A Voyage collection that holds a flagged
  document, or whose flags cannot be read, refuses without `--ack-external-embedder`, naming the documents.

**9. Carrying the flag to results.** `SearchResult.metadata["sensitive"]`
comes from the engine, never from the chunk row. `sensitive_owners` comes
from chash-positions. A reader instruction joins `READER_INSTRUCTION`
(context_annotations.py:38) only when a sensitive hit is shown with its
content, which happens under `sensitivity.mcp_output: flag` and in a
terminal (Q2).

Output posture (phase 3, Q1). Under `withhold`, the default, an MCP read
tool reads with `include`, then replaces each hit whose `sensitive` field
(item 3) is true with one line, `[sensitive] <tumbler>: content withheld`,
with no title (`<tumbler>` is the hit's sensitive owner from item 3, the
first by tumbler order when there are several, or omitted when none
resolves), and states the number of withheld hits in the result, so
results do not thin without notice (Sam, 2026-11-04). The text never
leaves the tool. Under `flag` the hit is returned with its content and a
`[sensitive]` badge.

**10. Doctor rows** (`nx catalog doctor` and `nx doctor`):
`sensitive_in_aspect_eligible` (warn: a sensitive document whose
collection has an extractor by `select_config` or already has aspect
rows), `sensitive_with_aspects` (fail: a sensitive document that already
has aspect rows or queued rows, so it leaked before it was flagged; the
remedy prints `nx enrich delete --tumbler`), and
`sensitive_in_external_embedder` (warn: a sensitive document in a Voyage
collection). On a tenant with no sensitive documents each row reports
not-applicable, never a green pass.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Document flag | `catalog_documents.metadata` JSONB | Replace for this purpose: a column, because upserts replace metadata wholesale |
| Owner resolution on hits | `search_engine._attach_from_chash_positions` | Extend: carry `sensitive` per owner; enforcement stays in the engine |
| Collection-level extraction gate | `aspect_extractor.select_config`, `docs_collection_opted_in` | Reuse for the doctor row; the document gate sits in front of it |
| Dispatch environment | `operators/dispatch._build_dispatch_env` | Extend: export the posture |
| Capability probe | `GET /version` `*_supported` keys; `HttpVectorClient.supports_chash_allow`, `HttpCatalogClient._version_flags` | Extend: two new keys and probes; the conexus edge allowlist (paired) |
| Closed table | `src/nexus/tables/` (TOML plus loader, RDR-201) | Extend: new `llm-surfaces` table |
| Catalog write surface | `catalog_protocol.CATALOG_WRITE_OPS` | Extend: add `set_sensitivity` (a write proxy that lacks it raises at call time, the nexus-kgos1 shape) |
| Disclosure text | `docs/privacy-policy.md` §2 | Extend: per-verb table generated from the registry |

### Decision Rationale

A column is the only place where no existing writer can clear the flag by
accident: every writer that omits it leaves the default, and the `OR`
leaves an existing `true` alone. Checking at the three dispatch roots
covers every call that exists today (A4); the registry lint and the canary
are what keep a new surface from shipping unchecked, where a check per
call site is complete only until the next surface. The scope makes the dispatch refuse rather
than trust a caller's declaration of what text it carries, which matters
for `nx_answer`, where operator inputs arrive as `$stepN` text several hops
from any id, and for the tool-access children, which declare nothing.
Filtering inside the engine is where document identity is still known; by
the time text reaches a prompt, it is not.

## Alternatives Considered

### Alternative 1: Flag in catalog metadata

**Description**: Store `sensitive` as a key in `catalog_documents.metadata`.

**Pros**:

- No schema change.

**Cons**:

- Any wholesale metadata write clears it: the three engine upserts, any
  Python seam that skips the merge wrapper, and imports.

**Reason for rejection**: The failure is silent and is exactly Gap 2's
failure moved to a new place.

### Alternative 2: Flag in chunk metadata

**Description**: Stamp `sensitive` on each chunk row, as the bead first
proposed.

**Pros**:

- Search results carry it with no join.

**Cons**:

- Puts a document fact on chunk rows, reversing RDR-108 and RDR-236.
- A chunk shared by a sensitive and a non-sensitive document is one row;
  the last writer decides, so the answer is wrong for one of them.

**Reason for rejection**: Wrong for shared chunks, and contrary to the
catalog/T3 split.

### Alternative 3: A check at every surface

**Description**: Add a document check at each of the census rows.

**Pros**:

- Simple to read; each check sits next to the code it guards.

**Cons**:

- The next surface forgets, which is how the problem began. Nothing fails
  when a check is missing.

**Reason for rejection**: Kept as a complement (document-keyed pipelines
still check), rejected as the main mechanism.

### Alternative 4: One gate on declared provenance

**Description**: Callers put the tumblers or chashes their prompt came
from on `DispatchRequest`; the dispatch root checks them.

**Pros**:

- One gate.

**Cons**:

- Only as good as the declaration. `nx_answer` operator inputs lose their
  ids several steps before the prompt, and child processes with MCP tools
  declare nothing.

**Reason for rejection**: Too weak alone; the scope plus filtered reads
does not depend on callers telling the truth.

### Briefly Rejected

- **A repo-wide `nx index repo --sensitive`**: rarely what is meant; a
  glob option is proposed instead, in phase 3 (Q8).
- **A client-side store of sensitive ids**: violates the NO-SQLITE rule and
  would not reach other machines on the tenant.

## Trade-offs

### Consequences

- Positive: protection belongs to the document and survives rename,
  import, merge and re-index at the same source URI while the row or its
  tombstone exists.
- Negative: inheritance is keyed on source URI, so the flag does not follow
  a file moved or copied to a new path, and delete-then-re-index loses it
  once `nx catalog purge-trash` has removed the tombstone
  (`nexus_rdr/243-research-17`).
- Positive: a new LLM surface cannot ship without a registry row, a scope,
  and a canary run.
- Negative: every existing dispatch caller must be wrapped in the scope in
  the same change that adds the assert, or it raises.
- Negative: a topic with one sensitive member loses its LLM label in phase
  1 and keeps only its terms label.
- Negative: sensitive results rank by vector order alone, because Q4 keeps them
  from the Voyage reranker.

### Risks and Mitigations

- **Risk**: A read made for an LLM-bound surface passes `include` or
  reaches the engine without the argument.
  **Mitigation**: the `sensitivity` keyword has no default, so an omitted
  argument fails at call time; the posture never rides on thread state
  (A7, refuted); the canary runs `nx_answer` retrieval over collections of
  at least two embedding models, so the search fan-out pools execute
  (`nexus_rdr/243-research-18`), and asserts that a pool ran.
- **Risk**: A client sends `sensitivity=exclude` to an engine that ignores
  unknown parameters and returns sensitive rows.
  **Mitigation**: the client reads `vector_sensitivity_exclude_supported`
  first and raises when it is absent; both engine changesets and the edge
  allowlist ship before the client release that depends on them.
- **Risk**: The filter thins search results (A3).
  **Mitigation**: measure before phase 2 lands; the iterative scan already
  in use is the expected remedy.
- **Risk**: A T2 note quotes a sensitive document (census row 18).
  **Mitigation**: none in this RDR; stated as not enforced in
  `docs/sensitivity.md`.

### Failure Modes

- A surface outside the scope: `DispatchOutsideSensitivityScope` names the
  root; the fix is to wrap the surface and add its registry row.
- A sensitive document skipped: a structured log line names the tumbler
  and the surface; the command summary counts skips.
- An old engine, or a `/version` probe that fails: the probe never raises
  and reads a failed or absent answer as unsupported, so `exclude` reads and
  `--sensitive` writes raise. The message names the engine version and the
  minimum required when the engine answered without the key, and says it
  could not confirm support when the probe got no answer.
- A document that leaked before it was flagged: `nx catalog doctor` fails
  `sensitive_with_aspects` and prints the delete command.

## Implementation Plan

### Prerequisites

- [ ] A1, A3 and A5 verified before the phase that depends on each (A1
  before phase 1 closes; A3 and A5 before phase 2 starts). A7 is refuted
  and no longer a prerequisite (Technical Design item 6).
- [x] Sam's answers to Q1 to Q8: all as recommended (2026-11-03).

### Minimum Viable Validation

The canary test: a document indexed with `--sensitive` carries a unique
string; every surface in the registry whose behaviour is `refuse` or `skip`
runs against a corpus that includes it, with a capturing fake LLM backend
and an `httpx` mock transport, and the string appears in no captured
payload. Deleting any one gate turns its row red. This runs in phase 1.

Phases 1 and 2 ship in one release: `--sensitive` is not released until
the read filter is in, so no release describes protection it does not
give (Sam, 2026-11-04). Phase 3 can ship later because each registry row
carries the phase that enforces it, and the privacy-policy table and the
`--sensitive` help text are generated only from rows enforced in the
release being built. The input-side external rules ship with phases 1 and
2, not phase 3: a sensitive PDF never goes to a MinerU URL (row 21), the
Q3 acknowledgement and bge routing, and the MCP `store_put` cloud refusal
(item 8) are Phase 1 Step 3 work.

### Phase 1: Minimal real protection (input side, document-keyed)

#### Step 1: Tests first

Write the canary test and the registry lint, run them on develop, and
record the red runs on the bead. They are the pin.

#### Step 2: Engine

Changeset `catalog-0NN` (column, partial index, audit table,
`chunk_sensitive` functions, grants); the write rules in Technical Design
item 2; the audited route; `sensitive` in document JSON and in
chash-positions rows; the `catalog_sensitivity_supported` key on
`GET /version`. Java repository tests on PostgreSQL.

#### Step 3: Python flag and verbs

`CatalogEntry.sensitive`; `set_sensitivity` in the client and in
`CATALOG_WRITE_OPS`; `--sensitive` on `index pdf`, `index md`,
`dt index`, `dt capture`, `store put`, and MCP `store_put`;
`nx catalog sensitive`; `nx catalog list --sensitive`; the
`catalog_sensitivity_supported` probe and the write refusal (item 3); the
MinerU rule (row 21: `--sensitive` takes the local subprocess path); the
Q3 `--ack-external-embedder` flag and bge routing, and the MCP `store_put`
cloud refusal (item 8); the cloud tenant disclosure of row 22: in cloud mode
`--sensitive` requires `--ack-external-embedder` and prints, once, before the
write, one line saying the document's chunk text and Voyage embeddings are
stored in the managed tenant at the named endpoint and sent to Voyage AI. The
CLI verbs print it; MCP `store_put` has no acknowledgement, so its cloud
refusal carries the same reason.

#### Step 4: Scope and document gates

`nexus.sensitivity`; the assert in the three roots; every current surface
wrapped, with the three thread hops of Technical Design item 5 handled. Document gates in aspect enqueue, drain and
extract, the validation sample frame, bib and CrossRef selection, and
taxonomy sample names.

#### Step 5: Registry, doctor, policy

`llm-surfaces.toml` and loader with every census row and the phase that
enforces it; the doctor rows `sensitive_in_aspect_eligible` and
`sensitive_with_aspects`; the privacy-policy §2 table, generated from the
rows enforced in the release, and the `--sensitive` help from the same rows,
each with one generated line saying that search results are still shown to
the session model until the output posture ships (accept residual S-E); a
`docs/sensitivity.md` stub.

Exit: the canary is green for every `refuse` or `skip` row, and each pin
has been seen to fail with its gate removed.

### Phase 2: Read-side filtering for `nx_answer` and tool-access children

Engine `sensitivity=exclude` in every search family and in get-by-id
(a new `vectors-0NN` changeset), `sensitive` per row on the get-by-id and
store-get routes, the route parity test, the
`vector_sensitivity_exclude_supported` key; the conexus edge allowlist for
both keys. `HttpVectorClient` read methods take the explicit, default-less
`sensitivity` argument and fail closed; `search_cross_corpus` passes it to
every pool task; every existing caller is updated. Runner and bundle
hydration refusals. `NX_SENSITIVITY_POSTURE` exported to children and read
by the nested MCP server. `nx_tidy` prefetch exclusion. The engine release
that carries both changesets, and the edge allowlist, precede the client
release that carries phases 1 and 2.

### Phase 3: Output side and external services

The MCP output posture (Q1); the CLI badge, `--json` field and Claude Code
posture (Q2); the reader instruction line; rerank exclusion (Q4);
DEVONthink exclusion
import (Q7, A6; this RDR adds no write to DEVONthink, Sam 2026-11-04); taxonomy discover
without sensitive members; the `sensitive_in_external_embedder` doctor row;
complete `docs/sensitivity.md` (the Phase 1 stub grows).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `catalog_documents.sensitive` | In scope (`nx catalog list --sensitive`) | In scope (`nx catalog show`) | In scope (`nx catalog sensitive --clear --reason`) | In scope (doctor rows) | In scope (export and recovery bundle carry it) |
| `catalog_sensitivity_changes` | Deferred | Deferred | N/A (append-only audit) | N/A | In scope (database backup) |
| `llm-surfaces` table | In scope (it is a file) | In scope | N/A | In scope (registry lint) | N/A (in git) |
| `sensitivity.mcp_output` config key | N/A | In scope (`nx config`) | N/A | In scope (doctor) | N/A |

A CLI view of the audit table is deferred: the rows exist from phase 1 and
can be read with SQL; a verb is added when someone needs it.

### New Dependencies

None.

## Test Plan

- **Scenario**: Canary over the registry — **Verify**: for each row, the
  canary string is absent from every captured LLM payload and outbound
  request; the corpus spans collections of at least two embedding models,
  so the search fan-out pools run, and the test asserts a pool ran; the
  test is parametrized over the table, so a new row brings its own case.
- **Scenario**: Registry lint — **Verify**: every call to
  `claude_dispatch` or `run_text_sync`, every `claude -p` argv, and every
  `httpx` request outside the engine-client modules sits in a function the
  table names; the non-vacuity floor is the count at landing.
- **Scenario**: Dispatch outside the scope — **Verify**: each of the three
  roots raises `DispatchOutsideSensitivityScope`; each of the three thread
  hops (item 5) dispatches inside the scope without raising.
- **Scenario**: Re-register without the flag — **Verify**: the flag stays
  true (Java, PostgreSQL).
- **Scenario**: Re-register an existing live document with the flag —
  **Verify**: the flag becomes true and an audit row is written.
- **Scenario**: Tombstone, then re-index without the flag — **Verify**:
  the new tumbler is sensitive and the audit row names the predecessor.
- **Scenario**: Clear without a reason; clear with one — **Verify**: the
  first is refused; the second writes an audit row.
- **Scenario**: General update route sets `sensitive` — **Verify**: 400.
- **Scenario**: Export, import, recovery bundle, catalog merge, collection
  rename — **Verify**: the flag survives each.
- **Scenario**: Two tenants — **Verify**: RLS keeps one tenant's flag and
  audit rows out of the other's reads.
- **Scenario**: Writer census — **Verify**: a source scan of
  `CatalogRepository.java` finds no statement that inserts into
  `catalog_documents` outside the set the test names (A2).
- **Scenario**: Shared chunk — **Verify**: a chunk owned by one sensitive
  and one non-sensitive live document is excluded; after the sensitive
  owner is tombstoned it is not.
- **Scenario**: Route parity (phase 2) — **Verify**: every engine route
  that returns chunk text accepts `sensitivity` and filters; the list is
  derived from the route table and `pg_proc`, not typed by hand.
- **Scenario**: Old engine (phase 1) — **Verify**: a `--sensitive` write
  refuses when `catalog_sensitivity_supported` is absent or the probe fails.
- **Scenario**: Old engine (phase 2) — **Verify**: an `exclude` read raises
  when `vector_sensitivity_exclude_supported` is absent or the probe fails;
  an `include` read works.
- **Scenario**: `nx_answer` over a corpus with collections of at least two
  embedding models and a sensitive canary document (phase 2) — **Verify**: the canary is absent
  from every operator prompt and from the final answer.
- **Scenario**: An `HttpVectorClient` read without `sensitivity` (phase 2)
  — **Verify**: it raises at call time.
- **Scenario**: Child process with MCP tools (phase 2) — **Verify**: its
  `search` returns no sensitive hits.
- **Scenario**: MCP `store_put(sensitive=True)` (phase 1) — **Verify**: cloud mode
  refuses with the reason; local mode stores into the subject's bge
  collection.
- **Scenario**: Cloud tenant disclosure (phase 1, row 22) — **Verify**: in
  cloud mode each of `index pdf`, `index md`, `dt index`, `dt capture` and
  `store put` refuses `--sensitive` without `--ack-external-embedder` before
  any register call; with it, the one-line disclosure naming the endpoint,
  the tenant and Voyage is printed before the write.
- **Scenario**: `--ack-external-embedder` (phase 1) — **Verify**: with the
  flag, a sensitive write into a Voyage collection keeps that collection and
  prints the notice; without it, local mode takes the bge sibling and cloud
  mode refuses.
- **Scenario**: bge routing in local mode (phase 1) — **Verify**: a
  `--sensitive` write without the acknowledgement lands in
  `<prefix>__<subject>__bge-base-en-v15-768__v1`, parametrized over
  `knowledge__`, `docs__`, `rdr__` and `code__`.
- **Scenario**: A sensitive PDF and MinerU (phase 1, row 21) — **Verify**:
  with an explicit non-loopback URL, with a loopback URL and with a pid-file
  local server, no URL is resolved, probed or contacted and the extraction
  runs as the local subprocess; with no subprocess the run refuses.
- **Scenario**: `--sensitive` index of a document already registered in a
  Voyage collection (phase 1) — **Verify**: matched by source URI and
  matched by file path, it refuses without the acknowledgement, naming the
  tumbler and the collection, and changes nothing (no chunk in the bge
  sibling, `physical_collection` and `sensitive` as before); with the
  acknowledgement it indexes in place; a document already in a bge
  collection proceeds without it.
- **Scenario**: MCP `search` under `withhold` (phase 3) — **Verify**: a
  sensitive hit appears as the stub line with no title and the withheld
  count; the canary string is absent from the response.
- **Scenario**: Doctor rows on a tenant with no sensitive documents —
  **Verify**: not-applicable, not pass.
- **Scenario**: Aspect drain after a late flag — **Verify**: a row queued
  before the flag was set is dropped from the queue, the log names the
  tumbler and the surface, and no content is sent.

## Validation

### Testing Strategy

1. **Scenario**: The canary over every registered surface, run on the
   engine substrate.
   **Expected**: no outbound payload contains the canary; each gate, when
   deleted, turns its own row red, and that red run is recorded on the
   bead.
2. **Scenario**: The A3 spike, before phase 2.
   **Expected**: result counts equal to `k` at 0%, 5% and 50% sensitive,
   with latency recorded against today's.

### Performance Expectations

The batched `chunk_sensitive` lookup adds one indexed query per search
response; the filter inside the search predicate costs what A3 measures.
No figure is claimed until then.

## Open Questions (decided)

Sam decided all eight as recommended on 2026-11-03. Each entry keeps the
question and the reason; the recommendation is the decision. Q5 and Q6 were
the drafter's recommendations, the design having left them open.

- **Q1.** Should the MCP read tools withhold sensitive content from the
  session model by default, or return it with a flag?
  *Decided (as recommended)*: withhold by default, with a config key
  `sensitivity.mcp_output: withhold | flag`, never a tool argument. Nothing
  stops a process with shell access from changing the key (inferred, not
  read). Every MCP response goes to Anthropic, so
  returning content contradicts "exempt from every LLM surface"; this is
  DEVONthink's behaviour.
- **Q2.** Should `nx search` run from Claude Code's Bash tool (detected by
  `CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT`, session.py:96) withhold the way
  the MCP tools do?
  *Decided (as recommended)*: yes. Output read through the Bash tool reaches the same
  model; a terminal user still sees content with a `[sensitive]` badge.
- **Q3.** Should `--sensitive` into a Voyage-embedded collection (cloud
  mode, or keyed local mode) be refused, need an explicit acknowledgement,
  or only print a disclosure?
  *Decided (as recommended)*: require an acknowledgement flag, and in local mode route
  a sensitive document to a bge collection by default. Embedding sends the
  full chunk text to Voyage, which is the same exposure the flag exists to
  prevent.
- **Q4.** Should sensitive candidates be kept from the Voyage reranker even
  in a search a person runs?
  *Decided (as recommended)*: yes, engine-side; sensitive hits keep their vector order.
  Reranking sends the chunk text to Voyage, and the only cost is ranking
  quality for those hits.
- **Q5.** May an agent set sensitivity through MCP (`store_put` with
  `sensitive=True`), given that clearing stays CLI-only with a reason?
  *Decided (as the drafter recommended)*: yes for
  `store_put`, no through catalog `update`. Setting is the protective
  direction and an operator can clear a wrong set; clearing stays a CLI
  verb with no MCP tool, which a process with shell access can still run
  (see the threat scope under Approach).
- **Q6.** Should an LLM backend whose endpoint is loopback (a local
  OpenAI-compatible server) be exempt from the dispatch refusal?
  *Decided (as the drafter recommended)*: no exemption in
  this RDR. A loopback URL can be a tunnel (`ssh -L`) to another host, so
  the URL does not prove the model is local; an operator who wants it can
  have a later, explicit config allow.
- **Q7.** Should `nx dt index` treat DEVONthink's exclusion as sensitive
  automatically?
  *Decided (as recommended)*: yes. The operator already marked the record; importing it
  unmarked would undo their choice. Depends on A6.
- **Q8.** Does `nx index repo --sensitive-glob` belong in phase 1?
  *Decided (as recommended)*: no; phase 3. The bead's case is PDFs and DEVONthink
  records, and phase 1 lists only those verbs.

## Finalization Gate

### Contradiction Check

### Assumption Verification

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `registerDocument`, `upsertDocument`, `importDocumentsBatch`, `doImportDocument` | engine `CatalogRepository` | Source Search |
| `UPDATABLE_DOC_COLUMNS` refusal (400) | engine `CatalogRepository` | Source Search |
| `hnsw.iterative_scan` | pgvector | Source Search (vectors-030); behaviour under the new filter is A3 |
| `contextvars.copy_context`, `asyncio.to_thread` | CPython | Docs Only |
| DEVONthink exclusion properties | DEVONthink 4 AppleScript | Unverified (A6) |

### Scope Verification

### Cross-Cutting Concerns

- **Versioning**: engine changesets ship in an engine release ahead of the
  client release that sends `sensitivity=exclude`, with both `/version`
  keys allowlisted on the conexus edge; the client probes each key and
  refuses on an older engine.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: same schema and code in local and cloud mode; the
  mode changes only the index-time disclosure and the Voyage rows.
- **IDE compatibility**: N/A
- **Incremental adoption**: existing documents default to false with no
  backfill and no upgrade rung; `nx catalog sensitive --collection
  docs__default` converts today's side channel into the flag.
- **Secret/credential lifecycle**: N/A
- **Memory management**: N/A

### Proportionality

## References

- Bead nexus-rfvyy; design record T2 `nexus/design-rfvyy-sensitivity-2026-11-03`.
- Beads nexus-z70w (docs__ unregistered), nexus-kk4ut and nexus-l46pu
  (docs__ opt-in), nexus-ke45f (update merges metadata), nexus-kgos1
  (write-proxy op list).
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`
  (842, 1131, 1894, 9697, 12049, 12102).
- `src/nexus/operators/dispatch.py`, `src/nexus/operators/llm_backend.py`,
  `src/nexus/aspect_extractor.py`, `src/nexus/aspect_worker.py`,
  `src/nexus/mcp/core.py`, `src/nexus/plans/runner.py`,
  `src/nexus/search_engine.py`, `src/nexus/context_annotations.py`.
- `docs/privacy-policy.md` §2.
- DEVONthink MCP `excludeFromChat` contract (input and output protection).

## Revision History

- 2026-11-03: drafted from the design record; five corrections against the
  code recorded in Key Discoveries (re-registration path, rename keeps
  tumblers, `docs__` opt-in, privacy policy already names `openai_compat`,
  lint and parity domains).
- 2026-11-03: Sam decided Q1 to Q8 as recommended; the question list, the
  surface table, Prerequisites and the cross-references now state the
  decisions.
- 2026-11-04: Gate round 1 — BLOCKED (1 Critical, 9 Significant, 1 ship-blocker(s)); commit `4cf8e820c`; critique `nexus_rdr/243-gate-critique-2026-11-03-r1`.
- 2026-11-04: Gate round 2 — PASSED (0 Critical, 10 Significant, 0 ship-blocker(s)); commit `653c8c792`; critique `nexus_rdr/243-gate-critique-2026-11-04-r2`.
- 2026-11-04: Sam's rulings: remedy (a) for the round-1 Critical; Gap 5 narrowed to the sensitive case; no DEVONthink writeback tag. After the round-2 PASSED gate, Sam asked for all ten Significants: phases 1 and 2 ship together; MCP `store_put(sensitive=True)` refuses in cloud mode and routes to bge locally; withheld MCP hits are a stub with no title and a withheld count; a sensitive PDF never goes to a MinerU URL, loopback or not. Revision: these designs, the write-path capability check, the dispatch-side thread hops, the client read-method list, the canary's embedding-model condition, engine egress as a hand-kept list, and wording fixes.
- 2026-11-04: fix round on the revision's fix check (`nexus_rdr/243-fix-check-bb08311f3`): two `GET /version` capability keys (catalog in phase 1, vectors in phase 2) with the conexus edge allowlist; the input-side external rules move into phase 1 and registry rows carry their enforcing phase; the stub `get` read path; engine egress under `EgressGuardCensus`; the stub's per-row field.
- 2026-11-04: Gate round 3 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); commit `5d5d93d30`; critique `nexus_rdr/243-gate-critique-2026-11-04-r3`.
- 2026-11-04: Accepted. Round-3 residual dispositions: S-A `nexus-rfvyy.1` (Phase 3); S-B `nexus-rfvyy.2` (Phase 1 Step 3); S-C `nexus-rfvyy.3` (Phase 2, needs Sam); S-D `nexus-rfvyy.4` (Phase 1 Step 3); S-E `nexus-rfvyy.5` (Phase 1 Step 5).
- 2026-11-05: assumptions A1, A3, A5 and A6 verified (`nexus_rdr/243-research-9` to `-12`); Status lines updated.
- 2026-11-05: Sam's rulings on the Phase 1 engine review: merge duplicate adopts the flag; fresh flagged registration is audited; upsert and import inherit from a sensitive tombstone.
- 2026-11-05: Sam's rulings: the aspect drain drops a sensitive row (no skipped_sensitive status); a Voyage-profile local install refuses MCP store_put(sensitive); nx store import refuses a flagged document into a Voyage collection without --ack-external-embedder.
- 2026-11-05: Implementation notes under Technical Design item 8 record how Phase 1 applies the catalog flag (index paths, repo and batch skips, re-embed hold-back, note re-put, dry runs, reindex). No design change.
