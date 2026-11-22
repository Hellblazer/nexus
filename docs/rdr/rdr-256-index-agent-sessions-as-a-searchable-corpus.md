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
collection has a **content type** (one of `code`, `docs`, `rdr`,
`knowledge`), an **owner** and an embedding model. A `knowledge` collection
is named for a **subject**, which is also its owner.

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
(256-research-5, -16). A live token reached a subagent transcript on
2026-09-25 (`conexus/hooks/scripts/routing/credential_print_guard.py:18-21`).
Indexing into T3 sends text to the embedder, which is Voyage AI (an outside
embedding service) on Sam's managed install, and stores it in the managed
tenant, the cloud database the nexus service keeps for that install
(256-research-8). Both are LLM or outbound surfaces under RDR-243.
Nexus's only redactor matches a value after a key word such as `token=`,
and would miss a bare token (`src/nexus/redact.py:13-15`).

#### Gap 4: Nothing keeps a corpus out of default searches

A session corpus searched by default would put conversation chatter into
every agent's `knowledge` search and into the topic list every subagent
starts with. The only way a search skips a live collection today is by its
lifecycle state, and a collection that is not live cannot be searched even
by name (256-research-9). Sam chose a knowledge subject with a new
explicit-only collection attribute (T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`, Q2).

## Relationship to Prior RDRs

Scanned the RDR index for session, transcript, conversation, summary and
collection-naming titles; read RDR-131, RDR-207, RDR-240, RDR-243, RDR-171
and RDR-229.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-131 T2 Session Rollup Summaries | Abandoned predecessor, different object | It proposed LLM rollup summaries over aged **T2 memory entries** (not transcripts) to bound context injection. It was abandoned on 2026-09-12 as a batch with RDR-132 and RDR-133 because it was an unresearched stub, explicitly "not a ruling that the idea is wrong" (rdr-131 `abandon_reason`). Nothing here repeats it: this RDR indexes transcripts, writes no summaries, and changes nothing about T2. |
| RDR-207 Summarization at the T2 Expiry Boundary | Precedent | It took RDR-131's live half and decided that a rollup is an attended command, never a session-end side effect (rdr-207:392), because session end is the worst context for an LLM call (:334-339) and a hallucinated summary is worse than none (:344-346). This RDR adds no LLM call, in a hook or otherwise, and keeps its documents out of every existing aspect-extraction path, automatic or attended, each of which would send them to one (Technical Design, Write path and hooks). |
| RDR-240 Procedures as Lua scripts | Origin of the transcript reader | Its `transcripts.sessions` verb owns the file listing and line framing (`src/nexus/lua_runtime/transcripts_read.py`). This RDR reuses those, not the verb: the verb is a read-only count surface and never returns tool results (rdr-240:361-365). It has no active-path walk (256-research-2). |
| RDR-243 Document sensitivity | Hard constraint | Its LLM-surface table (`src/nexus/tables/llm-surfaces.toml`) gets this RDR's surfaces as rows, and a session the scanner flags is stamped `sensitive`. The flag is not the corpus-wide protection, because it withholds a document from the session model by default (Q1), and agents are this corpus's readers (256-research-8). |
| RDR-103 / RDR-204 collection names and attributes | Considered | A fifth content type would join both RDRs' enumerations of content types; it is the rejected Alternative 3 (256-research-12). |
| RDR-229 The collection name becomes a label | Adjacent, in flight | Its Phase 3 is moving collections to minted labels found by attributes. The explicit-only attribute is read from the row, never from the name, so it needs no ordering against P3.6 or P3.8; it shares code with P3 at three sites and needs P3.9 to survive a recovery-bundle import (256-research-18). |
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
- RDR-229 Phase 3 is in flight on the collection registry (beads
  nexus-p2m1x.17, .26 and .28 in progress on 2026-11-21; 256-research-13).
- More terms. **bge** is the embedding model a local install runs on its
  own machine, so nothing leaves it. **Fan-out** is how a search over a
  corpus such as `knowledge` queries every collection of that type and
  merges the results. The **Knowledge Map** is the list of topics injected
  into every subagent's starting context; **taxonomy labelling** names
  those topics with an LLM. A **tumbler** is a catalog document's or
  owner's hierarchical id (such as `1.1.42`). The **knowledge curator** is
  the catalog owner that knowledge notes are filed under. A **label** is
  what RDR-229 turns a collection's name into, and a **minted** label is
  the `c<id>` form the engine gives a new collection once RDR-229's P3.8
  lands. **Liquibase** is the tool the engine uses to change its
  PostgreSQL schema. The **orphan reaper** is the engine job that deletes
  chunks no manifest names. A **canary** test plants a unique string in the
  input and asserts it never appears in any output.

## Research Findings

### Investigation

Read the transcript readers in `src/nexus/lua_runtime/` and
`src/nexus/memory_curate/`, the indexing write path in
`src/nexus/doc_indexer.py`, the corpus routing in `src/nexus/corpus.py`,
`src/nexus/mcp_infra.py` and `src/nexus/mcp/core.py`, the sensitivity code
and tables, the engine's embedding-profile and export classes, and RDR-131,
RDR-171, RDR-207, RDR-229, RDR-240 and RDR-243. Read diderot's
`internal/sessiondecode/FORMATS.md`,
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
| Client collection row cache | Yes | A fixed key list per collection from `list_collections` (mcp_infra.py:985-996). |
| detect-secrets 1.5.0 | Yes (sdist and a run) | Apache-2.0; `scan_line` in-process; network verification only when its verification filter is configured, which in-process Settings do not do by default; custom plugins load from a file path (256-research-15, -17). |

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
- **✅ Verified** (source search): a fifth content type would touch about
  thirty client and engine sites, four of which silently treat an unknown
  type as `code`. *Source: 256-research-12.*
- **⚠️ Documented** (source search): RDR-229 P3.6 and P3.8 are rewriting the
  name grammar and the render paths a fifth type would need. *Source:
  256-research-13.*
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
- **⚠️ Documented** (source search): an explicit-only attribute read from the
  collection row needs no ordering against RDR-229 P3.6 or P3.8; it shares
  three code sites with P3, and before P3.9 a recovery-bundle import drops
  it. *Source: 256-research-18.*

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
  session content well enough to be useful, with bge and with
  `voyage-context-3`. **Status**: Unverified. **Method**: Spike (the
  Minimum Viable Validation's retrieval check, run on each embedder; the
  `voyage-context-3` run needs Sam's go and an announced window).
- [x] **A4.** Re-sending a grown session's chunks re-embeds only new chunk
  text. **Status**: Verified (256-research-7). **Method**: Source Search.
- [ ] **A5.** The explicit-only attribute can be one `NOT NULL DEFAULT
  false` column on
  `catalog_collections`, returned by the engine's collection listing and so
  carried in the client's row cache, and exported whole by the tenant
  backup. **Status**: Unverified; the listing, cache and export shapes were
  read, the listing's Java query and the export parity test were not
  (256-research-18). **Method**: Source Search at Phase 1 Step 4.
- [x] **A6.** detect-secrets scans in-process without a network call when
  its verification filter is not configured. **Status**: Verified; the
  runs completed with sockets blocked (256-research-15, -17).
  **Method**: Spike.

## Proposed Solution

### Approach

Index the narrative of each main session into one knowledge subject,
`agent-sessions`, redacted, adding no LLM call and barring the subject
from every existing aspect-extraction path; keep the subject out of
default searches with an explicit-only collection attribute; record each
chunk's JSONL line range in the manifest; expand a hit by reading those
lines back from the transcript. Concretely:

1. **What is indexed.** Main transcripts only, active path only, narrative
   only: human-typed prompts, assistant text, plaintext thinking, subagent
   reports (the result of an `Agent` tool call, and a task notification that
   carries one), and one short line per tool call naming the tool and, for
   the tools the tool-line table in Technical Design lists, the one input
   field it names; a command's text is never on the line, so a Bash call is
   named by its tool alone (Sam, 2026-11-21). Every other tool input, every
   tool result other than a subagent report, harness-injected text
   (`isMeta`) and bookkeeping lines are never embedded; expand returns them.
2. **Where it lives.** The knowledge subject `agent-sessions`, owned by its
   subject under Sam's owner-is-source ruling (T2
   `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`). One catalog
   document per session, filed under the knowledge curator, with the
   repository it belongs to in its metadata. Every collection of the
   subject carries the explicit-only attribute, so it is searched only when
   a caller names it, and the Knowledge Map and automatic taxonomy
   labelling skip it (Sam, Q2).
3. **Privacy.** detect-secrets, pinned, with four nexus plugins for the
   shapes it misses, runs in-process over every chunk before it is written
   and over every expand result before it is returned; a match becomes
   `[redacted:<type>]`. A session with any match is also stamped
   `sensitive` (RDR-243). Indexing is off until the install gives a
   one-time acknowledgement that names where the text goes; on Sam's
   managed install that is Voyage and the tenant (Sam, Q1).
4. **When.** An explicit `nx index sessions` command. A session written to
   in the last five minutes indexes only its sealed chunks. A later phase
   adds an opt-in SessionEnd hook that runs the same command for the ending
   session; it adds no LLM call.
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
records `(record_id, line_no, kind, text, tool_name, tool_target, ts)`;
`tool_target` holds only the field the tool-line table below names for
that tool, and is empty for every other tool. Ids follow diderot's scheme: the native `uuid`, with
`<uuid>:<kind>:<block>` when one line becomes several records. The active
path follows diderot's rule (FORMATS.md:42-48) with one change, taken from
256-research-4: at a `compact_boundary` whose `logicalParentUuid` is not in
the file, the walk continues from the previous uuid-bearing record in file
order. The later of two lines with one uuid wins. A parent cycle rejects the
file, which is logged and skipped. The decoder version is an integer stored
on the document.

**Renderer and chunks.** Each record renders to one block, for example
`[user] ...`, `[assistant] ...`, `[thinking] ...`,
`[subagent report: <description>] ...`, `[tool Read] src/nexus/corpus.py`,
`[tool Bash]`.

*Tool lines.* A tool line carries the tool's name and, for the tools in
this table, one input field; every tool not in it, Bash, Agent, Task and
every MCP tool among them, is named by its tool alone.

| Tool | Field on the tool line |
| --- | --- |
| `Read`, `Edit`, `Write`, `NotebookEdit` | the file path |
| `Grep`, `Glob` | the pattern, and the path when one is given |
| `WebSearch` | the query |
| `WebFetch` | the URL with its query string and fragment dropped |

Step 3 measures these fields; a tool whose field carries a secret-shaped
string the scanner misses moves to "named alone" before Phase 1 exits.

*Chunks.* Chunks are filled greedily at record boundaries from the start
of the path up to a target. The target is bounded twice: by
`SAFE_CHUNK_BYTES`, the 12,288-byte target of every chunker
(`db/limits.py:39`), and by the embedder's token window, which the session
chunker takes from `window_for_collection` as the existing chunkers do
(`doc_indexer.py:2643, 2757`): 512 tokens on a bge collection, and none on
a Voyage collection, whose 32,000-token window a chunk under the byte
target cannot reach (`embed_window.py:4-13, 29-35`). `MAX_DOCUMENT_BYTES`
(16,384) is not a ceiling here: it is a storage quota the upsert path
does not enforce (`db/http_vector_client.py:3881-3896`). Step 2 records
one target per embedder as a research finding.

*Over-size records.* A record whose rendering exceeds the target is split
deterministically, at line breaks and then at sentence ends, into parts
with ids `<record_id>:p<n>` (n from 1). Each part is its own chunk, carries
the record's full line range, and is never packed with another record.
Expand re-renders the record, splits it the same way and compares part
n's SHA-256 with the chunk's chash.

*Title.* The session's title and date are not in the chunk text, so a
title that arrives late (a `custom-title` record) changes no chash. Search
output prints them before a hit, read from the catalog document's title
and meta at display time.

The renderer version is stored on the document; a change to it re-renders
every session at its next run, which costs embeds and is announced in the
command's output.

**Placement.** The subject is typed bare, `agent-sessions`, as
`docs/collections.md` Rule 3 requires; the catalog renders or mints the
collection's name, before and after RDR-229 P3.8 alike. The owner is the
subject. The collection is a `knowledge` collection, so it embeds with
`voyage-context-3` on a Voyage install and with bge on a bge install, as
every knowledge collection does; a sensitive session in local mode goes to
the subject's bge sibling under RDR-243 Q3. Session documents register
under the knowledge curator, never a repository owner, and carry
`category=agent_session`; the repository is document metadata (the
owner tumbler, when `transcripts_projects.py`'s membership rules place the
session's working directory in a registered repository), which a `where`
filter can narrow on. Fine distinctions go on the document, not in new
collections (`docs/collections.md` Rule 1).

**The explicit-only attribute.**

- *Schema.* One Liquibase changeset adds `explicit_only BOOLEAN NOT NULL
  DEFAULT false` to `catalog_collections`. It is a property of the
  collection, not part of the lookup tuple `(content_type, owner_id,
  embedding_model)`.
- *Set, and inherited by siblings.* Registration takes `explicit_only`. The
  engine also sets it on a newly registered collection when a live row with
  the same `(tenant_id, content_type, owner_id)` has it, so a model sibling
  (the bge sibling a sensitive session creates, or one a model change
  mints) can never drop it. Nothing clears it except a CLI route that takes
  a reason, like RDR-243's lowering route.
- *Re-asserted.* Every `nx index sessions` run asserts the attribute on the
  collections it writes. This covers the one path that can lose it before
  RDR-229 P3.9 lands: a client recovery-bundle import, which registers the
  collection without its registry row (256-research-18). `nx doctor` reports
  a `knowledge` collection owned by `agent-sessions` without the attribute.
- *Read, through one seam.* The client row cache (`mcp_infra.py:985-996`)
  gains the key. One predicate decides it: `is_default_scope_row(row)`,
  new beside `is_live_collection_row` (`db/http_vector_client.py:3384`),
  true for a row that is live by that predicate and does not carry
  `explicit_only`. Every reader that builds a scope the caller did not name
  filters through it, and no reader tests the key itself. The readers:
  bare-corpus fan-out in `resolve_corpus` stage 2 (`corpus.py:2662`), which
  also serves the default corpora and the `corpus="all"` expansion
  (`mcp/core.py:4305-4316`); the MCP fan-out listing
  (`mcp_infra.get_live_collection_names`, `mcp_infra.py:1249`), which also
  feeds the collection-name hint of the `nx_answer` planner prompt
  (`mcp/core.py:13225`, where `_get_collection_names` is that function's
  alias, `mcp/core.py:88`); the `nx store list` and `nx store get`
  knowledge scope (`commands/store.py:_store_scope`, `:550-576`), which
  filters `db.list_collections` itself, outside both of those; the
  reusable-subject list an agent preamble prints
  (`commands/command_context.py:_knowledge_collections`, `:1299-1331`); the
  Knowledge Map (`nexus.context:generate_context_l1`
  and `hooks/subagent_start:_knowledge_map_section`, the targets of the
  Knowledge Map row of `llm-surfaces.toml`); and automatic taxonomy
  discovery and labelling (`commands/taxonomy_cmd.py`
  `discover_for_collection` and `_generate_labels_batch`). The aspect gate
  (Write path and hooks) reads the same row but is not a scope builder.
  Naming the collection, or the corpus `knowledge__agent-sessions`,
  searches it.
- *The lint is positive.* The existing live-listing lint checks a fixed
  list of routing modules
  (`tests/test_bc7ps_routing_uses_live_listing.py:287-295`), so a scope
  builder outside that list, as `commands/store.py` is, goes unseen. The new
  lint walks every module under `src/nexus` and finds every listing call
  (`list_collections`, `get_collection_names`, `live_collection_rows`,
  `list_live_collections`, `get_live_collection_names`). Each such call must either have its rows pass
  through `is_default_scope_row` in the same function, or sit on an
  allowlist entry with a reason (an inventory, doctor, maintenance or
  explicit-name path that must see every collection). An unlisted call
  fails, and an allowlist entry that no longer matches a call fails.
- *Ordering against RDR-229.* None needed for meaning: the attribute is
  read from the row, never from the name, so P3.6's label rule and P3.8's
  minted labels leave it unchanged (256-research-18). Three code sites are
  shared with P3 work: the row-cache key list, `register_collection`
  (`catalog/http_catalog_client.py:3685`), which P3.8 rewrites to return
  the row, and the tenant export (`TenantExport.java:355`). Whichever change
  lands second carries the attribute; Step 4 checks each against develop
  when it starts. P3.9 should carry the attribute in the recovery bundle's
  registry row; until it does, the re-assertion above covers imports.

**Catalog and manifest.**

- Document identity is the session uuid. `source_uri` is
  `claude-session:<session-uuid>` (scheme name provisional). A new scheme
  enters `_KNOWN_URI_SCHEMES` only once its reader is registered in
  `nexus.aspect_readers` (`catalog/types.py:64-66`), and the engine owns
  `source_uri` at register time (`:67-69`), so whether it accepts the scheme
  is a Step 1 spike, before any write depends on it. The title is
  the session's `custom-title` or `agent-name` record when present, else
  the first typed prompt cut to 80 characters.
- Document meta: host name, project directory, repository owner tumbler
  when there is one, transcript path, file size and mtime at the last run,
  decoder, renderer and scanner versions, record and chunk counts.
- Each manifest row sets `line_start` and `line_end` to the JSONL line
  numbers (1-based, inclusive) of the first and last record in the chunk.

**Write path and hooks.** The existing chunk writer is
`doc_indexer._write_chunks_with_owner_rows` (`doc_indexer.py:1687`, a
private name), which writes the chunks with their manifest and fires no
hook. Its caller fires the post-store hooks, and with no registry given it
installs the default chain (`install_default_hooks`,
`hook_registry.py:544-574`), as `_index_document` does
(`doc_indexer.py:2208-2233`). The session indexer is a new caller, and it
takes on what `_index_document` does around the writer:

- The writer is promoted to a public name (or wrapped by one) in
  `doc_indexer`, so the sessions module calls no private name across
  modules.
- The content hash it stamps is the fence key, and only the functions in
  the fence-key lint's `_ORIGINS` may make one, as `_sha256(<file param>)`
  (`tests/test_fence_key_is_a_file_hash_lint.py:44-55`). The session
  indexer's entry function is added there by hand, with the key
  `_sha256(transcript_path)`: the file's bytes at hashing time. A write to
  the transcript after hashing shows up as a changed hash at the next run.
- It is a new `fire_batch` producer, so it gets an entry in the producer
  allowlist of `tests/test_vw594_fence_coverage_gate.py` (`:16-24`), and it
  owns the index-run fence: it begins the fence, writes with
  `defer_completion=True`, fires the hooks, and sends the completion stamp
  last, as `_index_document` does (`doc_indexer.py:2197-2202, 2237-2245`).
- It builds its own `HookRegistry` holding only
  `taxonomy_assign_batch_hook`, so the aspect hook is never installed for
  a session write; the manifest hook is not needed, because the writer
  already wrote the manifest (`doc_indexer.py:2212-2217`).

| Default hook | What it does | For `agent-sessions` |
| --- | --- | --- |
| `manifest_write_batch_hook` | writes the catalog manifest | not installed; the writer already wrote it |
| `taxonomy_assign_batch_hook` | the engine assigns the new chunks to their nearest topics (`mcp_infra.py:2529-2535`); no LLM call | runs; discovery and labelling skip the subject (Read, above), so it has no topics of its own (inferred, not read, what assignment does for a collection with none; Step 4 reads it) |
| `aspect_extraction_enqueue_hook` | queues the document for aspect extraction; a `knowledge` collection's base config is `scholarly-paper-v1`, and a prose document is re-routed to `general-prose-v1`, both run through `claude -p` (`aspect_extractor.py:1054-1062`; `llm-surfaces.toml` rows `aspect-enqueue-hook`, `aspect-worker-drain`, `aspect-extraction-cli`, `aspect-extraction-api`, `enrich-aspects`) | not installed, and barred by the gate below in any case |

*The aspect gate.* Not installing the hook covers one path; others reach
the extractor without it: the batch enqueue in the indexer
(`indexer.py:6455-6459`), the worker drain, and the attended
`nx enrich aspects <collection>`. All of them pass `select_config`
(`aspect_extractor.py:1048`), and `extract_aspects` returns nothing when
it gives no config (`aspect_extractor.py:1583-1585`) (256-research-22). So
the gate goes there: `select_config` returns `None` for a collection whose
row carries `explicit_only`, and also, failing closed, for any collection
whose candidate subject (`corpus.candidate_knowledge_subject`,
`corpus.py:890-905`) is `agent-sessions`, whatever the row says or
whether it can be read. The subject check covers a row that is missing,
unreadable, or stale in the 60-second row cache before a re-assertion is
seen. It reads the name, which the attribute itself never does (Ordering
against RDR-229, below): it is an extra bar for one subject, not the
attribute's meaning. It works while names carry the subject. After RDR-229
P3.8 a minted label carries none, but it also derives no content type, so
a collection with no readable row already gets no config
(`corpus.py:872, 883`); a stale cached row is then the remaining gap, and
by then P3.9 carries the attribute in the registry row from registration
on, so the row has it from its first read (inferred, not read; Step 4
checks P3.8 and P3.9 on develop). A gated call is logged as `aspect_gate_explicit_only`. `nx enrich
aspects` on such a collection refuses with its own message naming the
attribute, rather than the generic "no extractor config" text
(`commands/enrich.py:1754-1761`). A future `explicit_only` collection
outside the subject is gated by its row alone, so a stale row can let one
document of it through until the cache refreshes; only the subject has the
fail-closed check.

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
blocked. The redactor is `redact(text) -> (text, findings)`; a chunk it
cannot process is not written, and the run reports it. `scan_line` sees one
line at a time, so a private key's header line matches and its body lines
do not. The redactor therefore treats a `-----BEGIN ... PRIVATE KEY-----`
line as the start of a span that runs through the matching `-----END ...
PRIVATE KEY-----` line, and replaces the whole span, across lines, with one
`[redacted:private-key]`; a BEGIN with no END redacts to the end of the
record. Expand output passes through the same redactor, so a key in a tool
result comes back redacted, never verbatim. The choice and its
trade-offs are argued under Alternatives (Alternative 5).

**Sensitivity for flagged sessions.** A session whose narrative has any
scanner finding is written with `sensitive=true`; registration can only
raise the flag (RDR-243 Approach item 1). Its chunks are still redacted.
Under RDR-243 the session is then withheld from MCP read tools and from
`nx search` run inside Claude Code, kept from the reranker, and excluded
from LLM surfaces; a person at a terminal sees it with a `[sensitive]`
badge. In local mode it routes to the bge sibling of the subject, as
RDR-243 Q3 does for every sensitive document, and that sibling inherits
the explicit-only attribute. In managed mode, where no bge sibling exists,
the install acknowledgement below is the disclosure RDR-243 Q3 requires
(`--ack-external-embedder`), and its wording says so.

**Acknowledgement.** Indexing refuses until the install records an
acknowledgement. The first run prints this text, with the bracketed parts
filled from the install's posture:

> nx index sessions sends text from this machine's Claude Code session
> transcripts to [Voyage AI (api.voyageai.com) for embedding, and stores it
> in your nexus cloud tenant | the embedder on this machine, and stores it
> in your local nexus engine]. It sends your typed prompts, the assistant's
> replies and reasoning, subagent reports, and one line per tool call
> naming the tool and, for file, search and web tools, the path, pattern,
> query or URL it was given, never a command's text. A secret scan replaces
> what it recognises before anything is sent; it does not recognise every
> secret. No other tool input, and no other tool result, is sent by this
> command. This command calls no text-generating model. A session in which
> the scan
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
call sites. The table is one row per RDR-243 census row, with `census_row`
unique in 1..25 (`llm-surfaces.toml:6-17`) and `CENSUS_ROW_COUNT = 25`
(`tables/llm_surfaces.py:55`), pinned by
`tests/tables/test_llm_surfaces_table.py:73` (256-research-24). The two rows
take census rows 26 and 27: Step 4 raises the count to 27, widens the
header's range, updates that pin, and regenerates the privacy-policy table
(`scripts/gen_privacy_policy_table.py --write`). No other row is added:
this RDR adds no LLM call, and the aspect-extraction rows
(`aspect-enqueue-hook`, `aspect-worker-drain`, `aspect-extraction-cli`,
`aspect-extraction-api`, `enrich-aspects`) reach no document of the
subject, because `select_config` gives it no config (Write path and
hooks).

**Commands (names provisional).**

```text
nx index sessions [--project repo|all|<dir>] [--session <uuid>] [--since DATE] [--dry-run] [--ack]
nx sessions expand <tumbler> --position N [--json]
nx sessions forget <session-uuid> [--undo]
nx sessions list [--project ...] [--check]  # indexed sessions, counts, last run, statuses; --check re-verifies a sample through expand
nx collection list                          # existing verb; gains an explicit-only column
nx collection explicit-only clear <collection> --reason TEXT   # the one route that clears the attribute
```

`forget` removes the session's chunks and catalog document and adds its id
to `sessions.exclude` in `config.yml`, which every run skips, so the next
run does not index it again; `forget --undo` removes the entry, and the
next run indexes the session afresh. Removed chunks stay in engine backups
taken before the forget until those backups age out.

The command module is new (`src/nexus/commands/sessions.py`), registered on
the `index` group for the first verb; `commands/index.py` is near its size
ceiling (4,280 of 4,442 lines in `scripts/file-size-exceptions.txt`).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Project listing, line framing | `src/nexus/lua_runtime/transcripts_read.py` | Reuse: promote `_files`, `_lines` to public names. |
| Session-to-repository membership | `src/nexus/lua_runtime/transcripts_projects.py` | Reuse, for the repository in document meta. |
| Active-path decoder | none (256-research-2) | New. |
| Chunk write, embed skip, manifest | `src/nexus/doc_indexer.py` write path (`_write_chunks_with_owner_rows`, private; its caller fires the hooks and owns the fence and stamp) | Reuse: promote or wrap the writer; add the session origin to the fence-key lint and the producer to the vw594 allowlist. |
| Aspect-extraction gate | `aspect_extractor.select_config`, which every aspect path passes | Extend: no config for an `explicit_only` collection, failing closed for the subject. |
| Default-scope filter | `is_live_collection_row` (`db/http_vector_client.py:3384`) | Extend: `is_default_scope_row` beside it; a positive lint over every listing call. |
| LLM-surface registry | `llm-surfaces.toml`, `CENSUS_ROW_COUNT` | Extend: census rows 26 and 27. |
| Line-range positions | `catalog_document_chunks.line_start/line_end` | Reuse; no schema change. |
| Collection registration and row cache | `register_collection`, `mcp_infra` row cache | Extend with `explicit_only`. |
| Fan-out skip | lifecycle-state filter in `corpus.py` | Extend with the attribute. |
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
inputs or results, which are never embedded other than subagent reports
and a tool line's one named field.

A knowledge subject with one attribute is Sam's choice (Q2), made after the
census showed what a fifth content type costs (Alternative 3). The subject
needs no change to the content-type vocabulary, embeds like every other
knowledge collection, and uses the owner rule that already applies to
subjects. The attribute is read from the row, so it holds whatever RDR-229
does to names. Sibling inheritance and the writer's re-assertion close the
two ways a collection of the subject could appear without it.

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
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`, Q5). Not in this
RDR's plan. Any summary work needs a later decision from Sam.

