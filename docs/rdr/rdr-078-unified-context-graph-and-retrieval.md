---
title: "RDR-078: Unified Context Graph & Retrieval — Projection Link Promotion, Surface Alignment, Scenario Skills"
status: draft
type: feature
priority: P2
created: 2026-04-14
related: [RDR-063, RDR-070, RDR-075, RDR-077, RDR-053]
reviewed-by: self
---

# RDR-078: Unified Context Graph & Retrieval

Follow-up to RDR-077 surfaced during the v4.3.0 live demo on the ART repo. Cross-corpus retrieval is currently a three-legged stool: the catalog link graph, the taxonomy topic graph, and the RDR-077 projection graph. Each is useful in isolation; each is walled off from the others. Agents working on planning, review, analysis, debugging, and documentation write the same composition glue repeatedly, and the glue always degrades at the same joint — the link graph is too sparse where semantic similarity is strong.

This RDR closes three gaps in one iteration: promote projection assignments into the catalog link graph so the existing `query(follow_links=...)` walks a dense graph, align the retrieval-tool surfaces so a single call can express topic-and-link constraints together, and add five thin scenario skills so agents reach for this machinery reflexively.

## Problem

### Problem 1: Catalog link graph is sparse where it matters most

The catalog heuristic linker (`nexus.catalog.link_generator.generate_code_rdr_links`) matches code-file *stem* against RDR *title* tokens. It produces `implements-heuristic` edges with acceptable precision but poor recall:

- Current coverage: **25.9%** of code entries have at least one link (nexus catalog stats, 2026-04-14).
- `rdr` coverage: **28.4%**; `knowledge` coverage: **11.9%**; `prose`: **0.1%**.
- Live ART demo (RDR-002 "Fix Vision→Language Priming in CrossModalIntegrationTest"): the linker produced exactly one edge — to `CrossModalIntegrationTest.java`. Semantic search surfaced three additional production classes (`LanguagePrimingSignal.java`, `LanguageToVisionPipeline.java`, `Phase4CrossModalPrimingTest.java`) that implement the RDR. None were linked because their filenames share no tokens with the RDR title.

The coverage gap is real, not theoretical — the filename heuristic cannot see through paraphrase. Every richer link generator we might build has to compute *something* that overlaps with raw semantic similarity. That compute already exists: RDR-077's `topic_assignments` table.

### Problem 2: Projection graph and link graph are disconnected

RDR-077 wired a full cross-collection projection pipeline. As of live state, `topic_assignments` holds projection rows with raw cosine `similarity`, `assigned_at`, and `source_collection`. The ICF-weighted threshold filter on write already gates out low-quality matches. `compute_icf_map()` exposes the hub-suppression signal at query time.

But nothing promotes those rows to catalog edges. A code chunk that projects into an RDR topic with adjusted similarity 0.82 is semantic-implements-level evidence and it sits in a SQLite table that `query(follow_links=...)` will never traverse.

The two graphs share the same node identifiers (tumblers, doc_ids, collection names). The bridge is mechanical.

### Problem 3: `search` and `query` MCP tools expose orthogonal subsets of the engine

Both tools call `search_cross_corpus()`. Neither exposes all of its capability.

| Capability | `search` (chunk-level) | `query` (document-level) |
|---|---|---|
| Corpus selection | yes | yes |
| Metadata filter (`where=`) | yes | yes |
| Topic pre-filter (`topic=`, RDR-070) | yes | no |
| Cluster output (`cluster_by="semantic"`) | yes | no |
| Pagination (`offset`) | yes | no |
| Catalog author filter | no | yes |
| Catalog content_type filter | no | yes |
| Catalog subtree filter | no | yes |
| Link traversal (`follow_links`, `depth`) | no | yes |

An agent that wants "RDRs linked to the current module, containing chunks in topic Y" cannot express it in one call. They must: (a) `query(follow_links=..., subtree=...)` to get the linked set, (b) `search(topic=...)` across the result's collection set, (c) intersect client-side. The two-call pattern loses topic-boost ranking fusion because neither tool knows about both constraints.

This is feature bifurcation, not feature redundancy. The tools differ in output granularity (chunks vs best-chunk-per-document); they should *not* differ in the retrieval constraints they accept.

