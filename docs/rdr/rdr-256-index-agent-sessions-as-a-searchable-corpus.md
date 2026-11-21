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
related_rdrs: [RDR-131, RDR-171, RDR-207, RDR-223, RDR-240, RDR-243, RDR-246]
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
its **chash**, the SHA-256 of its text.

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

## Relationship to Prior RDRs

Scanned the RDR index for session, transcript, conversation and summary
titles; read RDR-131, RDR-207, RDR-240, RDR-243 and RDR-171.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-131 T2 Session Rollup Summaries | Abandoned predecessor, different object | It proposed LLM rollup summaries over aged **T2 memory entries** (not transcripts) to bound context injection. It was abandoned on 2026-09-12 as a batch with RDR-132 and RDR-133 because it was an unresearched stub, explicitly "not a ruling that the idea is wrong" (rdr-131 `abandon_reason`). Nothing here repeats it: this RDR indexes transcripts, writes no summaries in its first phase, and changes nothing about T2. |
| RDR-207 Summarization at the T2 Expiry Boundary | Precedent for summaries | It took RDR-131's live half and decided that a rollup is an attended command, never a session-end side effect (rdr-207:392), because session end is the worst context for an LLM call (:334-339) and a hallucinated summary is worse than none (:344-346). This RDR adopts both rules: no LLM call in any hook, and summaries, if ever built, cite record ranges a reader can expand. |
| RDR-240 Procedures as Lua scripts | Origin of the transcript reader | Its `transcripts.sessions` verb owns the file listing and line framing (`src/nexus/lua_runtime/transcripts_read.py`). This RDR reuses those, not the verb: the verb is a read-only count surface and never returns tool results (rdr-240:361-365). It has no active-path walk (256-research-2). |
| RDR-243 Document sensitivity | Hard constraint | Its LLM-surface table (`src/nexus/tables/llm-surfaces.toml`) gets this RDR's surfaces as rows. Its `sensitive` flag withholds a document from the session model by default (Q1), so it cannot be the protection for a corpus whose readers are agents (256-research-8). |
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
an LLM (map), folds the chunk summaries into a session summary (reduce),
re-maps only the tail chunk as a session grows, and expands a hit to the
verbatim record range (256-research-6).

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
- Collection naming follows `docs/collections.md`: four content types
  (`code`, `docs`, `rdr`, `knowledge`), and only `knowledge` is named by
  hand, after a subject.
- More terms. **bge** is the embedding model a local install runs on its
  own machine, so nothing leaves it. **Fan-out** is how a search over a
  corpus such as `knowledge` queries every collection of that type and
  merges the results. The **Knowledge Map** is the list of topics injected
  into every subagent's starting context; **taxonomy labelling** names
  those topics with an LLM. A **tumbler** is a catalog document's
  hierarchical id (such as `1.1.42`). The **knowledge curator** is the
  catalog owner that knowledge notes are filed under. **Liquibase** is the
  tool the engine uses to change its PostgreSQL schema. The **orphan
  reaper** is the engine job that deletes chunks no manifest names. A
  **canary** test plants a unique string in the input and asserts it never
  appears in any output.

## Research Findings

### Investigation

Read the transcript readers in `src/nexus/lua_runtime/` and
`src/nexus/memory_curate/`, the indexing write path in
`src/nexus/doc_indexer.py`, the corpus routing in `src/nexus/corpus.py`, the
sensitivity code and tables, and RDR-131, RDR-171, RDR-207, RDR-240 and
RDR-243. Read diderot's `internal/sessiondecode/FORMATS.md`,
`internal/sessioningest/{summarizer,publish,pushexpand,pushretention}.go`,
`internal/sessionship/sync.go`, `internal/attachments/files/redact.go`,
`docs/session-ingest.md` and `docs/design.md`. Ran five read-only Python
scans over Sam's own nexus transcripts (byte composition, active-path
coverage, secret-shaped strings, subagent composition, prefix stability),
printing counts only.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Claude Code transcript format | Yes (real files) | Records carry `uuid`, `parentUuid`; `compact_boundary` records carry `logicalParentUuid`, which often names a record absent from the file (256-research-4). No published schema; the format can change without notice. |
| diderot `sessiondecode` | Yes | Active-path rule, record splitting and id scheme (FORMATS.md:31-62). Fixtures are synthetic (FORMATS.md:33-35). |
| Engine document write | Yes | One write per document, manifest replaced, re-embed skipped for held text (doc_indexer.py:2384-2389, :2844-2850). |
| gitleaks default rules | No | Inferred, not read: an MIT-licensed set of about 150 regexes; diderot uses it at v8.30.1 (diderot `go.mod`). Needs a license and rule read before Phase 1 Step 3. |

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
  inputs and tool results, never in assistant text, thinking or user text,
  across all 137 main nexus transcripts. *Source: 256-research-5.*
