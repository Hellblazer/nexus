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

**Verified.** The four rows above.

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

**Phase 2 — the client method.** Add a `hybrid_search` method to
`HttpVectorClient` calling `POST /v1/vectors/hybrid-search`, with the same
result shape as the existing search methods.

**Phase 3 — the surface decision, and it is Sam's.** Whether the lexical leg is
a new flag, a default, or a per-corpus setting. This RDR does not pre-empt it.
Note that `--hybrid` survives as the git-frecency switch and reusing the name
for a second meaning would be worse than either alternative.

**Phase 4 — the recurrence detector, which ships with Phase 2 and not after
it.** BUG-0148's failure mode is live and planner-dependent; its remediation
was a manual `ANALYZE`, not a fix. Any nexus caller inherits that exposure.
The detector: a fused query that returns zero rows where the vector leg alone
returns some is a failure, asserted on the fused path rather than reported.
Zero rows in production while every health signal stayed green is the same
shape as a gate passing over a scan that matched nothing, and this project's
own doctrine (nexus-moht0) is that a sweep which found nothing to check is a
failure, not a pass.

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

Reopens RDR-156's accepted trade-off and adds a dense-gate regression risk
(6.50x at 25,000 rows) for no client-visible benefit. The client cannot reach
either implementation today, so which one it reaches is a separate question.

### Alternative 3: Delete the lexical indexes and stop paying for them

Not available: conexus depends on them through `/hybrid-search`.

### Alternative 4: Build a new client-side lexical path

This is what ripgrep was. It was deleted this week for good reasons.
