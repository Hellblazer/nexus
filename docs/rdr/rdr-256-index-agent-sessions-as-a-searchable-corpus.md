---
title: "Index agent sessions as a searchable, expandable corpus"
id: RDR-256
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-21
accepted_date:
related_issues: []
related_rdrs: [RDR-103, RDR-131, RDR-171, RDR-204, RDR-207, RDR-223, RDR-229, RDR-240, RDR-243, RDR-246]
---

# RDR-256: Index agent sessions as a searchable, expandable corpus

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

Terms used throughout. A **session** is one run of Claude Code, the coding
agent this project is developed with. Claude Code writes every session to a
**transcript**: a JSONL file (one JSON object per line) under
`~/.claude/projects/<project>/<session-id>.jsonl`, with one more file per
**subagent** (a helper agent the session dispatched) under
`<session-id>/subagents/`. Each line is a **record**: a user prompt, an
assistant reply, a tool call, a tool result, or one of about thirty kinds of
bookkeeping line the harness writes. Records point to their parent record by
id, so a transcript is a tree. The **active path** is the branch from the
last record back to the first that the conversation actually followed;
records the user rewound past, and subagent traffic copied into the main
file, are off it. **Compaction** is what Claude Code does when the context
window fills: it replaces the conversation the model sees with a short
summary and writes a `compact_boundary` record into the file. The records
before the boundary stay in the file.