- **⚠️ Documented** (source search): diderot embeds summaries only and
  redacts no session content. *Source: 256-research-6.*
- **✅ Verified** (source search): the incremental mechanism exists (one
  write per document, embed skip), and the manifest already has
  `line_start`/`line_end` position columns. *Source: 256-research-7.*
- **⚠️ Documented** (source search): the `sensitive` flag withholds a
  document from agents by default; on a managed install every collection
  embeds through Voyage and is stored in the tenant. *Source:
  256-research-8.*
- **✅ Verified** (source search): content types are a closed four-tuple
  repeated across client and engine; bare-corpus fan-out has no way to skip
  a live collection. *Source: 256-research-9.*
- **⚠️ Documented** (source search): RDR-131, RDR-207 and RDR-171
  constraints as tabled above. *Source: 256-research-10.*

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
  session content well enough without LLM summaries. **Status**:
  Unverified. **Method**: Spike (the Minimum Viable Validation's question
  set).
- [x] **A4.** Re-sending a grown session's chunks re-embeds only new chunk
  text. **Status**: Verified (256-research-7). **Method**: Source Search.
- [ ] **A5.** A live knowledge collection can be kept out of bare
  `corpus="knowledge"` fan-out, the SubagentStart Knowledge Map and
  automatic taxonomy labelling by one catalog attribute. **Status**:
  Unverified; no such attribute exists (256-research-9). **Method**: Source
  Search during Phase 1 Step 4.

## Proposed Solution

### Approach

Index the narrative of each main session into one knowledge collection,
redacted, with no LLM call; record each chunk's JSONL line range in the
manifest; expand a hit by reading those lines back from the transcript.
Concretely:

1. **What is indexed.** Main transcripts only, active path only, narrative
   only: human-typed prompts, assistant text, plaintext thinking, subagent
   reports (the result of an `Agent` tool call, and a task notification that
   carries one), and one short line per tool call naming the tool and its
   target (a file path, the first 160 characters of a command, a query).
   Tool results, harness-injected text (`isMeta`), and bookkeeping lines are
   not embedded; expand still returns them.
2. **Where it lives.** One knowledge collection, subject `agent-sessions`,
   owned by its subject per Sam's ruling of 2026-11-21 (T2
   `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`). One catalog
   document per session. The collection carries a new **explicit-only**
   attribute: it is searched only when named, and the Knowledge Map and
   automatic taxonomy labelling skip it.
3. **Privacy.** A pattern secret scanner runs over every chunk before it is
   written and over every expand result before it is returned; a match
   becomes `[redacted:<rule>]`, and a chunk the scanner cannot process is not
   written. Indexing is off until an install opts in with an acknowledgement
   that names where the text goes (the embedder and, on a managed install,
   the tenant). Sessions can be excluded by id or project.
4. **When.** An explicit `nx index sessions` command. A session written to
   in the last five minutes indexes only its sealed chunks. A later phase
   adds an opt-in SessionEnd hook that runs the same command for the ending
   session; it makes no LLM call.
5. **Incremental by content addressing.** Chunk boundaries are computed
   from the start of the active path, so earlier chunks keep their text as a
   session grows. Each run re-decodes a changed file, re-renders all chunks
   and writes the document; the engine embeds only chunks it has not seen.
6. **No summaries in the first phase.** They are a later, attended command
   with its own LLM-surface row, built only if the validation shows a need.

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
a target size (initial value 4,000 characters, set by the chunk-size check
in Phase 1 Step 2 and stated once in Research Findings when measured). A
chunk carries a one-line header naming the session's title and date so a
hit reads in context. The renderer version is stored on the document; a
change to it re-renders every session at its next run, which costs embeds
and is announced in the command's output.

**Catalog and manifest.**

- Document identity is the session uuid. `source_uri` is
  `claude-session:<session-uuid>` (scheme name provisional; inferred, not
  read, whether the catalog accepts an unregistered scheme). The title is
  the session's `custom-title` or `agent-name` record when present, else
  the first typed prompt cut to 80 characters.
- Document meta: host name, project directory, repository owner tumbler
  when the project maps to a registered repo
  (`transcripts_projects.repo_project_dirs`), transcript path, file size and
  mtime at the last run, decoder and renderer versions, record and chunk
  counts.