### Problem 4: Agents don't reach for the graph for design, review, or analysis

`nx:query` exists and dispatches the query-planner agent — but by design only for *novel analytical pipelines* (multi-step extract / compare / generate). For the five everyday scenarios the user described — design/planning, critique/review, analysis/synthesis, dev/debug, documentation — there is no skill that tells the agent "call `query(follow_links=..., depth=2, topic=..., subtree=...)` before writing anything."

The primitives are usable today; the *reach* is the gap.

## Proposed Design

Three phases. Each stands alone and each unlocks value independently, but they compose.

### Phase 1: Projection → Catalog link promotion

New link generator `generate_semantic_implements_links(catalog, taxonomy)` in `src/nexus/catalog/link_generator.py`. Runs as part of `nx catalog link-generate`.

**Input:** `topic_assignments` rows where `assigned_by = 'projection'` and `source_collection IS NOT NULL`.

**Filter:** ICF-weighted adjusted similarity above a per-corpus-type floor. Reuses `CatalogTaxonomy.compute_icf_map()` so gate values stay consistent with what the projection writer itself uses. Rows whose target topic has ICF below a minimum threshold (i.e., hubs) are excluded — generic topics should not produce bridging edges.

**Mapping:** each qualifying row `(doc_id, topic_id, similarity, source_collection)` becomes:
- From: catalog entry whose `physical_collection == source_collection` and whose doc-level identity covers `doc_id` (the file the chunk belongs to).
- To: catalog entry that represents the target topic — pick the RDR/doc entry with the highest `doc_count` in the topic's owning collection.
- Link type: `semantic-implements` (new).
- `created_by`: `projection`.
- Metadata: `{raw_similarity, adjusted_similarity, source_chunk_id, topic_id}` — carried for later audit and recomputation.

**Idempotency:** prefer-higher UPSERT on `(from, to, link_type)`. Re-running the generator updates similarity without duplicating edges. Mirrors the RDR-077 Phase 2 write-path prefer-higher invariant.

**Chunk→document aggregation:** many chunks of the same file may project into the same topic. The generator aggregates by `(source_file, target_entry)` and emits one edge carrying `max(adjusted_similarity)` across the chunk set. Raw per-chunk evidence stays in `topic_assignments`; the catalog layer holds the aggregated judgment.

**Why a new link type and not `implements`:** agents and humans judge evidence differently. `implements` is a hand-curated assertion. `implements-heuristic` is filename overlap — cheap signal, high precision, low recall. `semantic-implements` is projection-derived — medium precision, high recall. Keeping them distinct lets `query(follow_links=...)` scope by confidence.

### Phase 2: Retrieval surface alignment

Additive. No deprecation. No breakage.

**Add to `search()` MCP tool** (chunk-level):
- `author: str = ""` — catalog author filter.
- `content_type: str = ""` — catalog content-type filter.
- `subtree: str = ""` — tumbler prefix scope.
- `follow_links: str = ""` + `depth: int = 1` — link graph expansion before semantic search.

**Add to `query()` MCP tool** (document-level):
- `topic: str = ""` — topic label pre-filter / boost (mirrors `search()`).
- `cluster_by: str = ""` — optional Ward/semantic grouping of documents.
- `offset: int = 0` — proper pagination.

**Default corpus alignment:** both tools default to `"knowledge,code,docs,rdr"`. (Currently `search` defaults to three, `query` to one — inconsistent.)

**Shared sub-engine:** factor the catalog-routing logic in `query()` into `nexus.search_engine.resolve_catalog_collections()` so `search()` can reuse it verbatim. The topic pre-filter is already in `search_cross_corpus()`; exposing it on `query()` is a one-line passthrough.

**Fusion ranking** (RDR-070 already has the machinery): when both topic filter and link traversal are set, the engine pre-filters to linked collections, applies topic boost during ranking, and returns results in fused distance order. No new math — the combinations are independent filters over the same chunk set.

**The output-granularity distinction remains the one reason to pick one tool over the other:** `search` for "where are the matching pieces," `query` for "which documents match." Every other retrieval constraint works on both.

### Phase 3: Scenario skills

Five `nx` plugin skills, one markdown file each. Each teaches the agent exactly which `search()` / `query()` shape to use for one of the five scenarios the user identified. These are triggers + templates, not new code.

