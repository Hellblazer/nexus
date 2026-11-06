---
title: "Multi-hop entity-resolution retrieval: an exact-key resolve step and a resolution repair inside the coverage boundary"
id: RDR-147
type: Architecture
status: accepted
resumed_date: 2026-11-03
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-03
accepted_date: 2026-11-03
deferred_date: 2026-07-08
deferred_reason: "All 11 tracking beads, including epic nexus-bwct4, are DEFERRED (P1/P2/P3 rungs incl. entity-linker hook, resolve operator, type-mismatch trigger, phase-review gates, E2E/MVV) as of 2026-07-08. Corrects 2026-08-18 audit finding: this deferral was visible in beads but not reflected in the RDR frontmatter, the audit's highest-value single correction."
related_issues: []
---

# RDR-147: Multi-hop entity-resolution retrieval

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Revision note (2026-11-03, nexus-t9npq)

This revision replaces the June 2026 design. It is a redesign, not an anchor
refresh, and it needs a new gate before any implementation bead starts. The
status stays `deferred`; Sam moves every lifecycle step.

What changed, and why:

- **The June numbers were measured with a broken harness.** The benchmark read
  search hits with `nx search -c`, which cuts every hit at 200 characters. In 25
  of the 37 Slack messages that carry a customer id, the first id starts at
  character 190 or later (median 219), so "company recall 0.000" and "hop 1 surfaces
  the wrong customers" were not findings about nexus. Step 0 (nexus-3lu23,
  merged 2026-11-02 as assetops-kg PR #3, merge commit
  https://github.com/Hellblazer/assetops-kg/commit/3997c6a) rebuilt the harness and measured
  again. Its numbers replace the June ones everywhere in this record.
- **The June end-to-end score was a hand-written script, not nexus.** With the
  harness fixed, that script already scores above the old bar ("recall > 0")
  with no nexus change. The new bar measures `nx_answer` itself: join
  completeness and precision ≥ 0.95 are gated, and recall and F1 are reported
  beside the fixed script's score (§Minimum Viable Validation).
- **The headline is new.** Today `nx_answer` declines all 7 company questions
  with `[non-answer: uncovered]`, while the evidence it retrieved holds the
  customer ids. The mechanism that addresses that is a *resolution repair*
  inside RDR-232's coverage boundary (defined in §Proposed Solution), not a new
  planner trigger. Measured on 2026-11-03, the coverage repair already enters on
  these questions (Critical Assumption #5, 147-research-7).
- **Mechanism B's June seam is gone.** (Mechanism B was June's planner-side
  type-mismatch trigger; Mechanism A was the ingest-time entity linker.) The inline planner is one-shot, cannot
  emit a `filter` step, and the dimension and shape names June proposed collide
  with RDR-232's vocabulary. The trigger moves to the one place that already
  inspects evidence after retrieval and adds work.
- **Exact-key lookup mostly exists now.** `find_by_title_exact` and RDR-236's
  catalog-answered `where title=` give a deterministic single-key lookup. What is
  missing is a plan step that takes a list of keys.
- **The entity-linker (Mechanism A) is off the critical path.** On HERB, company
  names live inside the customer records' own text, so resolving a customer id
  to its record is enough. The linker stays in this record as a conditional step.
- **The two aggregation questions are out of the bar.** "Which company has the
  most resolved/unresolved bugs" needs counting, not lookup. Sam (2026-11-03):
  report them separately, keep them out of the Minimum Viable Validation
  (MVV, the one end-to-end check that says this RDR worked), and add no planner
  aggregation step in this revision.

The design of record for these changes is the re-plan memo, T2
`nexus/plan-rdr147-replan-2026-11-02`; the measured numbers are T2
`nexus/rdr147-step0-baselines-2026-11-02` and assetops-kg
`results/herb/step0-baselines.json` at that merge. Every code anchor below was
checked against nexus `develop` at `b7c423ea8`.

## Problem Statement

Some questions name one kind of thing but are answered by another kind that the
evidence only *refers to*. HERB (a benchmark of enterprise search over Slack
messages, documents, pull requests and records, from Salesforce AI Research,
arXiv 2506.23139) has a clean case. In its SearchFlow product, "which companies
were affected by issue X?" retrieves Slack messages that name **customer ids**
(`CUST-0018`). The answer needs **company names** (`FusionTech`). The link
`CUST-0018 → FusionTech` lives in a separate customer record, one short record
per customer. Answering takes two retrievals in sequence: find the messages,
then look up each customer id they name. This is a *multi-hop join*, and the
second hop is an *entity resolution*: turning an identifier into the record it
names.