- Documents register under the knowledge curator, never a repository owner,
  so no repo-collection owner trigger can act on them while RDR-229's owner
  work is in flight.
- Each manifest row sets `line_start` and `line_end` to the JSONL line
  numbers (1-based, inclusive) of the first and last record in the chunk.

**Expand.** `expand(tumbler, position) -> ExpandResult` reads the manifest
row, opens the transcript named in meta on this host, decodes the line range
with the current decoder, re-renders it, and compares the SHA-256 of the
rendering with the row's chash. On a match it returns the verbatim records
of the range on the active path, tool results included, each cut to 32 KiB,
through the secret scanner. Otherwise it returns a status and the chunk's
own text:

| Status | Meaning |
| --- | --- |
| `ok` | verified; verbatim records returned |
| `source-gone` | the file no longer exists (Claude Code cleanup, or another host) |
| `changed` | the re-rendering does not match (rewind, format change, or a broken A1) |
| `other-host` | meta names a different host |

**Secret scanner.** A checked table, `src/nexus/tables/secret-patterns.toml`,
holding regex rules ported from gitleaks' default ruleset, plus the prefixes
256-research-5 used. The redactor is a pure function `redact(text) -> (text,
hits)`. Its interface does not change if Sam picks a library instead (open
question 3).

**Surfaces.** Two new rows in `llm-surfaces.toml`: "Session corpus:
indexing" (sends redacted narrative chunks; endpoints `voyage` and
`cloud-tenant` on a managed install, `none` on a local bge install;
behaviour `disclose`), and "Session corpus: expand" (sends redacted verbatim
records to `session-model`; behaviour `withhold`, so an excluded session is
never returned). The registry lint then covers both call sites.

**Opt-in.** Config key `sessions.index` (default `false`) and
`sessions.exclude` (session ids and project globs). The first run on an
install with `sessions.index` unset prints the disclosure and refuses; the
operator sets the key with an `--ack` flag that records the time and the
endpoint list in the config. This mirrors `--ack-external-embedder`
(`src/nexus/sensitive_write.py:58-69`) at install scope.

**Explicit-only collections.** One Liquibase changeset adds a nullable
boolean to the engine's collection attributes. Bare-corpus fan-out
(`corpus.py` and `mcp_infra.get_live_collection_names`), the Knowledge Map
and automatic taxonomy labelling skip a collection that has it; naming the
collection or its prefix in `corpus=` searches it. The census lint of the
fan-out sites (as `tests/test_bc7ps_routing_uses_live_listing.py` does for
live listing) pins the skip.

**Commands (names provisional).**