| Skill | Scenario | Trigger | Core instruction |
|---|---|---|---|
| `nx:research-plan` | Design / arch / planning | User asks to build a plan, design a feature, write an RDR, extend a subsystem | Before writing: `query(question=..., follow_links="semantic-implements", depth=2, subtree=<module>)` to gather prior art + linked code. Then `search(topic=..., corpus="rdr")` for related decisions. |
| `nx:review-context` | Critique / audit / review | User asks to review a PR, audit a design, critique a document | Before critique: `query(question=<summary>, follow_links="implements,semantic-implements,supersedes", depth=2)` to surface authoring decisions, prior art, related incidents. |
| `nx:analyze-corpus` | Analysis / synthesis / research | User asks to analyze across a corpus, compare approaches, synthesize findings | `query(question=..., topic=..., follow_links="cites", depth=2)` with `subtree` scoping; consider dispatching `nx:query` planner for multi-step extract/compare. |
| `nx:debug-context` | Dev / debug | User asks why code looks a certain way, what an error means, how a subsystem works | `catalog links-for-file <path>` → RDRs explaining *why*; Serena for symbol navigation covers *what*. |
| `nx:doc-scope` | Documentation | User asks what code a doc should reference, what docs a code file needs | `query(question=..., follow_links="cites,documented-by", depth=1)` for existing references; `catalog suggest-links` for candidates. |

Each skill is ~50 lines of markdown with a triggering-condition block, the `query` / `search` / `catalog` invocation template, and a short rationale. The query-planner agent stays for analytical pipelines; these skills cover the 80% design/review/analyze workflows that don't need one.

## Success Criteria

