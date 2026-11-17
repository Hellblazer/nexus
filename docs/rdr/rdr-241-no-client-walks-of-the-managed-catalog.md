---
title: "No Client Walks of the Managed Catalog: the Engine Answers Tenant-Wide Questions and Runs Tenant-Wide Jobs"
id: RDR-241
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-11-02
accepted_date: 2026-11-16
related_issues: [nexus-mq980, nexus-75w5d, nexus-ohxzu, nexus-ial1f, nexus-0lv0r, nexus-f3yqd.2, nexus-f3yqd.6]
related_rdrs: [RDR-193, RDR-146, RDR-192, RDR-168, RDR-236, RDR-155, RDR-243, RDR-246, RDR-248]
---

# RDR-241: No Client Walks of the Managed Catalog

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A *client walk* is client code that pages through every catalog document of a
tenant over HTTP (`all_documents(limit=0)` and its siblings) to compute
something about the whole tenant. A *tenant* is one customer's isolated data in
the engine (the Java service that stores the catalog); the *catalog* is the
table of document records that the CLI, the MCP tools (the Model Context
Protocol tools an agent calls) and the indexer share. Each record is addressed
by a *tumbler*, the catalog's dotted document id such as `1.2.5`. On
a managed service the catalog lives in the engine's Postgres, so a question like
"which documents have this author?" is one SQL statement there, and 44 or more
HTTP pages from a client. The cost of every walk grows with the catalog, and the
catalog grows: about 22,000 documents on 2026-08-02 (nexus-ohxzu), 43,745 on
2026-11-02 (the census count, Research Findings). A walk that is fine today is a
timeout at the next doubling.

At the 7.94.0 shakedown (T2 `nexus/shakedown-2026-11-02-umbrella-post-7.94.0`)
a plain `nx doctor` on the laptop's managed tenant ran past a 30-minute cap
with no output. Stack dumps showed three walks. That instance is fixed (see
"Shipped since the draft"). The class is not: on 2026-11-16 (develop
`2e09b8cfc`) 28 calls of `all_documents` remain in 19 files, and four of those
files gained a walk between 2026-11-05 and 2026-11-07, after the census was
taken (Key Discoveries), because nothing checks for a new one.

**Scope, narrowed 2026-11-16.** Sam took this RDR to the gate in a narrower
form (T2 `nexus/draft-rdr-triage-2026-11-16`, its RDR-241 paragraph, and Sam's
decision of 2026-11-16 by dialog, relayed by the coordinator):

1. A ratchet: a lint that lists every call site, refuses a new one, and lets the
   count only fall (Gap 4).
2. The unattended walks (Gap 3): MCP catalog search by author, and one more this
   revision found, the read that operator dispatch makes.
3. The engine routes the remaining walks need (an author filter, a metadata
   filter, a sensitive-document locator route), and switching walks to engine
   routes that already exist (Gap 1).

Moving tenant-wide data migrations into the engine (Gap 2) waits for the
background-job mechanism of nexus-f3yqd.2 (designed in RDR-246) and is deferred
inside this RDR (see "Deferred inside this RDR"). The gap numbers keep their
draft values, so Gap 2 is missing from the list below on purpose.

The census (a read-only count of every call site, Research Findings) sorts each
site into a class, and this RDR uses the names throughout. AGGREGATE: the site
asks a question the engine could answer (a count, a set, the documents with a
given value). BULK JOB: the command's job is to read or change every row, with
per-row work on the client. BOUNDED: the read is capped to one page. HOT PATH: a
walk on the index path itself; none is left.

### Enumerated gaps to close

#### Gap 1: Tenant-wide questions are answered by client walks, and some already have an engine route

Ten call sites (of 25, Key Discoveries) ask a question the engine could answer
without sending the client every document: a set of collections, the documents
whose `source_uri` starts with a prefix, the documents with a given `meta` value
or author, the sensitive documents. Five of the ten have an engine route today
that the client does not use. `enrich.py:4176-4179` builds a set of collection
names from every document although `distinct_doc_collections()`
(`http_catalog_client.py:1594`) returns it. The two `backfill` commands read
every document to find the `chroma://` or `https://` ones although the engine
lists by `source_uri` prefix (`CatalogHandler.java:684`, `:689`). `nx doctor
--check-references` (`doctor_references.py:154-164`, `:288`) walks the catalog
to find documents whose `source_uri` is not a `file://` path, and `nx doctor`'s
sensitive-document rows fall back to a walk when the bounded `/sensitive` read
is unavailable or capped (`catalog/sensitivity_doctor.py:136-142`). Four more
need a route that does not exist: an author filter (MCP search), a `meta` filter
(`nx catalog list --meta` and `nx catalog gc`), and a way to list the sensitive
documents with their file locations (operator dispatch). The tenth, the citation
join in `link_generator._all_entries`, has no route designed here (Critical
Assumptions).

#### Gap 3: Unattended reads still walk the catalog

A walk in a command a person typed costs that person time. A walk reached
without anyone typing a command costs shared capacity for every session at
once: 19 sessions started together on 2026-10-31 (nexus-75w5d). Most of the
unattended paths the draft named are closed (see "Shipped since the draft").
Two remain, and both are reads an agent triggers:

- The MCP tool `catalog_search` called with `author` as its only filter lists
  every document and filters on the client (`mcp/catalog.py:279-281`; the
  comment there reads "the engine has no author filter"). The walk was left on
  purpose: a capped page reported absence for authors past the cap
  (nexus-3bafq), and `tests/test_3bafq_catalog_search_at_scale.py:83-90` pins
  that the answer is complete.
- Operator dispatch. An MCP operator (summarize, rank and similar) runs its
  prompt in a child `claude -p` process. When that child is allowed Read, Grep
  or Glob (`operators/dispatch.py:1261-1262`), `file_tool_restriction` lists
  every catalog document to find the sensitive ones and write deny rules for
  their files (`operators/sensitive_paths.py:261`). This walk landed on
  2026-11-07 (`5b5c5c328`, RDR-243), after the census, and the draft did not
  name it.

Gap 3 survives, scoped to these two. The other unattended shapes the census
listed read one collection, not the tenant (a store batch's
`CollectionDocumentsCache`, `mcp_infra.py:482`; the `store_list` documents mode,
`catalog/store_hook.py:366`), so they fall outside the definition above and stay
in the census.

#### Gap 4: Nothing stops a new walk from landing

The class has been fixed instance by instance (nexus-ohxzu: walk once instead
of per owner; nexus-75w5d: refuse under `--auto`; the 7.94.0 shakedown: one
walk per doctor run) and recurs. No lint names the call sites. Four walks
arrived in three days after the census (Key Discoveries), and a new
`all_documents(limit=0)` in a diagnostic or a hook passes review today.

### Shipped since the draft (not gaps)