```text
nx index sessions [--project repo|all|<dir>] [--session <uuid>] [--since DATE] [--dry-run]
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
| Active-path decoder | none (256-research-2) | New. |
| Chunk write, embed skip, manifest | `src/nexus/doc_indexer.py` write path | Reuse. |
| Line-range positions | `catalog_document_chunks.line_start/line_end` | Reuse; no schema change. |
| Orphan cleanup when a tail chunk changes | engine orphan reaper (anti-join on manifest) | Reuse. |
| Redaction | `src/nexus/redact.py` | Keep for its log use; new pattern scanner beside it. |
| Fan-out skip | lifecycle-state filter in `corpus.py` | Extend with the explicit-only attribute. |
| Transcript counts | `transcripts.sessions` verb | Unchanged. |

### Decision Rationale

Embedding the narrative directly, rather than summaries, is the cheaper and
more honest first step. Nexus has a lexical leg and a reranker that diderot
lacks (memo axis 2), so verbatim narrative is searchable without an LLM
turning it into prose first. The narrative is about 1 percent of the bytes,
so volume is bounded. The scope also follows the secrets: every
secret-shaped string found sat in tool inputs or results, which are not
embedded. And no LLM call means no new LLM cost, no hallucinated record,
and nothing for RDR-207's failure modes to bite on.

Line ranges in the manifest, checked by re-rendering against the chash,
give expansion a stamp that survives appends, since only the tail chunk's
text changes, and fail visibly when the file changes or disappears.

## Alternatives Considered

### Alternative 1: Diderot's design, summaries embedded

**Description**: Map/reduce LLM summaries per chunk and per session, embed
the summaries, expand to records.

**Pros**:

- Summaries read well and match "which session did X" questions.
- Diderot's incremental digest scheme bounds re-summarization.

**Cons**:

- An LLM pass over all narrative, roughly 200,000 input tokens per average
  session (inferred from 256-research-3: 15.9 MB over 20 sessions, at 4
  bytes per token), on every session.
- A summary can be wrong, and RDR-207 found a wrong record worse than none.
- A new LLM surface that sees the full narrative.

**Reason for rejection**: deferred, not rejected. It becomes Phase 3 only if
the validation shows narrative search missing session-level questions.

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
returns.

### Alternative 3: A fifth content type, `session`

**Description**: `session__<repo-owner>__<model>__v1`, one collection per
repository, owned by the repo's tumbler.

**Pros**:

- Kept out of `knowledge` fan-out by construction; per-repo scoping by
  collection.
- Fits owner-is-source as cleanly as the knowledge option does.

**Cons**:

- Content types are a closed set in 17 client modules and engine regexes
  (256-research-9); a public naming surface changes.

**Reason for rejection**: cost out of proportion to a first phase; kept as
open question 2 for Sam.

### Alternative 4: The `sensitive` flag as the protection

**Description**: Index every session as a sensitive document.

**Cons**:

- MCP tools then withhold the corpus from the session model, which is its
  reader (256-research-8); on a managed install `--sensitive` needs the
  acknowledgement anyway.

**Reason for rejection**: it defeats the purpose. A session the operator
wants kept away from everything is excluded from indexing instead.

### Briefly Rejected

- **Diderot's strict active-path rule**: drops 61 percent of records on
  real files (256-research-4).
- **Subagent transcripts in phase 1**: little narrative of their own; their
  reports are already indexed through the main transcript.
- **A local index store outside the engine**: the NO-SQLITE hot rule.
- **A dchash span per chunk**: stale at the next append (Gap 2).
- **A push server, revisions, quotas (diderot's ingest protocol)**: files
  are local; one user.

## Trade-offs

### Consequences

- Positive: past sessions answer questions in seconds; a compacted session
  can find what it said before compaction.
- Positive: no LLM cost and no new model surface beyond the embedder.
- Negative: on a managed install, redacted narrative goes to Voyage and the
  tenant, which is the reason for the opt-in.
- Negative: verbatim expansion lasts only as long as Claude Code keeps the
  file, about 30 days by default.
- Negative: the decoder depends on an undocumented file format.

### Risks and Mitigations

- **Risk**: a secret the scanner does not know reaches the embedder.
  **Mitigation**: tool inputs and results, where every measured hit sat,
  are not embedded; `nx sessions forget` removes a session; the canary test
  plants tokens in narrative.
- **Risk**: Claude Code changes the transcript format.
  **Mitigation**: the decoder rejects a structurally broken file and logs
  it; fixtures from real files (stripped) pin each known shape; the decoder
  version re-renders on upgrade.
- **Risk**: the corpus crowds curated knowledge out of agents' searches.
  **Mitigation**: explicit-only collection.
- **Risk**: agents treat transcript text as decisions.
  **Mitigation**: hits are labelled `session transcript`; T2 stays the
  record of decisions (see Relation to T1, T2 and the board).

### Failure Modes

- Indexing fails on one file: the run reports it with its reason and
  continues; the document keeps its previous chunks.
- The engine is unreachable: nothing is written; the next run redoes the
  work.
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

- [ ] A2, A3 and A5 verified at the steps named.
- [ ] Sam's answers to the open questions below.
- [ ] gitleaks rule license read (Dependency Source Verification).

### Minimum Viable Validation

On a local engine with the bge embedder (no cloud, no Voyage), index ten
real nexus sessions, five of them compacted. Twelve questions, each with
the session and line it should find, written before the run from Sam's T2
records of those sessions: eight must return a chunk from the right session
in the top five, and the expand of that chunk must verify (`ok`). Then
append a copy of one transcript with new records and re-run: the engine's
embed count equals the number of new or changed chunks. A fixture
transcript with planted tokens in assistant text and in a subagent report
yields no token in any written chunk, expand output, or outbound payload.

### Phase 1: Code Implementation

#### Step 1: Decoder and active path

Tests first, on fixtures built from real transcripts with their text
replaced: active path with the missing-logical-parent fallback, rewinds,
duplicate uuids, sidechain lines, oversized lines. Verify A2 on three
compacted sessions.

#### Step 2: Renderer, chunking, chunk-size check

Deterministic rendering and greedy chunking; a test that appending records
changes only the tail chunk's chash. Measure retrieval at two chunk sizes
on the validation set and record the chosen size as a research finding.

#### Step 3: Secret scanner

The checked table, the redactor and its canary test; a chunk that fails the
scanner is not written.

#### Step 4: Collection, explicit-only attribute, write path

The Liquibase changeset, the fan-out, Knowledge Map and taxonomy skips with
their lint, the document and manifest write, the opt-in and exclusion
config, the two `llm-surfaces.toml` rows.

#### Step 5: Commands and expand

`nx index sessions`, `nx sessions expand`, `forget`, `list`, with
`--dry-run` printing what would be indexed and the bytes it would send.

### Phase 2: Operational Activation

#### Activation Step 1: SessionEnd hook

An opt-in SessionEnd hook runs `nx index sessions --session <id>` detached
for a main session. No LLM call. It runs on native Windows before its push
(AGENTS.md rule 14, since it lands under `conexus/hooks/`).

#### Activation Step 2: MCP expand

An MCP tool for expand, so agents need not shell out; it goes through the
MCP read-tool output rules of RDR-243.

### Phase 3: Summaries (conditional)

Only if the validation's misses are session-level questions: an attended
`nx index sessions --summarize`, incremental by input digest as diderot does,
summaries citing line ranges, its own `llm-surfaces.toml` row, never run
from a hook.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `knowledge__agent-sessions` collection | In scope (`nx sessions list`, `nx collection list`) | In scope (`list` shows per-session counts, versions, last run) | In scope (`nx sessions forget`; the collection by the usual collection delete) | In scope (`expand` statuses; `list --check` re-verifies a sample) | Existing tenant backup (RDR-248) covers it; verbatim records are not backed up (the transcript is the source) |
| `sessions.index`, `sessions.exclude` config | In scope (`nx config get`) | N/A | In scope (unset) | N/A | N/A |

### New Dependencies

None if open question 3 is answered with a ported rule table. A secret
scanner library otherwise, with its license checked.

## Test Plan

- **Scenario**: compacted session whose boundary names a missing logical
  parent. **Verify**: records before the boundary are on the path.
- **Scenario**: rewound session. **Verify**: rewound records are off the
  path; after re-index the manifest has no chunk from them.
- **Scenario**: session grows by 50 records. **Verify**: earlier chunks keep
  their chash; only the tail and new chunks are embedded.
- **Scenario**: planted tokens in assistant text, thinking, a subagent
  report and a tool result. **Verify**: none appears in any chunk, expand
  result or outbound request (canary).
- **Scenario**: install without opt-in. **Verify**: the command prints the
  disclosure and writes nothing.
- **Scenario**: bare `search(corpus="knowledge")`. **Verify**: no hit from
  `agent-sessions`; naming it returns hits.
- **Scenario**: expand after the transcript is deleted, after it is altered,
  and on another host. **Verify**: `source-gone`, `changed`, `other-host`,
  each with the chunk text.
- **Scenario**: excluded session id and excluded project glob. **Verify**:
  nothing indexed; a previously indexed excluded session is reported, not
  silently kept.
- **Scenario**: a run whose engine write fails half way. **Verify**: the
  document keeps its prior manifest; the next run completes.

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation question set.
   **Expected**: 8 of 12 found in the top five, each expand `ok`.
2. **Scenario**: the canary over every registered session surface.
   **Expected**: zero planted tokens outbound.
3. **Scenario**: the fan-out lint.
   **Expected**: every bare-corpus site honours explicit-only.

### Performance Expectations

Decoding a changed file costs about one second per 45 MB (256-research-3);
the largest nexus transcript, about 260 MB, re-decodes in about six seconds
(inferred from that rate). A full first backfill of the nexus project's
main files is decode time of about a minute plus the embedding of about 1
percent of 2.9 GB.

## Finalization Gate

### Contradiction Check

Two tensions, both resolved in the design. RDR-243's `sensitive` flag is
the project's privacy tool, yet this RDR does not use it for the corpus;
the flag withholds documents from agents, who are the corpus's readers, so
the design protects by scope, redaction and opt-in instead and keeps
exclusion for sessions the operator names. And the research memo proposed
the dchash span and diderot's active-path rule; the findings show both fail
on real transcripts (256-research-4, -10), so the design departs from the
memo there. No contradiction remains between the findings and the proposed
solution.

### Assumption Verification

A4 is verified. A1 is partly verified: the prefixes of the five newest
nexus transcripts, hashed at the start and the end of this research, did
not change while four of them grew (256-research-11); a resume, a rewind
and a Claude Code upgrade are still untested, and Phase 1 Step 1 adds them
to its fixtures. A2, A3 and A5 stay unverified and are verified at Phase 1
Steps 1, 2 and 4 before later steps start.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Document write with manifest replace | nexus engine `/v1` via `doc_indexer` | Source Search |
| Re-embed skip for held chunk text | nexus engine (RDR-181) | Source Search |
| `line_start`/`line_end` manifest columns | catalog `document_chunks` | Source Search (key table); write path not yet read |
| `claude-session:` source URI | catalog | Assumed, Phase 1 Step 4 |
| Transcript record shape | Claude Code (undocumented) | Spike on real files |

### Scope Verification

The Minimum Viable Validation runs in Phase 1, on a local bge engine, as
part of Step 5's close: the twelve-question set, the append re-run with
its embed count, and the planted-token canary. None of it is deferred.

### Cross-Cutting Concerns

- **Versioning**: decoder and renderer versions on each document; a change
  re-renders at the next run and the command says so.
- **Build tool compatibility**: N/A.
- **Licensing**: the gitleaks rule port needs its license read first.
- **Deployment model**: off by default; local bge sends nothing off the
  machine; managed mode sends redacted narrative to Voyage and the tenant
  after the acknowledgement. Expand works only on the host that holds the
  transcript.
- **IDE compatibility**: N/A.
- **Incremental adoption**: opt-in per install, exclusions per session and
  project, `--since` for a partial backfill.
- **Secret/credential lifecycle**: secrets found are redacted, never stored;
  the scanner's rule table is versioned; `forget` removes an indexed
  session. The command never reads credentials.
- **Memory management**: a file is decoded line by line with the existing
  64 MB line cap (`transcripts_read.py:35`); the active-path walk holds one
  id map per file (about 110,000 entries for the largest, inferred from the
  line rate of 256-research-3).

### Proportionality

The first phase is one decoder, one renderer, one table, one engine
changeset and four commands, with no LLM. Summaries and the MCP tool are
later and conditional. The alternatives section could shrink at lock time
once Sam answers the open questions.

## Open Questions (for Sam)

1. **Managed mode.** May redacted session narrative go to Voyage and the
   managed tenant after a one-time acknowledgement, or is the first phase
   local-only (refused on a managed install)? Sam's own install is managed,
   so local-only means he uses it only through a local engine.
   *Recommendation*: allow with the acknowledgement; the narrative held no
   secret-shaped string in the scan.
2. **Placement.** A knowledge subject `agent-sessions` with an
   explicit-only attribute (recommended), or a fifth content type
   `session` owned by each repository?
3. **Scanner.** Port gitleaks' default rules into a checked table
   (recommended, no new dependency), or add a Python secret-scanning
   library?
4. **Retention.** Keep indexed chunks after Claude Code deletes the
   transcript (recommended), knowing expand then returns chunk text only?
   And is a verbatim archive in PostgreSQL wanted, which would be its own
   RDR (RDR-171 deferred the same kind of stored copy), or a longer
   `cleanupPeriodDays` in Sam's own settings?
5. **Summaries.** Is Phase 3 wanted at all if narrative search passes the
   validation?

## References

- T3 `research-diderot-vs-nexus-2026-11-21 (1/2)`, `(2/2)` (collection
  nexus-architecture); T3 `architecture-diderot-overview`
  (distributed-systems).
- T2 `nexus_rdr/256-research-1` through `-11`;
  `nexus/sam-ruling-rdr229-owner-is-source-2026-11-21`.
- diderot clone: `docs/session-ingest.md`, `docs/design.md:190-198,
  :236-246, :589-600`, `internal/sessiondecode/FORMATS.md`,
  `internal/sessioningest/{summarizer,publish,pushexpand,pushretention}.go`,
  `internal/sessionship/sync.go:307-308`,
  `internal/attachments/files/redact.go`.
- nexus: `src/nexus/lua_runtime/transcripts_read.py`,
  `verbs_transcripts.py`, `src/nexus/memory_curate/sessions.py`,
  `src/nexus/doc_indexer.py`, `src/nexus/corpus.py`,
  `src/nexus/sensitive_write.py`, `src/nexus/redact.py`,
  `src/nexus/tables/llm-surfaces.toml`,
  `src/nexus/tables/chunk-metadata-keys.toml`, `docs/collections.md`,
  `docs/agents/catalog-t3-split.md`.
- RDR-131, RDR-171, RDR-207, RDR-240, RDR-243, RDR-246.

## Revision History

- 2026-11-21: drafted.
