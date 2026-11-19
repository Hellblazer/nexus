---
title: "Corpus Discovery So the Planner Can Compose Plans That Fit a Dataset"
id: RDR-255
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-19
accepted_date:
related_issues: []
---

# RDR-255: Corpus Discovery So the Planner Can Compose Plans That Fit a Dataset

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms used throughout:

- **`nx_answer`** is the nexus tool that answers a question from indexed
  material. It picks a **plan** (a fixed sequence of steps: searches, fetches,
  and model-backed operators such as `rank` or `generate`), runs it, checks the
  answer against its evidence, and returns a cited answer.
- The **plan library** is the stored set of plans. Plans get there two ways.
  **Curated** plans are written by hand and shipped as YAML files in
  `conexus/plans/builtin/`. **Grown** plans are saved automatically when the
  inline planner (below) produces a plan that answers a question (RDR-084).
- **Plan matching** compares the question text to the library by embedding
  similarity. A match at or above a confidence floor is reused as is.
- The **inline planner** is a model call that writes a new plan when no library
  plan matches. Its input is the question plus a fixed prompt.
- The **coverage boundary** (RDR-232) is the check that runs after retrieval.
  It asks whether the retrieved evidence covers each part of the question. When
  it does not, `nx_answer` declines with `[non-answer: uncovered]` rather than
  guessing. A decline scores zero on a benchmark.
- **HERB** is a public benchmark (arXiv 2506.23139) of about 39,000 synthetic
  enterprise artifacts (Slack threads, documents, meeting notes, pull requests)
  across 30 software products, with questions whose answers are sets of
  customer companies. A customer appears in artifacts as an **entity key**, a
  stable identifier such as `CUST-0115`; a separate record maps each key to its
  company name.
- **Company F1** is HERB's score for one question: the harmonic mean of
  precision (share of named companies that are correct) and recall (share of
  correct companies named), times 100. **Cust** is its mean over the question
  set.
- **reach@k** is the share of a question's correct companies that have at least
  one key in the top k retrieved chunks. It measures whether the answer could
  have been found, before any reading or judging.

## Problem Statement

On 2026-11-19 `nx_answer` answered HERB's 130 company questions over one
pooled collection of every product's artifacts, with local embeddings and one
attempt per question. It scored Cust 28.06. HERB's own reference agent (ReAct
over GPT-4o, one attempt, full retrieval) scores 41.35
(T2 `nexus_rdr/herb-full-pool-company-result-2026-11-19`).

The loss is concentrated in declines. 55 of 130 answers were coverage
non-answers. The 75 answered questions averaged F1 48.6, above the ReAct
average. The declines were not an over-strict coverage check. A follow-up
re-check at zero spend ran plain retrieval at depth 80, the depth `nx_answer`
uses, for every question (T2 `nexus_rdr/herb-full-pool-decline-recheck-2026-11-19`):

| Group | reach@10 | reach@40 | reach@80 | reach@80 = 0 |
| --- | --- | --- | --- | --- |
| declined (55) | 0.00 | 0.105 | 0.208 | 37 |
| answered (75) | 0.46 | 0.77 | 0.856 | 4 |