| What the draft listed | State on 2026-11-16 | Evidence |
| --- | --- | --- |
| `nx doctor` walks the catalog three times | The three index-run rows (stale fences, failed runs, allocator drift) read two bounded engine census routes. The health sweep that `nx doctor` and the console run (`health.run_health_checks`), and `nx doctor --check-index-runs`, make no catalog walk; on an engine without the routes the three rows read "not checked". | Engine: `CatalogCensusRoutes.java:20-31` (javadoc), `:51` `indexRunCensus`, `:73` `nextSeqDrift`. Client: `http_catalog_client.py:1269-1286`. Pinned by `tests/test_doctor_one_catalog_walk.py:97-136`. Not covered by that test: the sensitive-document rows that `nx doctor` adds (`commands/doctor.py:2975`), which still fall back to a walk (Gap 1). |
| `ManifestSectionLabelsRung.verify()` reaches the whole-catalog scan with no unattended guard | `verify()` returns a deferral under `--auto` on a managed service and scans nothing. | `upgrade_ladder/rungs/manifest_section_labels.py:971-977`; test `tests/upgrade/test_manifest_section_labels_rung.py:1132`. |
| `nx catalog suggest-links` reads one page of 10,000 and silently truncates | It reads each content type in full through the server-side type filter. | `commands/catalog_cmds/links.py:606-610`; test `tests/test_catalog_cli.py:2635` (the RDR is the 10,001st document). |
| Link generation on the index path is a HOT PATH walk | The index path lists no whole content type. The generators take their documents from the indexer's hook, and `_entries_of_type` is reached only by the `nx catalog generate-links` full pass (a BULK JOB). | RDR-193 Step 7 (nexus-u5m5g.8), shipped in conexus 8.1.2 (`rdr-193:1611`); RDR-193 closed 2026-11-16; `catalog/link_generator.py:53-66`. |
| Exports and recovery bundles must stream | The tenant export (`nx backup`, cloud arm) is a session of short requests over an engine-held snapshot, which is the only shape the cloud edge (the public gateway in front of the engine) allows (60 seconds an exchange). RDR-248 states it is not a client walk. | RDR-248 (closed), `rdr-248:246-260`. `nx catalog recovery export` still walks (`catalog/recovery_bundle.py:178-182`, `:491`) and stays in the table as a BULK JOB. |

Three terms the table uses: a *rung* is one step of the `nx upgrade` ladder (a
data migration with `detect`, `converge` and `verify` steps); a *manifest* is a
document's ordered list of chunk records; *T3* is the store of chunk text and
vectors.

The draft also listed a read-only per-owner high-water route and an
`index_state` census as missing. Both exist now as the census routes above, so
they are not in this RDR's plan.

### Deferred inside this RDR: Gap 2, tenant-wide jobs run as client loops

Fourteen call sites (of 25) must touch every row and stay client loops for now:
export and recovery bundles, backfills, reconciles, prune, audit, and the two
upgrade rungs that scan every document
(`manifest_section_labels.py:503`, `manifest_span_fill.py:333`). A data
migration such as the `manifest-section-labels` rung took about 15 minutes per
session on a managed catalog (nexus-75w5d), and a migration of rows that live
in the engine belongs in the engine, not in a client loop. The engine's
mechanism for that is a batched, resumable background job: nexus-f3yqd.2, whose
design is RDR-246 (accepted 2026-11-16) and whose children are mostly open
(`bd show nexus-f3yqd.2`). Building a second mechanism here would duplicate it.

**Trigger to revive Gap 2:** nexus-f3yqd.2's job mechanism is built, which is
observable as nexus-f3yqd.2.22 closed (RDR-246's Minimum Viable Validation: an
upgrade boot with a registered move over `nexus.chunks`, with its progress shown
in `nx doctor`). Until then these sites are frozen in the ratchet table as BULK
JOB rows, so they can shrink and cannot grow. Which rungs can be expressed as a
registered move is decided at revival, not here.

## Relationship to Prior RDRs

Searched docs/rdr for "unbounded", "walk", "all_documents", "managed" and
"aggregate route"; read the beads named in the frontmatter. Statuses read on
2026-11-16.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-193 (closed 2026-11-16) | Adjacent | Same diagnosis for INDEX-TIME work: the per-owner reconcile loop and housekeeping moved to the engine (Part A). Its Step 7 removed the HOT PATH row this RDR named. Its Part B (taxonomy compute) moved to RDR-251. Scope boundary below. |
| RDR-246 (accepted 2026-11-16) | Mechanism for the deferred Gap 2 | Data moves run as batched, resumable background jobs, never at engine boot (nexus-f3yqd.2). The engine-side migrations this RDR once planned wait for it. |
| RDR-248 (closed) | Shipped streamed export | Reads "streamed" as a session of short requests over an engine-held snapshot (`rdr-248:246-260`). |
| RDR-243 (closed) | Source of two walks | Added the sensitive-document reads: the doctor rows, `nx catalog sensitive`, and the dispatch-time path denial (Gap 3). |
| RDR-146 (closed) | Precedent | Gap 4 was an unbounded client BFS on the link graph (nexus-41otf), fixed by routing it to the engine. Same class, same remedy, shipped. |
| RDR-192 (closed) | Adjacent | Keyset cursor on the listing route; makes a walk correct, not cheap. Kept for BULK JOB exports. |
| RDR-168 (closed) | Origin | Defined the `all_documents(limit=0)` contract this RDR retires for managed tenants. |
| RDR-236 (accepted) | Adjacent | Engine batch routes capped at 1000 per call; catalog-owned document metadata makes several aggregates answerable in SQL. |
| RDR-155 (closed) | Context | T3 lives in engine PG, so T3 group-by aggregates are engine-answerable too. |

Scope boundary with RDR-193 (now closed, so each row says where the work stands):

| Concern | RDR-193 | RDR-241 |
| --- | --- | --- |
| Per-owner reconcile, housekeeping, rename detection in `nx index` | shipped (Part A) | no |
| Taxonomy discover compute | RDR-251 | no |
| Link generation on the index path (census HOT PATH, `link_generator.py:60`) | shipped (Step 7, nexus-u5m5g.8): the index path has no tenant-wide listing left, and `_entries_of_type` is reached only by the `nx catalog generate-links` full pass (BULK JOB) | lists the remaining full-pass call in the ratchet table |
| Tenant-wide questions in catalog verbs, MCP tools and dispatch | no | yes (Gaps 1, 3) |
| Upgrade-ladder data migrations | no | deferred inside this RDR (Gap 2, trigger above) |
| The ratchet lint over call sites | no (`rdr-193:309` left it here) | yes |

## Context

### Background

Found by the 7.94.0 shakedown (above). The doctor timing, live: killed at
1800 s with no output on 7.94.0; 60 s on the shakedown branch (one walk, a
measurement of an intermediate state). Doctor's three walks were then replaced
by the census routes (nexus-f3yqd.6), so the 60 s figure no longer describes
doctor; it is kept here as the measurement that started this RDR.

### Technical Environment

