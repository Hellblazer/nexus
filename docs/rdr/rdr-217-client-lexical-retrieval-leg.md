---
title: "A Lexical Leg for the nexus Client: Reach the Engine's FTS Hybrid Route"
id: RDR-217
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: unreviewed
created: 2026-09-19
related_issues: [nexus-06aei]
related_rdrs: [RDR-026, RDR-155, RDR-156, RDR-180, RDR-188]
---

# RDR-217: A Lexical Leg for the nexus Client: Reach the Engine's FTS Hybrid Route

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside the template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** On 2026-09-19 Sam asked whether the client's ripgrep path could
retire, saying he wanted to "make full use of the fts indexing we already pay
the price for". FTS is full-text search: Postgres builds a searchable index of
the words in a document, so a query can match on the words themselves rather
than on a vector's notion of similar meaning. A census answered the first half
(T2 `nexus/census-engine-fts-hybrid-search-2026-09-19` [26452]) and the ripgrep
path was deleted the same day (nexus-06aei, commits 2da7be464, 7149dbf72,
9d0ab7ded). That deletion removed the client's broken *substitute* for lexical
search. It did not deliver the thing Sam asked for, and this record is that
second half.

## Problem Statement

The engine stores, for every chunk of text it holds, two lexical indexes
alongside the vector index. Nothing the nexus client can call reads either one.
`nx search` is vector-only, and has been since the engine's hybrid route was
built.

A chunk is a passage of text the engine has embedded and can retrieve. For each
one, `nexus.chunks` carries `chunk_tsv`, a `GENERATED ALWAYS AS
(to_tsvector('english', chunk_text)) STORED` column, indexed by the GIN index
`idx_chunks_tsv`; and a second GIN index `idx_chunks_trgm` over `chunk_text`
using `gin_trgm_ops`, which supports trigram matching (comparing three-character
sequences, so `authenticat` can match `authentication` without either being a
whole word). Every chunk write pays both index insertions, in addition to the
one vector index insertion it needs.

#### Gap 1: `nx search` has no lexical leg, though every chunk pays for one

`/v1/vectors/search` and its alias `/v1/vectors/query` dispatch to
`VectorHandler#handleSearch`, which calls `PgVectorRepository#searchWithTokens`.
That method contains no reference to `text_gate`, `chunk_tsv`,
`plainto_tsquery` or `word_similarity`. It is purely a vector search.

Only `/v1/vectors/hybrid-search` reaches the lexical indexes, via
`PgVectorRepository#hybridSearchWithTokens`. No Python, shell script or
configuration in this repository calls that route, and
`src/nexus/db/http_vector_client.py` defines no method for it.

So a user searching for an exact identifier gets whatever the embedding model
thinks is semantically near it, and the index that would have matched the
identifier itself is present, populated, and unread.

#### Gap 2: the route is unreachable from nexus's own test suite, and that has already cost a production outage

This is the gap that does not depend on retrieval quality improving at all.

`/v1/vectors/hybrid-search` is not unused. It has a production consumer:
conexus, the cloud deployment. The RDR-180 post-mortem records the consequence
of that asymmetry in its process observations
(`docs/rdr/post-mortem/180-content-address-chash-binary-32byte.md:104`):

> `/v1/vectors/hybrid-search` had zero nexus-side callers — conexus is its only
> consumer — so no nexus journey could ever have driven hybrid-in-window, and
> BUG-0148's surface was structurally unreachable from our test suite.

BUG-0148 itself (2026-07-19, engine v0.1.48) is the outage that lesson came
from. An `ALTER TYPE` conversion rewrote its tables and reset the planner's
statistics. The planner, now working from stale statistics, moved sparse
text-gate hybrid queries off the GIN-bitmap plan and onto a budget-bounded
HNSW plan, and **hybrid-search returned zero rows in production while
`/health`, `/version`, the smoke round-trip and the aggregate STEP-6 exit code
all stayed green**. Remediation was a manual `ANALYZE`.

A nexus caller would make that endpoint reachable from nexus journeys for the
first time. The coverage hole is closed by the wiring, whatever the retrieval
numbers say.

#### Gap 3: two fusion implementations exist, neither is reachable from the client, and one is dead

Fusion here means combining a vector ranking and a lexical ranking into one
ordered result list.

The live implementation is a Java dispatch in `PgVectorRepository`, which
probes how selective the text gate is and picks a query plan accordingly
(the "lcogi" path, after the bead that built it). It backs `/hybrid-search`.

The second is a family of Postgres functions, `nexus.hybrid_search_384`,
`_768` and `_1024`, which do the whole fusion in one round trip using
Reciprocal Rank Fusion: `score = 1/(60+vec_rank) + 1/(60+ts_rank) +
1/(60+trgm_rank)`. Nothing calls them. Their only consumer anywhere in the
repository is `HybridSearchFunctionParityIntegrationTest.java`. They cost
maintenance on every schema change that touches them (three changesets across
three dimensions, nine `CREATE OR REPLACE` statements for a function nothing
calls) and they ship nine generated jOOQ classes in the built jar.

RDR-156 P5 built those functions explicitly to replace the Java dispatch. Its
P5.G gate measured them at production scale and returned GO: the function ties
or beats the Java path in the selective-gate regime the redesign targeted
(6/6 recall, 638ms against 661ms), but it has no escape valve for dense gates,
and the cost ratio grows with gate size — 0.97x at 6 rows, 2.57x at 1,830,
6.50x at 25,000 (683ms against 105ms). The follow-up bead nexus-76352 was
closed WONTFIX on 2026-08-28 with the reasoning that nobody would notice, and
the caller was never switched. That is an accepted trade-off, not an
oversight, and this RDR does not reopen it.