In 37 of the 55 declines no customer-naming message reached the top 80 at all,
so declining was the correct reaction to what retrieval returned. Retrieval
found the right product and the right threads (94% of the declines' top-10
chunks came from the question's own product) but ranked the key-bearing
messages too low.

A shape count sharpens this (T2 `nexus_rdr/herb-full-pool-decline-shapes-2026-11-19`,
counted from the run's answer and score records). The 130 questions fall into
three shapes:

| Shape | Example form | Questions | Declined |
| --- | --- | --- | --- |
| affected | "companies affected by issues in <component>" (mean F1 58.7) | 50 | 0 |
| max-bugs | "company with the most resolved / unresolved / reopened bugs in <product>" | 60 | 46 |
| prev-release | questions about a previous release | 20 | 9 |

Every decline is an aggregate or release question. The "affected" shape, which
is closest to a single relevance search, never declined.

The planner chose the same plan for nearly all of them. Plan matching reused
library plan 10 (`hybrid-factual-lookup`, a curated top-k relevance plan) on 51
of the 55 declines. The inline planner never ran. These questions are
**set-enumeration** and **aggregation** questions: collect every record that
matches a facet (a component, an impact, a bug status) inside a named scope (a
product), join each record to its entity key, then list or count by entity.
Top-k relevance retrieval returns the k chunks most similar to the question
text. It is the wrong tool for "every record that matches", and it has no
counting step for "the most".

#### Gap 1: The planner cannot see the corpus

The inline planner's input is the question text, a list of collection names,
fixed scoping rules and a fixed tool reference (`src/nexus/mcp/core.py:13178`
to `13254`). It is not told which metadata fields the collection carries (for
HERB, the product and the artifact type), which record types carry entity keys,
what the facet vocabularies are (component names, bug states), or what question
shapes the corpus supports. Without these it cannot write a step that filters
by product or by artifact type, even though the `search` tool it is given
accepts a `where` metadata filter (`src/nexus/mcp/core.py:3995`).

#### Gap 2: A weak library match is never re-examined

> **Under review (research finding 11).** A read-only probe of `plan_match` on HERB company questions, on both develop and the stock run's code, scores `entity-resolution` (plan 8) 0.50-0.54 and `hybrid-factual-lookup` (plan 10) 0.14-0.17, below the floor. The stock run still executed plan 10 on 51 of 55 declines, so a step between matching and execution chose it. The gap stands as "the routed plan was the wrong shape", but the mechanism below is not yet confirmed.

Plan matching accepts any match at cosine confidence 0.40 or above
(`_PLAN_MATCH_MIN_CONFIDENCE`, `src/nexus/mcp/core.py:10947`; hit test at
`11154`). Above the floor the matched plan runs as is, whatever the question's
shape. Plan 10's description is about single-fact lookup, yet HERB company
questions clear the floor against it. Nothing measures whether the matched plan
suits the question's shape, so the inline planner is not reached for the
questions that most need it.

#### Gap 3: The library has no filter-and-scan or count plan, and the planner is not told the operators that could build one

All 13 curated plans begin with ranked retrieval. `entity-resolution.yml`, the
plan written for HERB-style listing questions (RDR-147), is a copy of plan 10's
steps (`conexus/plans/builtin/entity-resolution.yml:1-20`). The plan runner
already maps `filter`, `groupby` and `aggregate` operators
(`src/nexus/plans/runner.py:2144-2155`; RDR-093) and the scoped retrieval tools
`search_metadata_scoped` and `search_aspect_scoped`
(`src/nexus/plans/runner.py:627-633`; RDR-156). The inline planner's tool
reference names none of these five (`src/nexus/mcp/core.py:11013-11150`), so a
generated plan cannot use them.

#### Gap 4: Outcomes do not feed back by question shape

A non-answer is recorded as a failure against the matched plan
(`src/nexus/mcp/core.py:16228`, `_nx_answer_record_complete(..., success=False)`; whether it increments the plan's failure counter is not yet read). The matcher skips a plan only when it has at
least 3 failures and no successes (`_ALWAYS_FAILING_MIN_FAILURES`,
`src/nexus/plans/matcher.py:142`; test at `173`). Plan 10 succeeds on most of
its traffic, so a run of failures on one question shape never changes routing,
and nothing proposes a different plan for that shape.

#### Gap 5: The corpus has no filterable scope, and no plan step can scan

Two facts found in research (findings 12 and 13). First, a note stored through
`store_put` carries only `chunk_text_hash` and `embedding_model` as chunk
metadata (`src/nexus/metadata_schema.py:270-291`). HERB's product lives only in
the catalog `tags` array (`src/nexus/store_hook.py:836-866`), which neither
`search(where=...)` nor catalog meta equality can match
(`src/nexus/catalog/meta_filter.py:48-61` compares the list as a string). So a
scope a profile could name is not something a filter can use until it is
written as real metadata at ingest. Second, no plan step enumerates records
without ranking. Operators take at most 100 items (`_OPERATOR_MAX_INPUTS`,
`src/nexus/plans/runner.py:2159`), retrieval returns at most 300, and the one
unranked, paged enumerator (catalog `GET /v1/catalog/docs/by-meta`, exact count,
pages of 1000) is not dispatchable by the plan runner.

## Relationship to Prior RDRs

Searched the RDR index for plan library, plan matching, inline planning,
aspects, taxonomy, entity keys, coverage boundary and HERB.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-084 Plan Library Growth (closed) | Origin | Created grown plans: a successful inline plan is saved. Its rationale, that the library compounds with use, holds, but it only grows from inline runs, and Gap 2 means a well-matched-but-wrong plan suppresses inline runs. |
| RDR-089 Structured Aspect Extraction (closed) | Origin | Per-document structured fields via an LLM at ingest. The shipped schemas are scholarly-paper, general-prose and RDR frontmatter (`src/nexus/aspect_extractor.py:300`, `398`, `413`); none extract domain facets such as component or bug status. |
| RDR-090 Realistic AgenticScholar Benchmark (closed) | Precedent | Added `force_dynamic` (skip plan matching). Its own finding: inline-planning value "cannot be measured until ... the plan library carries genuinely multi-step compositional plans". This RDR's experiment is that measurement on a second dataset. |
| RDR-093 GroupBy and Aggregate Operators (closed) | Precedent | Shipped the counting operators and an SQL fast path over aspects (`src/nexus/operators/aspect_sql.py`). It deferred "planner-side auto-resolution" to a separate RDR. This is that RDR, in part. |
| RDR-097 Hybrid Retrieval Plan Template (closed) | Origin | Created plan 10 for single-fact questions. Its scope still holds; the problem is plan 10 serving questions outside it. |
| RDR-147 Multi-hop Entity Resolution (closed) | Precedent | Introduced entity keys (`entity.collections`) and the `entity-resolution` curated plan. It fixed routing for HERB's "affected" shape; the aggregate shapes are what remain. |
| RDR-156 Vector-Store Capability Leverage (closed) | Origin | Shipped `search_metadata_scoped` and `search_aspect_scoped`, which a filter-and-scan plan needs. |
| RDR-232 Answer-Shape Eligibility and Evidence Sufficiency (closed) | Adjacent, closed | Owns the coverage boundary and the one repair. Its decline behaviour is correct here (the decline re-check shows it). This RDR changes what reaches it, not how it judges. |
| RDR-250 Multi-hop Answers Past the Six-Chunk Budget (closed) | Adjacent, closed | The pool key scan reads keys from the unread tail of one search step. It cannot help when the keys are outside the top 80 altogether, which is 37 of the 55 declines. |
| RDR-251 Taxonomy Compute on the Engine (draft) | Adjacent draft | Moves `nx taxonomy discover` onto the engine. A corpus profile (option A below) would read its topics; it does not depend on 251 landing, but would be cheaper after it. |

## Context

### Background

Sam's framing, in session on 2026-11-19: nexus has both curated and grown
plans. The AgenticScholar plans that nexus's operator set was modelled on were
curated, not grown. The question is whether nexus needs a learning, tuning or
discovery phase around a new dataset, meaning utility routines that gather,
examine and describe the corpus before questions arrive, and whether the
framework is sound regardless.

HERB is the first dataset where the gap shows clearly. It is not the only case
that matters: any collection with structured metadata and entity keys (tickets,
CRM exports, chat archives) has questions of the same shape.

### Technical Environment

`nx_answer` lives in `src/nexus/mcp/core.py` (entry at line 13620). The plan
library is a T2 table, seeded from `conexus/plans/builtin/*.yml` (13 files).
The matcher is `src/nexus/plans/matcher.py`. The runner is
`src/nexus/plans/runner.py`. All references are to develop at 71289a35b.

## Research Findings

### Investigation

**Plan matching.** `plan_match` (`src/nexus/plans/matcher.py:666`) embeds the
question, queries the library, filters by dimensions (verb, scope) and scores by
cosine plus a scope-fit bonus (`_SCOPE_FIT_WEIGHT` 0.15, line 67). Grown plans
need 0.55 (`_GROWN_PLAN_MIN_CONFIDENCE`, line 82); curated plans need the
caller's floor, 0.40 by default. `nx_answer` calls it at
`src/nexus/mcp/core.py:14763` and treats the top match as a hit at
`14787`. `force_dynamic=True` skips matching entirely (`13679`, `14704`).

**Inline planning.** `_nx_answer_plan_miss` (`src/nexus/mcp/core.py:13178`)
builds the prompt from: up to 24 collection names (`13199-13216`), up to 3
near-miss library plans as few-shot examples (`13219-13240`), the request's
coverage facets (`_nx_answer_planner_request_block`, `13098`), the scoping
rules (`10981`) and the tool reference (`11013`). It tells the planner nothing
about a collection's contents.

**What the planner may call.** The tool reference lists `search`, `query`,
`traverse`, `store_get_many`, `extract`, `rank`, `compare`, `summarize`,
`generate`, `check` and `verify`. The runner also accepts `filter`, `groupby`,
`aggregate` (`runner.py:2144-2155`) and the four scoped search tools
(`runner.py:627-633`). Grepping the tool reference block for those names
returns zero hits.

**Metadata filtering.** `search` takes `where` with comparison operators
(`core.py:3995`); `search_metadata_scoped` takes equality `where` plus catalog
filters (`core.py:4708`). Both are ranked: they return the top `limit` rows
inside the filter, and `search` caps `limit` at 300 (`core.py:3993`). There is
no unranked "every chunk matching this filter" primitive in the plan vocabulary.

**Aspects.** `search_aspect_scoped` (`core.py:5112`) filters by one of five
fixed fields: `problem_formulation`, `proposed_method`,
`experimental_datasets`, `experimental_baselines`, `experimental_results`. These
fit papers. A HERB Slack thread has no proposed method; its useful facets are
product, component, bug status and customer. Extraction is a Haiku call per
document (`aspect_extractor.py:308`).

**Entity keys.** `entity.collections` maps a collection glob to a key regex
(`docs/configuration.md:247-261`); `find_key_tokens`
(`src/nexus/entity_keys.py:181`) finds keys in text. The coverage boundary's
resolution branch and the RDR-250 pool scan use it. The config states the key
shape; it does not record which record types carry keys or how many.

**Taxonomy.** `nx taxonomy discover` clusters a collection's embeddings with
HDBSCAN and labels clusters with c-TF-IDF terms
(`src/nexus/commands/taxonomy_cmd.py:296`, `731`). Topics are usable as a
`topic=` filter on `search`. They describe what the text is about, not the
metadata fields.

**Coverage repair.** After a decline-bound first assessment the boundary runs
one repair: at most 6 queries built from the uncovered facets' own text
(`src/nexus/plans/coverage_repair.py:1-40`). These are again relevance
searches, so they inherit the same ranking miss.

**Pool scan.** RDR-250's scan reads entity keys from the search step's unread
results, default off (`NX_ANSWER_POOL_KEY_SCAN`,
`src/nexus/plans/switches.py:141`; `src/nexus/plans/coverage_pool.py:1-40`). It
widens reading within the retrieved set, not retrieval itself.

**Outcome recording and growth.** `_nx_answer_record_outcome`
(`core.py:12664`) increments success or failure on the matched plan. A
non-answer is recorded with `success=False` (`core.py:16228`). A successful inline plan is saved as
a grown plan with a TTL (`core.py:16291-16300`). `plan_save`
(`core.py:8659`) requires a verb and an answer kind and strips the `grown`
provenance tag from callers, so only the grow path can mark a plan grown.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus plan runner | Yes | `filter`/`groupby`/`aggregate` are dispatchable today; not offered to the planner. |
| nexus aspect extractor | Yes | Schemas are fixed per collection prefix; no domain-facet schema exists. |
| HERB data | Partly (via the benchmark harness records) | Artifacts carry product and artifact type; customers are keys resolved through a separate mapping. |

### Key Discoveries

- **Verified** (measured, T2 records cited above): Cust 28.06 against 41.35;
  55 declines; answered mean F1 48.6; declined reach@80 0.208 against 0.856.
- **Verified** (stated in the experiment pre-registration, from the stock
  run's records): plan 10 matched 51 of 55 declines; no inline plan was
  composed.
- **Documented** (source reading): the planner prompt carries no corpus
  description; the planner's tool reference omits five dispatchable tools.
- **Documented**: aspect schemas are paper-shaped; no shipped schema extracts
  HERB's facets.
- **Verified** (counted, T2 `nexus_rdr/herb-full-pool-decline-shapes-2026-11-19`):
  all 55 declines are aggregate (46 of 60) or prev-release (9 of 20) questions;
  the 50 "affected" questions never declined and score mean F1 58.7.
- **Assumed**: a planner told the fields and the extra tools would write a
  filter-and-scan or filter-and-count plan. The pre-registered experiment
  (below) tests this.

Research records (T2 `nexus_rdr/255-research-1` to `-14`, 2026-11-19):

- **✅ Verified** (spike) — All 55 HERB declines are aggregate (max-bugs, 46 of 60) or prev-release (9 of 20) questions; the 50 "affected" questions never declined and score mean F1 58.7.
  *Source: T2 `nexus_rdr/herb-full-pool-decline-shapes-2026-11-19` (255-research-1)*
- **✅ Verified** (spike) — The declines are retrieval misses: depth-80 reach of gold keys is 0.21 declined against 0.86 answered, zero for 37 of 55; the top 10 are 94% own-product.
  *Source: T2 `nexus_rdr/herb-full-pool-decline-recheck-2026-11-19` (255-research-2)*
- **✅ Verified** (source search) — The stock run executed plan 10 on 51 of 55 declines; no inline plan was composed.
  *Source: plan ids in the run's non-answer texts (255-research-3)*
- **⚠️ Documented** (docs only) — AgenticScholar pairs a predefined plan library with dynamic DAG generation and a validator; per-dataset adaptation and aggregation handling are not described in what was read.
  *Source: arXiv 2603.13774, abstract and secondary sources (255-research-4)*
- **⚠️ Documented** (docs only) — LangChain's SelfQueryRetriever gives the model per-field descriptions and has it write the metadata filter: shipped precedent for Gap 1.
  *Source: LangChain docs (255-research-5)*
- **⚠️ Documented** (docs only) — Aggregation questions defeat top-k RAG (GlobalQA, best baseline 1.51 F1) and are answered by filter-then-aggregate pipelines (AGGBench, S-RAG).
  *Source: arXiv 2510.26205, 2602.01355, 2511.08505, abstracts (255-research-6)*
- **⚠️ Documented** (docs only) — Schema induction is automated in AutoSchemaKG (no cost figures, no approval step); Agent Workflow Memory learns workflows from successes; no precedent found for shape-keyed routing or feedback (Options B, D).
  *Source: arXiv 2505.23628, 2409.07429 (255-research-7)*
- **✅ Verified** (source search) — The tuple space already carries this kind of work: the aspect queue does idempotent posting, leased claims with renew, ack/nack, dead-letter after 6 attempts, durable tenant-isolated state and per-queue counts.
  *Source: `aspects.yaml`, `http_aspect_queue.py`, `aspect_worker_tuples.py` (255-research-8)*
- **✅ Verified** (source search) — Tuple-space limits for a profile job: 4,096-byte bodies, no fan-in, no job-level status, no claim-time spend cap, oldest-first claims, 10,000 live rows per template.
  *Source: `TupleLimits.java`, `TupleRepository.java`, templates (255-research-9)*
- **✅ Verified** (source search) — Taxonomy clustering cannot be tuple work (it fits the whole vector set); RDR-251 plans it on the memory-only `EngineJobs` registry.
  *Source: `taxonomy_cmd.py`, `taxonomy_compute.py`, `EngineJobs.java` (255-research-10)*
- **✅ Verified** (spike) — `plan_match` scores plan 8 at 0.50-0.54 and plan 10 at 0.14-0.17 on HERB questions on both code versions; why the run executed plan 10 is not yet identified (see the Gap 2 note).
  *Source: scratchpad `rdr255-match-probe*.py` (255-research-11)*
- **✅ Verified** (source search) — No plan step can scan: operators cap at 100 items, retrieval at 300, and the unranked catalog by-meta route is not dispatchable.
  *Source: `runner.py:2159`, `db/limits.py:53`, `aspect_sql.py:72-80` (255-research-12)*
- **✅ Verified** (source search) — HERB's product is not filterable metadata: note chunks carry only hash and model; tags live in the catalog and no filter matches them.
  *Source: `metadata_schema.py:270-291`, `store_hook.py:836-866`, `meta_filter.py:48-61` (255-research-13)*
- **✅ Verified** (source search) — The inline planner reads no per-collection context; a profile block fits at `_nx_answer_plan_miss` (core.py:13178, prompt 13241-13250), and `force_dynamic` is already a public argument.
  *Source: `src/nexus/mcp/core.py` (255-research-14)*


### Critical Assumptions

- [ ] A generic plan (no HERB-specific strings) recovers a meaningful part of
  the gap when it filters by scope and counts by entity — **Status**:
  Unverified — **Method**: Spike (experiment part 1)
- [ ] The inline planner, given a corpus profile and the full tool list,
  composes a plan close to the hand-written one — **Status**: Unverified —
  **Method**: Spike (experiment part 2)
- [ ] A corpus profile can be produced without reading every document with a
  model, so its cost scales with metadata, not corpus size — **Status**:
  Unverified — **Method**: Source Search (metadata and taxonomy are model-free
  today; facet vocabularies may not be)

## Proposed Solution

### Approach

Five options. They are not exclusive; the experiment decides which of A to D carry
weight. Each is marked generic (works for any corpus once built) or
dataset-specific (needs per-dataset work).

**Option A: a corpus profile.** A new command, working name `nx corpus profile
<collection>`, gathers and stores a description of a collection:

1. Metadata fields and their value sets, with counts (for HERB the source has
   30 products and six artifact sections; whether the pooled collection
   carries both as chunk metadata is to be confirmed). Model-free: one
   aggregate query per field over chunk metadata.
2. Entity keys: for each key pattern from `entity.collections`, which artifact
   types carry keys and at what rate, and where the key-to-name mapping lives.
   Model-free: a regex pass over the text, which `find_key_tokens` already
   does.
3. Facet vocabularies: the values a question can name (component names, bug
   states). Two sources. Taxonomy topics are model-free given embeddings.
   Structured facets need extraction: either a new aspect schema per corpus
   (dataset-specific, LLM spend per document) or a model pass over a sample
   to propose a schema, followed by extraction (generic method,
   dataset-specific result).
4. Question-shape hints: the shapes this corpus supports (enumerate entities
   by facet within scope; count entities by status within scope). Proposed by
   a model from items 1 to 3; a few calls, not per document.

The profile is stored in T2, keyed by collection, and stamped with the
collection's chunk count so a stale profile is detectable. `nx_answer` adds the
profile of the scoped collection(s) to the inline planner's prompt.

*Cost.* Items 1, 2 and 4 are cheap: queries plus a few model calls. Item 3 is
where spend sits. Per-document extraction on HERB is about 38,600 Haiku calls (one per artifact, before batching);
the per-document cost has not been measured for this content and should be,
on a sample of a few hundred, before any full run. The cheaper path is to
extract only on the record types that carry keys, which the item-2 census
identifies.

*Generic versus specific.* The command and items 1, 2 and 4 are generic. A
facet schema is dataset-specific even when a model proposes it.

**Option B: a match-quality gate.** Make a library match prove it fits before
it runs. Candidates, cheapest first: raise the curated-plan floor for questions
whose request coverage (RDR-232) names an aggregate or listing answer kind; or
compare the question's shape (from the profile's shape hints, or from the
request's facets) with a shape declared on the plan, and fall through to the
inline planner on a mismatch. Generic. Costs one inline planner call per
fall-through. Its risk is the reverse failure: routing questions away from
plans that answer them, which a gate set too high would do. It needs Option A
or a declared plan shape to be more than a threshold change.

**Option C: a filter-and-scan plan shape in the curated library.** Add a
curated plan, or two, with no relevance ranking at the front: filter by the
scope field, scan every matching record for entity keys (or for facet values),
then `groupby` entity and `aggregate` (count, max) where the question asks for
"the most". This needs the planner's tool reference to list `filter`,
`groupby`, `aggregate` and the scoped search tools (Gap 3), and probably an
unranked scan primitive, since ranked `where` search stops at 300 rows. The
plan text is generic if its bindings come from the profile (which field is the
scope, which pattern is the key); it is dataset-specific if they are written
in. Cheapest to build; the question is whether it generalises beyond HERB.

**Option D: an outcome feedback loop.** Record outcomes per (plan, question
shape), not per plan. When a shape accumulates non-answers on one plan, take
that plan out of matching for that shape and send those questions to the
inline planner; a successful inline plan is then grown through the existing
RDR-084 path and found by matching next time. Generic. Slow: it needs repeated
failures before it acts, so it does nothing for the first questions on a new
corpus, which is exactly when a discovery pass helps. It also depends on a
stable shape label, which Options A or B supply.

**Option E: discovery work and lifecycle state on the tuple space.** Run the
discovery pass (Option A) as tuple-space work instead of one long command, and
keep every run's lifecycle state there. A profile job posts one work tuple per
collection; that fans out into census, taxonomy and sampled facet-extraction
tuples that workers claim under a lease. Each stage writes a result tuple, and
the profile is assembled when all of them exist. This is the shape the aspect
queue already uses (indexing HERB left its 10,000 tasks there as available,
claimed and dead counts). What it buys:

- parallel workers without new scheduling code;
- restart for free, since an expired lease returns the work;
- stuck work visible as dead tuples instead of silent;
- live progress through subscriptions, readable in the console.

Outcome records keyed by (plan, question shape) can live there too, which is
the data Option D needs.

Scope, by Sam's direction (2026-11-19): discovery and lifecycle state only.
The `nx_answer` execution and lookup path stays in-process. Moving plan steps
onto tuples would let independent branches run in parallel, but it adds an
engine round trip per step and requires idempotent steps and claim-time spend
caps. It is considered only if a measured gain justifies that cost. Tuples
carry references (collection names, chunk hashes, tumblers), never bulk text.
Generic.

### Technical Design

To be written once the experiment reads out. Interfaces that are likely
regardless of which options survive:

```text
// Illustrative; names are working names
nx corpus profile <collection> [--sample N] [--facets taxonomy|aspects|none]
  -> writes T2 project "corpus_profiles", title "<collection>"
profile: {collection, chunk_count, fields: {name: {values, counts}},
          entity_keys: {pattern, carrier_types: {type: rate}, mapping_source},
          facets: {name: [values]}, shapes: [text]}
_nx_answer_plan_miss(..., profile: CorpusProfile | None)
  // adds a "Corpus profile" block to the planner prompt
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Metadata value census | `search`/`search_metadata_scoped` `where` support, chunk metadata on the engine | Extend: add an aggregate query, read-only |
| Entity-key census | `src/nexus/entity_keys.py` `find_key_tokens` | Reuse |
| Facet vocabularies | `nx taxonomy discover`; `aspect_extractor.py` registry | Reuse taxonomy; extend aspects with a per-collection schema |
| Planner prompt | `_nx_answer_plan_miss`, `_PLANNER_TOOL_REFERENCE` | Extend: profile block; list the five omitted tools |
| Counting | `operator_groupby`, `operator_aggregate`, `aspect_sql.py` | Reuse |
| Plan growth | RDR-084 grow path, `plan_save` | Reuse; Option D adds a shape key to outcome records |
| Discovery work and lifecycle state | Tuple space (claims, leases, subscriptions, stats; the aspect queue already runs on it) | Reuse (Option E); not in the answer path |

### Decision Rationale

Not decided. The pre-registered experiment separates two questions the options
depend on: is there a better plan at all (part 1), and can the planner find it
when told about the corpus (part 2).

## The Deciding Experiment

Pre-registered before any plan was written
(T2 `nexus_rdr/herb-plan-experiment-prereg-2026-11-19`). Results are pending.

- **Split.** Development set: SearchFlow, ActionGenie, AnomalyForce,
  AutoTuneForce, CoachForce (27 questions). Test set: the other 25 products
  (103 questions), each part run on it exactly once, with nothing about its
  answers read beforehand.
- **Fixed.** Local bge embeddings, the same pooled collection, nexus at
  dc0244253 (code changes for part 2 recorded as a diff), one attempt, HERB's
  GPT-4o company scoring. Spend cap $20 across both parts.
- **Part 1, the curated ceiling.** A hand-written plan, generic: nothing
  HERB-specific beyond what a profile would supply (field names, the key
  pattern already in config), no question-specific strings, built from existing
  operators, stored only in the sandbox library.
- **Part 2, generation.** A hand-written corpus profile (from development
  products only, then generalised), given to the inline planner, with planning
  forced inline.
- **Readout on the test set.** Cust for part 1, part 2 and the stock plan on
  the same 103; decline counts; mean F1 on answered rows; reach@80 under each
  plan's retrieval where recoverable. No pass bar: it is a measurement. "The
  planner can generate it" reads as part 2 within 5 Cust points of part 1.

What each outcome means for the design:

| Part 1 vs stock | Part 2 vs part 1 | Reading | Design consequence |
| --- | --- | --- | --- |
| Large gain | Within 5 | The plan exists and the planner finds it when told about the corpus. | Build A (profile) and B (gate) first. C is unnecessary as curation; D is a later refinement. |
| Large gain | More than 5 below | The plan exists; the planner cannot compose it even with a profile. | Build C (curated shape) with profile-supplied bindings, plus B to route to it. A still pays for C's bindings. Investigate why part 2 fell short before investing in D. |
| Small or no gain | (moot) | Plan shape is not the lever on this corpus; ranking or embeddings are. | Do not build C. A and B drop in priority. The held Voyage arm and the ranking work become the next step. |
| Gain only on some shapes (e.g. max-bugs, not prev-release) | either | The lever is shape-specific. | Option D's per-shape outcome keys matter; B needs a shape label, which favours A's shape hints. |

## Alternatives Considered

### Alternative 1: Fix it in retrieval only (better embedder, deeper search)

**Description**: Keep plan 10; raise retrieval depth or switch to Voyage
embeddings so key-bearing messages rank higher.

**Pros**: no planner change; the Voyage arm is already pre-registered.

**Cons**: a count over "every record in the product" is not a ranking problem.
Even perfect ranking returns k chunks, and the max-bugs shape needs all of
them. It also does nothing for the next corpus's shapes.

**Reason for rejection**: not rejected outright. It is the right reading if
part 1 shows no gain. It is a complement to this RDR, not a substitute.

### Alternative 2: Curate a HERB plan and stop

**Description**: Write the HERB-shaped plan into the library, as RDR-147 did
for the "affected" shape.

**Pros**: cheapest; Agentic Scholar's plans were curated this way.

**Cons**: each new dataset needs a person to notice the gap and write the plan.
`entity-resolution.yml` already carries HERB-worded match text in the shipped
library, which is the pattern this would repeat.

**Reason for rejection**: kept as Option C only in its generic form, with
bindings from a profile.

### Briefly Rejected

- **Lower the match floor to force more inline planning**: routes every
  question through a paid planner call without giving the planner anything
  new to plan with; Gap 1 remains.

## Trade-offs

### Consequences

- A profile adds an index-time or on-demand step that a user must run (or that
  runs on a schedule) before answers on a new corpus improve.
- Facet extraction adds model spend proportional to the documents extracted.
- A larger planner prompt (profile plus five more tools) costs tokens on every
  inline plan.

### Risks and Mitigations

- **Risk**: The profile leaks benchmark answers into planning.
  **Mitigation**: the profile contains field names, value sets, key patterns
  and shapes, never question text or gold; the experiment builds it from
  development products only.
- **Risk**: A stale profile describes a collection that has since changed.
  **Mitigation**: stamp the chunk count and collection version; warn and skip
  the profile when they differ.
- **Risk**: Option B misroutes questions that plan 10 answers well (the 75
  answered HERB rows include 14 perfect scores).
  **Mitigation**: measure B on the same 130 questions plus a non-HERB question
  set before turning it on.

### Failure Modes

A profile that is missing or stale makes the planner behave as it does today,
which is the safe direction. A filter-and-scan plan on a corpus with no
usable scope field returns too much; its scan needs a row cap and a decline
when the cap is hit. A shape-keyed feedback loop with a noisy shape label
retires plans wrongly; outcome records should carry the label so this is
diagnosable from telemetry.

## Implementation Plan

Deferred until the experiment reads out. The phase order in the readout table
above is the plan's skeleton.

### Minimum Viable Validation

The pre-registered experiment, both parts, on the 103 test questions.

## Test Plan

- **Scenario**: profile on a collection with no metadata fields beyond
  defaults — **Verify**: profile records the absence; planner prompt unchanged.
- **Scenario**: profile then add documents — **Verify**: chunk-count mismatch
  is detected and the profile is not used.
- **Scenario**: planner with profile on the HERB development set — **Verify**:
  the generated plan uses a scope filter and a count step on max-bugs
  questions.
- **Scenario**: match-quality gate on a lookup question that plan 10 answers —
  **Verify**: still routed to plan 10.

## Finalization Gate

Not run; this RDR is a draft.

## Open Questions for Sam

1. **When does discovery run?** At index time (always paid, always ready), on
   demand (`nx corpus profile`), or the first time `nx_answer` meets an
   unprofiled collection (pays on first question)?
2. **Facet extraction spend.** Is per-document facet extraction acceptable for
   a new corpus if limited to key-carrying record types, and what per-corpus
   ceiling should it have before it asks?
3. **Who owns a facet schema?** A model-proposed schema per collection, or a
   person-approved one? A proposed schema is generic but can be wrong; an
   approved one is curation by another name.
4. **Curated versus grown, as policy.** Should the shipped library carry
   dataset-worded match text at all (`entity-resolution.yml` does today), or
   should curated plans be shape-generic with bindings from profiles?
5. **Scope of this RDR.** Profile and gate together, or the profile first
   with the gate as its own RDR once a shape label exists?
6. **The held Voyage arm.** Run it before this design is decided, since it
   separates the ranking question from the planning question?
7. **Where the discovery job lives.** RDR-251 (draft) plans taxonomy discover
   on the engine's in-memory job registry (`EngineJobs`, lost on restart);
   Option E puts discovery on the tuple space. Research finding 10 suggests a
   split: engine compute for clustering, tuples for stage fan-out and
   lifecycle status. Choose, or split.

## References

- T2 `nexus_rdr/herb-full-pool-company-result-2026-11-19`
- T2 `nexus_rdr/herb-full-pool-decline-recheck-2026-11-19`
- T2 `nexus_rdr/herb-plan-experiment-prereg-2026-11-19`
- T2 `nexus_rdr/herb-full-pool-company-prereg`
- T2 `nexus_rdr/herb-full-pool-decline-shapes-2026-11-19` (shape census)
- HERB: arXiv 2506.23139, Table 2
- Source: `src/nexus/mcp/core.py`, `src/nexus/plans/matcher.py`,
  `src/nexus/plans/runner.py`, `src/nexus/aspect_extractor.py`,
  `src/nexus/operators/aspect_sql.py`, `src/nexus/entity_keys.py`,
  `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/plans/coverage_repair.py`,
  `src/nexus/plans/coverage_pool.py`, `conexus/plans/builtin/`

## Revision History

- 2026-11-19: drafted. Experiment results pending.
- 2026-11-19: shape census moved from Assumed to Verified (T2 record).
- 2026-11-19: research findings 1-14 added; Gap 2 flagged under review; Gap 5 added; open question 7 (RDR-251 split).
- 2026-11-19: Option E added (tuple space for discovery and lifecycle state; execution path excluded unless measured value justifies it, per Sam).