Managed service: engine (Java, Postgres 17) behind the public edge
`api.conexus-nexus.com`; the client reaches the catalog through
`HttpCatalogClient` (`src/nexus/catalog/http_catalog_client.py`). Local mode
runs the same engine on the laptop, so the same routes serve both. A *route* is
one HTTP path the engine answers (`GET /v1/catalog/docs/distinct-collections`).
The engine's release rules are in `docs/agents/engine-service-release.md`, the
ledger of engine routes a client depends on is `docs/wire-contract-pending.md`,
and the rule against new SQLite state is the first hot rule of `AGENTS.md`;
there is no `service/AGENTS.md` at `2e09b8cfc`.

## Research Findings

### Investigation

Two measurements, both read-only. The first is a census of every whole-tenant
walk on develop `dfcd64679` (2026-11-02), T2 `nexus/rdr-241-walk-census`
(36176): it traced each call site to the command, hook, MCP tool or daemon that
reaches it and classified it. The second is this revision's re-count on develop
`2e09b8cfc` (2026-11-16): an `ast` walk over `src/nexus` that counts every call
whose callee is named `all_documents` and records the enclosing function. The
script is about fifteen lines; Phase 1 Step 1 keeps it as the ratchet's scanner.
I read each of the 25 sites, the engine routes it could use, and the code the
draft's claims point at.

### Key Discoveries

- **Verified (source):** on `2e09b8cfc`, `grep -rn 'all_documents(' src/nexus`
  prints 42 lines in 23 files. Of those, 28 are call expressions in 19 files
  (25 distinct functions), 2 are the definitions of `all_documents`
  (`catalog_protocol.py:41`, `http_catalog_client.py:2714`), 2 are the helper
  `enumerate_all_documents` (`recovery_bundle.py:178`, its caller `:491`), and 10
  are comments, docstrings or messages. The ratchet counts the 28 call
  expressions, which is the number that matters.
- **Verified (source):** the census counted 30 call expressions in 17 files
  (it counted the recovery-bundle call twice). Since then five calls went away
  (the four in `health.py`, when doctor moved to the census routes, and
  `suggest-links`) and four files gained a walk after the census was taken:
  `catalog/sensitivity_doctor.py` (`b16ec9f42`, 2026-11-05),
  `commands/catalog_cmds/sensitivity.py` (`fc5820f9a`, 2026-11-05),
  `upgrade_ladder/rungs/manifest_span_fill.py` (`7e205178e`, 2026-11-06) and
  `operators/sensitive_paths.py` (`5b5c5c328`, 2026-11-07). Nothing flagged any
  of them.
- **Verified (source):** by the census's classes the 25 sites are: AGGREGATE 10
  (a question the engine can answer), BULK JOB 14 (every row needs per-row work
  or a write), BOUNDED 1 (`catalog_list`, two calls that fetch one page of
  `limit + 1`). HOT PATH is 0. The table in Technical Design lists each site
  with its disposition. The classes are my reading of what each function does
  with the rows; the first live ratchet run puts them under test.
- **Verified (source):** the engine already exposes `/v1/catalog/stats` (`CatalogHandler.java:290`),
  `/docs/collection-counts` (`:385`), `/docs/distinct-collections` (`:384`),
  `/docs/orphaned` (`:387`), `/docs/absolute-paths` (`:388`),
  `/owners/all-with-roots` (`:389`), `/links/orphaned` (`:326`),
  `/verify/relation-counts` (`:422`), `/sensitive` (`:316`), `/index-run/census`
  (`:342`) and `/owners/next_seq_drift` (`:348`), and `GET /list` filters by
  collection, content type, corpus, owner, file path, `source_uri` and
  `source_uri_prefix` (`:677-709`). Client wrappers exist for the ones the
  switches below use: `distinct_doc_collections` (`http_catalog_client.py:1594`),
  `list_scoped` (`:2682`, the `source_uri_prefix` read),
  `list_by_collection` (`:2749`), `resolve_many` (`:2775`), `sensitive_documents` (`:1145`).
- **Verified (source):** missing routes that sites need: an author filter and a
  metadata key filter (`handleList`, `CatalogHandler.java:666-711`, takes
  neither, and the route switch at `:272-422` has no path for them), and a
  route that lists the sensitive documents with their locations
  (`/sensitive` returns only `tumbler` and `physical_collection`,
  `CatalogBoundedReadRoutes.java:44-79`). The draft also listed a per-owner
  high-water, an `index_state` census, link group-by counts and a T3
  group-by as missing. The first two exist (the census routes). The last two
  serve `nx catalog link-audit` (a typed command; its MCP wrapper
  `catalog_link_audit` is a demoted plain function, `mcp/catalog.py:5`,
  `:950`) and the `store_list` documents mode (one collection), so neither is
  an unattended tenant walk and neither is in this RDR's plan.
- **Verified (source):** the author filter cannot be the engine's existing
  full-text search. The MCP tool promises a case-insensitive substring
  (`mcp/catalog.py:191`, the predicate at `:241`), while the engine's
  `searchDocuments` matches whole tokens
  (`CatalogRepository.java:3053`, `plainto_tsquery`): a partial name such as
  "hild" is not a word of "Hildebrandt", so the search would not return it.
  A new substring route is needed to keep the answer complete.
- **Verified (source):** the engine adds a read route as a new path, never as a
  parameter on an existing route, because an old engine ignores an unknown
  parameter and still answers 200, which a client reads as a real answer
  (`CatalogCensusRoutes.java:20-31`, `CatalogBoundedReadRoutes.java:44-52`).
  On `/list` an ignored author filter would return the whole catalog, which is
  the walk this RDR removes. Each new route also needs a `/version` flag and,
  for cloud clients, an entry in the public edge's allowlist
  (`VersionHandler.java:566-572`; the edge is outside this repo).
- **Verified (source):** the metadata filter has reading rules the engine must
  match: values compare as text, a boolean reads `true` or `false`, an absent
  key matches nothing except a key with a stated default
  (`extraction_source` means `file`; `catalog/meta_filter.py:13-18`, `:25`,
  `:46-59`). `nx catalog gc` also needs an integer lower bound
  (`int(meta["miss_count"]) >= 2`, `maintenance.py:89`).
- **Verified (source):** the three existing routes the switches use return the
  same documents the walks read: live only (`deleted_at IS NULL`) in
  `listDocuments` (`CatalogRepository.java:3145-3149`), `documentsByCollection`
  (`:3407-3412`), `documentsByOwnerContentTypes`
  (`CatalogLinksRepository.java:184-211`, which matches the URI prefix
  literally and case-sensitively, as `str.startswith` does) and
  `distinctDocCollections` (`CatalogRepository.java:12483-12490`).
- **Verified (source):** `nx doctor`'s sensitive rows read `/sensitive` with a
  limit of 10,000 and fall back to a walk when the engine lacks the route,
  answers 503 or the list is capped (`sensitivity_doctor.py:69`, `:110`, `:117-142`).