## Relationship to Prior RDRs

**RDR-155 P3.2** built `/hybrid-search` deliberately as a parity-harness seam
with, in its own words, "no new public surface". That constraint is the direct
reason this gap exists, and this RDR proposes to lift it for the client. The
seam's purpose was to let the engine's fusion be compared against a reference
without committing to an API; five months of conexus traffic have since
answered the question the seam was hedging.

**RDR-156 P5** is the trade-off above. This RDR takes the Java dispatch as
given and does not propose switching to the SQL function family. If a future
record revisits that, Gap 3's maintenance cost is the argument for deleting the
dead family rather than for adopting it.

**RDR-026** ("Hybrid Search — Exact-Match Score Boosting", closed 2026-03-08)
is the closest prior art on the client side and predates the engine's route.

**RDR-216** ("Retrieval Granularity: Measure It Before Changing It") was
abandoned by Sam on 2026-09-19. Its measurements survive in T2 as
`216-research-1` through `-19`. **They are not a usable baseline for this
work**, for a reason given below under Critical Assumptions.

## Context

### Background

The client has never had a lexical retrieval leg that worked. Until
2026-09-19 it had `nx search --hybrid`, which merged hits from a locally
maintained ripgrep line cache. That path was opt-in and off by default, it
went stale by design between full indexes, it silently truncated any repository
whose cache passed a 500 MB ceiling, and until commit 2c2b1b045 it passed the
user's query to ripgrep as though it were a command-line option, so a query
beginning with `-` was rejected as an unknown flag and the leg returned
nothing. It was deleted at nexus-06aei.

Deleting it removed a broken substitute and left the underlying need
unaddressed. The engine's route is the non-broken version of the same idea,
built on indexes the client already pays to maintain.

### Technical Environment