**Measured (Step 0, 2026-11-02; conditions in §Step 0 measurements):**
`nx_answer` (nexus's plan-then-execute answer tool) answers none of the 7 SearchFlow company questions. All 7 end as
`[non-answer: uncovered]`: RDR-232's coverage check found that no part of the
question was supported, and skipped generating an answer. Yet the evidence each
run retrieved (20 to 80 chunks) names 2 to 10 customer ids per question. The ids
are in hand, and nothing turns them into companies. The coverage check's one
repair (a second retrieval) ran on the 6 questions that reached the boundary
and changed no verdict (Critical Assumption #5).

### Enumerated gaps to close

#### Gap 1: No ingest-time entity linking

An entity record whose key (a customer record titled `CUST-0018`) appears
verbatim inside other artifacts implies a foreign-key reference. nexus indexes
both and creates no catalog link between them, so `traverse` (the catalog graph
walk) has nothing to follow. **Revised standing:** on HERB this gap is not on the
path to an answer, because the company name is inside the customer record and
resolving the id (Gap 2) is enough. It stays a gap for corpora where the answer
sits one link further out. See §Technical Design, Step 2.

#### Gap 2: No plan step resolves a list of keys exactly

Dense retrieval cannot find an exact key among near-identical records. The 120
SearchFlow customer records are near-identical sentences (`Customer CUST-xxxx:
contact N, R at company C`), so their embeddings collide. **Verified (June):**
`search("CUST-0035")` returns `CUST-0097`, `CUST-0046`, `CUST-0079`, never
`CUST-0035`.

**Revised standing:** single-key exact lookup now exists. The catalog's
`find_by_title_exact` returns every live document whose title equals a string
exactly, across collections (details in §Technical Environment). RDR-236 also
answers `where title=` from the catalog document's own title. What is missing is
a step a plan can run over a *list* of keys, that reports keys it could not
resolve and keys that matched more than one document.

#### Gap 3: Nothing acts on "the evidence holds identifiers, not answers"

The signal that a resolution hop is needed is that the evidence names the
answer's entities only by identifier. **Verified (June):** with a hand-written
rule in its context, the inline planner (the `claude -p` call that writes a plan
when no saved plan matches) stated "these are customer accounts referenced only
by ID, not company names". Nothing acts on that signal without the hand prompt.

**Revised standing:** the place that already inspects evidence after retrieval
and can add one round of retrieval is RDR-232's coverage boundary (the runner
step that checks whether the evidence supports each part of the question, called
a *facet*, before the final answer is generated). Its one *repair* (a second,
targeted retrieval for unsupported facets) runs semantic queries on the facet's
text. For an exact-key join that repeats the embedding collision of Gap 2. The
gap is that the repair has no exact-key path. **As built:** the exact-key branch
scans the coverage anchors only, not all retrieved evidence. On HERB the backend
search-engine issue question holds customer ids in its evidence and none in its
anchors in every valid MVV run (147-research-13, -14, -17; 8 ids in each run of the
final MVV, `docs/rdr/evidence/rdr-147/herb-scorecard.json`), so it names no company;
widening the scan was left out of this RDR (Sam, 2026-11-06, nexus-z938f closed).

#### Gap 4: No gating; blanket iteration harms simple questions

Iterative retrieval hurts single-hop and simple comparison questions: added
documents dilute precision and bury the gold documents (Beyond Static Retrieval,
arXiv 2509.25530; **Documented**). A resolution hop must fire only on its signal,
never on every question.

## Context

### Background

Found while benchmarking nexus on HERB in an isolated local sandbox. nexus's
composed `nx_answer` arm matched GPT-4o agentic ReAct on People search (about 34
against about 32, with bge-768, a local embedding model, and no metered API)
but scored 0 on the company questions. Step 0 reproduced that 0 for `nx_answer`,
for a different reason than June diagnosed (see the revision note). Root-causing that number produced the findings in T2/T3
(`herb-nexus-comparison-results`, `discovering-multihop-query-plans-from-structure`,
`multihop-retrieval-literature-map-for-nexus`). The design is derived from the
structure of the task and corpus, not copied from a higher-scoring system.

The literature (StepChain GraphRAG 2510.02827; ByoKG-RAG 2507.04127; PRISM
2510.14278; PAR-RAG 2504.16787; Self-RAG 2310.11511; all indexed in T3
`knowledge`) agrees on the approach: graph-augmented retrieval exists because
dense retrieval cannot do exact-key joins (PRISM, citing Weller 2025: "no single
embedding can represent all query–document relevance patterns as candidate sets
grow combinatorially"). StepChain's `Extract(chunk)` + `Link(entity_a, entity_b,
chunk)` + deterministic graph walk is this RDR's Mechanism A under another name.

Since June, two RDRs changed what this one has to build. RDR-232 (closed) added
the coverage boundary and the request *answer kind* (what sort of answer a
question asks for: lookup, listing, synthesis and so on). RDR-236 (chunk-row
metadata normalization) moved document fields such as `title` onto the catalog
document, so a `where title=` filter is answered from the catalog per owning
document.

### Technical Environment

All anchors are nexus `develop` at `b7c423ea8`. There is no daemon, Chroma or
SQLite path in this design: the catalog and T3 (the permanent vector store) are
the engine service's Postgres tables, reached over HTTP. A *tumbler* is a
catalog document's hierarchical address (`1.2.5`); a *chash* is the SHA-256 of a
chunk's text, which serves as the chunk's identity; a document's *manifest* is
its ordered list of chunk chashes.

- **Exact title lookup.** `HttpCatalogClient.find_by_title_exact(title, *,
  content_type=, owner=, chunk_count=)` (`src/nexus/catalog/http_catalog_client.py:2110`)
  calls `GET /v1/catalog/by_title` (`CatalogHandler.java:48`, routed at `:167`),
  which runs `CatalogRepository.documentsByTitle` (`CatalogRepository.java:3380`).
  Exact and case-sensitive, live documents only, every collection, returns a
  list because titles are not unique. `resolve_many(doc_ids)` (`:2376`) and the
  MCP `store_get_many` (`src/nexus/mcp/core.py:6700`) turn results into records.
- **Catalog-answered `where`.** RDR-236's manifest stage answers document keys
  from the catalog document. Its catalog-meta key allowlist is
  `ManifestLabelStage.META_KEYS` (`ManifestLabelStage.java:195-196`: tags,
  category, ttl_days, source_agent, extraction_method, quality_gate_overridden,
  extraction_source, frecency_score), plus the document fields (title,
  content_type and the bibliographic keys). The engine compiles `$eq` and `$in`
  on those keys (`ManifestPushdown.java:114`, `:157`, `:263`). The Python
  `where=` string grammar has no `$in`: it accepts only `KEY op VALUE` with
  `op` in `>=, <=, !=, >, <, =` (`src/nexus/filters.py:15-19`).
- **Plan runner.** `src/nexus/plans/runner.py`: `_NON_EMBEDDING_TOOLS` (`:603`,
  holds `traverse`), step references `$stepN.<field>` (`_STEPREF_RE` `:704`,
  `_resolve_value` `:946`), the operator map `_OPERATOR_TOOL_MAP` (`:1982-1993`,
  which includes `filter`, `groupby` and `aggregate`), `_OPERATOR_MAX_INPUTS =
  100` (`:1997`). The runner hands `operator_filter` the hydrated texts as
  `items` (`:3011-3014`).
- **Inline planner.** `_nx_answer_plan_miss` (`src/nexus/mcp/core.py:12173`) is
  one-shot; its retries only repair malformed plans. Its tool allowlist
  `_ALLOWED_TOOLS` (`:12332-12336`) is search, query, traverse, store_get_many,
  extract, rank, compare, summarize, generate, check and verify: no `filter`, no
  `groupby`, no `aggregate`. MCP `plan_save` (`:7930`) takes `verb`, `scope_tags`
  and tags but no free dimensions; the dimension registry
  (`conexus/plans/dimensions.yml`) holds verb, scope, strategy, object and domain,
  and unregistered keys fail strict validation (`src/nexus/plans/loader.py:254-324`).
- **RDR-232 vocabulary.** `AnswerShape` (`src/nexus/plans/answer_shape.py:46`)
  classifies the terminal text; the request kind is `PlanAnswerKind`
  (`src/nexus/plans/answer_kind.py:105`). Plan eligibility by kind is the table in
  `src/nexus/plans/eligibility.py:12-24`. A question with a plural head after
  "which/what" goes through `_plural_head` (`src/nexus/plans/request_coverage.py:803`)
  and `_infer_kind` (`:877`).
- **Coverage boundary.** `CoverageBoundary` (`src/nexus/plans/coverage_ledger.py:875`);
  `run` (`:1073-1091`) is `evaluate` plus at most one repair. The repair enters
  only for facets in `repair_target_ids` (`:564-566`): facets `unsupported` with
  reason `no_supporting_evidence` (`REPAIRABLE_REASON`, `:251`), plus covered
  facets whose anchors were mostly unread. A facet decided by a missing named
  source gets reason `source_missing` (`_blocked`, `:951-970`) and is not a repair
  target. `state_for` returns `unavailable`/`uncovered` and skips generation when
  every facet is unsupported and none is unassessed (`:842-843`). The ledger's
  *anchors* are the evidence items it assesses each facet against. The repair's
  queries are the facet's own text (`src/nexus/plans/coverage_repair.py:11-13`).
  `nx_answer` builds the boundary at `core.py:14590` whenever `NX_ANSWER_COVERAGE`
  is on (the default); nothing in `src/` sets `max_repair_queries` to 0.
- **Catalog links.** `link_if_absent` (`http_catalog_client.py:2810`) checks then
  POSTs; the engine upserts (`CatalogRepository.upsertLink`, `:4270`), so it is
  idempotent. There is no bulk link create. `link_type` is open `TEXT`
  (`catalog-001-baseline.xml:132`). `mentions` is not in
  `DEFAULT_GRAPH_LINK_TYPES` (`CatalogRepository.java:4517-4520`) nor in
  `_KNOWN_LINK_TYPES` (`src/nexus/plans/purposes.py:77`), so a `traverse` over it
  must name it.
- **Deletion and links.** Hard delete cascades to links through foreign keys
  (`catalog-032-links-tumbler-fk.xml:137-155`, `ON DELETE CASCADE`; reached by
  `purgeTrash` `CatalogRepository.java:2786`). Delete to trash is a tombstone
  (`deleteDocument` `:2387`, `restoreDocument` `:2472`) and links survive it.
  `graphBFS` joins both endpoints to live documents (`:4658-4668`), so `traverse`
  never walks a tombstone; `linksFrom` (`:4406`) and `linksTo` (`:4419`) do not
  filter tombstoned endpoints.
- **Ingest hooks.** `install_default_hooks` (`src/nexus/hook_registry.py:526`),
  `register_batch` (`:169`, records whether the hook takes `catalog_doc_id`,
  `:182`), `fire_batch` (`:214`, passes `catalog_doc_id` at `:307`, filters by
  `batch_grain` at `:298`). In `store_put` the order is `put_note`
  (`core.py:5951`) → `_catalog_auto_link` (`:5996`) → `fire_note_chains`
  (`:6021`, with the tumbler) → `stamp_note` (`:6026`). Post-hoc link generators
  live in `src/nexus/catalog/link_generator.py` (for example
  `generate_rdr_filepath_links` `:240`, `generate_prose_filepath_links` `:706`;
  `DryRunLinkWriter` `:1069`).
- **No entity key exists.** Nothing in `src/` names `entity_key` or classifies
  entity records. Catalog document meta accepts any key (merged with
  `jsonb_concat`, `CatalogRepository.java:2195-2197`), but a `where` on a new meta
  key needs it added to `META_KEYS` and the pushdown. Per-collection
  configuration precedents are `config.py` `_DEFAULTS` (`src/nexus/config.py:1357`)
  and the per-repo `.nexus.yml` reader (`:1795`).
- **CLI read.** `nx search -c` truncates each hit at `_CONTENT_MAX_CHARS = 200`
  (`src/nexus/commands/search_cmd.py:49`, applied `:779`); `--json` returns full
  chunk text.

## Research Findings

### Investigation

June 2026: benchmarked nexus on HERB SearchFlow in a local sandbox, wrote a
deterministic multi-hop resolver (assetops-kg `benchmark/multihop_resolve.py`),
probed the inline planner with a structural rule and `force_dynamic`, and
surveyed six multi-hop and GraphRAG papers (indexed in T3).

November 2026: a read-only re-plan survey against nexus `develop` (T2
`nexus/plan-rdr147-replan-2026-11-02`) found the harness defects below. Step 0
(nexus-3lu23) then rebuilt the benchmark on the PG engine and measured again
(§Step 0 measurements).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `HttpCatalogClient.find_by_title_exact` / `GET /v1/catalog/by_title` | Yes (b7c423ea8) | Exact, case-sensitive, live documents only, all collections, returns a list; optional owner, content_type and chunk_count narrowing. The single-key lookup Gap 2 needs. |
| RDR-236 manifest stage (`ManifestLabelStage`, `ManifestPushdown`) | Yes (b7c423ea8) | `where title=` is answered from the catalog document title. The engine compiles `$in`; the Python `where=` grammar lacks it. A new meta key needs an allowlist and pushdown change. |
| `nexus.plans.runner` | Yes (b7c423ea8) | No `resolve` step. `traverse` already runs as a `$ref`-seeded step. `filter`, `groupby`, `aggregate` are in the operator map but not in the inline planner's allowlist. |
| `nexus.plans.coverage_ledger` / `coverage_repair` | Yes (b7c423ea8) | The one place evidence is inspected after retrieval and one more retrieval may run. The repair is semantic, on facet text, and enters only for `no_supporting_evidence` facets. |
| `nexus.hook_registry` | Yes (b7c423ea8) | `register_batch` hooks get `(doc_ids, collection, contents, embeddings, metadatas)` and, if declared, `catalog_doc_id`. The seam for a linker's forward pass. |
| Catalog links (`link_if_absent`, `upsertLink`, `graphBFS`) | Yes (b7c423ea8) | Idempotent typed links; no bulk create; `mentions` must be named in `traverse`; hard-delete cascade exists; `traverse` filters tombstones, `links_from`/`links_to` do not. |
| Exact-token enumeration (FTS) | Yes (re-plan survey) | Does not exist. The `lexical` search leg (`search_engine.py:1704`) is ranked, capped at `n_results`, and fuzzy on its trigram arm. No route returns every chunk containing a token. |

### Key Discoveries

- **Verified** — Embedding collision: `search("CUST-0035")` returns other
  customer records, never the exact one (June).
- **Verified** — Exact title lookup resolves every customer id: P2 resolution
  accuracy is 1.000 on the 29 SearchFlow member customers (Step 0).
- **Verified** — The inline planner, hand-prompted with the structural rule,
  detects "ids, not names" (June).
- **Verified (re-plan survey)** — The June harness read hits with `nx search -c`,
  which cuts each hit at 200 characters. In SearchFlow, 37 Slack messages carry a
  customer id; in 25 of them the first id starts at character 190 or later
  (median 219). The harness also returned `""` on any `nx` error, so a broken
  sandbox read as recall 0. Both are fixed in Step 0 (`--json`, and errors raise).
- **Verified (re-plan survey)** — The June end-to-end rung scored
  `answer_company()`, a hand-written script, not `nx_answer`. With the harness
  fixed it scores above the June bar with no nexus change (Step 0: issue
  questions recall 0.667).
- **Refuted by Step 0** — June's "even with resolution wired, recall stayed 0
  because hop 1 surfaced the wrong customers" was a truncation artifact. On the 5
  issue questions, hop-1 precision at k=20 is 0.967. Hop 1's limit is recall: it
  surfaces 4 to 6 of the 10 to 12 ids the cited artifacts name.
- **Measured (Step 0)** — `nx_answer` declines all 7 company questions as
  `[non-answer: uncovered]` while holding the ids.
- **Measured (2026-11-03, 147-research-7)** — The facet states on develop
  7b85c02cb, on a rebuilt Step 0 sandbox: no question names a source
  (`named_sources=[]`), each has one whole-question facet `f1`, and no facet is
  `source_missing`. q1 took plan 7 and did not reach the boundary. On q2 to q7,
  `f1` is `unsupported` / `no_supporting_evidence`, `repair_target_ids=[f1]`, and
  the repair ran: q2, q3, q4 and q7 found no new evidence after 1 query; q5 merged
  1 item and q6 merged 2; all six stayed `[non-answer: uncovered]`. Plan 9 (q2 to
  q4) gave the boundary 6 anchors out of 80 chunks. Step 0's
  `repair_entered=False` / `not_attempted` came from its probe on a different
  question.
- **Measured (2026-11-03, 147-research-8)** — A resolved customer record merged
  alone does not make the re-check cover `f1`: 0 of 8 runs. The re-check reads
  only the new anchors, and a record names a company but not the issue.
- **Measured (2026-11-03, 147-research-9)** — A joined item (the message that
  named the id, then the resolved record, as one item) does: `f1` covered in 10 of
  10 valid runs, and the generated answer named exactly the companies of the
  merged pairs. Of the 45 joined items merged over those runs, the re-check cited
  35 as supporting (147-research-10).
- **Documented** — StepChain, ByoKG, HippoRAG2 and LEGO-GraphRAG build explicit
  entity graphs for this join; PRISM and PAR-RAG decompose with precision
  filtering; Self-RAG and FLARE gate retrieval reflectively; Beyond-Static
  documents that blanket iteration hurts simple questions.

### Step 0 measurements

Conditions for every number in this section and wherever this record quotes
Step 0: nx 7.94.0, engine service 0.1.175, embedding `bge-768` (BAAI
bge-base-en-v1.5, 768 dimensions, local ONNX), an isolated PG sandbox (own HOME,
loopback ports, no credentials), 1559 HERB SearchFlow artifacts ingested through
the `store_put` note path (1559 catalog documents, 1578 chunks), hop-1 depth
`k=20` (the first search returns 20 hits). Recorded 2026-11-02 in assetops-kg
`results/herb/step0-baselines.json` at the Step 0 merge (assetops-kg PR #3).

The 7 company questions split into 5 *issue* lookups ("companies affected by
issue X", **issue5**) and 2 *aggregation* questions ("company with the maximum
number of resolved/unresolved bugs", **agg2**). Company scores are means over
questions of per-question recall, precision and F1 (the harmonic mean of the
two). The table holds the recorded values; prose elsewhere in this record
rounds them to three places. The *script* is the fixed hand-written pipeline: search the question,
read customer ids from the hits' full text, look each up by exact title, read
the company from the record. The *oracle* replaces hop 1 with the artifacts HERB
cites for the question.

| Baseline | issue5 recall | issue5 precision | issue5 F1 | agg2 F1 | all7 F1 |
| --- | --- | --- | --- | --- | --- |
| Fixed script | 0.6666666666666667 | 0.9666666666666666 | **0.7642857142857142** | 0.0 | 0.5459183673469388 |
| `nx_answer` (today) | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| Oracle: cited artifacts as hop 1 | 1.0 | 0.6154761904761905 | 0.7593006993006993 | 0.18181818181818182 | 0.5943056943056942 |
| Oracle: k=300 retrieval, kept only if cited | 1.0 | 0.6521428571428571 | 0.7873959373959374 | 0.26785714285714285 | 0.6389562818134247 |

Ladder rungs: P2 resolution accuracy 1.0 on 29 members. P3 hop-1 precision
0.6904761904761905 over all 7 (issue5 0.9666666666666666, mean 4.8 ids surfaced;
agg2 0.0, mean 0.5 ids surfaced).

`nx_answer` detail: all 7 rows `coverage_state=unavailable`,
`coverage_reason=uncovered`; 20 to 80 evidence chunks per question; 2 to 10
distinct customer ids in the hydrated evidence (the retrieved chunks fetched
as full text) per question. The path varied and
the outcome did not: plan 9 matched on 3 of 7 questions in runs 1 and 3, and the
inline planner ran in run 2. Spend 0.6607 USD over 3 runs and 1 probe (ledger
`results/herb/step0-nx-answer-costs.jsonl`).

The re-plan memo predicted these before measuring (§4 of the memo): script
recall 0.40 over all 7 (measured 0.476), 0.52 on issue5 (0.667), 0.10 on agg2
(0.0); P3 0.55 (0.690); P2 1.000 (1.000); `nx_answer` recall 0.10 (0.0) and
precision below 0.30 (0.0); oracle recall 1.00 (1.0) and issue5 precision 0.61
(0.615). The predictions erred low on the script and high on `nx_answer`.

**Derived, not measured:** if `nx_answer` named every company that the customer
ids in its recorded evidence resolve to, its issue5 F1 would be 0.823 (recall
0.867, precision 0.835), computed from the same file's `evidence_companies`
rows. **Measured with the joined items (147-research-9):** the answer named
exactly the companies its resolved ids give, and issue5 F1 was 0.7167 (recall
0.6267, precision 0.9667) in both repetitions. It is below the derived 0.823
because the measured runs' evidence held fewer customer ids (q2 retrieved 1 id
against 5 gold companies). Hop-1 retrieval recall, not the join, sets the gap.

**Reading.** On the issue lookups, hop-1 precision is not the problem (0.967).
The problem is that `nx_answer` never turns held ids into names. Hop-1 recall at
k=20 is the next limit (0.667 against the oracle's 1.0), and it is out of scope
here (§Risks). The cited-artifact oracle has perfect recall but a lower F1 than
the script, so recall alone does not make the answer better than the script's.

### Critical Assumptions

- [x] **#1 Entity records carry a stable key that appears verbatim in referencing
  artifacts** — **Status**: Verified (HERB) / Refuted (general corpora) —
  **Method**: Spike + Source Search (2026-06-05), re-checked on b7c423ea8.
  "Title" now means the catalog document's title (RDR-236), which
  `find_by_title_exact` matches exactly. Two June preconditions dissolve: entity
  records need not be single-chunk (by_title returns documents, and the manifest
  gives all their chunks), and the target collection need not be known in advance
  (by_title searches every collection, narrowable by owner or content_type). The
  boundary that remains: title equals key only where the caller chose it
  (`store_put(title=<key>)`, as HERB's 120 customer records were stored). Batch
  index paths reshape titles: `derive_title()` title-cases filename tokens
  (`cust-0035.md` → "Cust 0035"), code chunks are titled `rel_path:Lstart-Lend`,
  PDF and markdown titles are human-readable. Step 1's `entity_key` covers
  store_put and configured collections; md/pdf extraction is deferred (Step 1).
  The stamp does not widen what `resolve` finds: `resolve` matches titles only
  (`BY_VALUES = ("title",)`), so a record resolves only where its title equals
  its key, and a caller-set `entity_key` that differs from the title is read by
  nothing until the conditional linker or a meta-key filter exists
  (147-research-11).
- [deferred: nexus-81183] **#2 A post-store batch hook can create catalog links** — **Status**: To
  re-verify on the PG engine (Step 2, only if the linker is built). The June
  verification reasoned about the T2 daemon's `asyncio.Lock` and an `fcntl` lock
  that no longer exist. The current precedent is `fire_note_chains` firing with
  the document's tumbler after `put_note` and `_catalog_auto_link` have returned
  (`core.py:5951-6021`), and `manifest_write_batch_hook` writing to the catalog
  from inside `fire_batch` on every store_put. The `nx index repo` flush grain
  carries no tumbler, so a linker there must look the tumbler up.
- [x] **#3 `traverse` runs as a plan step seeded by `$stepN.tumblers`** —
  **Status**: Verified (with precondition) — **Method**: Source Search + Spike
  (2026-06-05), still holds on b7c423ea8 (`_NON_EMBEDDING_TOOLS`, `_STEPREF_RE`,
  `_resolve_value`; builtin plans `citation-traversal.yml`,
  `hybrid-factual-lookup.yml`, `traverse-then-generate.yml`;
  `tests/test_traverse_step.py`). Precondition: seeds must be catalog-registered.
  Addition: `mentions` is not a default graph type, so a `traverse` over mentions
  links must pass `link_types=["mentions"]` explicitly.
- [x] **#4 Exact-key resolution of a token list needs one new step** —
  **Status**: Verified (rewritten) — **Method**: Source Search on b7c423ea8. The
  June finding "not expressible today" still holds for plans: there is no
  `resolve` step, no fan-out over items inside a step, and no `$in` in the
  `where=` string. But the engine already compiles `$in` on title, and
  `find_by_title_exact` already does one exact lookup. So the new step is thin: a
  runner-native `resolve` that calls `find_by_title_exact` per token and reports
  `missing` and `ambiguous`. A `$in` string grammar is not needed for it.
- [x] **#5 The resolution repair can enter on the HERB ledger** — **Status**:
  Verified — **Method**: Spike, 2026-11-03 (147-research-7; T2
  `nexus/rdr147-s1-facet-measurement-2026-11-03`). On q2 to q7 the single facet
  `f1` is `unsupported` / `no_supporting_evidence` and `repair_target_ids=[f1]`,
  so the repair enters; no facet is `source_missing` and no question names a
  source. q1 does not reach the boundary (plan 7). Step 0's
  `repair_entered=False` came from its probe on a different question.
- [x] **#6 RDR-232's single re-check judges a joined item supporting** —
  **Status**: Verified on the 2026-11-03 spike (HERB, 10 of 10); weakened on
  2026-11-05 to 11 of 15 (below) — **Method**: Spike, 2026-11-03
  (147-research-8, 147-research-9, 147-research-10). A record alone: 0 of 8. A
  joined item: `f1` covered in 10 of 10 valid runs. Caveats: one corpus and one
  question shape (the 5 issue questions, twice each); the verdict is a model
  call; the re-check cited 35 of the 45 joined items as supporting; the 2 q2
  runs ran on grown plan 14 and covered `f1` on their single joined item.
  Weakened 2026-11-05 (147-research-12, 147-research-13): in four MVV runs the
  re-check accepted 11 of 15 entered joins, and the same item flipped between
  runs. Since option 1 (Approach item 2) the MVV no longer needs it: with the
  switch on, an entered join that merged at least one item generates whatever
  the re-check says.

## Proposed Solution

### Approach

Three changes. Only the first two are on the path to the MVV, together with
the routing template of Step 4 branch (c), which sends these questions to a plan
with a coverage boundary (147-research-15, 147-research-16).

1. **A `resolve` step (Gap 2).** A runner-native plan step that takes a list of
   key strings and returns the documents whose catalog title equals each one,
   exactly. Keys that match nothing and keys that match more than one document
   are reported, never dropped or guessed.
2. **A resolution repair inside the coverage boundary (Gaps 3, 4).** When the
   coverage boundary finds a facet unsupported and the coverage anchors it holds
   (the evidence chunks it assessed) contain tokens that a configured *entity-key pattern* recognises (for
   example `CUST-\d{4}`), its one repair runs `resolve` on those tokens and adds
   each resolved record *joined* to the evidence item that named its key (the
   item's text, then the record's text, as one item) before the single re-check.
   The re-check judges only new items (RDR-232's rule, `coverage_ledger.py`
   `evaluate(..., prior=...)`), so a record alone, which names a company but not
   the issue, is never judged supporting (0 of 8, 147-research-8), while the
   joined item usually is (10 of 10, 147-research-9; 11 of 15 in the 2026-11-05
   MVV runs, 147-research-13). Then the answer is generated from evidence that
   holds the names. **Option 1** (Sam, 2026-11-05, T2
   `nexus/rdr147-option1-decision-2026-11-05`, taking the fallback of T2
   `nexus/rdr147-c1-decision-2026-11-03`: "when the exact-key branch entered, the
   re-check's verdict is not required to generate; the answer notice discloses
   that the answer rests on an exact-key join"). This RDR reads "entered" as
   entered AND merged at least one joined item, since an entered branch whose
   pairs all dropped joined nothing. Then a final ledger that leaves the facets
   uncovered does not stop generation. Every answer generated with a merged join
   leads with a notice that it rests on an exact-key join, as the chosen option's text asks;
   when the final ledger was uncovered, the notice adds that the coverage check
   did not confirm it. The facet states stay as the re-check judged them. The gate is the signal
   itself: an unsupported facet AND entity-key tokens in the coverage anchors. A question
   with no such tokens never reaches it, so simple questions are untouched
   (Beyond-Static guidance), and no second planning round, new plan dimension or
   planner prompt change is needed.
3. **An ingest-time entity linker (Gap 1), conditional.** Built only if a corpus
   needs a hop past the entity record (Step 2). Not required on HERB.

Entity-key recognition is shared by 2 and 3: a per-collection key pattern in
configuration, plus an optional `entity_key` a caller sets on `store_put`,
stamped into catalog document meta (Step 1).

### Technical Design

**`resolve` step (Step 3).** Interface (illustrative; verify during
implementation):

```text
resolve(tokens: list[str], by="title", content_type=None, owner=None)
  -> {tumblers, ids, collections, missing, ambiguous}
// for each distinct token:
//   docs = find_by_title_exact(token, content_type=, owner=)
//   0 docs  -> missing[token]
//   >1 docs -> ambiguous[token] = [tumblers]   (never first-match)
//   1 doc   -> its tumbler, plus its manifest chashes as ids
```

It sits beside `traverse` in `_NON_EMBEDDING_TOOLS`, so it costs no embedding
and no model call, and its output fits the standard `{tumblers, ids,
collections}` retrieval contract that `$stepN` references and hydration already
read. One HTTP call per token is fine at HERB's scale (2 to 10 tokens per
question). A batch by-titles route is added only if a measured call exceeds about
300 tokens. Tombstoned documents cannot reach `resolve`, because `by_title`
returns live documents only.

**Resolution repair (Step 4).** Inside `CoverageBoundary.run`
(`coverage_ledger.py:1073`), after the first `evaluate`:

```text
if first ledger has an unsupported facet (entry rule: see Step 4 sub-step 1)
   and anchors hold tokens matching the collection's entity-key pattern:
     tokens = distinct matches, capped
     r = resolve(tokens)
     for each (anchor, token) with a resolved record:
       one joined item = anchor text, then the resolved record's text
     merge the joined items into the reduction's input
       (the existing repair_stream supplement, inside its cap of
        MAX_REPAIR_ITEMS = 12 items; HERB needs at most 10)
     report r.missing and r.ambiguous in the RepairReport
     re-evaluate once (the existing single re-check, unchanged:
       it judges the new joined items only)
     if at least one joined item merged and the final ledger is uncovered:
       generate anyway (option 1); facet states unchanged;
       RepairReport.generated_on_join = true
     every answer generated with a merged join leads with the exact-key-join
       notice naming the distinct keys of the merged items
       (RepairReport.joined_keys); generated_on_join adds "the coverage check
       did not confirm this answer"
```

When the branch enters, it runs in place of the semantic facet queries, the
case 147-research-9 measured (joined items merged with no semantic repair
query); running both together is unmeasured and not built. In `_repair` the
branch runs before the two `no_new_evidence` returns
(`coverage_ledger.py:1313` and `:1335` at 532b9d186), which otherwise end the
repair before any re-check when the semantic queries find nothing new, as they
did on q2, q3, q4 and q7 (147-research-7). The repair's existing
deadline and USD rules apply unchanged; `resolve` is a zero-cost step. The
`missing` and `ambiguous` lists are disclosed, so a partial resolution is
visible in the run summary rather than silent.

**Generation after an entered join (option 1, Sam 2026-11-05).** This is the one
exception this RDR adds to RDR-232's rule that a ledger covering no facet stops generation
(`skip_generation`, and `decide_outcome` in `coverage_outcome.py` reading an
`unavailable` / `uncovered` ledger as a non-answer). It applies only when the
resolution branch entered and merged at least one joined item, under the same
switch (`NX_ANSWER_RESOLUTION_REPAIR`), so a confined run, a run whose keys all
failed to resolve, and every run with the switch off keep RDR-232's rule. The
ledger is not rewritten: the facets keep the re-check's states and the final
coverage state stays `unavailable` / `uncovered`. What changes, in this order:
the boundary's result has `skip_generation` false; the RepairReport gains
`generated_on_join: true` (on the wire and in the run summary's `resolution_*`
counts as `resolution_generated_on_join`); `decide_outcome` gives such a run
the answered shape, counts it as an answer, and sets `no_grow`, so an answer the
coverage check did not confirm never grows a plan, but only when the generated
text classifies as an answer: other text keeps today's `no_usable_answer`
downgrade. The notice line is `[exact-key join: <n> record(s) joined by exact
key (<keys>)]`, where `<keys>` are the distinct keys of the merged items
(`RepairReport.joined_keys`) and `<n>` their count, with `; the coverage check
did not confirm this answer` added when `generated_on_join` is true. It leads
the prose of every answer generated with a merged join, ahead of the existing
`[insufficient-evidence: ...]` notice on a partial answer (that notice's own
wording would contradict an uncovered answer, so it is not reused for it).
The generator gets no unresolved-facets note on these runs: `_note_unresolved`
serves only `partial` and `complete` ledgers and skips an `unavailable` one, and
this ledger stays `unavailable`; whether such a note would help is unmeasured and
not built. Readers that took an uncovered final state to mean a withheld answer
are handled in two ways. `answer_text_class` consults `generated_on_join` (a
redacted row of such an answer stays `redacted` rather than
`non_answer/uncovered`); `nx answer-runs` does not read the flag, it skips a
leading join line so that the line does not hide a partial answer. The risk is an answer the coverage check did
not confirm. The join fixes which record a key names, not whether the anchor
message answers the question; that relevance is what the re-check judged, and
nothing else on the coverage path checks it (R9). The 2026-11-05 precision (pooled 1.00 in every
run, 147-research-12, 147-research-13) covers only answers the re-check
accepted: 11 company-naming runs, 4 in `mvv-2026-11-05.jsonl` and 7 in
`mvv-2026-11-05.run1.jsonl`, each of precision 1.00. The one template-7 row (a listing that named no
company, which never reached the coverage boundary) is outside the precision
mean, as is any run that names no company, and the 4 backend-question runs (2 in
each file, the branch not entered) named none. The rejected joins produced
non-answers, so the answers that only option 1 admits are unmeasured until the
fresh MVV, which reports precision split by `generated_on_join`.

**Entity-key classification (Step 1).** A per-collection pattern in config, for
example `entity.collections: {"knowledge__herb-*": "CUST-\\d{4}|eid_[0-9a-f]{8}"}`,
and an optional caller-set `entity_key` on `store_put`, stamped into catalog
document meta (never chunk metadata, per RDR-236). One helper,
`classify_entity(doc) -> key | None`, serves the store_put stamp and the linker.
The same patterns find key tokens in evidence text
(`find_key_tokens(text, collection)`), which is what the repair uses. The glob
therefore serves two roles: it names the collections whose records get a title
stamp, and the collections whose text the repair scans for keys. The repair
looks the pattern up by each anchor's collection, so the collection holding the
referencing artifacts (HERB: the Slack messages) must be configured as well as
the one holding the records (147-research-11). Code is excluded; md/pdf
extractors are deferred (Step 1).

**Entity linker (Step 2, conditional).** If built:

- *Forward pass*: a batch hook regex-scans the new artifact's own `contents` for
  keys, looks each up with `find_by_title_exact`, and calls
  `link_if_absent(artifact, record, "mentions", "entity-linker")`. Exact, and
  needs no full-text index.
- *Reverse pass*: a `generate_mention_links` generator in `link_generator.py`,
  with a dry run, for entity records that arrive after the artifacts that name
  them. This replaces June's "find every artifact containing the token", which
  has no backing route (§Dependency Source Verification).
- Default off; `mentions` stays out of the default `traverse` types.

**What happens to the June template.** June's canonical plan was `search →
operator_filter → extract → resolve → traverse → generate`, saved with
dimensions `{verb: lookup, answer_shape: entity-name, evidence_shape:
entity-ref}`. It is withdrawn. The inline planner cannot emit `filter`; the
runner hands `filter` text items only, so tumblers do not carry through it;
`plan_save` takes no free dimensions; and `answer_shape` already names RDR-232's
terminal-text classifier. Step 0 also shows the filter is not needed on the issue
lookups (hop-1 precision 0.967). The one curated template built is for routing
(Step 4, branch c): it carries `hybrid-factual-lookup`'s steps, not June's chain.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve` step | `HttpCatalogClient.find_by_title_exact`, `resolve_many`; runner `_NON_EMBEDDING_TOOLS` | Add: a thin runner-native step over the existing exact lookup. New code (about 120 lines plus tests). |
| Resolution repair | `CoverageBoundary.run`, `coverage_repair`, `repair_stream` | Extend: an exact-key branch inside the existing one repair, and option 1's generation rule (`coverage_outcome.decide_outcome`, the notice in `mcp/core.py`). New code (about 300 lines plus tests). |
| Entity-key patterns and `entity_key` | `config.py` `_DEFAULTS`, `.nexus.yml`; catalog document meta | Add: config key, stamp on store_put, `classify_entity` helper (about 150 lines). |
| Routing for a `discovery/listing` reading (Step 4 branch c) | builtin plan YAMLs; `matcher.py` category route | Add: `entity-resolution.yml` with `hybrid-factual-lookup`'s steps (105 lines), and a `cosine-only` tag the category route skips (19 lines in `matcher.py`). |
| Graph expansion over `mentions` | `traverse` step | Reuse unchanged; it already runs as a `$ref`-seeded step. June's "extend the runner" was wrong. |
| `mentions` links | `link_if_absent`, `upsertLink` | Reuse, conditional on Step 2. |
| Key → artifact token match for the linker | none | Not FTS (there is no exhaustive token route). Forward pass reads the artifact's own text; reverse pass is a post-hoc generator. |
| Hop-1 precision filter | `operator_filter` | Not used. June's "no new code" was wrong for inline plans (not in `_ALLOWED_TOOLS`) and Step 0 shows it is not load-bearing. |
| Type-mismatch trigger in the planner prompt | none | Withdrawn; replaced by the resolution repair. |
| Deletion cascade for links | catalog-032 FK cascade; `graphBFS` live join | Reuse; pin with a test. No new cascade code. |

### Decision Rationale

The resolution repair is chosen because Step 0 locates the failure precisely:
the ids are retrieved, hop-1 precision on the issue lookups is high, and the
answer is declined at the coverage boundary. The boundary is where nexus already
decides "the evidence does not support this facet, try once more", so an
exact-key branch there acts at the moment of failure, on the evidence that
failed, with RDR-232's budgets and disclosure. A planner-side trigger would act
before the evidence exists, on a one-shot planner that cannot re-plan.

The entity linker is no longer the most defensible path for HERB. It answers a
different question (which artifacts mention this record), and on HERB the answer
needs only the record. It stays for corpora where the answer is one more link
away, and its cost (a write per mention, a maintenance burden on delete) is paid
only when such a corpus exists.

## Alternatives Considered

### Alternative 1: Bigger or better embedding model

**Description**: Replace bge-768 with a larger embedder to separate
near-identical entity records.

**Pros**: No architecture change.

**Cons**: Cannot work in principle: no single embedding represents every
relevance pattern as candidate sets grow (PRISM, Weller 2025). The collision is
an instance of that limit, not a model-quality artifact.

**Reason for rejection**: Treats a structural limit as a tuning knob.

### Alternative 2: Pure-LLM iterative re-query

**Description**: Let the agent re-query semantically for each extracted id.

**Pros**: No ingest-time work.

**Cons**: Verified to fail: a semantic re-query for `CUST-0035` returns other
records. RDR-232's existing repair is this alternative in effect (semantic
queries on facet text) and it did not help on HERB.