- **Verified (source):** the stat registry `nx doctor --check-references` uses
  covers five non-file schemes: `obsidian`, `x-devonthink-item`, `nx-scratch`,
  `nx-note`, `https` (`aspect_readers.py:2120-2127`). The check today also
  lists a document whose scheme is registered nowhere (for example `chroma://`,
  or a Windows drive path read as scheme `c`) and reports it as unknown, which
  never sets the check's exit code (the golden test of nexus-uwggq.1.4).
- **Documented:** catalog size 22k (2026-08-02) to 43.7k (2026-11-02). No repo
  document states a growth rate.

### Critical Assumptions

- [x] The ratchet's scanner finds every call of the form `x.all_documents(...)`
  — **Status**: Verified (source): the `ast` walk gave 28 calls in 19 files on
  `2e09b8cfc`, matching a manual read of the grep. It cannot see a call
  through an alias or `getattr`, which the design states as a blind spot and
  covers by listing the wrapper helpers — **Method**: Source Search
- [x] The engine's existing list routes return the same rows the walks read —
  **Status**: Verified (source), the Key Discovery on live rows above —
  **Method**: Source Search
- [ ] Dropping documents with an unregistered `source_uri` scheme from
  `nx doctor --check-references` is acceptable — **Status**: Assumed. Those rows
  can only ever report "unknown", which never sets the check's exit code, and
  `nx catalog backfill source-uri` exists to rewrite the `chroma://` ones. A
  registered scheme spelled in capitals (`HTTPS://`) is counted under its
  scheme today; a case-sensitive per-scheme read would drop it, which is
  outside this assumption. Sam decided on 2026-11-16 (ask uwggq15-scheme-case,
  option C, "Use the engine route instead") that registered schemes in any case
  stay checked, so the `--check-references` switch (nexus-uwggq.1.5) moves to
  Phase 2 behind an engine route that matches the scheme prefix without regard
  to case. Checked before the switch ships by
  running the check on one fixture before and after and diffing the reports
  — **Method**: Test
- [ ] An engine filter on `author` or on a `meta` key answers in one statement
  fast enough without a new index at 50,000 documents — **Status**: Unverified.
  I found no index on `catalog_documents.metadata` or on `author` in the
  Liquibase changelogs (only the full-text vector and the collection and
  `source_uri` columns; a comment at
  `resources/db/changelog/vectors-044-manifest-search-meta-keys.xml:25-26`
  mentions a containment index from nexus-6pfgb.38 that I could not locate), so
  each statement is assumed to scan the tenant's rows — **Method**: Spike
  (`EXPLAIN ANALYZE`, Postgres's timed query plan, in an engine integration
  test on a 50,000-row fixture; an index only if it is slow, by a Liquibase
  changeset)
- [ ] The citation join in `link_generator._all_entries` (bib ids across all
  content types, `link_generator.py:39-50`) is expressible as SQL —
  **Status**: Unverified, and not planned here: the site stays in the table
  as AGGREGATE until someone designs a route — **Method**: Source Search

## Proposed Solution

### Approach

One rule, stated for managed and local alike because both run the same
engine: **client code does not page through every document of a tenant to
answer a question.** The engine answers tenant-wide questions through routes.
A client walk survives only where the command's job is to read or change every
row, and those sites are listed and frozen until the engine's job mechanism can
take them (Gap 2, deferred).

Applied to this RDR's scope, in the order the work can ship:

- **Ratchet first (client only).** A lint lists every call site of
  `all_documents` with its class, refuses a new one, and lets the list only
  shrink. It lands before any switch, so nothing new arrives while the rest
  moves.
- **Switch to routes that exist (client only).** Eight sites (Gap 1) read
  through an engine route instead of a walk, or instead of the walk's
  unscoped branch. A switch that removes a call lowers the table in the same
  commit.
- **Add the routes that are missing (engine, then client).** Three new read
  routes: an author filter, a metadata filter and a sensitive-document locator
  route. They serve MCP search (Gap 3), operator dispatch (Gap 3) and two catalog
  verbs (Gap 1).
- **A client never falls back to a walk.** When the engine lacks a route, the
  client reports "not checked" or an error that names the engine version that
  has it, or, for operator dispatch, takes the file tools away (the rule that
  already applies when the catalog cannot be listed,
  `operators/dispatch.py:1263-1270`).
- **BULK JOB and BOUNDED sites stay.** A BOUNDED site keeps its bound where it is
  set. A BULK JOB site stays a client loop, frozen in the table, until
  nexus-f3yqd.2's mechanism is built (trigger under Gap 2).

### Technical Design

#### The ratchet

A *ratchet* is a check whose limit can move only one way: here, the number of
walk call sites can fall and cannot rise. Its parts:

- **Table.** `src/nexus/tables/walk-sites.toml`, read through
  `src/nexus/tables/walk_sites.py`, the shape of
  `chunk-metadata-keys.toml` (RDR-236) and checked by
  `tests/test_tables_lint.py` like every table there. One dimension, `site`,
  lists every walk site as `<repo-relative path>::<qualified function>` (a
  line number is not part of the key; it moves on every edit). One row per site
  carries `class` (a closed set: `AGGREGATE`, `BULK JOB`, `BOUNDED`), `calls`
  (how many `all_documents` calls the function holds), `reason` (one sentence)
  and, for a site that is going away, `next` (the step that removes it).
- **Scanner.** A test, `tests/test_walk_site_ratchet.py`, in the lint bucket
  (`pytestmark = pytest.mark.lint`, with one cached scan, `tests/AGENTS.md:157`).
  It parses every `src/nexus/**/*.py` with `ast` and counts the calls whose
  callee is named `all_documents`, per enclosing function.
- **Rules.** The scan and the table must match exactly. A call in a function the
  table does not list fails, naming `path:line` and the function. A listed
  function with no call fails. A function whose call count differs from its row
  fails, whether it rose or fell. So the count falls only by a commit that also
  edits the table, which a reviewer sees. A `class` outside the set fails.
- **Not vacuous.** A planted source tree with one new call must be reported, and
  a scan that parses no files must fail (the nexus-moht0 rule). The first live
  run prints files examined, files that failed to parse (a failure, not a
  skip), call expressions and sites, and that run goes into the bead before
  close. Expected on `2e09b8cfc`: 28 calls, 25 sites, 19 files, 0 skipped.
- **Blind spots, stated.** A call through an alias or `getattr`
  (`f = cat.all_documents; f()`) escapes an AST scan. A new caller of a helper
  that wraps a walk escapes it too. The table therefore lists the four existing
  wrappers as ordinary sites and names each one's callers in its `reason`:
  `enumerate_all_documents` (`recovery_bundle.py:178`), `_all_entries` and
  `_entries_of_type` (`link_generator.py:39`, `:53`) and
  `reference_only_candidates` (`doctor_references.py:154`). Other whole-tenant
  shapes that are not `all_documents` calls (`find_all`'s grow-until-short loop,
  `link_audit`'s page loop, `bulk_unlink`'s dry-run count) stay in the census
  and out of this scanner; adding them is a later extension of the same table.