- **SC-1** — `generate_semantic_implements_links` generator lands in `link_generator.py` with per-corpus-type ICF-adjusted similarity thresholds (code__* 0.60, knowledge__* 0.45, docs__*/rdr__* 0.50; these are starting points — PQ-1).
- **SC-2** — Running `nx catalog link-generate` on the current workspace increases code-entry link coverage from 25.9% to ≥ 60% without introducing more than 5% false-positive bridges (measured against a 50-pair hand-labeled audit set — RF-1).
- **SC-3** — `semantic-implements` edges carry metadata: `raw_similarity`, `adjusted_similarity`, `source_chunk_id`, `topic_id`. `nx catalog show <tumbler>` renders them distinctly from `implements-heuristic`.
- **SC-4** — `search()` accepts `author`, `content_type`, `subtree`, `follow_links`, `depth` with the same semantics as `query()`. Same question against the same collection set with the same catalog filter returns overlapping results between the two tools (chunk subset ⊆ document's chunk pool).
- **SC-5** — `query()` accepts `topic`, `cluster_by`, `offset`. A `topic=` pre-filter on `query()` produces the same document set that `search(topic=...)` followed by doc-level grouping would.
- **SC-6** — Both tools default to `"knowledge,code,docs,rdr"`.
- **SC-7** — Five scenario skills ship in the `nx` plugin. Each has a triggering-condition block, an invocation template, and a worked example. `plugin-dev:skill-reviewer` passes each one.
- **SC-8** — RDR-002 ART live re-demo: `query(question="vision language priming", follow_links="semantic-implements", depth=2, subtree="1.11")` returns RDR-002 **plus** `LanguagePrimingSignal.java`, `LanguageToVisionPipeline.java`, and `Phase4CrossModalPrimingTest.java` in one call. Current live result: RDR-002 and one test file.
- **SC-9** — Prefer-higher UPSERT on `semantic-implements` edges preserved across re-runs. Lowering the ICF gate threshold does not delete existing higher-confidence edges.
- **SC-10** — Zero regressions: full test suite green before tagging v4.4.0.

## Research Findings

- **RF-1** — The filename-overlap heuristic has measurable recall ceiling. For a 50-pair hand-curated ART RDR↔code set (to be authored in Phase 1 pre-work), current linker should recover ≤ 30% of pairs; projection-promoted edges should push that to ≥ 80% at the proposed thresholds.
- **RF-2** — ICF gating already handles the hub-suppression problem. Topics with `ICF == 0` (DF == N_effective) contribute nothing to `semantic-implements` promotion because `adjusted_similarity = raw * ICF = 0 < threshold` for any `threshold > 0`. The hub problem that motivated RDR-077 is exactly the noise this generator must not amplify, and RDR-077's math does the right thing by construction.
- **RF-3** — `search_cross_corpus()` in `src/nexus/search_engine.py` already accepts `topic=`, `catalog=`, `taxonomy=` kwargs — the chunks-side topic boost (RDR-070) is live. Exposing `topic` on `query()` is a parameter-passthrough, not a new algorithm. `follow_links` on `search()` is similar: the catalog-collection-resolution logic already exists in `mcp/core.py:240-291` and needs factoring to a helper.
- **RF-4** — Link type disambiguation is supported by the existing catalog model. `link_generator.generate_citation_links` and `generate_code_rdr_links` write distinct `link_type` values. Adding `semantic-implements` is additive.
- **RF-5** — The chunk→file aggregation step matters. 63,101 chunks projected in the live demo; a per-chunk link emission would produce thousands of edges per file pair. Per-`(source_file, target_entry)` aggregation with `MAX(adjusted_similarity)` keeps the graph navigable and preserves the evidence pointer to the top chunk.
- **RF-6** — The user's own observation: "the query planner was supposed to be doing this with the knowledge graph." The primitive is already there (`query(follow_links=..., depth=N)`); what makes it feel absent is that the *graph being walked* is sparse. Phase 1 densifies the graph. Phase 2 widens the surface that reaches it. Phase 3 trains the agent layer.

## Proposed Questions

- **PQ-1** — Starting per-corpus-type ICF-adjusted similarity thresholds are chosen conservatively. Calibration against RF-1's hand-labeled set in implementation may move them. Threshold table becomes a tunable — possibly `.nexus.yml` exposure in a follow-on RDR (same PQ-2 open question that RDR-077 left unresolved).
- **PQ-2** — Should `semantic-implements` edges decay? Centroid drift after re-discover can leave stale edges. Options: (a) re-run generator on `taxonomy-meta.last_discover_at` change, (b) TTL on the edge, (c) lazy recomputation during `nx catalog link-audit`. Defer to implementation review.
- **PQ-3** — The chunk→file aggregation picks `MAX(adjusted_similarity)` as the edge weight. Alternative: `AVG` (more stable, less sensitive to outliers) or `weighted_sum` (bias toward file pairs with many corroborating chunks). MAX is simplest and easiest to explain; if query-time ranking suffers we revisit.
- **PQ-4** — `documented-by` link type appears in the `nx:doc-scope` skill example but is not proposed here. Do we need it as a distinct type, or is `cites` sufficient? Split on clarity of agent guidance; leave for a follow-up RDR once the skill lands and tells us what shape the gap takes.
- **PQ-5** — Scenario skills may overlap with the existing `nx:research-synthesis` skill. Audit that one during Phase 3 and either absorb or cross-reference. Don't duplicate.
- **PQ-6** — Should the surface alignment deprecate `query()` or `search()` in the long run? This RDR says no; every call site that picks one over the other does so on output granularity, which is a real distinction. A future RDR may revisit if the duplication costs exceed the readability benefit. For now, alignment is cheaper than unification.

## Related

- **RDR-063** — Catalog domain split and link-graph origins.
- **RDR-070** — HDBSCAN topic discovery; `topic` pre-filter and boost machinery this RDR extends to `query()`.
- **RDR-075** — Cross-collection projection plumbing (what RDR-077 added similarity scores to).
- **RDR-077** — Projection quality, similarity storage, ICF computation. This RDR's Phase 1 is the logical continuation — promoting RDR-077's signal into the graph `query()` walks.
- **RDR-053** — Xanadu-in-Nexus link-graph design doctrine.

## Out of Scope (deferred)

- Unified `retrieve()` tool with `mode="chunks"|"documents"` (option 2 from the design discussion). Reviewed; rejected for this iteration because feature alignment (option 1) delivers the same agent capability without breakage.
- `query`-planner agent changes. The existing analytical-pipeline agent stays unchanged; scenario skills cover the 80% workflow that doesn't need decomposition.
- Link graph visualization / UI.
- Per-project configuration of hub stopwords (PQ-3 from RDR-077).