### Alternative 2: Index everything, tool results included

**Description**: Embed every record on the active path.

**Pros**:

- One can search for an error message a tool printed.

**Cons**:

- About eight times the embedded volume (256-research-3).
- Every secret-shaped string found is in this part (256-research-5, -16).
- Duplicates the code and docs corpora, since most tool results are file
  reads.

**Reason for rejection**: volume and exposure, for content expand already
returns. Sam's Q1 answer also states that tool inputs and results are never
embedded, other than subagent reports and a tool line's one named field.

### Alternative 3: A fifth content type, `session`, owned per repository

**Description**: A new collection content type beside `code`, `docs`,
`rdr` and `knowledge`; one `session` collection per repository, owned by
the repository's tumbler, with a curator for sessions outside any
repository; kept out of default searches as a property of the type.

**Pros**:

- Exclusion by construction, with no per-collection flag.
- Per-repository ownership like code and docs.

**Cons**:

- The census found about thirty client and engine sites that enumerate
  content types, four of which silently treat an unknown type as `code`, so
  a missed site would embed or score sessions as code with no error
  (256-research-12).
- It collides with RDR-229 Phase 3: P3.6 and P3.8 are rewriting the name
  grammar and every render-for-new-collection path the type would need, so
  the type would have to wait for P3.8 (256-research-13).