The frozen list on `2e09b8cfc` (25 sites, 28 calls). "P1" is Phase 1 (client
only), "P2" is Phase 2 (engine, then client):

| Site | Class | Calls | What it does with the rows; disposition |
| --- | --- | --- | --- |
| `catalog/link_generator.py::_all_entries` | AGGREGATE | 1 | Citation join over bib ids. No route designed; stays (Critical Assumptions). |
| `catalog/link_generator.py::_entries_of_type` | BULK JOB | 1 | `nx catalog generate-links` full pass (RDR-193 Step 7). Stays. |
| `catalog/meta_backfill.py::backfill_meta` | BULK JOB | 1 | Writes meta on every document. Stays. |
| `catalog/recovery_bundle.py::enumerate_all_documents` | BULK JOB | 1 | `nx catalog recovery export`. Stays. |
| `catalog/sensitivity_doctor.py::_walk_sensitive` | AGGREGATE | 1 | Fallback when `/sensitive` is unavailable or capped. P1: report "not checked", no walk (the call leaves the table). |
| `commands/catalog.py::list_cmd` | AGGREGATE | 1 | `--meta` and `--sensitive` read every document of a type. P1: `--sensitive` through `/sensitive` and `resolve_many`. P2: `--meta` through the metadata route. The call then remains only in its bounded mode, so the row becomes BOUNDED. |
| `commands/catalog.py::reconcile_cmd` | BULK JOB | 1 | Heals manifest gaps for every document. Stays. |
| `commands/catalog_cmds/backfill.py::backfill_source_uri_cmd` | AGGREGATE | 1 | Wants the `chroma://` rows. P1: `list_scoped(source_uri_prefix="chroma://")`. |
| `commands/catalog_cmds/backfill.py::backfill_etags_cmd` | AGGREGATE | 1 | Wants the `https://` rows when no owner is given. P1: `list_scoped(source_uri_prefix="https://")`. |
| `commands/catalog_cmds/doctor.py::_run_store_put_integrity` | BULK JOB | 1 | Reads each note's manifest and T3 chunks (`doctor.py:1309-1345`). Stays. |
| `commands/catalog_cmds/integrity.py::_audit_membership_all` | BULK JOB | 1 | Per-row membership. Stays. |
| `commands/catalog_cmds/integrity.py::_verify_full` | BULK JOB | 1 | Per-row verify; the ghost rows feed the manifest attribution. Stays. |
| `commands/catalog_cmds/maintenance.py::gc_cmd` | AGGREGATE | 1 | Wants documents with `miss_count >= 2`. P2: the metadata route with an integer lower bound. |
| `commands/catalog_cmds/reconcile_fences.py::reconcile_fences` | BULK JOB | 1 | Per-candidate manifest check and stamp. Stays. |
| `commands/catalog_cmds/reconcile_stale.py::_classify` | BULK JOB | 1 | Per-row T3 presence. Stays. |
| `commands/catalog_cmds/remediation.py::prune_stale_cmd` | BULK JOB | 2 | File existence on disk. P1: the `--collection` branch uses `list_by_collection` (calls 2 to 1). |
| `commands/catalog_cmds/remediation.py::remediate_paths_cmd` | BULK JOB | 2 | Per-row path resolution. P1: the `--collection` branch uses `list_by_collection` (calls 2 to 1). |
| `commands/catalog_cmds/sensitivity.py::_targets` | BULK JOB | 1 | `nx catalog sensitive` with no tumbler, `--collection` or `--owner`. Stays. |
| `commands/enrich.py::_aspect_bearing_collections` | AGGREGATE | 1 | Builds a set of collection names. P1: `distinct_doc_collections()`. |
| `doctor_references.py::reference_only_candidates` | AGGREGATE | 1 | Wants non-`file://` rows. P1: one `list_scoped` read per registered scheme. |
| `mcp/catalog.py::catalog_list` | BOUNDED | 2 | One page of `limit + 1`. Stays. |
| `mcp/catalog.py::catalog_search` | AGGREGATE | 1 | Author alone, unattended (Gap 3). P2: the author route. |
| `operators/sensitive_paths.py::file_tool_restriction` | AGGREGATE | 1 | Per dispatch, unattended (Gap 3). P2: the locator route. |
| `upgrade_ladder/rungs/manifest_section_labels.py::ManifestSectionLabelsRung._scan` | BULK JOB | 1 | Gap 2, deferred. Guarded unattended on a managed service. |
| `upgrade_ladder/rungs/manifest_span_fill.py::ManifestSpanFillRung._scan` | BULK JOB | 1 | Gap 2, deferred. Guarded the same way (`manifest_span_fill.py:599`, `:639`, `:712`). |