**Reason for rejection**: The failure we measured.

### Alternative 3: June's planner-side type-mismatch trigger

**Description**: Extend the inline planner's prompt to name an answer shape,
type the hop-1 evidence, and emit a resolution sub-plan, saved under new plan
dimensions.

**Pros**: Plans become reusable through `plan_match` (the lookup that reuses a
saved plan for a similar question).

**Cons**: The planner is one-shot, so it cannot see hop-1 evidence before
writing the plan. It cannot emit `filter`. `plan_save` has no dimensions
parameter, and the proposed `answer_shape` key collides with RDR-232.

**Reason for rejection**: Its seam no longer exists; the coverage boundary is
where evidence is seen.

### Briefly Rejected

- **Per-corpus hardcoded joins** (a SearchFlow-specific CUST → company step):
  failure-derived and not general.
- **Index entity records only, no exact resolution**: the June state; fuzzy
  search cannot find the exact record.
- **A `$in` `where=` grammar instead of a `resolve` step**: it would make one
  search step resolve many titles, but it gives no `missing` or `ambiguous`
  report and still returns chunks ranked by similarity. Not ruled out as a later
  convenience (nexus-4gzc8 closed unbuilt).

## Trade-offs

### Consequences

- (+) Works for any join whose entity records carry a stable key: store_put
  records titled by key, or collections with a configured key pattern. Code is
  out of scope; md/pdf need the deferred extractor. Shown on one corpus (HERB)
  and one question shape so far (Assumption #6).
- (+) Deterministic resolution, immune to embedding collision, with missing and
  ambiguous keys disclosed.
- (+) No planner change; the trigger lives in one runner-owned place with
  existing budgets.
- (−) The coverage boundary gains a second repair path to maintain.
- (−) Per-collection key patterns are configuration someone must write.
- (−) The scan reads the coverage anchors only, so a question whose keys sit in
  other retrieved chunks is still declined (the HERB backend question; Gap 3).
- (−) If the linker is built: an extra write per mention and link maintenance on
  delete.

### Risks and Mitigations

- **R1, the bar passes vacuously.** The script already beats the June bar.
  **Mitigation**: the MVV measures `nx_answer` itself and gates join
  completeness and precision ≥ 0.95; the script's F1 is reported beside it and
  cannot satisfy the MVV.
- **R2, repair noise.** RDR-232's semantic repair on "company names" pulls
  near-identical customer records, which could move recall by chance while
  precision falls. **Mitigation**: the ladder reports precision and F1 for
  `nx_answer`, and the resolution branch reports how many anchors it added.
- **R3, the repair may not enter (Assumption #5).** Retired by measurement: the
  repair enters on q2 to q7 (147-research-7).
- **R4, request kind.** "Find the names of companies …" is likely read as
  `unknown` by the fixed rules and decided by the model call. Measured on
  2026-11-06 (147-research-15): the call read `lookup` 13 times in 14, which
  leaves only `hybrid-factual-lookup` (a coverage boundary), and once
  `discovery/listing`, which leaves only the single-query listing templates, so
  `document-discovery` answered with no coverage boundary and the MVV run lost a
  question. **Mitigation**: Step 4 branch (c), a curated `discovery/listing`
  template with the coverage-bearing body, so either reading reaches a coverage
  boundary. Tagged `cosine-only`, it is never offered by the below-floor
  category route; on the cosine path it wins only at or above the 0.40 floor,
  and the FTS5 fallback, which ignores the tag, can still admit it. Accepted residual (Sam, 2026-11-06, 147-research-16): "Which documents
  mention the customers affected?" and two borderline wordings, read as
  `discovery/listing` or in the shadow regime, now get this template, a
  synthesized answer instead of a document list.
- **R5, answering from every held id may cost precision.** The evidence holds
  more customer ids than the script reads (on the issue questions, 4 to 10
  against 4 to 6). Measured with the joined items, precision was 0.9667 on the
  issue questions (147-research-9), above the MVV's 0.95; a corpus with noisier
  evidence may not hold it. The derived issue5 F1 of 0.823 was not reached
  (measured 0.7167) because the measured runs' evidence held fewer customer ids
  (§Step 0 measurements).
- **R6, hop-1 recall.** At k=20 hop 1 surfaces 4 to 6 of the 10 to 12 ids the
  cited artifacts name. Retrieving deeper raises recall (k=300 filtered to cited:
  recall 1.0) but needs precision filtering. Out of scope here; recorded as the
  next lever.
- **R7, company-level gold.** The gold counts every customer of a gold company
  as affected, so P3 precision overstates the customer-level figure. Named in the
  ladder, not fixed.
- **R8, `links_from` shows tombstoned endpoints.** Matters only if the linker
  ships and a consumer other than `traverse` reads mentions links.
- **R9, option 1 answers what the coverage check did not confirm.** An anchor
  that holds a key but does not concern the question now yields an answer
  instead of a decline. **Mitigation**: only with the switch on and at least one
  merged join; the notice says so; `no_grow` keeps such answers from growing a
  plan (a library plan that answered still has its success counted, as for a
  partial answer today); the MVV reports precision split by `generated_on_join`, and the held-out
  set counts answered outcomes as well as firings.

### Failure Modes

- **Visible**: `resolve` returns keys in `missing` or `ambiguous`; the repair
  report and run summary carry them, and the answer is generated from whatever
  did resolve, or, when nothing merged and the ledger is uncovered, declined as
  today.
- **Silent (if the linker ships)**: a stale `mentions` link after an entity
  record is trashed. `traverse` already skips tombstoned endpoints and `resolve`
  sees only live documents, so stale links cannot reach either; `links_from` and
  `links_to` can still show them (R8). Hard delete cascades.
- **Recovery**: the resolution repair and the linker are each behind a switch;
  off is today's behavior. The branch (c) template has no switch: `nx plan
  disable <id>` keeps it off on one install until a release edits its
  description (147-research-16).

## Implementation Plan

### Prerequisites

- [x] Step 0 measured and merged (nexus-3lu23; assetops-kg PR #3).
- [x] This revision gated (`/conexus:rdr-gate 147`) and accepted. No
  implementation bead starts before that.
- [x] Critical Assumption #5 measured (Step 4 sub-step 1, 2026-11-03,
  147-research-7): the repair enters; Step 4 takes branch (a). Branch (c) was
  rewritten as the routing fix after the 2026-11-06 MVV (147-research-15).

### Minimum Viable Validation

On the Step 0 sandbox and conditions (nx and engine at the versions under test,
bge-768, k=20, the same 1559 artifacts), with grown user plans disabled for the
run (a plan grown from one question otherwise captures later ones,
147-research-9), and two runs per question, the MVV gates what the exact-key join
controls (Sam, 2026-11-03, superseding the F1-against-the-script bar):

- **Join completeness:** on every issue question whose coverage anchors hold at
  least one entity key (the run's `anchor_key_ids` is non-empty: the domain, as
  the harness counts it; Sam, 2026-11-06, T2
  `nexus/rdr147-domain-decision-2026-11-06`), the answer names every company
  those keys resolve to (measured 10 of 10, 147-research-9). A question whose
  evidence holds ids but whose anchors hold none is outside the domain, and the
  run reports it. At most one in-domain question may miss per run, with at
  least 4 of the 5 issue questions in the domain (Sam, 2026-11-05,
  147-research-12: the 2026-11-05 miss was the re-check's variance on the same
  joined item).
- **Precision:** the companies the answer names have precision ≥ 0.95 against
  HERB ground truth, as the per-run mean over those of the 5 issue questions whose
  answer names at least one company; a question whose answer names none is left out
  (Sam, 2026-11-05, 147-research-12; measured 0.9667 on 2026-11-03,
  147-research-9).

Reported, not gated: issue5 company recall and F1 beside the script's
0.7642857142857142 (asserted exactly, as the diagnostic baseline), and precision
split by `generated_on_join`. Hop-1
retrieval recall, which with the declined backend question (Gap 3) sets the
remaining gap (issue5 F1 0.7167 on 2026-11-03; 0.500 in both runs of the final
MVV, 147-research-17), is separate later work (nexus-io7tw).

The measured system is `nx_answer`, through the harness's product rung (company
names read from `final_text` against the 10-name company universe). The script
rung stays as a diagnostic and cannot satisfy the MVV. The 2 aggregation
questions are reported (recall, precision, F1) and not gated. A run that clears
the bar only through the semantic repair, with the resolution branch not
entering, does not satisfy the MVV: the run summary must show the resolution
branch entered on the questions that pass. The bar as revised on 2026-11-05
applies to a fresh run on code that carries option 1 and branch (c), not to the 2026-11-05
rows: the first two runs predate the bar revision, and all four predate option
1's code.

### Benchmark Ladder — per-phase observable metrics

The harness (assetops-kg `benchmark/herb_ladder.py`) writes
`results/herb/herb-scorecard.json`; a copy is checked into nexus
(`docs/rdr/evidence/rdr-147/herb-scorecard.json`, the final MVV's) as the
regression baseline and updated per phase PR. Each number from a deterministic
rung (P2, P3, the E2E script) is asserted exactly, so silent drift fails; the
`nx_answer` rung's numbers come from model calls and are asserted against their
gate thresholds, not exactly (plan T2 `nexus/plan-rdr147-2026-11-03`).

| Rung | Metric | Graded against | Step 0 value | Target |
| --- | --- | --- | --- | --- |
| P1 (Step 2, conditional) | `mentions` link recall and precision | 37 artifact → customer-id pairs (`fixtures/herb/artifact_mentions.json`; all from Slack messages) | not built | recall 1.0, precision 1.0 |
| P2 (Step 3) | resolution accuracy | the 29 SearchFlow member customers (`p2_member_customers.json`) | 1.0 (by the harness's own exact-title lookup) | 1.0 through the `resolve` step, with `missing` and `ambiguous` empty |
| P3 (diagnostic) | hop-1 precision | question → affected customers | all7 0.6904761904761905; issue5 0.9666666666666666 | reported, not gated |
| E2E script (diagnostic) | company recall / precision / F1 | HERB ground truth | issue5 F1 0.7642857142857142 | pinned exactly; reported beside `nx_answer` |
| E2E `nx_answer` (MVV) | join completeness; company precision; recall / F1 reported | HERB ground truth | issue5 F1 0.0 | ≥ 4 of 5 issue questions in the domain, every resolved company named, at most one miss per run; per-run mean precision ≥ 0.95 over the questions whose answer names a company |
| Aggregation (reported) | company recall / precision / F1 on agg2 | HERB ground truth | script 0.0, `nx_answer` 0.0 | reported, not gated |

**Jointness, as measured.** On HERB the MVV needs: the question reaches a plan
with a coverage boundary under either request-kind reading (Step 4 branch c,
147-research-15) AND the resolution repair enters
(Assumption #5) AND `resolve` returns the right records (P2) AND the joined
items reach generation (since option 1 whether or not the re-check covers them;
it covered 11 of 15, 147-research-13) AND the generated answer names the
resolved companies. Hop-1 precision (P3) is not a factor on the
issue lookups at k=20. The linker (P1) is not on the path. Each phase review
asserts its own rung and reports the `nx_answer` rung honestly.

### Phase 1: Code Implementation

#### Step 0: Benchmark rebuild and baselines (done)

nexus-3lu23, merged as assetops-kg PR #3: PG sandbox, store_put ingest,
full-text reads with loud errors, P2 scoped to the 29 members, P1 gold as
artifact → customer-id pairs, an `nx_answer` product rung, and the baselines in
§Step 0 measurements.

#### Step 1: Entity-key classification and `entity_key` stamping

Per-collection key patterns in config; optional caller-set `entity_key` on
`store_put`, stamped into catalog document meta; the `classify_entity` helper.
`nx index repo` (code) is excluded: `rel_path:Lstart` titles are not keys.
**Deferred, named here so it is not dropped silently:** regex or NLP key
extraction for `nx index` markdown and `nx index pdf`, to a separate bead. A
`where entity_key=` filter is also out of this step; it would need `META_KEYS`
and pushdown changes in the engine.

#### Step 2: Entity linker (conditional)

Built only if the revision's gate or a later corpus keeps P1. Forward-pass batch
hook plus a reverse-pass generator with dry run, default off, `mentions` outside
the default traverse types. Re-verify Assumption #2 first.

#### Step 3: `resolve` step

The runner-native step of §Technical Design, with `missing` and `ambiguous`
reported. P2 is re-measured through it. June's second half ("enable `traverse` as
a `$ref` step") is dropped: it already works.

#### Step 4: Resolution repair in the coverage boundary

1. **Measure the entry state.** Done 2026-11-03 (147-research-7): on q2 to q7
   the one facet is `unsupported` / `no_supporting_evidence` and is a repair
   target; q1 does not reach the boundary.
2. **Branch on the result:**
   - (a) Facets are `unsupported` / `no_supporting_evidence` and the repair
     enters (the measured case, 147-research-7): add the exact-key branch to the
     existing repair as designed, merging joined items in place of the semantic
     facet queries and before the `no_new_evidence` returns (§Technical Design;
     147-research-9).
   - (b) Excluded by measurement: no facet is `source_missing` and no question
     names a source (147-research-7). Kept for the record: facets `source_missing`
     would mean the repair is skipped by RDR-232's rule.
     Find which named source was judged missing and why. If a product or
     component name in the question is being read as a missing document, that is
     a named-source detection defect, fixed under RDR-232's rules as its own bead,
     before the resolution branch can be measured. This RDR does not change
     RDR-232's rule that a missing named source decides its facets.
   - (c) Routing (built after the 2026-11-06 MVV, Sam "proceed.", T2
     `nexus/rdr147-branch-c-decision-2026-11-06`; 147-research-15). The MVV lost
     run a because the request-kind model call read one question as
     `discovery/listing`, and the only eligible plans for that kind were the
     single-query templates, which never reach the coverage boundary. Ship one
     curated builtin, `conexus/plans/builtin/entity-resolution.yml`
     (`strategy: entity-resolution`, `verb: research`, `scope: global`, tags
     including `builtin-template` and `cosine-only`), declaring `answer_kind: discovery/listing`, with the
     step body of `hybrid-factual-lookup.yml` (search, traverse, fetch, rank,
     generate), so a `discovery/listing` reading of these questions gets a plan
     with a coverage boundary and a `lookup` reading still gets
     `hybrid-factual-lookup`. Its match text names entity-listing requests ("Names
     of companies that were affected by issues in the product, and the clients
     hit by the outage. Name of the company with the most resolved bugs."); with
     the bundled MiniLM it scores 0.474 to 0.542 on the 7 questions, above the
     0.40 floor (147-research-16; the offline estimate before the YAML existed was 0.535
     to 0.643, 147-research-15, and the first committed wording scored 0.414 to
     0.545, review r1 of 7e76d5387). The
     `cosine-only` tag keeps it out of the below-floor category route
     (`matcher.py` `_is_cosine_only_plan`), where it stole requests that are not
     entity listings. No golden discovery request changes plan; the accepted
     residual is in R4.
     Deterministic tests over the real matcher and the YAML on disk pin: for each
     request kind (`lookup`, `discovery/listing`, `unknown`, `synthesis`, and the
     shadow regime) the HERB questions and paraphrases reach a plan that is not a
     single query; the golden discovery requests keep their plans; deleting the
     YAML brings plan 7 back. The builtin count pins move from 12 to 13. The
     earlier contingency text (`filter` in `_ALLOWED_TOOLS`, ids through
     `filter`) is not built: its trigger, hop-1 precision, did not fire (MVV
     precision 1.00). Then a fresh MVV run on a reseeded sandbox.
3. **Gate condition (Gap 4).** The resolution branch fires on the HERB issue
   questions and does not fire on a held-out set of single-hop questions; count
   both and record the counts.
4. **Generation after an entered join (option 1, Sam 2026-11-05;
   147-research-13).** Added after the 2026-11-05 MVV, where the re-check
   rejected 4 of 15 entered joins: when the branch entered and merged at least
   one joined item, an uncovered final ledger does not stop generation, and the
   answer carries the exact-key-join notice (§Technical Design, Generation after
   an entered join). Tests pin both sides: an entered join with an uncovered
   ledger generates, carries the notice with its not-confirmed clause, reports
   `generated_on_join`, and sets `no_grow`; a covered or partial answer from a
   merged join carries the notice without the clause; generated text that is
   not an answer keeps the `no_usable_answer` downgrade; with an uncovered
   ledger, a run where the branch did not enter, merged nothing, ran confined,
   or ran with the switch off is still a non-answer with no notice; a redacted
   row of a `generated_on_join` answer is not classed as a non-answer. The harness splits precision
   by `generated_on_join` and counts answered held-out outcomes. Then a fresh
   MVV run.

#### Step 5: Pin link deletion behavior

No new cascade code. Pin the existing hard-delete cascade with a test. Decide
whether `links_from`, `links_to` and `link_query` should filter tombstoned
endpoints as `traverse` does; this matters only if Step 2 ships.

### Phase 2: Operational Activation

**Turned on 2026-11-06 for the resolution repair** (Sam, nexus-bwct4.2; the bead
closes on develop CI):
`NX_ANSWER_RESOLUTION_REPAIR` is on by default; `0`, `false`, `off` or `no` turns
it off, and an unrecognised value reads as off. An install with no
`entity.collections` configured does not consider the branch at all, so its
repair and wire are as before RDR-147. The preconditions, as measured
(147-research-17): the MVV green, P2 29 of 29, the single-hop held-out set
entered 0 of 20, and precision on `generated_on_join` answers reported with
n = 0 in the final MVV (every entered join, 8 of 8, passed the re-check; the only live option-1
answers are 2 of 2 correct on 2026-11-06, 147-research-14). The linker stays off.

As planned: the resolution repair and the linker default off; enable on the HERB sandbox
first. Default-on for the resolution repair requires: the MVV green, P2 = 1.0
through `resolve`, Step 4's single-hop non-firing and answered counts recorded,
and the MVV's precision on `generated_on_join` answers reported (the switch now
also carries option 1's generation rule). The linker
has its own gate if built: P1 at its targets.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Entity-key patterns | config (`nx config`) | config | config | the repair's entered count in run summaries | config file |
| `mentions` links (if Step 2 ships) | `link_query` | `links_from` / `links_to` | hard delete cascades by FK; trash leaves links, which `traverse` skips | `link_audit` | catalog backup (existing) |

### New Dependencies

No new external packages. New components:

- `resolve` step: about 120 lines plus tests.
- Resolution repair branch in the coverage boundary, with option 1's generation
  rule and notice: about 300 lines plus tests.
- Entity-key config, `entity_key` stamp and `classify_entity`: about 150 lines.
- Step 4 branch (c): the `entity-resolution` builtin (105 lines of YAML) and
  the `cosine-only` tag in `matcher.py` (19 lines), plus tests.
- Entity linker, conditional: about 300 lines (forward hook, reverse generator,
  dry run).

Reused unchanged: `find_by_title_exact` and the by-title route, the coverage
boundary's budgets, `traverse`, the hook registry, catalog typed
links and the FK cascade.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles and the
proposed solution. The tension the round-2 gate named (the repair might never
enter on the HERB questions) is resolved by measurement: it enters on q2 to q7
(147-research-7). The round-3 gate found four sites that still stated the
retired F1-against-the-script bar; they now state the MVV's terms (join
completeness and precision gated, F1 reported; nexus-wunaf).

### Assumption Verification

#1, #3 and #4 are verified by source search on b7c423ea8 (research records
147-research-3 to 147-research-5). #2 (a post-store batch hook can write catalog
links) is to be re-verified on the PG engine, and only if the conditional linker
is built (Step 2). #5 was acknowledged unverified at gate round 2 (Sam,
2026-11-03; 147-research-6) and is now verified by spike (147-research-7). #6
(the re-check judges a joined item supporting) is verified by spike on HERB only
(147-research-8 and 147-research-9; counts in 147-research-10), with the caveats
listed under it; weakened on 2026-11-05 to 11 of 15 (147-research-12, -13), and
since option 1 the MVV no longer needs it.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `HttpCatalogClient.find_by_title_exact` / `GET /v1/catalog/by_title` | nexus client and engine | Source Search (b7c423ea8) |
| `traverse` plan step seeded by `$stepN.tumblers` | nexus plans runner | Source Search + Spike (June), re-read on b7c423ea8 |
| `CoverageBoundary.run` and its single repair | nexus plans (RDR-232) | Source Search (b7c423ea8) |
| `link_if_absent` / engine link upsert | nexus client and engine | Source Search (re-plan survey) |

### Scope Verification

The Minimum Viable Validation is in scope and is the Step 4 exit test: on the
Step 0 sandbox and conditions, with grown plans disabled and two runs per
question, on every issue question whose coverage anchors hold an entity key the
answer names every company those keys resolve to (Sam, 2026-11-06), with at
least 4 of the 5 issue questions in that domain and at most one miss per run, at precision ≥ 0.95 over those of the 5 issue
questions whose answer names a company (Sam, 2026-11-05); recall and F1 are reported beside
the script's 0.7642857142857142. The 2 aggregation questions are reported, not gated (Sam,
2026-11-03).

**Result (147-research-17):** passed on develop 1ecf3ca6d in the one invocation
of two runs on the final code, at the floor: 4 of 5 issue questions in the
anchor domain per run, every one complete, precision 1.00. This bar was revised
after earlier failing runs (Revision History); it gates the join, not retrieval:
issue5 F1 was 0.500 in both runs against the script's 0.764, and the backend
question stays outside the domain and declined. The two earlier 2026-11-06 runs
on code without branch (c) passed once and lost a question to routing once.

### Cross-Cutting Concerns

- **Versioning**: no wire change for `resolve` (it calls the existing by-title
  route); entity keys live in catalog document meta, so no schema change.
- **Deployment model**: client-only steps; the engine is unchanged unless a
  batch by-titles route is later needed (more than 300 tokens per call).
- **Incremental adoption**: the resolution repair is gated on an unsupported
  facet plus recognised key tokens, so other questions take today's path; since
  Phase 2 it is on by default, and an install with no `entity.collections`
  configured does not consider it at all. The linker is off by default. The branch (c) template is not behind a switch:
  entity-listing questions it wins take the multi-step plan on every install
  that has not run `nx plan disable` on it.
- **Memory management**: `resolve` holds one call's tokens and their manifests;
  bounded by the plan step's input.
- Build tool compatibility, Licensing, IDE compatibility, Secret/credential
  lifecycle: N/A.

### Proportionality

Right-sized for a redesign of three mechanisms and the validation bar. The
document is long because it carries measured numbers with their conditions and
the anchors an implementer needs; the implementation itself is about 570 lines
plus tests on the MVV path (resolve, the repair branch, entity keys), and about
125 more for the branch (c) routing template, with the linker conditional.

## Revision History

- 2026-06-05: gate PASSED (round 1).
- 2026-11-03: revised around the Step 0 measurements (nexus-t9npq); status
  deferred -> draft for the re-gate.
- 2026-11-03: Gate round 2 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); commit `68c697ec1`; critique `nexus_rdr/147-gate-critique-2026-11-03-r2`.
- 2026-11-03: Gate round 2 — BLOCKED (1 Critical, 1 Significant, 1 ship-blocker(s)); commit `0de44d26a`; critique `nexus_rdr/147-gate-critique-2026-11-03-r3`.
- 2026-11-03: Gate round 3 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `7e79312f0`; critique `nexus_rdr/147-gate-critique-2026-11-03-r4`.
- 2026-11-03: Accepted. Round 3 residual dispositions: S1 nexus-wunaf; S2 nexus-v3g1n; S3 nexus-nx6eq (Phase 1 Step 4).
- 2026-11-05: MVV bar revised after the 2026-11-05 run (nexus-irndi, Sam, T2 `nexus/rdr147-mvv-bar-revision-2026-11-05`, 147-research-12): one in-domain miss allowed per run; precision taken over the answers that name a company.
- 2026-11-05: option 1 taken after the MVV rerun (Sam, T2 `nexus/rdr147-option1-decision-2026-11-05`, 147-research-13): an entered exact-key join generates whatever the re-check says, with a notice; Assumption #6 weakened to 11 of 15; Step 4 sub-step 4 added. Fix-check observations of 80e18913a applied (floor in the table and Scope Verification, the 0.9667 citation, the fresh-run clause).
- 2026-11-05: fix-check 3e08dbecd findings applied: the option 1 outcome mapping (answered shape, `no_grow`, `generated_on_join`, its own notice), the notice scoped to uncovered answers, "merged at least one" marked as this RDR's reading, stale Assumption #6 and Jointness sites, R9, the precision-evidence caveat, the fresh-run clause, sizes.
- 2026-11-05: text brought into line with the option 1 code (69cc82790) and fix check b60dab40e: the notice leads every answer from a merged join (Sam's wording), the not-confirmed clause only when uncovered; `<n>` and `<keys>` from `joined_keys`; the answered shape only for answer text; the readers that consult `generated_on_join`.
- 2026-11-05: option 1 review round 1 applied (nexus-irndi): the readers sentence corrected (`answer_text_class` consults `generated_on_join`; `nx answer-runs` does not, it skips a leading join line; this line corrects the history line above it, which said both consult it); "the chosen option's text" for "Sam's wording" in the Approach; the precision caveat's template-7 clause (a listing that named no company, outside the mean; 11 company-naming runs, the 4 backend-question runs named none); sub-step 4's redacted-row test scoped to a `generated_on_join` answer; the generator gets no unresolved-facets note; precision split by `generated_on_join` reported, not gated.
- 2026-11-06: the join-completeness domain is the questions whose anchors hold a key (Sam, T2 `nexus/rdr147-domain-decision-2026-11-06`); branch (c) rewritten as the routing fix the 2026-11-06 MVV needs (Sam "proceed.", 147-research-15); R4 updated with the measured request kinds.
- 2026-11-06: wording follows the reviewed branch (c) code (c764cc95f, standing grant 3; 147-research-16): the routing template on the MVV path, its `cosine-only` tag and shipped match text and scores, no switch, the residual Sam accepted; and the anchor domain (Sam's 2026-11-06 decision) in Scope Verification.
- 2026-11-06: close-time critique (partial, 0 Critical, 3 Significant) answered in the text: the scan reads the coverage anchors, not all evidence, and the HERB backend question stays declined (Gap 3, Approach, Consequences); the final MVV result and its F1 (147-research-17) in Scope Verification and the ladder; the scorecard path; Phase 2 done (Sam, nexus-bwct4.2: on by default, silent with no entity config); prerequisite and Assumption #2 checkboxes dispositioned.