Nexus has three storage tiers. **T1** is per-session scratch, **T2** is
durable project memory (decisions, findings, the threads board), and **T3**
is the permanent semantic store, where text is split into **chunks**, each
embedded as a vector for similarity search. A T3 document is a **catalog**
row (the engine's PostgreSQL index of every indexed file or note), and its
**manifest** (`document_chunks`) lists its chunks in order. A chunk's id is
its **chash**, the SHA-256 of its text. Chunks live in **collections**; each
collection has a **content type** (today one of `code`, `docs`, `rdr`,
`knowledge`), an **owner** and an embedding model.

What a session reasoned, tried and rejected lives only in its transcript.
Nexus reads transcripts today only to count things, and Claude Code deletes
them after about 30 days. This RDR makes past sessions searchable in T3 and
lets a hit open the verbatim records it came from.

### Enumerated gaps to close

#### Gap 1: A past session's reasoning cannot be searched

Nothing in `src/` or `conexus/` puts transcript content into T3
(256-research-1). The only readers count: `transcripts.sessions`
(RDR-240) returns per-session counts, tool-call inputs and, on request,
assistant text, for procedures to tally; `nx memory curate` reads only each
session's first record; `rdr-audit` counts matching directories. To find
"the session where we decided to drop the lexical leg, and why", an agent
greps 2.9 GB of JSONL by hand, which the `rdr-audit-checklist` skill already
says a subagent cannot do (`conexus/skills/rdr-audit-checklist/SKILL.md:57`).
Compaction makes this worse inside a single session: on 2026-10-31 two
compactions of about 969,000 tokens each kept about 0.5 percent of the
context (`conexus/hooks/scripts/compact_checkpoint.py:9-11`). The rest is
still in the file and nowhere else.

#### Gap 2: A search hit cannot be expanded to what was actually said

Even with an index, a chunk is a fragment. A reader who finds "we rejected
option B" needs the surrounding records verbatim, including the tool result
that showed B failing. Nexus's span forms do not fit a transcript: RDR-171's
`dchash` span hashes the whole document's text and needs a readable source
file, so a transcript that grows makes every span stale at its next write,
and Claude Code's deletion makes it unresolvable (256-research-10).

#### Gap 3: Transcripts carry secrets, and indexing them sends text to new parties

A transcript holds everything the session saw: command lines, file
contents, environment output. A scan of all 137 main nexus transcripts found
44 secret-shaped strings (key prefixes, private-key headers, bearer
headers), every one in a tool-call input or a tool result
(256-research-5). A live token reached a subagent transcript on
2026-09-25 (`conexus/hooks/scripts/routing/credential_print_guard.py:18-21`).
Indexing into T3 sends text to the embedder, which is Voyage AI (an outside
embedding service) on Sam's managed install, and stores it in the managed
tenant, the cloud database the nexus service keeps for that install
(256-research-8). Both are LLM or outbound surfaces under RDR-243.
Nexus's only redactor matches a value after a key word such as `token=`,
and would miss a bare token (`src/nexus/redact.py:13-15`).

#### Gap 4: No content type fits a session corpus

A session corpus searched by default would put conversation chatter into
every agent's `knowledge` search and into the topic list every subagent
starts with. The four content types are a closed set, and the only way a
search skips a live collection today is by its lifecycle state
(256-research-9). Sessions also belong to a repository the way code and
docs do, which a `knowledge` subject cannot express. Sam chose a fifth
content type, `session`, owned per repository (T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`, Q2).

## Relationship to Prior RDRs

Scanned the RDR index for session, transcript, conversation, summary and
collection-naming titles; read RDR-131, RDR-207, RDR-240, RDR-243, RDR-171
and RDR-229.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-131 T2 Session Rollup Summaries | Abandoned predecessor, different object | It proposed LLM rollup summaries over aged **T2 memory entries** (not transcripts) to bound context injection. It was abandoned on 2026-09-12 as a batch with RDR-132 and RDR-133 because it was an unresearched stub, explicitly "not a ruling that the idea is wrong" (rdr-131 `abandon_reason`). Nothing here repeats it: this RDR indexes transcripts, writes no summaries, and changes nothing about T2. |
| RDR-207 Summarization at the T2 Expiry Boundary | Precedent | It took RDR-131's live half and decided that a rollup is an attended command, never a session-end side effect (rdr-207:392), because session end is the worst context for an LLM call (:334-339) and a hallucinated summary is worse than none (:344-346). This RDR makes no LLM call anywhere, in a hook or otherwise. |
| RDR-240 Procedures as Lua scripts | Origin of the transcript reader | Its `transcripts.sessions` verb owns the file listing and line framing (`src/nexus/lua_runtime/transcripts_read.py`). This RDR reuses those, not the verb: the verb is a read-only count surface and never returns tool results (rdr-240:361-365). It has no active-path walk (256-research-2). |
| RDR-243 Document sensitivity | Hard constraint | Its LLM-surface table (`src/nexus/tables/llm-surfaces.toml`) gets this RDR's surfaces as rows, and a session the scanner flags is stamped `sensitive`. The flag is not the corpus-wide protection, because it withholds a document from the session model by default (Q1), and agents are this corpus's readers (256-research-8). |
| RDR-103 / RDR-204 collection names and attributes | Constraint | RDR-103's name grammar and RDR-204's per-content-type embedding profile both enumerate content types; `session` joins them (256-research-12). |
| RDR-229 The collection name becomes a label | Sequencing dependency | Its Phase 3 is rewriting the client's name grammar and render paths and will make new collections minted `c<id>` labels found by attributes (P3.6, P3.8, in progress). This RDR's content-type step lands after P3.8 (256-research-13). |
| RDR-171 dchash spans | Considered, not used | Its span does not survive an append or a deleted source (Gap 2). Expansion here uses the manifest's line columns instead. |
| RDR-223 / RDR-181 document write and embed skip | Reused | A document write replaces its manifest in one write, and the engine skips re-embedding a chunk whose text it already holds (256-research-7). That is the incremental mechanism. |
| RDR-246 Data moves are background jobs | Constraint | The backfill of existing transcripts is an explicit command, never engine-boot work. |

## Context

### Background

The comparison of nexus with diderot (an open-source question-answering
service from Bluesky that indexes Slack, Notion and coding-agent sessions)
ranked this the clearest gap (T3 `research-diderot-vs-nexus-2026-11-21
(2/2)`, axis 6 and shortlist item 1). Diderot decodes each harness's
format into a private record model, keeps the active path only, splits a
session at record boundaries into about 30 KiB chunks, summarizes each with
an LLM, and expands a hit to the verbatim record range (256-research-6).

Two facts about diderot change what nexus should copy. Diderot embeds the
**summaries only**; the verbatim records are never embedded, only returned
by expand. And its redaction is "the producer's job": the gitleaks scanner
it ships redacts image captions, not sessions (256-research-6).

### Technical Environment

- Transcripts on Sam's laptop: 14 GB in all; the nexus project holds 2.9 GB
  in 137 main files and 7.9 GB in 4,403 subagent files; the oldest main
  file is from 2026-10-22, consistent with Claude Code's roughly 30-day
  cleanup (`cleanupPeriodDays`; `src/nexus/memory_curate/__init__.py:148-153`)
  (256-research-3).
- Sam's install runs `mode: managed`: T3 lives in the managed cloud tenant
  and every collection embeds through Voyage (256-research-8).
- New persistent state goes to PostgreSQL through the engine (the
  NO-SQLITE hot rule in `AGENTS.md`).
- RDR-229 Phase 3 is in flight on the collection registry, the name grammar
  and the render paths (beads nexus-p2m1x.17, .26 and .28 in progress on
  2026-11-21; 256-research-13).
- More terms. **bge** is the embedding model a local install runs on its
  own machine, so nothing leaves it. **Fan-out** is how a search over a
  corpus such as `knowledge` queries every collection of that type and
  merges the results. The **Knowledge Map** is the list of topics injected
  into every subagent's starting context; **taxonomy labelling** names
  those topics with an LLM. A **tumbler** is a catalog document's or
  owner's hierarchical id (such as `1.1.42`). A **curator** is a catalog
  owner that is not a repository, used for corpora no repository owns. A
  **label** is what RDR-229 turns a collection's name into, and a
  **minted** label is the `c<id>` form the engine gives a new collection
  once RDR-229's P3.8 lands. **Liquibase** is the tool the engine uses to
  change its PostgreSQL schema. The **orphan reaper** is the engine job
  that deletes chunks no manifest names. A **canary** test plants a unique
  string in the input and asserts it never appears in any output.

## Research Findings

### Investigation

Read the transcript readers in `src/nexus/lua_runtime/` and
`src/nexus/memory_curate/`, the indexing write path in
`src/nexus/doc_indexer.py`, the corpus routing in `src/nexus/corpus.py` and
`src/nexus/mcp/core.py`, the sensitivity code and tables, the engine's
embedding-profile classes, and RDR-131, RDR-171, RDR-207, RDR-229, RDR-240
and RDR-243. Read diderot's `internal/sessiondecode/FORMATS.md`,
`internal/sessioningest/{summarizer,publish,pushexpand,pushretention}.go`,
`internal/sessionship/sync.go`, `internal/attachments/files/redact.go`,
`docs/session-ingest.md` and `docs/design.md`. Ran read-only Python scans
over Sam's own nexus transcripts (byte composition, active-path coverage,
secret-shaped strings, subagent composition, prefix stability), printing
counts only, and ran detect-secrets 1.5.0 in an isolated environment with
sockets blocked. Read the licence, release and plugin metadata of seven
candidate scanners.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Claude Code transcript format | Yes (real files) | Records carry `uuid`, `parentUuid`; `compact_boundary` records carry `logicalParentUuid`, which often names a record absent from the file (256-research-4). No published schema; the format can change without notice. |
| diderot `sessiondecode` | Yes | Active-path rule, record splitting and id scheme (FORMATS.md:31-62). Fixtures are synthetic (FORMATS.md:33-35). |
| Engine document write | Yes | One write per document, manifest replaced, re-embed skipped for held text (doc_indexer.py:2384-2389, :2844-2850). |
| Engine embedding profile | Yes | Content type to model token is a constant map (`EmbeddingProfile.java:65-68`); `embedding_profile` is keyed `(tenant_id, content_type)` (catalog-036). |
| detect-secrets 1.5.0 | Yes (sdist and a run) | Apache-2.0; `scan_line` in-process; network verification only when its verification filter is configured, which in-process Settings do not do by default (256-research-15). |

### Key Discoveries

- **✅ Verified** (source search): no code path indexes transcript content
  into T3; the existing readers count. *Source: 256-research-1.*
- **✅ Verified** (source search): nexus's decoder has no active-path walk;
  it drops sidechain lines and repeated tool-call ids only. *Source:
  256-research-2.*
- **✅ Verified** (spike): narrative is about 1 percent of transcript bytes.
  In the 20 newest main nexus transcripts, assistant text, thinking,
  human-typed prompts and subagent reports total about 15.9 MB of about
  1.5 GB; tool results and tool-call inputs are about 125 MB; the rest is
  bookkeeping. Subagent transcripts hold almost no assistant prose (0.2 MB
  in a 300-file sample); a subagent's substance reaches the main transcript
  as its report. A full decode runs at about 45 MB/s in Python. *Source:
  256-research-3.*
- **✅ Verified** (spike): diderot's active-path rule, run on these files,
  keeps 38.8 percent of user and assistant records, because the walk stops
  at a compaction boundary whose logical parent is missing from the file.
  Continuing from the previous record in file order at such a boundary
  keeps 98.1 percent. *Source: 256-research-4.*
- **✅ Verified** (spike): secret-shaped strings appear only in tool-call
  inputs and in tool results other than subagent reports, never in
  assistant text, thinking, user text, Agent-tool results (5,263 blocks) or
  task notifications (15,006 blocks), across all 137 main nexus
  transcripts. *Source: 256-research-5, 256-research-16.*
- **⚠️ Documented** (source search): diderot embeds summaries only and
  redacts no session content. *Source: 256-research-6.*
- **✅ Verified** (source search): the incremental mechanism exists (one
  write per document, embed skip), and the manifest already has
  `line_start`/`line_end` position columns. *Source: 256-research-7.*
- **⚠️ Documented** (source search): the `sensitive` flag withholds a
  document from agents by default; on a managed install every collection
  embeds through Voyage and is stored in the tenant. *Source:
  256-research-8.*
- **✅ Verified** (source search): bare-corpus fan-out has no way to skip a
  live collection except lifecycle state. *Source: 256-research-9.*
- **⚠️ Documented** (source search): RDR-131, RDR-207 and RDR-171
  constraints as tabled above. *Source: 256-research-10.*
- **✅ Verified** (spike): the five newest nexus transcripts kept their
  earlier bytes unchanged while four of them grew. *Source:
  256-research-11.*
- **✅ Verified** (source search): the census of sites a fifth content type
  touches, the "Site list" table under Technical Design. Four client sites
  fall back to `code` for an unknown prefix without an error, so a missed
  site embeds or scores sessions as code silently. No live engine name
  regex and no packaged table enumerates content types. *Source:
  256-research-12.*
- **⚠️ Documented** (source search): RDR-229 P3.6 and P3.8 rewrite the name
  grammar and every render-for-new-collection path; after P3.8 a new
  collection needs no grammar change. *Source: 256-research-13.*
- **✅ Verified** (source search): scanner candidates' licences, release
  dates and plugin coverage. *Source: 256-research-14.*
- **✅ Verified** (spike): detect-secrets without its two entropy plugins
  flagged no line of 42,365 assistant-text lines and caught 8 of 12
  synthetic secret shapes; with them it flagged 99.44 percent of lines.
  *Source: 256-research-15.*
- **✅ Verified** (spike): the chosen combination, those 25 plugins plus
  four nexus plugins, caught 12 of 12 synthetic shapes and flagged no line
  of 42,456 assistant-text lines or 24,256 subagent-report lines from the
  same five sessions. *Source: 256-research-17.*

### Critical Assumptions

- [ ] **A1.** Claude Code only appends to a transcript and never rewrites an
  earlier line, so a chunk's line range stays valid while the file exists.
  **Status**: Partly verified; diderot states it ("Claude Code never edits
  an entry", FORMATS.md:37-41), and the prefixes of five live nexus
  transcripts stayed byte-identical while four of them grew
  (256-research-11). Not yet checked across a resume, a rewind or a Claude
  Code upgrade; expand's re-render check catches a violation either way.
  **Method**: Spike.
- [ ] **A2.** At a compaction boundary whose logical parent is missing, the
  previous record in file order is the record the conversation continued
  from. **Status**: Unverified; only coverage was measured
  (256-research-4), not order. **Method**: Spike (Phase 1 Step 1 compares
  the reconstructed path with the compaction summary's own account on
  three real sessions).
- [ ] **A3.** Vector and lexical search over the narrative rendering finds
  session content well enough to be useful. **Status**: Unverified.
  **Method**: Spike (the Minimum Viable Validation's retrieval check).
- [x] **A4.** Re-sending a grown session's chunks re-embeds only new chunk
  text. **Status**: Verified (256-research-7). **Method**: Source Search.
- [ ] **A5.** After RDR-229 P3.8, the engine registers a collection with
  `content_type = session` and finds it by `(content_type, owner_id,
  embedding_model)` with no further engine change than the embedding
  token. **Status**: Unverified; the client refuses only an empty content
  type and no engine vocabulary check was found, but the Java registration
  path was not read (256-research-12). **Method**: Source Search at Phase 1
  Step 4.
- [x] **A6.** detect-secrets scans in-process without a network call when
  its verification filter is not configured. **Status**: Verified; the
  42,365-line run completed with sockets blocked (256-research-15).
  **Method**: Spike.

## Proposed Solution

### Approach

Index the narrative of each main session into a per-repository `session`
collection, redacted, with no LLM call; record each chunk's JSONL line range
in the manifest; expand a hit by reading those lines back from the
transcript. Concretely:

1. **What is indexed.** Main transcripts only, active path only, narrative
   only: human-typed prompts, assistant text, plaintext thinking, subagent
   reports (the result of an `Agent` tool call, and a task notification that
   carries one), and one short line per tool call naming the tool and its
   target (a file path, the first 160 characters of a command, a query).
   Tool inputs, tool results, harness-injected text (`isMeta`) and
   bookkeeping lines are never embedded; expand returns them.
2. **Where it lives.** A fifth content type, `session`. A repository's
   sessions go in one collection whose owner is the repository's tumbler;
   sessions whose working directory is in no registered repository go in
   one collection owned by a `sessions` curator. One catalog document per
   session. A `session` collection is searched only when a caller names it.
3. **Privacy.** detect-secrets, pinned, runs in-process over every chunk
   before it is written and over every expand result before it is returned;
   a match becomes `[redacted:<type>]`. A session with any match is also
   stamped `sensitive` (RDR-243). Indexing is off until the install gives a
   one-time acknowledgement that names where the text goes; on Sam's
   managed install that is Voyage and the tenant (Sam, Q1).
4. **When.** An explicit `nx index sessions` command. A session written to
   in the last five minutes indexes only its sealed chunks. A later phase
   adds an opt-in SessionEnd hook that runs the same command for the ending
   session; it makes no LLM call.
5. **Incremental by content addressing.** Chunk boundaries are computed
   from the start of the active path, so earlier chunks keep their text as a
   session grows. Each run re-decodes a changed file, re-renders all chunks
   and writes the document; the engine embeds only chunks it has not seen.
6. **Expand through a source interface.** Phase 1 reads the transcript file;
   the verbatim archive in PostgreSQL that Sam chose (Q4) is the next RDR,
   and expand will read from it through the same interface.

### Technical Design

**Decoder.** A new module, `src/nexus/sessions/decode.py` (name provisional),
reusing `transcripts_read.py`'s project listing, line framing and
oversized-line guard (promoted from their underscore names). It emits
records `(record_id, line_no, kind, text, tool_name, tool_target, ts)`.
Ids follow diderot's scheme: the native `uuid`, with
`<uuid>:<kind>:<block>` when one line becomes several records. The active
path follows diderot's rule (FORMATS.md:42-48) with one change, taken from
256-research-4: at a `compact_boundary` whose `logicalParentUuid` is not in
the file, the walk continues from the previous uuid-bearing record in file
order. The later of two lines with one uuid wins. A parent cycle rejects the
file, which is logged and skipped. The decoder version is an integer stored
on the document.

**Renderer and chunks.** Each record renders to one block, for example
`[user] ...`, `[assistant] ...`, `[thinking] ...`,
`[subagent report: <description>] ...`, `[tool Bash] git -C ... status`.
Chunks are filled greedily at record boundaries from the start of the path to
a target size, set by the chunk-size check in Phase 1 Step 2 and recorded
once as a research finding. A chunk carries a one-line header naming the
session's title and date so a hit reads in context. The renderer version is
stored on the document; a change to it re-renders every session at its next
run, which costs embeds and is announced in the command's output.

**The `session` content type.**

- *Name and label.* RDR-103's grammar is
  `<content_type>__<owner>__<embedding_model>__v<n>`. Under RDR-229 the name
  becomes a label, and from P3.8 a new collection gets a minted `c<id>`
  label and is found by its attributes `(content_type, owner_id,
  embedding_model)` (256-research-13). This RDR registers `session`
  collections only after P3.8, so it never renders a `session__...` name:
  the writer looks the collection up by attributes and, when none exists,
  registers one by attributes and takes the minted label. `session` is
  still added to `_CONTENT_TYPES`, because that tuple is also the
  vocabulary `resolve_corpus` accepts as a bare word (`corpus.py:2662`).
- *Owner is the source.* Per Sam's ruling of 2026-11-21 (T2
  `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`), the owner is set
  once at registration and never re-derived. For a session whose recorded
  working directory belongs to a repository, by the membership rules of
  `transcripts_projects.py` (main checkout, worktrees, Claude's isolation
  worktrees), the owner is that repository's catalog owner tumbler; the
  repository must be registered (it has been indexed with `nx index repo`).
  Otherwise the owner is a curator named `sessions`, one per tenant
  (`(name, owner_type)` is unique, `http_catalog_client.py:952`). A session
  filed under the curator stays there when its repository registers later;
  `nx sessions forget` and a re-index moves it. Documents register under
  the same owner as their collection.
- *Embedder.* The engine's content-type token map
  (`EmbeddingProfile.java:65-68`) gains `session` with the prose token:
  `voyage-context-3` on a Voyage install, `bge-768` on a bge install,
  matching `knowledge` and `docs`. The client's
  `canonical_embedding_model` (`corpus.py:220`) returns `voyage-context-3`
  for it.
- *Explicit corpus only.* Without a per-collection attribute, exclusion is a
  property of the type: `corpus.py` gains
  `EXPLICIT_ONLY_CONTENT_TYPES: frozenset[str] = frozenset({"session"})`.
  The default corpora are literal lists that do not name `session` and stay
  as they are. The one expansion that enumerates every live content type,
  `corpus="all"` (`mcp/core.py:4305-4316`), drops the set. Taxonomy's
  default prefixes do not include it, so no topics, and so no Knowledge Map
  entries, are made for sessions. Naming `session`, or a session
  collection's label, searches it. A lint test pins it: every site that
  iterates `CONTENT_TYPES` or expands `all` either drops
  `EXPLICIT_ONLY_CONTENT_TYPES` or is listed as addressing a type a caller
  named.
- *No aspects.* Aspect extraction (an LLM surface) is selected by content
  type (`aspect_extractor.py:1081`), and an unregistered type gets none. A
  test pins that `session` stays unregistered.

**Site list.** Every site from the grep census (256-research-12), with what
it needs. "Add" means `session` joins the set with the prose behaviour;
"Exclude" means the site must drop it; "Leave" means it stays out by
design; "Read" means Step 4 reads the site and decides by the rule stated.

| Site | What it does | Disposition |
| --- | --- | --- |
| `corpus.py:28`, `:139` | content-type tuple and name-grammar alternation | Add |
| `corpus.py:220` `canonical_embedding_model` | type to Voyage model; raises on unknown | Add (`voyage-context-3`) |
| `corpus.py:1134` `voyage_model_for_collection` | name prefix to model; unknown becomes `voyage-code-3` | Add (silent fallback) |
| `corpus.py:1167` default threshold | unknown becomes code's 0.70 | Add (knowledge's value until calibrated) |
| `corpus.py:1274` | unknown becomes `code` | Add (silent fallback) |
| `corpus.py:2662` `resolve_corpus` | bare word matched against row content type | Add, through `_CONTENT_TYPES` |
| `db/http_vector_client.py:1062` | CCE prefix set | Add |
| `search_engine.py:793`, `:914` | per-type thresholds and prose over-fetch | Add |
| `scoring.py:509` | quality boost for curated prose | Leave |
| `topic_route.py:34` | prose types for taxonomy routing | Leave |
| `mcp/core.py:4305-4316` | `corpus="all"` expansion | Exclude |
| `mcp/core.py:5391` | `query(content_type=...)` routing | Add |
| `mcp/core.py` defaults `:3411`, `:3992`, `:4712`, `:4863`, `:4967`, `:5122`; `commands/search_cmd.py:174`, `:331`; `plans/runner.py:682`; `plans/owner_narrowing.py:87` | default corpora | Leave |
| `catalog/owner_scope.py:26` | corpus prefixes that take an owner scope | Add (search one repo's sessions) |
| `plans/evidence.py:639` | corpus words a plan step may name | Add |
| `plans/owner_narrowing.py:81` | project-owned types | Add |
| `indexer.py:564`, `:612`, `:624`, `:665`; `repo_identity.py:389` | repo-owned type checks and migration types | Read: add where the check means repo-owned; leave the migration list |
| `collection_shape.py:417` | repo-type default-owner rule | Add |
| `commands/taxonomy_cmd.py:2894`, `:3490` | taxonomy prefixes | Leave |
| `commands/catalog_cmds/collections.py:177` | CLI content-type choice | Add |
| `aspect_extractor.py:1081` | extractor registry by type | Leave unregistered; test pins it |
| Engine `vectors/EmbeddingProfile.java:65-68`, `EmbedderRouter.java:236-263` | type to model token, profile seeding | Add (prose token) |
| Engine `db/TenantExport.java:373-374` | content types seeded for export | Add |
| Engine `db/CatalogRepository.java:4393` | file-routed types | Leave (a session document's identity is its uuid, not a path) |
| Engine Liquibase `hygiene-002`, `hygiene-004` | one-off name walks, already applied | Leave (an applied changeset is never edited) |
| `src/nexus/tables/*.toml` | no table lists content-type values | None |

**Catalog and manifest.**

- Document identity is the session uuid. `source_uri` is
  `claude-session:<session-uuid>` (scheme name provisional; inferred, not
  read, whether the catalog accepts an unregistered scheme). The catalog
  document's own `content_type` is `session`. The title is the session's
  `custom-title` or `agent-name` record when present, else the first typed
  prompt cut to 80 characters.
- Document meta: host name, project directory, transcript path, file size
  and mtime at the last run, decoder and renderer versions, scanner version,
  record and chunk counts.
- Each manifest row sets `line_start` and `line_end` to the JSONL line
  numbers (1-based, inclusive) of the first and last record in the chunk.

**Expand.** `expand(tumbler, position) -> ExpandResult` reads the manifest
row and asks the transcript sources, in order, for the lines of that range.
It decodes them with the current decoder, re-renders the range and
compares the SHA-256 of the rendering with the row's chash. On a match it
returns the verbatim records of the range on the active path, tool results
included, each cut to 32 KiB, through the scanner. Otherwise it returns a
status and the chunk's own text:

| Status | Meaning |
| --- | --- |
| `ok` | verified; verbatim records returned |
| `source-gone` | no source holds the transcript (Claude Code cleanup, or only another host has it) |
| `changed` | the re-rendering does not match (rewind, format change, or a broken A1) |
| `other-host` | the file is not here and meta names a different host |

The source interface is what lets the archive RDR plug in without touching
expand:

```text
// Illustrative; signatures settle in Phase 1 Step 5
class TranscriptSource(Protocol):
    name: str                                   # "local-file", later "archive"
    def locate(self, session_uuid: str, meta: Mapping[str, Any]) -> SourceRef | SourceMiss
    def read_lines(self, ref: SourceRef, start: int, end: int) -> Iterator[tuple[int, bytes]]
```

Phase 1 ships one implementation, `LocalFileSource`, which opens the path in
meta on the host named there. The archive RDR adds `ArchiveSource` and puts
it after the local file. Its contract, stated here so that RDR inherits it:
it returns the same bytes at the same 1-based line numbers the file had, so
a manifest row written from the file verifies against the archive. Whether
the archive stores raw or redacted lines is that RDR's decision; expand
scans what any source returns either way. Until it lands, a deleted
transcript gives `source-gone` and the chunk text.

**Scanner.** detect-secrets 1.5.0, pinned exactly (`detect-secrets==1.5.0`),
called through `scan_line` inside `transient_settings` with every plugin
except `Base64HighEntropyString` and `HexHighEntropyString`. Its 25
remaining plugins miss four shapes this project's transcripts carry
(256-research-15), so nexus adds four plugins of its own, subclasses of
detect-secrets' `RegexBasedDetector` in `src/nexus/sessions/secret_plugins.py`
(name provisional), loaded through detect-secrets' custom-plugin path:

| Plugin | Matches |
| --- | --- |
| `AnthropicKeyDetector` | `sk-ant-` followed by 20 or more key characters |
| `VoyageKeyDetector` | `pa-` followed by 30 or more key characters, not inside a longer word |
| `BearerTokenDetector` | `Bearer` followed by 20 or more token characters |
| `MixedCaseTokenDetector` | a bare run of 32 or more letters and digits that mixes upper case, lower case and digits, with Shannon entropy of at least 4.2; hex strings (chashes, commit shas) and uuids cannot match |

Measured together, the 29 plugins caught all 12 synthetic shapes and
flagged no line of 42,456 assistant-text lines or 24,256 subagent-report
lines (256-research-17). Their patterns are a reviewed nexus list, so a new
provider format is a one-line plugin and a test. detect-secrets caches its
plugin mapping, so the plugins are configured before its first use in the
process (256-research-17). The verification filter is never configured, so
no candidate is sent anywhere, and a test runs the scanner with sockets
blocked. The redactor is `redact(text) -> (text, findings)`; a
chunk it cannot process is not written, and the run reports it. The choice
and its trade-offs are argued under Alternatives (Alternative 5).

**Sensitivity for flagged sessions.** A session whose narrative has any
scanner finding is written with `sensitive=true`; registration can only
raise the flag (RDR-243 Approach item 1). Its chunks are still redacted.
Under RDR-243 the session is then withheld from MCP read tools and from
`nx search` run inside Claude Code, kept from the reranker, and excluded
from LLM surfaces; a person at a terminal sees it with a `[sensitive]`
badge. In local mode it routes to the bge sibling of its collection, as
RDR-243 Q3 does for every sensitive document. In managed mode, where no bge
sibling exists, the install acknowledgement below is the disclosure RDR-243
Q3 requires (`--ack-external-embedder`), and its wording says so.

**Acknowledgement.** Indexing refuses until the install records an
acknowledgement. The first run prints this text, with the bracketed parts
filled from the install's posture:

> nx index sessions sends text from this machine's Claude Code session
> transcripts to [Voyage AI (api.voyageai.com) for embedding, and stores it
> in your nexus cloud tenant | the embedder on this machine, and stores it
> in your local nexus engine]. It sends your typed prompts, the assistant's
> replies and reasoning, subagent reports, and one line per tool call
> naming the tool and its target. A secret scan replaces what it
> recognises before anything is sent; it does not recognise every secret.
> Tool inputs and tool results are never sent. A session in which the scan
> found something is also marked sensitive, which keeps it from agents and
> language-model features, and is still sent in its redacted form. To
> accept, run `nx index sessions --ack`.

`--ack` writes a `sessions.ack` block to the client config (`config.yml`,
the existing home of install choices; it is configuration, not a new
database): the wording version, the endpoint list it named, the embedder,
the store (tenant or local engine), the host and the UTC time. Every run
compares the install's current endpoint list with the acknowledged one and
refuses on a difference (a local install moved to managed mode, or a bge
install that gained a Voyage key), naming both. The block is per machine,
because the transcripts are. `sessions.exclude` (session ids and project
globs) skips sessions entirely.

**Surfaces.** Two new rows in `llm-surfaces.toml`: "Session corpus:
indexing" (sends redacted narrative chunks; endpoints `voyage` and
`cloud-tenant` on a managed install, `none` on a local bge install;
behaviour `disclose`), and "Session corpus: expand" (sends redacted verbatim
records to `session-model`; behaviour `withhold`, so a sensitive or excluded
session is never returned to the model). The registry lint then covers both
call sites. No other row is added: this RDR makes no LLM call.

**Commands (names provisional).**

```text
nx index sessions [--project repo|all|<dir>] [--session <uuid>] [--since DATE] [--dry-run] [--ack]
nx sessions expand <tumbler> --position N [--json]
nx sessions forget <session-uuid>
nx sessions list [--project ...]          # indexed sessions, counts, last run, statuses
```

The command module is new (`src/nexus/commands/sessions.py`), registered on
the `index` group for the first verb; `commands/index.py` is near its size
ceiling (4,280 of 4,442 lines in `scripts/file-size-exceptions.txt`).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Project listing, line framing | `src/nexus/lua_runtime/transcripts_read.py` | Reuse: promote `_files`, `_lines` to public names. |
| Session-to-repository membership | `src/nexus/lua_runtime/transcripts_projects.py` | Reuse. |
| Active-path decoder | none (256-research-2) | New. |
| Chunk write, embed skip, manifest | `src/nexus/doc_indexer.py` write path | Reuse. |
| Line-range positions | `catalog_document_chunks.line_start/line_end` | Reuse; no schema change. |
| Collection lookup and registration by attributes | RDR-229 P3.8 | Reuse once landed. |
| Orphan cleanup when a tail chunk changes | engine orphan reaper (anti-join on manifest) | Reuse. |
| Sensitive stamping and routing | RDR-243 write path (`sensitive_write.py`) | Reuse. |
| Redaction | `src/nexus/redact.py` | Keep for its log use; detect-secrets beside it. |
| Transcript counts | `transcripts.sessions` verb | Unchanged. |

### Decision Rationale

Embedding the narrative directly is the cheaper and more honest design.
Nexus has a lexical leg and a reranker that diderot lacks (memo axis 2), so
verbatim narrative is searchable without an LLM rewriting it first. The
narrative is about 1 percent of the bytes, so volume is bounded. The scope
also follows the secrets: every secret-shaped string found sat in tool
inputs or results, which are never embedded.

The fifth content type is Sam's choice (Q2). It keeps sessions out of
default searches by what they are, not by a flag each collection must
carry, and it gives each repository its own session collection, owned by
the repository like its code and docs. Registering after RDR-229 P3.8 means
the type needs vocabulary and routing entries only, not a second rewrite of
the name grammar P3 is replacing.

Line ranges in the manifest, checked by re-rendering against the chash,
give expansion a stamp that survives appends, since only the tail chunk's
text changes, and fail visibly when the source changes or disappears. The
source interface makes the archive a second source rather than a second
expand.

## Alternatives Considered

### Alternative 1: Summaries, as diderot does

**Description**: Map/reduce LLM summaries per chunk and per session, embed
the summaries, expand to records.

**Pros**:

- Summaries read well and match "which session did X" questions.

**Cons**:

- An LLM surface that reads the full narrative of every session.
- A summary can be wrong, and RDR-207 found a wrong record worse than none.

**Decision**: deferred by Sam on 2026-11-21 ("no summaries for now"; T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`). Not in this RDR's
plan. Any summary work needs a later decision from Sam.

### Alternative 2: Index everything, tool results included

**Description**: Embed every record on the active path.

**Pros**:

- One can search for an error message a tool printed.

**Cons**:

- About eight times the embedded volume (256-research-3).
- Every secret-shaped string found is in this part (256-research-5).
- Duplicates the code and docs corpora, since most tool results are file
  reads.

**Reason for rejection**: volume and exposure, for content expand already
returns. Sam's Q1 answer also states that tool inputs and results are never
embedded.

### Alternative 3: A knowledge subject with an explicit-only collection attribute

**Description**: One `knowledge` collection, subject `agent-sessions`,
owned by its subject, kept out of default searches by a new nullable
collection attribute that fan-out, the Knowledge Map and taxonomy honour.

**Pros**:

- No new content type; one Liquibase column.

**Cons**:

- Exclusion is a per-collection flag that every new collection must carry
  and every fan-out site must remember.
- No per-repository ownership.

**Decision**: rejected by Sam on 2026-11-21 in favour of the fifth content
type (Q2). This was the first draft's recommendation.

### Alternative 4: The `sensitive` flag as the corpus-wide protection

**Description**: Index every session as a sensitive document.

**Cons**:

- MCP tools then withhold the corpus from the session model, which is its
  reader (256-research-8).

**Reason for rejection**: it defeats the purpose. The flag is kept for
sessions the scanner flags, and exclusion for sessions the operator names.

### Alternative 5: Other secret scanners

All figures from 256-research-14 and -15.

- **gitleaks** (MIT, about 150 rules, active): a Go binary with no Python
  package. Shipping it means a per-platform binary inside a Python wheel,
  or a port of its rules into nexus, which Sam declined (Q3).
- **trufflehog** (AGPL-3.0, active): a Go binary built around verifying
  candidates over the network (inferred from its documentation, not read),
  the outbound call this design must not make.
- **ggshield** (MIT client): scanning is done by the GitGuardian API, which
  sends the text to a third party.
- **bc-detect-secrets** (Apache-2.0, released 2026-09-30): the same API as
  detect-secrets and more recently released, with fewer plugins (no OpenAI,
  GitLab or PyPI token). It is the fallback if Yelp's package stops
  installing on a supported Python.
- **whispers** (BSD-3): pins about 14 runtime dependencies, semgrep among
  them.
- **secrets-patterns-db** (CC-BY-SA-4.0): a regex list, not a library, under
  a share-alike data licence.

**Why detect-secrets.** It is a Python library that runs in-process and
offline, its Apache-2.0 licence can be combined into an AGPL-3.0 work, its
two dependencies are already locked, and it covers the most common provider
formats. The cost is maintenance: its last release is from 2024-05-06,
though the repository was still being pushed to in 2026. The exact pin and
the scanner version on each document make an upgrade a visible event.

**False positives against misses.** With its entropy plugins on, the
scanner flagged 99.44 percent of assistant-text lines (chashes, commit
shas and ids look random), which would redact the very identifiers people
search for. With them off, it flagged no assistant-text line and caught 8
of 12 synthetic shapes. The four nexus plugins close all four misses,
still with no flagged line in the real narrative (256-research-17).
`MixedCaseTokenDetector` is the one rule that guesses rather than matches
a known format, so it is where a false positive would come from; it is
kept narrow (mixed case, letters and digits only, length and entropy
floors) so that hex and uuids, the identifiers this narrative is full of,
never match. What remains missed is a secret with a format no rule knows
that is also hex, all one case, or broken by punctuation. The design
accepts that miss because no secret-shaped string of any kind appeared in
the embedded narrative in the scan, only in tool inputs and results, which
are never embedded (256-research-5, -16), and because `nx sessions forget`
removes a session after the fact. The zero is a measurement on 67,000
lines of one project, not a bound; Step 3 re-measures on every project's
sessions before the first managed-mode run.

### Briefly Rejected

- **Diderot's strict active-path rule**: drops 61 percent of records on
  real files (256-research-4).
- **Subagent transcripts in phase 1**: little narrative of their own; their
  reports are already indexed through the main transcript.
- **A local index store outside the engine**: the NO-SQLITE hot rule.
- **A dchash span per chunk**: stale at the next append (Gap 2).
- **A push server, revisions, quotas (diderot's ingest protocol)**: files
  are local; one user.
- **Rendering `session__...` names before RDR-229 P3.8**: edits the
  grammar and render paths P3.6 and P3.8 are rewriting (256-research-13).

## Trade-offs

### Consequences

- Positive: past sessions answer questions in seconds; a compacted session
  can find what it said before compaction.
- Positive: no LLM call and no new model surface beyond the embedder.
- Positive: each repository's sessions are scoped and owned like its code.
- Negative: on a managed install, redacted narrative goes to Voyage and the
  tenant, which is the reason for the acknowledgement.
- Negative: until the archive RDR lands, verbatim expansion lasts only as
  long as Claude Code keeps the file, about 30 days by default.
- Negative: a fifth content type adds a row to about thirty client and
  engine sites, and the content-type step waits for RDR-229 P3.8.
- Negative: the decoder depends on an undocumented file format.

### Risks and Mitigations

- **Risk**: a secret the scanner does not know reaches the embedder.
  **Mitigation**: tool inputs and results, where every measured hit sat,
  are never embedded; nexus plugins for the formats this project uses;
  `nx sessions forget`; the canary test plants tokens in narrative.
- **Risk**: a missed site in the census treats sessions as code (four sites
  fall back silently). **Mitigation**: a test calls each fallback site with
  a `session` collection and asserts the prose result; the lint pins the
  explicit-only rule.
- **Risk**: RDR-229 P3 slips, holding Step 4. **Mitigation**: Steps 1 to 3
  do not touch the registry or the name paths and proceed in parallel.
- **Risk**: detect-secrets stops being maintained. **Mitigation**: exact
  pin; bc-detect-secrets offers the same API.
- **Risk**: Claude Code changes the transcript format.
  **Mitigation**: the decoder rejects a structurally broken file and logs
  it; fixtures from real files (text replaced) pin each known shape; the
  decoder version re-renders on upgrade.
- **Risk**: agents treat transcript text as decisions.
  **Mitigation**: hits are labelled `session transcript`; T2 stays the
  record of decisions (see Relation to T1, T2 and the board).

### Failure Modes

- Indexing fails on one file: the run reports it with its reason and
  continues; the document keeps its previous chunks.
- The engine is unreachable: nothing is written; the next run redoes the
  work.
- The acknowledged endpoints no longer match the install: every run refuses
  and names both lists.
- Expansion cannot verify: it says why (`changed`, `source-gone`,
  `other-host`) and returns the chunk text, never silently wrong records.
- A rewind drops indexed records from the active path: the next run's
  rendering differs from the fork point on, the manifest is replaced, and
  the orphan reaper removes the chunks no manifest names.

### Relation to T1, T2 and the board

T1 holds a session's working notes and dies with it. T2 holds what someone
chose to keep: decisions, findings, the threads board of open work. The
session corpus holds what was said, unedited and unchosen. It does not
replace write-back: an agent that finds the answer in a past session writes
the decision to T2, citing the session's tumbler. It does not duplicate T2:
the content of a `memory_put` or `store_put` is a tool-call input, which is
not embedded. The compaction checkpoint and the board keep the open-work
state a resumed session needs; the corpus serves a different question, what
was reasoned before.

## Implementation Plan

### Prerequisites

- [ ] A2 verified at Step 1, A3 at the Minimum Viable Validation, A5 at
  Step 4.
- [ ] RDR-229 P3.8 (and P3.6, which it waits for) landed on develop before
  Step 4 starts.

### Minimum Viable Validation

On a local engine with the bge embedder (no cloud, no Voyage), index ten
real nexus sessions, five of them compacted. A retrieval check of twelve
questions, each with the session and line it should find, written before
the run from Sam's T2 records of those sessions: eight must return a chunk
from the right session in the top five, and the expand of that chunk must
verify (`ok`). The check measures retrieval only; its result triggers no
further feature. Then append a copy of one transcript with new records and
re-run: the engine's embed count equals the number of new or changed
chunks. A fixture transcript with planted tokens in assistant text and in a
subagent report yields no token in any written chunk, expand output, or
outbound payload, and that session is stamped sensitive.

### Phase 1: Code Implementation

#### Step 1: Decoder and active path

Tests first, on fixtures built from real transcripts with their text
replaced: active path with the missing-logical-parent fallback, rewinds,
duplicate uuids, sidechain lines, oversized lines, and a resumed session
for A1. Verify A2 on three compacted sessions. Independent of RDR-229.

#### Step 2: Renderer, chunking, chunk-size check

Deterministic rendering and greedy chunking; a test that appending records
changes only the tail chunk's chash. Measure retrieval at two chunk sizes
on the validation set and record the chosen size as a research finding.
Independent of RDR-229.

#### Step 3: Scanner

Add `detect-secrets==1.5.0`; the four nexus plugins, each with a test on
its synthetic shape bare and inside a sentence; the redactor; the
blocked-socket test; the canary test; flagged-session stamping through the
RDR-243 write path. Re-run the false-positive count of 256-research-17 over
the narrative of every project's sessions on Sam's machine, not only
nexus's, and record it as a research finding before the first managed-mode
run. Independent of RDR-229.

#### Step 4: The `session` content type (after RDR-229 P3.8)

Verify A5 in the engine's registration path. Apply the site list; the
engine token-map and export changes ship in one engine cut. The lint for
`EXPLICIT_ONLY_CONTENT_TYPES`, the fallback-site test, the unregistered
aspect test, the owner rule (repository tumbler or the `sessions`
curator), and the two `llm-surfaces.toml` rows.

#### Step 5: Commands, acknowledgement and expand

`nx index sessions` with the acknowledgement and its stored block, `nx
sessions expand` through `TranscriptSource` with `LocalFileSource`,
`forget` and `list`, with `--dry-run` printing what would be indexed and the
bytes it would send. Run the Minimum Viable Validation.

### Phase 2: Operational Activation

#### Activation Step 1: SessionEnd hook

An opt-in SessionEnd hook runs `nx index sessions --session <id>` detached
for a main session. No LLM call. It runs on native Windows before its push
(AGENTS.md rule 14, since it lands under `conexus/hooks/`).

#### Activation Step 2: MCP expand

An MCP tool for expand, so agents need not shell out; it goes through the
MCP read-tool output rules of RDR-243.

### Not in scope

- **Summaries** of chunks or sessions: deferred by Sam on 2026-11-21 ("no
  summaries for now"); they need a later decision from Sam.
- **The verbatim transcript archive in PostgreSQL** (Sam, Q4): the next RDR,
  number to be claimed. It implements `ArchiveSource` under the contract in
  Technical Design; until then a deleted transcript expands to
  `source-gone`.
- **Subagent transcripts** as their own documents.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `session` collections (one per repository, one for the `sessions` curator) | In scope (`nx sessions list`, `nx collection list`) | In scope (`list` shows per-session counts, versions, last run) | In scope (`nx sessions forget`; a collection by the usual collection delete) | In scope (`expand` statuses; `list --check` re-verifies a sample) | Existing tenant backup (RDR-248) covers chunks and catalog rows; verbatim records wait for the archive RDR |
| `sessions.ack`, `sessions.exclude` config | In scope (`nx config get`) | In scope (the ack block names its endpoints and time) | In scope (unset; indexing then refuses) | In scope (every run compares endpoints) | N/A |

### New Dependencies

`detect-secrets==1.5.0` (Apache-2.0; depends on `pyyaml` and `requests`,
both already locked; 256-research-14). No other.

## Test Plan

- **Scenario**: compacted session whose boundary names a missing logical
  parent. **Verify**: records before the boundary are on the path.
- **Scenario**: rewound session. **Verify**: rewound records are off the
  path; after re-index the manifest has no chunk from them.
- **Scenario**: session grows by 50 records. **Verify**: earlier chunks keep
  their chash; only the tail and new chunks are embedded.
- **Scenario**: planted tokens in assistant text, thinking, a subagent
  report and a tool result. **Verify**: none appears in any chunk, expand
  result or outbound request (canary); the session is stamped sensitive.
- **Scenario**: the scanner runs with sockets blocked. **Verify**: no
  connection attempt.
- **Scenario**: the 12 synthetic shapes of 256-research-17, bare and inside
  a sentence, and a fixture of real narrative lines holding chashes, commit
  shas and uuids. **Verify**: all 12 redacted; no identifier redacted.
- **Scenario**: install without the acknowledgement, and an install whose
  endpoints changed after it. **Verify**: the command prints the wording
  and writes nothing; the second refuses naming both endpoint lists.
- **Scenario**: bare `search(corpus="knowledge,code,docs")`, `corpus="all"`
  and `nx search` with no corpus. **Verify**: no hit from a `session`
  collection; `corpus="session"` returns hits.
- **Scenario**: each silent-fallback site called with a `session`
  collection. **Verify**: prose model, threshold and type, never `code`.
- **Scenario**: a session in a worktree of a registered repository, and one
  in a directory with no repository. **Verify**: the first lands in the
  repository's collection, the second in the `sessions` curator's.
- **Scenario**: expand after the transcript is deleted, after it is altered,
  and on another host. **Verify**: `source-gone`, `changed`, `other-host`,
  each with the chunk text.
- **Scenario**: a stub second `TranscriptSource` that serves the same lines.
  **Verify**: expand verifies through it after the file is deleted.
- **Scenario**: excluded session id and excluded project glob. **Verify**:
  nothing indexed; a previously indexed excluded session is reported, not
  silently kept.
- **Scenario**: a run whose engine write fails half way. **Verify**: the
  document keeps its prior manifest; the next run completes.

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation retrieval check.
   **Expected**: 8 of 12 found in the top five, each expand `ok`.
2. **Scenario**: the canary over every registered session surface.
   **Expected**: zero planted tokens outbound.
3. **Scenario**: the explicit-only lint and the fallback-site test.
   **Expected**: every enumerating site drops `session`; every fallback
   site maps it to prose.

### Performance Expectations

Decoding a changed file costs about one second per 45 MB (256-research-3);
the largest nexus transcript, about 260 MB, re-decodes in about six seconds
(inferred from that rate). The chosen scanner combination took 8.1 s for
the 42,456 assistant-text lines of five sessions (256-research-17), so it,
not the decode,
sets the pace of a first backfill (inferred; the narrative of all 137
sessions is roughly 110 MB by the 20-session rate).

## Finalization Gate

### Contradiction Check

Two tensions, both resolved in the design. RDR-243's `sensitive` flag is
the project's privacy tool, yet the corpus as a whole is not flagged; the
flag withholds documents from agents, who are the corpus's readers, so the
design protects by scope, redaction and acknowledgement, and keeps the flag
for sessions the scanner flags. And the research memo proposed the dchash
span, diderot's active-path rule and a knowledge collection; the findings
and Sam's Q2 ruling replace all three. A sequencing tension remains with
RDR-229 Phase 3, resolved by holding Step 4 until P3.8 lands. No
contradiction remains between the findings and the proposed solution.

### Assumption Verification

A4 and A6 are verified. A1 is partly verified: the prefixes of the five
newest nexus transcripts did not change while four of them grew
(256-research-11); a resume, a rewind and a Claude Code upgrade are still
untested, and Step 1 adds them to its fixtures. A2, A3 and A5 stay
unverified and are verified at Step 1, the Minimum Viable Validation and
Step 4 before the steps that rely on them.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Document write with manifest replace | nexus engine `/v1` via `doc_indexer` | Source Search |
| Re-embed skip for held chunk text | nexus engine (RDR-181) | Source Search |
| `line_start`/`line_end` manifest columns | catalog `document_chunks` | Source Search (key table); write path not yet read |
| Register and find a collection by attributes | nexus engine after RDR-229 P3.8 | Assumed (A5), Step 4 |
| `claude-session:` source URI | catalog | Assumed, Step 4 |
| `scan_line`, `transient_settings`, `RegexBasedDetector` | detect-secrets 1.5.0 | Source Search and Spike |
| Transcript record shape | Claude Code (undocumented) | Spike on real files |

### Scope Verification

The Minimum Viable Validation runs in Phase 1, on a local bge engine, as
part of Step 5's close: the twelve-question retrieval check, the append
re-run with its embed count, and the planted-token canary. None of it is
deferred.

### Cross-Cutting Concerns

- **Versioning**: decoder, renderer and scanner versions on each document;
  a change re-renders at the next run and the command says so.
- **Build tool compatibility**: one new pinned Python dependency.
- **Licensing**: detect-secrets is Apache-2.0, combinable into the
  AGPL-3.0-or-later repo (256-research-14).
- **Deployment model**: off until acknowledged; local bge sends nothing off
  the machine; managed mode sends redacted narrative to Voyage and the
  tenant after the acknowledgement. Expand reads only the host that holds
  the transcript until the archive RDR lands. The engine token-map change
  ships in an engine cut, after RDR-229 P3.8's.
- **IDE compatibility**: N/A.
- **Incremental adoption**: per-install acknowledgement, exclusions per
  session and project, `--since` for a partial backfill.
- **Secret/credential lifecycle**: secrets found are redacted, never
  stored; the scanner makes no network call; flagged sessions are stamped
  sensitive; `forget` removes an indexed session. The command reads no
  credentials.
- **Memory management**: a file is decoded line by line with the existing
  64 MB line cap (`transcripts_read.py:35`); the active-path walk holds one
  id map per file (about 110,000 entries for the largest, inferred from the
  line rate of 256-research-3).

### Proportionality

Phase 1 is one decoder, one renderer, one scanner dependency, one content
type applied across the site list, and four commands, with no LLM. The site
list is long because the type is cross-cutting, which is the cost Sam
accepted with Q2. The alternatives section can shrink at lock time.

## Decisions (Sam)

Sam answered the first draft's questions on 2026-11-21 (T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`):

1. **Cloud after acknowledgement.** Redacted narrative may go to Voyage and
   the tenant after a one-time acknowledgement that names where it goes;
   tool inputs and results are never embedded.
2. **Fifth content type**, `session`, owned per repository.
3. **A Python scanning library**, vetted and pinned: detect-secrets 1.5.0.
4. **A verbatim archive in PostgreSQL**, as its own RDR, next.
5. **No summaries for now.**

No question for Sam remains open in this RDR.

## References

- T3 `research-diderot-vs-nexus-2026-11-21 (1/2)`, `(2/2)` (collection
  nexus-architecture); T3 `architecture-diderot-overview`
  (distributed-systems).
- T2 `nexus_rdr/256-research-1` through `-17`;
  `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`;
  `nexus/sam-ruling-rdr256-session-corpus-2026-11-21`.
- diderot clone: `docs/session-ingest.md`, `docs/design.md:190-198,
  :236-246, :589-600`, `internal/sessiondecode/FORMATS.md`,
  `internal/sessioningest/{summarizer,publish,pushexpand,pushretention}.go`,
  `internal/sessionship/sync.go:307-308`,
  `internal/attachments/files/redact.go`.
- detect-secrets 1.5.0 sdist: `detect_secrets/core/scan.py`,
  `settings.py`, `plugins/base.py`, `filters/common.py`,
  `core/usage/filters.py`; `docs/plugins.md` (via Context7).
- nexus: `src/nexus/lua_runtime/transcripts_read.py`,
  `transcripts_projects.py`, `verbs_transcripts.py`,
  `src/nexus/memory_curate/sessions.py`, `src/nexus/doc_indexer.py`,
  `src/nexus/corpus.py`, `src/nexus/mcp/core.py`,
  `src/nexus/sensitive_write.py`, `src/nexus/redact.py`,
  `src/nexus/tables/llm-surfaces.toml`,
  `src/nexus/tables/chunk-metadata-keys.toml`, `docs/collections.md`,
  `docs/agents/catalog-t3-split.md`; engine
  `service/src/main/java/dev/nexus/service/vectors/EmbeddingProfile.java`,
  `EmbedderRouter.java`, `db/TenantExport.java`, `db/CatalogRepository.java`.
- RDR-103, RDR-131, RDR-171, RDR-204, RDR-207, RDR-229, RDR-240, RDR-243,
  RDR-246.

## Revision History

- 2026-11-21: drafted (a18cae81d0).
- 2026-11-21: revised for Sam's decisions. Placement moved from a knowledge
  subject with an explicit-only attribute (now Alternative 3, rejected by
  Sam) to a fifth content type `session` owned per repository, with the
  site census and sequencing after RDR-229 P3.8 (Gap 4 added). Scanner
  chosen: detect-secrets 1.5.0, with the comparison in Alternative 5. The
  acknowledgement's wording and storage written out; scanner-flagged
  sessions stamped sensitive. Expand reads through a `TranscriptSource`
  interface so the archive RDR can add a source. Summaries removed from the
  plan (Sam: "no summaries for now"); the validation is a retrieval check
  only. Research 256-research-12 to -17 added: the Agent-result secret
  scan (-16) and the measured scanner combination with four nexus plugins
  (-17).