If every P1 and P2 line lands as written, the count goes 28, then 21 after
Phase 1, then 18 after Phase 2: 14 BULK JOB, 3 BOUNDED (the two `catalog_list`
calls and `list_cmd`'s bounded mode) and one AGGREGATE (the citation join).
That is a forecast from reading the code, not a measurement; each step reports
the real count.

#### Engine routes (Phase 2)

Interfaces, not implementations. Each is a new path, `GET`, tenant-scoped like
every catalog route, and follows the shape of `CatalogBoundedReadRoutes`: a
bounded `limit` with a `capped` flag where the answer can be long, the
statement timeout answering 503 (the client reads 503 and 404 as "cannot say",
never as "none"), and a flag in `GET /version` for the client to probe. Names
are settled in the engine review; the rule that each is a new path is not.

- **By author.** `GET /v1/catalog/docs/by-author?author=<text>&limit=&offset=`:
  live documents whose `author` contains the text, case-insensitively and
  literally (`%` and `_` escaped, as `CatalogLinksRepository.likePrefix` does),
  in tumbler order, as `{documents, count}`. Replaces the walk at
  `mcp/catalog.py:279-281`; the tool's other filters still apply on the client
  to a page that is complete for the author.
- **By metadata.** `GET /v1/catalog/docs/by-meta?content_type=&meta.<key>=<value>&meta_min.<key>=<n>&meta_default.<key>=<text>&limit=&offset=`:
  live documents, optionally of one content type, whose `metadata` satisfies
  every `meta.` pair under the reading rules of `catalog/meta_filter.py`
  (text comparison, booleans as `true` and `false`, an absent key matches
  nothing) and every `meta_min.` bound (an integer at least `n`; a value that
  is not an integer matches nothing). A key with a stated default sends it as
  `meta_default.<key>`, so the default table stays in `meta_filter.py` and is
  not copied into the engine. Replaces the scans in `nx catalog list --meta`
  and `nx catalog gc`. An equivalence test runs `documents_with_meta` and the
  route over one fixture and requires the same documents.
- **Sensitive locators.** `GET /v1/catalog/sensitive/locators?limit=N`: the
  live sensitive documents and the live documents whose `alias_of` names one (a merged duplicate points at
  its canonical document through `alias_of`, and its file holds the same content),
  each as `{tumbler, source_uri, file_path, alias_of, physical_collection}`,
  in the envelope of `/sensitive` (`{count, limit, capped, documents}`, limit
  up to 10,000). These are the fields `_restrict` and `_locator` read
  (`sensitive_paths.py:107-121`, `:195-200`). A capped answer is "cannot say"
  and takes the file tools away, the existing rule for a catalog that cannot be
  listed.

No new table. The routes read `catalog_documents` through jOOQ (the Java SQL library the
engine uses). An index is added only if the spike (Critical Assumptions) shows
one is needed, through a Liquibase changeset (the engine's schema-migration
tool); there is no SQLite state anywhere in this design.

#### Client rules

A switch probes the route's `/version` flag first (`_version_flags`,
`http_catalog_client.py:3460`) and sends nothing to an engine without it. The
absent-route outcome is per site: MCP search answers an error row that names
the engine version that has the filter, `nx catalog list --meta` and
`nx catalog gc` exit with the same message, `nx doctor` prints "not checked",
dispatch drops the file tools. A client keeps `documents_with_meta` and the
MCP `_keep` predicate as the final filter over the route's page, so an engine
bug cannot widen an answer silently.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Read routes (author, metadata, sensitive locators) | `CatalogHandler`, `CatalogBoundedReadRoutes`, `CatalogRepository`; `/sensitive` as the pattern (`CatalogBoundedReadRoutes.java:44-79`) | Extend, as new paths |
| Switches to existing routes | `distinct_doc_collections`, `list_scoped`, `list_by_collection`, `resolve_many`, `sensitive_documents` on `HttpCatalogClient` | Reuse unchanged |
| Unattended guard for upgrade rungs | `upgrade_ladder/auto_guard.py`, `unattended.py` (covers `detect`, `converge`, `verify`) | Reuse unchanged; shipped |
| Doctor census reads | `CatalogCensusRoutes`, `http_catalog_client.py:1269-1286` | Reuse unchanged; shipped |
| Ratchet table | `src/nexus/tables/` checked tables, `tests/test_tables_lint.py` | Reuse the pattern |
| Engine-side migrations | nexus-f3yqd.2 / RDR-246 job mechanism | Deferred (Gap 2) |

### Decision Rationale

The engine already holds every row these walks read. Moving the question
there turns a cost that grows with the tenant into one query, and removes the
per-page round trip the client pays from every box. The instance fixes so far
(ohxzu, 75w5d, the shakedown, the doctor census routes) each removed one walk
and left the class in place, which the four walks added after the census show:
this is the same argument RDR-193 made for index time. The ratchet is the part
that stops the class recurring; the routes and switches shrink what it lists.
Gap 2 is deferred rather than designed here because a second job mechanism
beside nexus-f3yqd.2's would have to be thrown away when that one lands.

## Alternatives Considered

### Alternative 1: Keep the walks and bound them with deadlines

**Description**: every walk takes a deadline and reports partial results.

**Pros**: no engine work; works today.

**Cons**: a bounded walk answers a different question (a sample) and still
costs shared capacity up to the deadline on every session. The `--auto`
deadline that bounds a rung's walk on a local engine
(`upgrade_ladder/auto_guard.py:9`) is this alternative, and its cost still
grows with the tenant.

**Reason for rejection**: it moves the failure from "never finishes" to
"answers partially", which a doctor row or a search result cannot honestly
present as a verdict.

### Alternative 2: Cache the walk on the client

**Description**: one walk per day, cached on disk, read by every check.

**Reason for rejection**: stale by design, one walk per box per day is still
O(tenant), and a cache of catalog rows on the client is state the engine
already owns.

### Alternative 3: Answer author search with the engine's existing full-text search

**Description**: send the author text to `GET /v1/catalog/search`, then keep
the documents whose author matches.

**Cons**: that route matches whole tokens (`CatalogRepository.java:3053`), the
tool promises a substring, so a partial name would find nothing. The first
page of matches is also capped, which is the defect (nexus-3bafq) the
current walk was written to avoid.

**Reason for rejection**: it changes what the tool answers. A substring route
keeps the answer complete.

### Alternative 4: Fall back to the walk when the route is missing

**Description**: probe the route's flag and walk on an engine that lacks it, as
the doctor's sensitive rows do today.

**Reason for rejection**: the fallback is the walk, and it runs exactly on the
clients that are behind. A client and engine ship as a pair, so the case is
rare, and when it happens "not checked" or an error that names the engine
version is the honest answer.

### Briefly Rejected

- **Bigger pages**: a constant factor; the engine batch cap is 1000 (RDR-236).
- **The table under `docs/tables/`**: nothing reads the ratchet table at run
  time, so that repo-only home would keep it out of the wheel. The table stays
  under `src/nexus/tables/`, where the first draft and Sam's scope put it and
  where RDR-236's key table lives; it is a few kilobytes.

## Trade-offs

### Consequences

- Positive: MCP search, operator dispatch and the catalog verbs stop growing
  with the catalog, and a new walk cannot land unnoticed.
- Negative: three new engine routes, each with a wire-contract entry, a
  `/version` flag, an edge allowlist entry and an engine release; the client
  changes that use them need the engine first (the paired release
  choreography).
- Negative: a deliberate behaviour change in `nx doctor --check-references`
  (rows with an unregistered scheme leave the report; Critical Assumptions).
- Negative: the ratchet table is one more file a reviewer reads each time a
  walk is added or removed. That is the point of it.

### Risks and Mitigations

- **Risk**: a client must keep working against an older engine.
  **Mitigation**: each switch reads a `/version` flag and answers "not checked"
  or an error that names the engine version (never a walk) when the route is
  absent.
- **Risk**: the metadata route answers differently from `meta_filter.py`
  (booleans, absent keys, defaults).
  **Mitigation**: an equivalence test over one fixture, and the client keeps
  its own filter as the last step.
- **Risk**: the AST scanner misses a walk written through an alias.
  **Mitigation**: stated as a blind spot; the wrappers are listed; review is
  the backstop.
- **Risk**: the author or metadata statement is slow without an index.
  **Mitigation**: the spike at 50,000 documents before the engine release; an
  index by Liquibase if it is slow.

### Failure Modes

A missing route shows as a "not checked" row, an error row or dropped file
tools, each naming the engine version that has the route. A ratchet failure
names the new call site and its `path:line`.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions that gate Phase 2 verified: the engine spike
  (statement time at 50,000 documents) and the equivalence test of the metadata
  route. Phase 1 has no open assumption except the `--check-references`
  behaviour change, which its own test checks.
- [x] RDR-193 closed 2026-11-16: no prerequisite remains with it.
- Not a prerequisite: nexus-f3yqd.2. This plan does not depend on its job
  mechanism; only the deferred Gap 2 does.

### Minimum Viable Validation