**Decision**: chosen by Sam first, then rejected by him on 2026-11-21 in
favour of the knowledge subject, on that census and that collision (T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`, Q2).

### Alternative 4: The `sensitive` flag as the corpus-wide protection

**Description**: Index every session as a sensitive document.

**Cons**:

- MCP tools then withhold the corpus from the session model, which is its
  reader (256-research-8).

**Reason for rejection**: it defeats the purpose. The flag is kept for
sessions the scanner flags, and exclusion for sessions the operator names.

### Alternative 5: Other secret scanners

All figures from 256-research-14, -15 and -17.

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
two dependencies are already locked, it covers the most common provider
formats, and it takes custom plugins for the formats it lacks. The cost is
maintenance: its last release is from 2024-05-06, though the repository
was still being pushed to in 2026. The exact pin and the scanner version on
each document make an upgrade a visible event.

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
are never embedded beyond a tool line's one named field (256-research-5,
-16). That scan counted whole tool inputs and did not record which field
held a hit, so the named fields on rendered tool lines are not yet
measured; Step 3 measures them and moves a tool whose field carries a
missed secret to "named alone" (256-research-20). The design also accepts
the miss because `nx sessions forget`
removes a session after the fact, for good, since it also excludes it. The zero is a measurement on 67,000
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
- **A non-live lifecycle state for the subject**: a non-live collection is
  excluded even when named (256-research-9).

## Trade-offs

### Consequences

- Positive: past sessions answer questions in seconds; a compacted session
  can find what it said before compaction.
- Positive: no LLM call added and no new model surface beyond the
  embedder; no aspect-extraction path, automatic or attended, reaches the
  subject.
- Positive: no change to the content-type vocabulary, and no wait on
  RDR-229.
- Negative: on a managed install, redacted narrative goes to Voyage and the
  tenant, which is the reason for the acknowledgement.
- Negative: until the archive RDR lands, verbatim expansion lasts only as
  long as Claude Code keeps the file, about 30 days by default.
- Negative: one subject holds every repository's sessions; scoping to one
  repository is a metadata filter, not a collection.
- Negative: the decoder depends on an undocumented file format.

### Risks and Mitigations

- **Risk**: a secret the scanner does not know reaches the embedder.
  **Mitigation**: tool inputs and results, where every measured hit sat,
  are never embedded beyond a tool line's one named field, and a command's
  text never; four nexus plugins for the formats this project uses; Step
  3's measurement of the named fields; `nx sessions forget`, which also
  excludes the session from later runs; the canary test plants tokens in
  narrative, in each named field, and in inputs that are never on a line.
- **Risk**: a collection of the subject without the attribute leaks
  sessions into every default search. **Mitigation**: sibling inheritance at
  registration, re-assertion on every indexing run, a doctor check, and the
  fan-out lint.
- **Risk**: RDR-229 work and this RDR edit the same three sites.
  **Mitigation**: whichever lands second carries the attribute; Step 4
  starts by checking each site on develop.
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
- The attribute is missing on a collection of the subject: `nx doctor`
  names it, and the next indexing run sets it.
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

- [ ] A2 and the `claude-session:` scheme verified at Step 1, A3 at the
  Minimum Viable Validation (bge, then `voyage-context-3` in an announced
  window with Sam's go), A5 at Step 4.

### Minimum Viable Validation

First on a local engine with the bge embedder (no cloud, no Voyage), then,
inside an announced window with Sam's go, on a `voyage-context-3` engine
with the same sessions and questions, since a target chosen under bge's
512-token window says nothing about Voyage retrieval. On each, index ten
real nexus sessions, five of them compacted. A retrieval check of twelve
questions, each with the session and line it should find, written before
the run from Sam's T2 records of those sessions: eight must return a chunk
from the right session in the top five, and the expand of that chunk must
verify (`ok`). The check measures retrieval only; its result triggers no
further feature. Then append a copy of one transcript with new records and
re-run: the engine's embed count equals the number of new or changed
chunks. A bare `search(corpus="knowledge")` over the same engine returns no
session chunk. A fixture transcript with planted tokens in assistant text
and in a subagent report yields no token in any written chunk, expand
output, or outbound payload, and that session is stamped sensitive.

### Phase 1: Code Implementation

#### Step 1: Decoder and active path

Tests first, on fixtures built from real transcripts with their text
replaced: active path with the missing-logical-parent fallback, rewinds,
duplicate uuids, sidechain lines, oversized lines, and a resumed session
for A1. Verify A2 on three compacted sessions. Spike the `claude-session:`
source URI against a local engine: register one document with it, read it
back, and record whether the engine accepts the scheme and what a reader
registration in `nexus.aspect_readers` it needs (`catalog/types.py:64-69`);
if the engine refuses it, the scheme is chosen here, before Step 5 writes
any document. Decode tool calls to the tool-line table's fields only, with
`tool_target` empty for every other tool.

#### Step 2: Renderer, chunking, chunk-size check

Deterministic rendering and greedy chunking under the two bounds
(`SAFE_CHUNK_BYTES` and the collection's token window); the over-size
split into `:p<n>` parts; a test that appending records changes only the
tail chunk's chash, and one where the title record arrives after earlier
chunks were written and their chashes do not change. Measure retrieval at
two chunk sizes on the validation set and record the chosen target per
embedder as a research finding: bge first, `voyage-context-3` in the
announced window.

#### Step 3: Scanner

Add `detect-secrets==1.5.0`; the four nexus plugins, each with a test on
its synthetic shape bare and inside a sentence; the redactor; the
blocked-socket test; the canary test; the multi-line private-key span,
with a fixture holding a key across lines, one with no END line, and one
inside an expand result; flagged-session stamping through the RDR-243
write path. Re-run the false-positive count of 256-research-17 over the
narrative of every project's sessions on Sam's machine, not only nexus's,
with the rendered tool lines counted as their own class beside assistant
text and subagent reports. Separately, re-run the census of
256-research-5 (every secret-shaped string, whether the scanner flags it or
not) over the rendered tool-line fields of the same sessions, per tool; a
tool whose field holds one the scanner misses moves to "named alone" in
the tool-line table. Record both as research findings before the first
managed-mode run.

#### Step 4: Explicit-only attribute and the subject

Verify A5. Check the three sites shared with RDR-229 (row-cache key list,
`register_collection`, tenant export) on develop and fold in whatever P3
has landed. The Liquibase changeset, sibling inheritance in the engine's
registration, the client row-cache key, `is_default_scope_row` and the
readers that filter through it, the positive listing lint with its
allowlist, the `select_config` gate with its fail-closed subject check and
the `nx enrich aspects` refusal, a read of what taxonomy assignment does
for a collection with no topics, the doctor check, the explicit-only
column in `nx collection list` and the `nx collection explicit-only
clear` route, and the two `llm-surfaces.toml` rows with the census count
raised to 27. The engine part ships in an engine cut.

#### Step 5: Commands, acknowledgement and expand

`nx index sessions` with the acknowledgement and its stored block and the
attribute re-assertion, through the promoted writer with its own hook
registry, the index-run fence and the deferred completion stamp, the
fence-key origin and the vw594 allowlist entry; `nx sessions expand`
through `TranscriptSource` with `LocalFileSource`; `forget` with its
exclusion entry and `--undo`; `list` with `--check`; and `--dry-run`
printing what would be indexed and the bytes it would send. Run the
Minimum Viable Validation.

### Phase 2: Operational Activation

#### Activation Step 1: SessionEnd hook

An opt-in SessionEnd hook runs `nx index sessions --session <id>` detached
for a main session. It adds no LLM call, and the write it triggers reaches
no aspect extraction (Write path and hooks). It runs on native Windows before its push
(AGENTS.md rule 14, since it lands under `conexus/hooks/`).

#### Activation Step 2: MCP expand

An MCP tool for expand, so agents need not shell out; it goes through the
MCP read-tool output rules of RDR-243. Its output is the redactor's output,
as on the CLI: a private key, single-line or multi-line, comes back as
`[redacted:private-key]`, and any other finding as `[redacted:<type>]`.

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
| `agent-sessions` collections (one per embedding model) | In scope (`nx sessions list`, `nx collection list`) | In scope (`list` shows per-session counts, versions, last run; `nx collection list` shows the attribute) | In scope (`nx sessions forget`, which also excludes the session from later runs, `--undo` to reverse; a collection by the usual collection delete; engine backups keep removed chunks until they age out) | In scope (`expand` statuses; `list --check` re-verifies a sample; doctor checks the attribute) | Tenant backup (RDR-248) carries rows and chunks; a client bundle carries the attribute from RDR-229 P3.9, re-asserted until then; verbatim records wait for the archive RDR |
| `explicit_only` column | In scope (`nx collection list`) | N/A | In scope (`nx collection explicit-only clear <collection> --reason`) | In scope (doctor) | With the collection row |
| `sessions.ack`, `sessions.exclude` config | In scope (`nx config get`) | In scope (the ack block names its endpoints and time) | In scope (unset `sessions.ack`, after which indexing refuses; `nx sessions forget --undo` removes one exclusion) | In scope (every run compares endpoints) | N/A |

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
- **Scenario**: the session's `custom-title` record arrives after its
  first chunks were indexed. **Verify**: those chunks keep their chash;
  search output shows the new title.
- **Scenario**: a record whose rendering exceeds the target on a bge
  collection. **Verify**: it splits into `:p<n>` parts, none over the token
  window or `SAFE_CHUNK_BYTES`, each carrying the record's line range, and
  expand verifies each part `ok`.
- **Scenario**: planted tokens in assistant text, thinking, a subagent
  report, a tool result, a Bash command, a tool call's file path, a Grep
  pattern, a WebFetch query string, an Agent prompt and an MCP tool
  argument. **Verify**: none appears in any chunk, expand result or
  outbound request (canary); no rendered Bash, Agent or MCP line carries
  input text, and no WebFetch line carries a query string; the session is
  stamped sensitive.
- **Scenario**: a multi-line private key in a tool result and in
  assistant text, and a BEGIN line with no END. **Verify**: each becomes
  one `[redacted:private-key]` covering the body lines, in chunks and in
  expand output.
- **Scenario**: a session is indexed, then the worker drains, the indexer's
  batch enqueue runs over the collection, and `nx enrich aspects` is run
  on it. **Verify**: the aspect queue has no row for its document, no
  extractor runs, the log records `aspect_gate_explicit_only`, and
  `nx enrich aspects` exits with its explicit-only refusal.
- **Scenario**: a collection of the subject whose cached row lacks the
  attribute, and one whose row cannot be read. **Verify**: `select_config`
  still gives no config for either (the fail-closed subject check).
- **Scenario**: a listing call added in a new module that neither filters
  through `is_default_scope_row` nor has an allowlist entry, and an
  allowlist entry for a call that no longer exists. **Verify**: the
  positive lint fails on each.
- **Scenario**: the scanner runs with sockets blocked. **Verify**: no
  connection attempt.
- **Scenario**: the 12 synthetic shapes of 256-research-17, bare and inside
  a sentence, and a fixture of real narrative lines holding chashes, commit
  shas and uuids. **Verify**: all 12 redacted; no identifier redacted.
- **Scenario**: install without the acknowledgement, and an install whose
  endpoints changed after it. **Verify**: the command prints the wording
  and writes nothing; the second refuses naming both endpoint lists.
- **Scenario**: bare `search(corpus="knowledge")`, `corpus="all"`, the
  default corpus, `nx store list` (its default `-c knowledge`),
  `nx store get <DOC_ID>` for a session document and the MCP `store_get`
  on the bare `knowledge` scope, the `nx_answer` planner's collection hint,
  an agent preamble's reusable-subject list, the Knowledge Map and a
  taxonomy discover over all collections. **Verify**: no session chunk,
  document, name or topic; naming the collection returns hits.
- **Scenario**: a sensitive session in local mode creates the bge sibling.
  **Verify**: the sibling has the attribute without the writer setting it.
- **Scenario**: the subject's collection registered without the attribute,
  as a pre-P3.9 bundle import does. **Verify**: doctor names it; the next
  indexing run sets it.
- **Scenario**: tenant backup and restore. **Verify**: the attribute
  survives.
- **Scenario**: expand after the transcript is deleted, after it is altered,
  and on another host. **Verify**: `source-gone`, `changed`, `other-host`,
  each with the chunk text.
- **Scenario**: a stub second `TranscriptSource` that serves the same lines.
  **Verify**: expand verifies through it after the file is deleted.
- **Scenario**: excluded session id and excluded project glob. **Verify**:
  nothing indexed; a previously indexed excluded session is reported, not
  silently kept.
- **Scenario**: `nx sessions forget` on an indexed session, then a full
  run, then `forget --undo` and another run. **Verify**: the session is
  absent after the first run and indexed again after the second.
- **Scenario**: a run whose engine write fails half way. **Verify**: the
  document keeps its prior manifest; the next run completes.

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation retrieval check.
   **Expected**: 8 of 12 found in the top five, each expand `ok`.
2. **Scenario**: the canary over every registered session surface.
   **Expected**: zero planted tokens outbound.
3. **Scenario**: the positive listing lint.
   **Expected**: every listing call under `src/nexus` either filters
   through `is_default_scope_row` or has an allowlist entry with a reason;
   its first live run's counts (calls examined, filtered, allowlisted) go
   in its bead.

### Performance Expectations

Decoding a changed file costs about one second per 45 MB (256-research-3);
the largest nexus transcript, about 260 MB, re-decodes in about six seconds
(inferred from that rate). The chosen scanner combination took 8.1 s for
the 42,456 assistant-text lines of five sessions (256-research-17), so it,
not the decode, sets the pace of a first backfill (inferred; the narrative
of all 137 sessions is roughly 110 MB by the 20-session rate).

## Finalization Gate

### Contradiction Check

Gate round 1 found two contradictions, both fixed: the claim that the
RDR makes no LLM call, while the default hook chain would queue every
non-sensitive session for aspect extraction through `claude -p` (now
barred at `select_config` for every aspect path, Write path and hooks),
and the claim that tool inputs are never embedded, while the tool line
carried a command prefix (now dropped, Sam 2026-11-21). Gate round 2 found
the remaining wording wider than the design (the aspect gate covered one
path; the acknowledgement's "no tool result" sat beside subagent reports;
"path or query" was undefined per tool), each fixed in the round-2 fix. Two
tensions, both resolved in the design. RDR-243's `sensitive` flag is
the project's privacy tool, yet the corpus as a whole is not flagged; the
flag withholds documents from agents, who are the corpus's readers, so the
design protects by scope, redaction and acknowledgement, and keeps the flag
for sessions the scanner flags. And the research memo proposed the dchash
span and diderot's active-path rule; the findings show both fail on real
transcripts (256-research-4, -10), so the design departs from the memo
there. RDR-229 Phase 3 shares three code sites with Step 4 but no meaning,
so it imposes no order. One tension is left open on purpose: Sam's Q1
words say tool inputs are never embedded, and a tool line's named field is
part of a tool input; the design reads his option-1 ruling as the scope of
Q1 and keeps every other input field off the line.

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
| `explicit_only` in the listing, row cache and export | nexus engine and client | Assumed (A5), Step 4 |
| `claude-session:` source URI | catalog | Assumed; spiked at Step 1 |
| `scan_line`, `transient_settings`, `RegexBasedDetector`, custom-plugin path | detect-secrets 1.5.0 | Source Search and Spike |
| Transcript record shape | Claude Code (undocumented) | Spike on real files |

### Scope Verification

The Minimum Viable Validation runs in Phase 1, on a local bge engine, as
part of Step 5's close: the twelve-question retrieval check, the append
re-run with its embed count, the bare-search exclusion, and the
planted-token canary. None of it is deferred.

### Cross-Cutting Concerns

- **Versioning**: decoder, renderer and scanner versions on each document;
  a change re-renders at the next run and the command says so.
- **Build tool compatibility**: one new pinned Python dependency.
- **Licensing**: detect-secrets is Apache-2.0, combinable into the
  AGPL-3.0-or-later repo (256-research-14).
- **Deployment model**: off until acknowledged; local bge sends nothing off
  the machine; managed mode sends redacted narrative to Voyage and the
  tenant after the acknowledgement. Expand reads only the host that holds
  the transcript until the archive RDR lands. The Liquibase column and the
  sibling rule ship in an engine cut; a client that predates them ignores
  the column, and an engine that predates them leaves the subject in
  default searches, so the client refuses to index sessions against an
  engine without the column.
- **IDE compatibility**: N/A.
- **Incremental adoption**: per-install acknowledgement, exclusions per
  session and project, `--since` for a partial backfill.
- **Secret/credential lifecycle**: secrets found are redacted, never
  stored; the scanner makes no network call; flagged sessions are stamped
  sensitive; `forget` removes an indexed session and excludes it from later
  runs, and engine backups taken before it keep the removed chunks until
  they age out. The command reads no credentials.
- **Memory management**: a file is decoded line by line with the existing
  64 MB line cap (`transcripts_read.py:35`); the active-path walk holds one
  id map per file (about 110,000 entries for the largest, inferred from the
  line rate of 256-research-3).

### Proportionality

Phase 1 is one decoder, one renderer, one scanner dependency with four
plugins, one engine column with its readers, and four commands, with no
LLM call added. The alternatives section can shrink at lock time.

## Decisions (Sam)

Sam answered the first draft's questions on 2026-11-21 (T2
`nexus/sam-ruling-rdr256-session-corpus-2026-11-21`):

1. **Cloud after acknowledgement.** Redacted narrative may go to Voyage and
   the tenant after a one-time acknowledgement that names where it goes;
   tool inputs and results are never embedded, other than subagent reports
   and a tool line's path or query. A tool line names only the
   tool and its file path or query, never a command's text (Sam,
   2026-11-21, ask `rdr256-command-prefix`, option 1: "drop the prefix; the
   line names only the tool and its path or query, Q1 stays literally
   true").
2. **Knowledge subject plus explicit-only attribute.** First answered as a
   fifth content type, then changed by Sam in the same session after the
   census and the RDR-229 collision (Alternative 3).
3. **A Python scanning library**, vetted and pinned: detect-secrets 1.5.0
   with four nexus plugins.
4. **A verbatim archive in PostgreSQL**, as its own RDR, next.
5. **No summaries for now.**

No question for Sam remains open in this RDR.

## References

- T3 `research-diderot-vs-nexus-2026-11-21 (1/2)`, `(2/2)` (collection
  nexus-architecture); T3 `architecture-diderot-overview`
  (distributed-systems).
- T2 `nexus_rdr/256-research-1` through `-21`;
  `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`;
  `nexus/sam-ruling-rdr256-session-corpus-2026-11-21`.
- diderot clone: `docs/session-ingest.md`, `docs/design.md:190-198,
  :236-246, :589-600`, `internal/sessiondecode/FORMATS.md`,
  `internal/sessioningest/{summarizer,publish,pushexpand,pushretention}.go`,
  `internal/sessionship/sync.go:307-308`,
  `internal/attachments/files/redact.go`.
- detect-secrets 1.5.0 sdist: `detect_secrets/core/scan.py`,
  `settings.py`, `plugins/base.py`, `filters/common.py`,
  `core/usage/filters.py`, `core/plugins/util.py`; `docs/plugins.md` (via
  Context7).
- nexus: `src/nexus/lua_runtime/transcripts_read.py`,
  `transcripts_projects.py`, `verbs_transcripts.py`,
  `src/nexus/memory_curate/sessions.py`, `src/nexus/doc_indexer.py`,
  `src/nexus/corpus.py`, `src/nexus/mcp_infra.py`, `src/nexus/mcp/core.py`,
  `src/nexus/catalog/http_catalog_client.py`,
  `src/nexus/sensitive_write.py`, `src/nexus/redact.py`,
  `src/nexus/tables/llm-surfaces.toml`,
  `src/nexus/tables/chunk-metadata-keys.toml`, `docs/collections.md`,
  `docs/agents/catalog-t3-split.md`; engine
  `service/src/main/java/dev/nexus/service/db/TenantExport.java`,
  `vectors/EmbeddingProfile.java`.
- RDR-103, RDR-131, RDR-171, RDR-204, RDR-207, RDR-229, RDR-240, RDR-243,
  RDR-246.

## Revision History

- 2026-11-21: drafted.
- 2026-11-21: revised for Sam's first answers: fifth content
  type with its census, detect-secrets with four nexus plugins, the
  acknowledgement's wording and storage, sensitivity for flagged sessions,
  the `TranscriptSource` interface for the archive RDR, summaries removed
  from the plan. Research 256-research-12 to -17 added.
- 2026-11-21: revised for Sam's change to Q2. Placement returns to the
  knowledge subject `agent-sessions` with an explicit-only collection
  attribute, owned by its subject; the fifth content type moves to
  Alternative 3 as rejected by Sam on the census (256-research-12) and the
  RDR-229 P3.8 collision (256-research-13), and the wait on RDR-229 Phase 3
  that existed only for it is removed. The attribute's own ordering against
  RDR-229 was checked (256-research-18): none for meaning, three shared code
  sites, and a recovery-bundle gap until P3.9, covered by re-assertion.
  Gap 4 reworded.
- 2026-11-21: Gate round 1 — BLOCKED (2 Critical, 6 Significant, 2 ship-blocker(s)); blob `7577b9115929`; critique `nexus_rdr/256-gate-critique-2026-11-21-r1`.
- 2026-11-22: Gate round 2 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); blob `2cd057fe6cfd`; critique `nexus_rdr/256-gate-critique-2026-11-22-r2`.