- `chunk_tsv` uses the `english` text search configuration, chosen
  deliberately (vectors-001-baseline.xml, "152-FTS-tokenizer-DECISION Option
  B, prose lane only"). Changing it on a live corpus needs an `ALTER TABLE`
  and a full tsvector rebuild.
- English stemming handles prose and handles identifiers badly. The pg_trgm
  leg is the schema's intended answer for identifiers and substrings, with
  `pg_trgm.word_similarity_threshold` set to 0.6 by the caller
  (`PgVectorRepository.java:1348`), because the SQL functions are
  `LANGUAGE sql` / `STABLE` and cannot set the GUC themselves.
- The route is `POST /v1/vectors/hybrid-search`, registered at
  `VectorHandler.java:182`.

## Research Findings

### Investigation

Measured in this repository on 2026-09-19, at develop `7f7cb25a0`:

| Question | Method | Result |
|---|---|---|
| Does the client-reachable search path use FTS? | grep `text_gate\|chunk_tsv\|plainto_tsquery\|word_similarity` inside `searchWithTokens` | **0 matches** |
| Does the hybrid path? | same grep inside `hybridSearchWithTokens` | **4 matches** |
| Does any client code call `/hybrid-search`? | grep across `*.py *.sh *.md *.json`, excluding `service/` | **no callers** |
| Is there a client method for it? | `grep 'def .*hybrid' src/nexus/db/http_vector_client.py` | **none** |

### Key Discoveries

**Verified.** The four rows above, plus the contract analysis below
(codebase-deep-analyzer dispatch, 2026-09-19; full record T2
`nexus_rdr/217-hybrid-search-contract-analysis` [26496], findings
`nexus_rdr/217-research-1` through `-7`).

- **✅ Verified** (source search) — **The hybrid route is NOT a superset of
  `/search`.** A row with no text signal never appears, however close its
  vector; zero text candidates returns an empty list with no silent vector
  fallback. So this is a *different* retrieval, not an upgraded one, and
  making it the default would lose results `nx search` returns today.
  *Source: `PgVectorRepository.java:1140`.*
- **✅ Verified** (source search) — Rerank parity. Both routes share one tail,
  `VectorHandler#sendSearchResult`, so `rerank` / `rerank_top_k` dispatch
  identically. A client switching routes neither gains nor loses reranking.
  *Source: `VectorHandler.java:540`, invoked at `:516` and `:577`; pinned by
  `RerankStageIntegrationTest.java:270`
  `hybridSearchCarriesTheSameRerankEnvelope`.*
- **✅ Verified** (source search) — Request and response contracts are
  identical. Same field set on both handlers; at DDL level `plain_search_<dim>`
  and both branches behind `/hybrid-search` declare a byte-identical
  `RETURNS TABLE(id, content, collection, distance, metadata, retention)`. The
  live route selects **no** fusion component — no ts_rank, no trigram score,
  no RRF score, only cosine distance.
  *Source: `VectorHandler.java:503` and `:564`;
  `vectors-017-collection-scoped-tombstone-filter.xml`.*
- **✅ Verified** (source search) — The route is production-hardened, not
  harness-shaped. Dedicated tenant tests (cross-tenant bearer rejection,
  no-pg-backend), the same `tenantScope.withTenant` and `requireTenant` as
  `searchWithTokens`, and the skipped-collections header, all shared with
  `/search`. No conexus-only assumption found. This removes the
  scope-tripling risk this RDR was drafted under.
  *Source: `VectorHybridHttpTest.java`.*
- **✅ Verified** (source search) — `SearchResult.hybrid_score` and
  `topic_boost` in the Python client are a **naming collision only**:
  client-side post-retrieval scoring over the distance `/search` returns,
  computed in `nexus/scoring.py`. Nothing to do with engine-side fusion.
- **✅ Verified** (source search) — The selective-versus-HNSW-first dispatch
  branch is server-side and invisible to the client; no response field says
  which branch served a call. That dispatch is what BUG-0148 flipped under
  stale planner statistics. Exposing it would be a new engine field and is
  **out of scope** for a thin client method.

**Documented.** The BUG-0148 outage and the "zero nexus-side callers, conexus
is its only consumer" observation, both cited in Gap 2. The schema, index and
RDR-156 P5 trade-off details, from census [26452].

**Assumed, and explicitly not measured.** That adding a lexical leg improves
retrieval for real nexus queries. Nobody has measured this. The engine's own
P5.G numbers measure fusion implementations against each other, not fusion
against vector-only.

### Critical Assumptions

**RDR-216's recall numbers cannot serve as the before-measurement, and using
them would understate this work's value, plausibly to zero.** Its query set was
heading-derived prose over `docs/rdr`. A lexical leg's benefit is not spread
evenly across query shapes: it concentrates where exact tokens matter, on
identifiers, rare strings and substrings. That is precisely the population
`english` stemming handles worst and the trigram leg exists to catch. Measuring
a trigram-and-FTS gate with prose section headings applies an instrument
validated on one population to a different one, and the likely outcome is a
null result that gets read as "the lexical leg does not help" when the
instrument could not have seen the benefit. (Finding contributed by nexus-93,
who built and then abandoned that instrument.)

**What RDR-216's measurements DO establish**, supplied verbatim by nexus-93,
who took them:

> RDR-216 (abandoned, never landed) measured the current vector-only state and its
> figures remain usable for exactly what they cover. They establish: that
> file-indexed markdown retrieval already reaches 84 to 92 percent deep-band
> recall at 5 on `docs/rdr` under prose queries; that 309 of 310 single-chunk
> knowledge documents are note-written, at a median of 3,921 characters; and that
> one coarse vector loses retrievals on sets of near-identical documents, the
> owning part ranking first in 2 of 4 probes with one total miss on near-verbatim
> text. They establish nothing about lexical retrieval, identifier-shaped or
> rare-token queries, or the current index generation, and they must not be
> reused as a baseline for this RDR for the reasons in Critical Assumptions.
> Records: `nexus_rdr/216-research-1` through `-19`.

Two caveats inside that paragraph are load-bearing and are kept rather than
tidied. The 2-of-4 probe was adversarial by construction — four parts of one
article, chosen because near-identical documents are the case a single vector
handles worst — so it is a worst case and not a typical one. And the 84-to-92
figure came from an index a chunker generation behind.

Secondarily, those numbers came from the live `rdr` index, which their own
RF-5 records as a chunker generation behind — 8,746 chunks against the 10,170
the current chunker produces.

## Proposed Solution

### Approach

**Phase 1 — a baseline this work can actually be judged against.** Build a
query set that contains identifier-shaped and rare-token queries alongside
prose queries, and measure vector-only recall on a **freshly built** index,
within one session. Fresh build is not fastidiousness: a bulk-built HNSW index
retrieves measurably better than an incrementally grown one, and an autovacuum
reclaiming dead tuples moved one query's overlap from 0.818 to 0.333 with no
ranking code changing (nexus-4lnn1).

A second timing constraint arrived after this record was drafted, and it bears
on the note corpus only. Note chunk geometry changed on write: nexus-b2tld,
which landed on develop at `1be4146da`, splits a note stored through
`store_put` at 1,689 characters whenever its model imposes no window at all.
That is every Voyage collection: `window_for_model` returns no window when the
model's token limit exceeds the 12,288-byte chunk cap, and voyage-context-3
reads 32,000, so those notes were never split before. The change is
**write-only**: the 257 existing notes over 2,000 characters are not
re-embedded, which is Sam's decision and a bulk write over his live store.

The consequence for this phase is a measurement-window constraint, not a
blocker, and the window boundary is now a known commit. Notes written before
`1be4146da` are each exactly one piece; notes written after it are one or
more. A baseline drawn from notes therefore straddles a geometry change unless
it is taken on one side of that commit. So if this phase's query set draws on
notes at all, take the before-measurement and the after-measurement **within
one window**, or scope the measurement to the file-indexed corpora, which this
change does not touch.

**Phase 2 — the client method.** Add a `hybrid_search` method to
`HttpVectorClient` calling `POST /v1/vectors/hybrid-search`, with the same
result shape as the existing search methods.

**Phase 3 — the surface decision. Two thirds of it is now settled; the
remaining third waits on Phase 1.**

Research narrowed the decision before it was taken: **the hybrid route cannot
be a silent default**, because it is not a superset of `/search`. A row with
no text signal never appears on it, so switching `nx search` over wholesale
would lose results the vector path returns today. The surface must be additive
(a union of both legs) or an explicit mode.

**Settled (Sam, 2026-09-19), in his words: "2. your rec is fine. 3. yes.
this goes in the mcp" — answering a two-question ask that proposed `--lexical`
as the name and asked whether the MCP `search` tool should carry the leg.**

1. **The name is `--lexical`.** `--hybrid` stays as it is and keeps its
   current meaning, the git-frecency blend for code corpora (`search_cmd.py`
   help text: "Blend git frecency into the score for code corpora
   (0.7\*vector + 0.3\*frecency)"). That flag is a separate, surviving
   feature and reusing its name for a second meaning would be worse than
   either alternative. Renaming `--hybrid` to `--frecency` is the cleaner end
   state, and it stays out of this work because the flag HAS documented
   users: RDR-008 names `codebase-deep-analyzer` as one
   (`docs/rdr/rdr-008-nx-workflow-integration.md:157`),
   `conexus/skills/architecture/SKILL.md` recommends it in three places, and
   five docs files carry live usage examples. A rename therefore costs a real
   deprecation cycle across agent-facing guidance. An earlier draft of this
   line asserted the opposite, that the flag had no reported users; that was
   an unverified universal and it was false. The decision is unchanged and
   better supported by the correction than it was by the error.
2. **The MCP `search` tool gets the leg too.** It is not CLI-only. Its
   parameters today are `query`, `corpus`, `limit`, `offset`, `where`,
   `cluster_by`, `topic`, `structured` and `threshold`; this adds a tenth.
   The cost is one specific thing: the committed wire snapshot
   `tests/fixtures/mcp_wire_snapshot.json` pins each tool's parameters, so it
   must be regenerated in the SAME commit or develop reds on the pin rather
   than on the change, which reads as an unrelated failure. It is NOT the
   larger cost of registering a new tool: `HOOK_TOOLS`, the hook allowlists
   and the tool-count assertions are keyed on tool names, not on an existing
   tool's parameter list, so a parameter addition does not touch them.

**Open, and waiting on Phase 1.** Whether the leg is additive (union both
legs) or an explicit off-by-default mode. That turns on whether the lexical
leg finds things the vector leg misses on identifier-shaped queries, which is
exactly what Phase 1 measures. Deciding it before Phase 1 reports would be
guessing, and the Decision Rationale is built so that a flat Phase 1 result
answers it with "explicit, off by default" rather than invalidating the work.

**Phase 4 — the recurrence detector, which ships with Phase 2 and not after
it.** BUG-0148's failure mode is live and planner-dependent; its remediation
was a manual `ANALYZE`, not a fix. Any nexus caller inherits that exposure.
**It requires two calls, not one.** Research corrected an earlier draft of
this phase that assumed a single response could carry the signal: a zero-row
hybrid response is a legitimate outcome (no text candidates), and the response
carries no field naming match counts or which leg contributed, so one response
cannot distinguish a true zero from a gate that matched nothing for the wrong
reason. The detector calls `/search` and `/hybrid-search` and diffs them
client-side.

**The two-call diff alone is not the failure condition, and an earlier draft
of this phase said it was.** "Zero from the fused leg where the vector leg
returns rows" is also the ordinary, expected result for any query whose tokens
have no literal or trigram-similar match in the corpus but which has
semantically near chunks. English stemming and a 0.6 trigram threshold are
narrower targets than vector similarity, so that case is common rather than
exceptional. Telling it apart from a mis-planned zero needs one more thing the
response cannot supply: ground truth about whether the corpus contains a
lexical match for the query at all.

**So the detector is defined over a fixture that supplies that ground truth,
and only over such a fixture.** It runs against a corpus containing a chunk
whose text carries the query's literal tokens. On that fixture the fused leg
returning zero has exactly one explanation, since a lexical match provably
exists, and the assertion is sound.

**The comparator takes three inputs, not two, and an earlier version of this
phase left that unstated.** It reads the fused row count, the vector row
count, and the fixture's ground-truth flag: whether the corpus contains a
chunk carrying the query's literal tokens. It fails when, and only when, all
three hold:

```
ground_truth_lexical_match == True
  AND hybrid_rows == 0
  AND vector_rows  > 0
```

With the flag False the comparator never fails, whatever the diff shows,
because a fused zero is then the correct answer.

**The vector call is a control, not the signal.** Under the ground-truth flag
a fused zero is already a failure on its own, so the second call does not
supply the verdict. It establishes that the collection is reachable and the
query retrieves at all, so a fused zero is attributable to the gate or the
query plan rather than to an empty collection, a wrong collection name, or an
engine returning nothing for every call alike. This refines rather than
contradicts RF-3, whose two-call finding was reasoned without a ground-truth
fixture in view.

This is what makes the genuine-zero case and the planted-bug case
distinguishable at the fixture level rather than only in narrative. **The two
Test Plan fixtures differ in the flag alone: both carry `hybrid_rows == 0` and
`vector_rows > 0`.** A genuine-zero fixture built with `vector_rows == 0`
would retrieve nothing at all and would exercise no discrimination, which is
the vacuous-gate shape this RDR cites nexus-moht0 against, reproduced inside
the test written to prevent it.

**What this delivers, stated precisely, because the distinction matters for
what it is credited with.** Phase 4 proves the client-side comparison logic is
correct on a fixture where the answer is known. It is a CI assertion. It is
NOT production monitoring, and it would not by itself have caught BUG-0148,
whose defining property was that it was invisible in production while every
health signal stayed green. A general-purpose version running against
arbitrary live queries is explicitly out of scope here: without corpus ground
truth it would fire on every ordinary no-lexical-overlap query. Live
recurrence detection is named as future work under Failure Modes and is not
claimed by this RDR.

The doctrine this rests on is the project's own (nexus-moht0): a sweep that
found nothing to check is a failure, not a pass. A detector that has never
been observed failing is such a sweep, which is why the planted case is part
of the Minimum Viable Validation rather than a later addition.

### Technical Design

The route already exists and is production-hardened, so this design adds one
client method and one detector. No engine change.

**Wire contract.** `POST /v1/vectors/hybrid-search`, registered at
`VectorHandler.java:182`, handled by `VectorHandler#handleHybridSearch`
(`:564`). The request body field set is identical to `/search`'s: `query`,
`collections`, `n_results`, `where`, `include_source_uri`, `rerank`,
`rerank_top_k`. The response is the same flat row shape both routes return,
`(id, content, collection, distance, metadata, retention)`, declared
byte-identically by `plain_search_<dim>` and by both branches behind
`/hybrid-search` (`vectors-017-collection-scoped-tombstone-filter.xml`). The
live route selects no fusion component: no `ts_rank`, no trigram score, no RRF
score, only cosine `distance`. Verified by source search (RF-4).

**Data flow.** client to `handleHybridSearch`, then `requireTenant`, then
`PgVectorRepository#hybridSearchWithTokens`, then `tenantScope.withTenant`,
then `text_gate_probe_<dim>` to measure how selective the gate is, then either
`text_gated_search_by_chash_<dim>` or `text_gated_search_hnsw_first_<dim>`,
then flat rows, then `VectorHandler#sendSearchResult` (`:540`). That tail is
shared with `/search` and is where `rerank` and `rerank_top_k` are applied, so
a caller switching routes neither gains nor loses reranking. Verified by source
search and pinned by `RerankStageIntegrationTest.java:270` (RF-1).

**Client interface.** One new method on `HttpVectorClient`, mirroring
`search()`'s body construction at `http_vector_client.py:2948`:

```python
def hybrid_search(
    self,
    query: str,
    collection_names: list[str],
    n_results: int = 10,
    where: dict | None = None,
    *,
    include_source_uri: bool = False,
    rerank: bool = False,
    rerank_top_k: int | None = None,
    rerank_meta_out: dict | None = None,
) -> list[dict] | dict: ...
```

Every field in that signature is Verified (source search). None is Assumed.

`search()`'s three remaining keyword-only parameters, `cluster_by`,
`threshold` and `structured`, are deliberately absent. They are client-local
post-retrieval processing and never reach the wire, so carrying them here
would duplicate helpers rather than extend the route. A caller who wants them
applies the same helpers to this method's rows, and adding them later is
mechanical and needs no engine change. This is the one choice in Phase 2 that
research did not settle, and it is recorded here rather than left implicit.

**Error contract.** Three outcomes, all enforced server-side today and pinned
by `VectorHybridHttpTest.java`:

| Condition | Result | Pin |
| --- | --- | --- |
| bearer bound to another tenant | 422, fails loud | `VectorHybridHttpTest.java:170` |
| no pgvector backend configured | 503, never a silent vector fallback | `VectorHybridHttpTest.java:189` |
| gate matched no text candidates | empty list, a legitimate result and not an error | RF-3 |

That third row is the reason the Phase 4 detector cannot read one response.

**The detector's own error behaviour**, which the table above does not cover
because the table describes one call and the detector makes two. If either
call errors, times out or returns a non-200, the detector propagates and
fails loud. It does not treat an unanswered call as a pass, and it does not
substitute a zero-row reading for a call that never returned. The reasoning is
the project's fail-loud doctrine (nexus-moht0) and the specific circumstance:
a degraded engine is exactly when a second round trip is likeliest to fail, so
a detector that swallowed that failure would go quiet precisely when it is
needed. Retrying is deliberately not specified: a retry that masks a flapping
engine reintroduces the silence this detector exists to break.

**An extension point deliberately not taken.** Which branch served a call,
selective or HNSW-first, is server-side and invisible: no response field names
it. Exposing it would be a new engine field, out of scope for a thin client
method, and is recorded here as a separate ask should diagnostics ever want
it. Verified by source search (RF-7).

**Docstring reuse.** `http_vector_client.py:2914` already documents this
route's null-content exclusion inside the existing `search()` docstring,
written before any method for it existed. It checks out against the DDL and is
reusable for the new method (RF-6).

### Decision Rationale

Two independent arguments, and only one of them depends on retrieval getting
better.

The coverage argument stands alone: an endpoint with a production consumer is
unreachable from the test suite of the repository that builds it, the RDR-180
post-mortem named that as a process lesson, and nobody acted on it. A nexus
caller closes it.

The retrieval argument is a hypothesis with a stated test, not a claim. If
Phase 1's baseline shows no headroom on identifier-shaped queries, Phase 2
still buys the coverage and the surface decision in Phase 3 should be "off by
default".

## Alternatives Considered

### Alternative 1: Do nothing; conexus is served and `nx search` is adequate

Leaves Gap 2 open, which is the argument that does not depend on quality. Also
leaves every chunk paying for two indexes the client cannot read.

### Alternative 2: Switch the engine to the SQL RRF function family first

The one alternative that was seriously evaluated rather than briefly
rejected, because RDR-156 P5 built the family for exactly this purpose and its
gate returned GO.

**Description**: point the caller at `nexus.hybrid_search_384/_768/_1024`,
which perform the whole fusion in one round trip using Reciprocal Rank Fusion,
in place of the Java selectivity dispatch behind `/hybrid-search`.

**Pros**:

- One round trip instead of a probe plus a query.
- Ties or beats the Java path in the selective-gate regime the redesign
  targeted: 6/6 recall, 638ms against 661ms (RDR-156 P5.G).
- Would retire the maintenance cost recorded in Gap 3: nine
  `CREATE OR REPLACE` statements and nine generated jOOQ classes for a
  function family nothing calls.

**Cons**:

- No escape valve for dense gates. The cost ratio grows with gate size: 0.97x
  at 6 rows, 2.57x at 1,830, 6.50x at 25,000 (683ms against 105ms).
- It returns a different shape, `(id, content, collection, score)`, where the
  live route returns `(id, content, collection, distance, metadata,
  retention)`. Switching would change the client's response contract, not just
  its query plan.
- The trade-off was already accepted deliberately. Bead nexus-76352 was closed
  WONTFIX on 2026-08-28.

**Reason for rejection**: it buys no client-visible benefit and costs a
dense-gate regression risk. The client cannot reach either implementation
today, so which one it eventually reaches is a separate question from whether
it can reach one at all. If a future record revisits it, Gap 3's maintenance
cost argues for deleting the dead family rather than adopting it.

### Alternative 3: Delete the lexical indexes and stop paying for them

Not available: conexus depends on them through `/hybrid-search`.

### Alternative 4: Build a new client-side lexical path

This is what ripgrep was. It was deleted this week for good reasons.

## Trade-offs

### Consequences

- The Gap 2 coverage hole closes. An endpoint with a production consumer
  becomes reachable from the test suite of the repository that builds it, for
  the first time since it shipped. This consequence does not depend on
  retrieval improving.
- The two lexical indexes every chunk already pays to maintain become readable
  from the client.
- The client gains a second retrieval route whose semantics differ from the
  first. `/hybrid-search` is not a superset of `/search`, so "which route
  served this" becomes a question a reader of client code has to hold.
- The Phase 4 detector costs two engine calls where one would do. It runs on a
  diagnostic path and not on the user's search path, so the cost is bounded to
  where it buys something.
- The client takes a dependency on a route whose query-plan choice is made
  server-side and is invisible in the response. That is the exposure BUG-0148
  realised, and Phase 4 exists because of it.

### Risks and Mitigations

- **Risk**: BUG-0148 recurs. The planner flips sparse text-gate queries onto
  the budget-bounded HNSW plan under stale statistics, the route returns zero
  rows, and every health signal stays green.
  **Mitigation**: PARTIAL, and the part it does not cover is named here rather
  than left to be discovered. Phase 4's detector proves the client-side
  comparison logic is correct against a fixture with known ground truth, and
  its own non-vacuity is proven by planting the condition (see Test Plan).
  That is a CI assertion. Nothing in this RDR watches live traffic on any
  cadence, so a production recurrence would still be as quiet as BUG-0148 was.
  Live detection needs corpus ground truth the response does not carry and is
  future work, recorded under Failure Modes. **This risk is reduced, not
  closed, and this RDR should not be accepted on the belief that it is
  closed.**
- **Risk**: the surface decision defaults wrong and users silently lose
  results, because a row with no text signal never appears on the hybrid
  route.
  **Mitigation**: Phase 3 is explicit and is Sam's. Research has already
  narrowed it to additive or explicit-mode; a silent default is ruled out in
  the Approach, not left to implementation.
- **Risk**: Phase 1's baseline is measured on ground that moves underneath it,
  through note chunk geometry (nexus-b2tld), an autovacuum reclaiming dead
  tuples, or an index a chunker generation behind.
  **Mitigation**: one window, freshly built index, or scope the measurement to
  the file-indexed corpora. Stated in Phase 1.
- **Risk**: RDR-216's instrument gets reused for convenience, returns a null
  result, and the null is read as "the lexical leg does not help".
  **Mitigation**: Critical Assumptions says so in terms, and Phase 1 builds a
  new query set containing identifier-shaped and rare-token queries.
- **Risk**: the work is judged on retrieval numbers alone and abandoned if
  they are flat.
  **Mitigation**: Decision Rationale separates the two arguments. If Phase 1
  shows no headroom, Phase 2 still buys the coverage and Phase 3 answers "off
  by default".

### Failure Modes

**Breaks visibly.** A bearer bound to another tenant gets 422. A missing
pgvector backend gets 503 rather than a silent fall back to vector-only. Both
are pinned by existing tests.

**Fails silently, and this is the one that matters.** The gate matches nothing
and the route returns an empty list. That is a legitimate outcome when there
genuinely are no text candidates, and it is indistinguishable, from a single
response, from a gate that matched nothing because the planner chose the wrong
plan. The response carries no match count and no field naming which leg
contributed. This is exactly the shape of a gate passing over a scan that
found nothing to check, which this project already treats as a failure rather
than a pass (nexus-moht0).

**Diagnosis path, and its limit.** Call `/search` and `/hybrid-search` with
the same query and collections and diff the row counts. A fused result of zero
where the vector leg alone returns rows is the signal **only when the corpus
is known to contain a lexical match for that query**. Without that ground
truth the same reading is the ordinary result for a query with no lexical
overlap, so this is a diagnostic an operator runs against a query they have
chosen for the purpose, not a check that can be pointed at arbitrary traffic.
There is no server-side shortcut for this today.

Two limits on the diff itself. A write landing between the two calls can
present a genuine visibility difference as the BUG-0148 signature, so a
positive reading is confirmed by repeating it rather than acted on from one
observation. And if either call errors, times out or returns non-200, the
diff is not computed at all: see the detector's error behaviour in the
Technical Design.

**Live recurrence detection is future work and is not in this RDR.** Making
this an unattended production check needs a ground-truth corpus (a small set
of canary documents whose literal tokens are known) and a cadence to run it
on. Both are real design, neither is specified here, and no bead exists for
it yet. Naming it is how this RDR avoids being read as having closed the
BUG-0148 risk.

## Implementation Plan

### Prerequisites

- [x] Wire-contract assumptions (RF-1 to RF-7) verified by source search.
      These cover the route's request and response shape, rerank parity,
      tenant handling and the zero-row semantics. They are NOT the section
      titled Critical Assumptions, which concerns RDR-216 baseline reuse.
- [ ] The one assumption that gates Phase 3, that a lexical leg improves
      retrieval for real nexus queries, is deliberately unverified. See
      Finalization Gate > Assumption Verification. Phase 1 is the plan to
      verify it.
- [ ] Phase 1's baseline taken, since Phase 3's answer depends on it.

### Minimum Viable Validation

**A nexus-side test drives `POST /v1/vectors/hybrid-search` against the engine
substrate and the two-call detector fails when the zero-row condition is
planted.** One proof, covering both halves of the case: the first half closes
Gap 2 by making the route reachable from a nexus journey, and the second half
proves the detector is not vacuous.

The fixture carries the property the detector's soundness rests on: it
contains a chunk whose text holds the query's literal tokens, so a lexical
match provably exists and a zero from the fused leg has exactly one
explanation. Without that property the planted case and an ordinary
no-lexical-overlap query are the same observation and the proof is empty.

In scope for Phase 2 plus Phase 4, not deferred.

### Phase 1: an honest baseline

#### Step 1: build the query set

Identifier-shaped and rare-token queries alongside prose queries. Not
RDR-216's heading-derived prose set, for the reason in Critical Assumptions.

#### Step 2: measure vector-only recall

Freshly built index, one session, within one window. Record the query set with
the numbers so a later reader can tell which population was measured.

### Phase 2: the client method

#### Step 1: `HttpVectorClient.hybrid_search`

Per the Technical Design signature. Body construction mirrors `search()`.

#### Step 2: the wire test

Assert the body carries exactly the seven wire fields and no more.

Phase 2 stops there, and deliberately. The client method and its wire test
are semantics-independent: they are the same code whichever way the surface
question is answered. Everything user-visible waits for Phase 1, including
the parts of the surface Sam has already settled, because what is settled is
the NAME and the REACH, not the BEHAVIOUR. Building a flag before knowing
whether it unions two legs or selects one would be building the wrong thing
with the right name.

### Phase 3: the surface, both halves of it

Two surfaces, one decision, and neither is built before Phase 1 reports.

#### Step 1: the CLI flag `--lexical`

Name settled by Sam; behaviour depends on Phase 1's answer to additive union
versus explicit mode. `--hybrid` is untouched and keeps its frecency meaning.

#### Step 2: the MCP `search` parameter `lexical`

Settled by Sam as in scope, so the leg is not CLI-only. The tool gains a
tenth parameter, matching the flag in name, default and behaviour.

The one mechanical cost to carry into this step: the committed wire snapshot
`tests/fixtures/mcp_wire_snapshot.json` pins each tool's parameter list, so
it is regenerated in the SAME commit that adds the parameter. Otherwise
develop reds on the pin rather than on the change, which reads as an
unrelated failure. Registering a new TOOL would additionally touch
`HOOK_TOOLS`, the hook allowlists and the tool-count assertions; adding a
parameter to an existing tool does not, since those are keyed on tool names.

#### Step 3: the agent-facing guidance

`conexus/skills/architecture/SKILL.md` recommends `--hybrid` in three places
and RDR-008 names `codebase-deep-analyzer` as a user. Whatever Phase 1
decides, that guidance is reviewed against the new flag in the same change,
so the two flags' meanings do not drift apart in agent-facing prose.

### Phase 4: the recurrence detector

Ships with Phase 2. Two calls, diffed client-side, asserted.

### Day 2 Operations

This RDR creates no persistent resource: no collection, no index, no data
store, no config entry. The client method is stateless and the detector is a
test.

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| none created | N/A | N/A | N/A | N/A | N/A |

If Phase 3 chooses a surface that introduces a config key, that key is the
first persistent resource this work creates, and its Day 2 row belongs to
Phase 3's own change rather than here.

### New Dependencies

None. The route, its tenant handling and its rerank tail all exist; this adds
a caller.

## Test Plan

- **Scenario**: `hybrid_search()` builds its request body. **Verify**: exactly
  `query`, `collections`, `n_results`, and only the optional fields the caller
  set, matching `/search`'s body for the same arguments.
- **Scenario**: a nexus test calls the route against the engine substrate.
  **Verify**: rows come back in the flat shape, and the test is the first
  nexus-side caller, which is the Gap 2 closure itself.
- **Scenario**: rerank requested on the hybrid route. **Verify**: the same
  envelope `/search` produces, matching `hybridSearchCarriesTheSameRerankEnvelope`.
- **Scenario**: a bearer bound to another tenant. **Verify**: 422, not an
  empty result.
- **Scenario**: no pgvector backend. **Verify**: 503, not a silent vector
  fallback.
- **Scenario**: a query whose gate genuinely matches no text, against a corpus
  containing NO chunk with its literal tokens. **Verify**: an empty list, and
  the detector does NOT fire, since no lexical match exists to have been
  missed. The fixture's defining property is the absence of a lexical match,
  and the test asserts that absence directly rather than assuming it.
- **Scenario**: the BUG-0148 condition planted, against a corpus that DOES
  contain a chunk carrying the query's literal tokens, so the fused call
  returns zero where a lexical match provably exists. **Verify**: the detector
  FAILS. This is the non-vacuity proof: a detector that has never been
  observed failing is a sweep that found nothing to check.
- **Scenario**: the two scenarios above are compared as fixtures, not as
  narratives. **Verify**: they differ in whether the corpus contains a literal
  match, and that difference is asserted in the test setup. Two scenarios that
  assert opposite detector outcomes from an identical observable signature
  would prove nothing, and an earlier draft of this plan had exactly that
  defect.
- **Scenario**: one of the detector's two calls errors, times out or returns
  non-200. **Verify**: the detector propagates and fails loud. It never treats
  a failed call as a pass, and never silently substitutes a zero-row reading
  for an unanswered one.

## Validation

### Testing Strategy

1. **Scenario**: the MVV above, run in CI rather than by hand.
   **Expected**: green when the route is healthy, red when the planted
   zero-row condition is present.
2. **Scenario**: Phase 1's baseline re-run after Phase 2 lands, within the
   same measurement window.
   **Expected**: a number that can be compared. If the two measurements
   straddle a note chunk geometry change or an index rebuild, the comparison
   is void and is retaken rather than reported.

### Performance Expectations

No throughput target is set here. The one empirical comparison that bears on
the choice is RDR-156 P5.G's, which measured the SQL RRF function family
against the live Java dispatch and found the cost ratio growing with gate
size, 0.97x at 6 rows to 6.50x at 25,000. That is the evidence for
Alternative 2 being rejected, and it is not a target for this work.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles and the
proposed solution as it now stands. Six were found and corrected on the way
here, and they are listed rather than summarised because the count is the
honest measure of how much this document changed under review.

Two were corrected before the first gate:

1. Phase 4 assumed a single response could carry the zero-row signal, which
   RF-3 refuted.
2. The draft carried a scope-tripling worry about the route being
   harness-shaped, which RF-5 refuted.

One was corrected after gate round 1 (critique
`nexus_rdr/217-gate-critique-2026-09-19-r1`):

3. Phase 4 stated an unconditional firing condition, and the Risks table
   credited a CI test with mitigating a production recurrence. The detector
   is now defined only over a ground-truth fixture and the risk is marked
   PARTIAL.

Three were corrected by the fix check on that round's diff:

4. The corrected Phase 4 still left the comparator's inputs unstated, so an
   implementer could have built the genuine-zero fixture with no retrievable
   rows at all and proved nothing. The comparator's three inputs and both
   fixtures' shared shape are now explicit.
5. A claim that `--hybrid` had no reported users was an unverified universal
   and was false; RDR-008 and three recommendations in
   `conexus/skills/architecture/SKILL.md` contradict it.
6. The settled half of the surface decision had been pulled into Phase 2
   while its behaviour still depended on Phase 1, and only the MCP half at
   that. All surface work now sits in Phase 3, both halves together.

### Assumption Verification

All seven research findings are classified verified with verification method
source search. One assumption is stated and deliberately NOT verified: that
adding a lexical leg improves retrieval for real nexus queries. Nobody has
measured it. Phase 1 is the plan to verify it, and it runs before Phase 3
decides the surface. The Decision Rationale is constructed so that this
assumption failing does not invalidate the work.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/vectors/hybrid-search` | nexus-service | Source Search |
| `VectorHandler#handleHybridSearch` | nexus-service | Source Search |
| `PgVectorRepository#hybridSearchWithTokens` | nexus-service | Source Search |
| `VectorHandler#sendSearchResult` (rerank tail) | nexus-service | Source Search |
| `HttpVectorClient.search()` (body construction copied) | nexus client | Source Search |

### Scope Verification

The Minimum Viable Validation is in scope and executes during implementation,
not after. Specifically: a nexus-side test that drives the route against the
engine substrate, plus the planted-zero-row proof that the detector fails when
it should. Phase 4 ships with Phase 2 for this reason, rather than following
it.

### Cross-Cutting Concerns

- **Versioning**: N/A. No wire change; the route and its contract already
  exist and are unchanged.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A. No new dependency.
- **Deployment model**: addressed. The route behaves identically in local and
  cloud mode, and the client method carries no mode-specific branch. A local
  install reaches it through the same bundled engine that serves `/search`.
- **IDE compatibility**: N/A.
- **Incremental adoption**: addressed. Phase 2 adds a method nothing calls by
  default. Phase 3 decides the user-visible surface separately, so the client
  method can land and be exercised by tests without changing what any user
  sees.
- **Secret/credential lifecycle**: N/A. The route uses the same bearer and the
  same `requireTenant` path as `/search`; no new credential.
- **Memory management**: N/A. Row counts are bounded by `n_results`, capped at
  300 by `MAX_QUERY_RESULTS` like every other read.

### Proportionality

Right-sized, with one caveat worth stating rather than trimming silently. The
Research Findings and Critical Assumptions sections are long relative to the
change, because the change is small and the reasons it is small are the part
that took work to establish: five of the seven findings exist to show that
Phase 2 is thin rather than to design it. That is the document doing its job.
The section a future reader can safely skim is Gap 3, which records a dead
function family this RDR explicitly does not touch.

## References

- `service/src/main/java/.../VectorHandler.java` (`:182`, `:503`, `:516`,
  `:540`, `:564`, `:577`)
- `service/src/main/java/.../PgVectorRepository.java` (`:1140`, `:1149`,
  `:1348`)
- `service/src/test/java/.../VectorHybridHttpTest.java` (`:170`, `:189`)
- `service/src/test/java/.../RerankStageIntegrationTest.java` (`:270`)
- `src/nexus/db/http_vector_client.py` (`:2884`, `:2914`, `:2948`)
- `vectors-017-collection-scoped-tombstone-filter.xml`,
  `vectors-001-baseline.xml`, `vectors-002-trgm-index.xml`
- `docs/rdr/post-mortem/180-content-address-chash-binary-32byte.md:104`
- T2 `nexus/census-engine-fts-hybrid-search-2026-09-19` [26452]
- T2 `nexus_rdr/217-hybrid-search-contract-analysis` [26496]
- T2 `nexus_rdr/217-research-1` through `-7`
- Beads: nexus-06aei, nexus-b2tld, nexus-0hqez

## Revision History