On a local engine (not the managed tenant: a walk against it runs only in an
announced window), seeded with 50,000 documents of which a few carry a known
author, `catalog_search(author="lamport")` returns the same documents as
today, and the HTTP client counts no `GET /list` request without a filter
during the call. The same fixture run through `file_tool_restriction` makes one
request, to the locator route. Before Phase 2 the same calls make 50 paged
`GET /list` requests of 1,000 each (`http_catalog_client.py:2738-2746`).

### Phase 1: Client only (hours)

Ships as one client release, or rides the next one; needs no engine change.

#### Step 1: Ratchet first

Land `walk-sites.toml`, its loader, the scanner test and the planted-tree
test, with the 25 sites above as the frozen list. Run it live once and paste
the counts it examined and skipped (expected 28 calls, 25 sites, 19 files, 0
skipped) into the bead before close.

#### Step 2: Switch to routes that exist

One change per site, each with a test that counts HTTP calls on a fake
catalog and a test that the rows are the same as the walk's, and each lowering
the table in the same commit:

- `_aspect_bearing_collections` uses `distinct_doc_collections()`.
- `backfill_source_uri_cmd` and `backfill_etags_cmd` (no owner) use
  `list_scoped(source_uri_prefix=...)`.
- `reference_only_candidates` makes one `list_scoped` call per registered
  non-file scheme (`<scheme>:`), then applies its existing filters. The
  Critical Assumption on unregistered schemes is checked here.
- `prune_stale_cmd` and `remediate_paths_cmd` use `list_by_collection` for
  `--collection`.
- `nx catalog list --sensitive` reads `/sensitive`, then `resolve_many`. The
  route returns at most 10,000 rows, so on a tenant with more than 10,000
  sensitive documents a `--type` or `--meta` listing, or a page past row
  10,000, refuses and names `--owner`, where the walk listed them. Sam shipped
  that regression on 2026-11-17 (ask uwggq17-sensitive-cap, option C) and Phase
  2 closes it: the metadata route (nexus-uwggq.2.4, .2.14) and a type filter on
  the sensitive route.
- `sensitivity_doctor._walk_sensitive` stops falling back to a walk: an
  unavailable or capped read is "not checked", with the cause. The dedicated
  `nx catalog doctor --sensitive-with-aspects` check exits "could not check"
  (code 3, distinct from pass, fail and a usage error) when its row reads "not
  checked", and the general `nx doctor` sweep keeps those rows non-gating.

Expected count after this phase: 21 calls.

### Phase 2: Engine routes, then client (days, with an engine release)

Four routes, each a repository method, a handler case, a `/version` flag and
engine tests against real Postgres. The fourth, added by Sam's 2026-11-16
decision (option C), lists documents whose `source_uri` starts with a scheme
prefix matched without regard to case; `nx doctor --check-references`
(nexus-uwggq.1.5) switches to it, and the spike covers its statement (`CatalogBoundedReadRoutes` and its tests are
the pattern). Sized in days because of the engine release and the paired client
release, not because the routes are large; the estimate is mine and unmeasured.

#### Step 3: Spike and engine routes

Run the 50,000-row `EXPLAIN ANALYZE` first (Critical Assumptions). Then build
the author route, the metadata route and the locator route, with the
equivalence test for the metadata route. Each route that a client depends on
gets an `## Unshipped` entry in `docs/wire-contract-pending.md`, led by
`[additive]` with its direction-safety prose (old client with new engine is
safe: nothing calls a new path; new client with old engine is safe: the client
probes the flag and does not call it), moved to `## Shipped` when the client
release ships. The engine tag is Sam's to cut, and the cloud edge allowlist for
the three flags is the conexus side's to change (engine-release pre-tag check
3e), so the plan names both as handoffs, not steps.

#### Step 4: Client switches

After the engine release: `catalog_search` uses the author route,
`nx catalog list --meta` and `nx catalog gc` use the metadata route,
`file_tool_restriction` uses the locator route. Each behind its flag, each with
the absent-route outcome from Client rules, each lowering the table. A client
test per site drives a fake engine that has the route and one that does not.
Raise `REQUIRED_ENGINE_VERSION` in the client release that depends on them
(`.claude/rules/releases-and-engine.md`).

Expected count after this phase: 18 calls.

### Deferred: Gap 2 (trigger: nexus-f3yqd.2's job mechanism is built)

Not scheduled. When the trigger fires, rewrite Gap 2 against that mechanism and
the table's remaining BULK JOB rows, and decide which rungs can be expressed as
a registered move.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Ratchet table | In scope (the table) | In scope | N/A | In scope (the lint) | N/A |
| New routes | N/A | `/version` flags | N/A | engine tests | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: a new `all_documents(limit=0)` added in a diagnostic, in a
  function the table does not list — **Verify**: the ratchet fails naming
  `path:line` and the function.
- **Scenario**: a table row whose function no longer calls `all_documents`, and
  a row whose `calls` differs from the scan — **Verify**: each fails.
- **Scenario**: a planted tree with one call, and an empty tree — **Verify**:
  the first is reported, the second fails as a scan that examined nothing.
- **Scenario**: each Phase 1 switch on a fake catalog of 50,000 documents —
  **Verify**: the HTTP client counts the route's request and no unfiltered
  `GET /list`, and the rows equal the walk's rows on the same fixture.
- **Scenario**: `nx doctor --check-references` on one fixture with a row of each
  registered scheme, a `chroma://` row and a drive-path row, before and after
  the switch — **Verify**: the reports differ only by the two unregistered
  rows.
- **Scenario**: `nx doctor` sensitive rows when `/sensitive` is unavailable, and
  when it answers capped — **Verify**: both read "not checked" with the cause,
  and no walk is made.
- **Scenario**: the metadata route against `documents_with_meta` over one
  fixture (text, booleans, an absent key, `extraction_source`, an integer
  bound, a non-integer value) — **Verify**: the same documents.
- **Scenario**: `catalog_search(author="lamport")` on a fake engine with the
  author route and on one without — **Verify**: the first returns
  `tests/test_3bafq_catalog_search_at_scale.py:83-90`'s answers with no
  unfiltered `/list`; the second returns an error row naming the engine
  version.
- **Scenario**: operator dispatch with `Read` allowed, on a fake engine with the
  locator route, one without, and one that answers capped — **Verify**: deny
  rules from the first; file tools dropped in the other two; no `/list` in any.

## Validation

### Testing Strategy

Engine routes get repository tests against real PG, including the 50,000-row
spike; client switches get tests that count HTTP calls on a fake catalog; the
ratchet gets its planted-tree and empty-tree tests. The MVV runs on a local
engine. Nothing in this RDR measures against the managed tenant.

### Performance Expectations

The target is a cost that does not depend on the catalog size for the sites
this RDR moves: one request per author search, per dispatch, per `--meta` list.
The current cost is 50 paged requests at 50,000 documents (the MVV fixture),
about 44 at today's 43,745. The engine statement time at 50,000 documents is
the number the spike produces; this RDR states no latency target before it.

## Finalization Gate

### Contradiction Check

Checked the narrowed text against itself on 2026-11-16. The call-site count
appears as 28 calls in 19 files (42 grep lines in 23 files) in the Problem
Statement, Research Findings and the ratchet's starting value, and the
forecasts (21 after Phase 1, 18 after Phase 2) appear only as forecasts.
Gap 2 is described in one place, as deferred, and no phase builds it. Gap 3
names the same two unattended walks (MCP author search, operator dispatch's
sensitive-path list) wherever it is cited. No contradiction found.

### Assumption Verification

Research records `nexus_rdr/241-research-1` to `-5`. Three are verified by
source search: the call-site census, the two unattended walks, and the
shipped pieces with the existing routes a walk can switch to. Two are
assumed, each with a named check before the work it gates: the author and
meta filter speed at 50,000 documents (a spike in Phase 2, before those
routes ship) and the per-scheme switch in `nx doctor --check-references`
(a before-and-after fixture diff, before that one Phase 1 switch ships).
The first gates Phase 2 only. The citation join in `link_generator._all_entries` is unverified
and outside the plan: that site stays in the ratchet table as AGGREGATE.

### Scope Verification

The scope is the narrowed one Sam took on 2026-11-16: the ratchet (Gap 4),
the unattended walks (Gap 3), and the routes and switches for Gap 1. Every
in-scope gap has a phase: Gap 4 is Phase 1's first step, Gap 3 and the
Gap 1 switches to existing routes are Phase 1, and the new author, metadata
and sensitive-locator routes are Phase 2. Gap 2 (tenant-wide jobs) is
deferred with the trigger "nexus-f3yqd.2's job mechanism is built" and is not
a gate item. The operator-dispatch walk was found during the revision and is
inside Gap 3's definition (an unattended path that reaches a walk), so it is
in scope.

### Cross-Cutting Concerns

- **Versioning**: each new route is an additive path with a `/version` flag, an
  edge allowlist entry and an `## Unshipped` entry in
  `docs/wire-contract-pending.md` led by `[additive]`.
- **Deployment model**: engine release first, client after (paired
  choreography); the tag and the deploy are Sam's.
- **Incremental adoption**: Phase 1 is client-only and ships before any engine
  change; each Phase 2 client switch is behind its flag.
- **Persistent state**: none added. The routes read existing PG tables; the
  ratchet table is a TOML file. No SQLite (AGENTS.md hot rule 1).
- Build tool compatibility, Licensing, IDE compatibility, Secret/credential
  lifecycle: N/A.

### Proportionality

Phase 1 is client-only, about hours: a ratchet test over a checked table and
switches of existing walks to routes the engine already serves. Phase 2 is
three additive engine routes, a spike, and one engine release, about days,
and each client switch is gated on the route's `/version` flag. The design
adds no new storage and no new service. The size matches the problem: the
walks grew by four files in three days after the census, and nothing stops
the next one without the ratchet.

## References

- T2 `nexus/rdr-241-walk-census` (36176): the census (2026-11-09, base
  `dfcd64679`) and prior art. This revision's re-count (2026-11-16, base
  `2e09b8cfc`) supersedes its counts; the census record is left as written.
- T2 `nexus/draft-rdr-triage-2026-11-16`: the triage and Sam's decision to gate
  RDR-241 narrowed.
- T2 `nexus/shakedown-2026-11-02-umbrella-post-7.94.0` (36168): the finding.
- Beads nexus-mq980, nexus-75w5d, nexus-ohxzu, nexus-ial1f, nexus-0lv0r (all
  closed); nexus-f3yqd.2 (open, the Gap 2 trigger), nexus-f3yqd.6 (closed, the
  doctor census routes).
- RDR-193, RDR-243, RDR-246, RDR-248 (Relationship to Prior RDRs).
- `src/nexus/mcp/catalog.py`, `src/nexus/operators/sensitive_paths.py`,
  `src/nexus/catalog/http_catalog_client.py`, `src/nexus/catalog/meta_filter.py`,
  `src/nexus/tables/chunk-metadata-keys.toml`,
  `service/src/main/java/dev/nexus/service/http/CatalogHandler.java`,
  `CatalogBoundedReadRoutes.java`, `CatalogCensusRoutes.java`,
  `docs/wire-contract-pending.md`, `docs/agents/engine-service-release.md`.

## Revision History

- 2026-11-02: created (draft) after the 7.94.0 shakedown.
- 2026-11-16: narrowed to the scope Sam took on this date (T2
  `nexus/draft-rdr-triage-2026-11-16`, RDR-241 paragraph; Sam's decision on the
  draft triage, by dialog, relayed by the coordinator): the ratchet, the
  unattended walks, the routes and the switches to existing routes. Re-counted
  the call sites on `2e09b8cfc` (42 grep lines, 28 call expressions, 19 files,
  25 sites) and replaced the census's 30 and 17. Recorded as shipped: doctor's
  census routes, the `verify()` guard, `suggest-links`, the HOT PATH row
  (RDR-193 Step 7) and RDR-248's streamed export. Corrected the draft's missing-route list (the
  high-water and `index_state` reads exist). Gap 2 deferred to nexus-f3yqd.2
  with the trigger "its job mechanism is built". Gap 3 kept, scoped to MCP
  author search and, found in this pass, the operator-dispatch read
  (`sensitive_paths.py:261`). Implementation Plan rebuilt as Phase 1 (client
  only, hours) and Phase 2 (engine and client, days). Status stays draft.
- 2026-11-16: Gate round 1 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); blob `641b2a2bd7fe`; critique `nexus_rdr/241-gate-critique-2026-11-16-r1`.
- 2026-11-16: The acknowledged assumption on `nx doctor --check-references` corrected to match the code (standing grant 3; nexus-uwggq.1.4 golden test and its critique T2 `nexus/critique-0d91dac32-nexus-uwggq.1.4-2026-11-16-r1`): unregistered-scheme rows report "unknown", not "not stattable", and never set the exit code; the fixture holds two such rows; an upper-case registered scheme is outside the assumption.
- 2026-11-16: Sam chose the engine route for registered schemes in any case (ask uwggq15-scheme-case, option C, superseding his earlier option A): the `--check-references` switch (nexus-uwggq.1.5) moves from Phase 1 to Phase 2, which gains a fourth route, a case-insensitive scheme-prefix list.
- 2026-11-17: `nx catalog list --sensitive` (nexus-uwggq.1.7) ships with a refusal on filtered or past-10,000 reads of a tenant over 10,000 sensitive documents (Sam, ask uwggq17-sensitive-cap, option C); Phase 2 closes it with the metadata route and a type filter on the sensitive route (standing grant 3).
- 2026-11-17: The sensitive-document doctor rows (nexus-uwggq.1.8) read "not checked" with a cause that is true for each input and says what to do; `nx catalog doctor --sensitive-with-aspects` exits 3 ("could not check") when its row reads "not checked", and `nx doctor` keeps the rows non-gating (standing grant 3).
