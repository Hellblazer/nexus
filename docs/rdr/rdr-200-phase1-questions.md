---
title: "RDR-200 Phase 1 Frozen Question Set (nexus-4e75w.7 gate input)"
parent_prereg: docs/rdr/rdr-200-phase1-prereg.md
parent_rdr: RDR-200
parent_bead: nexus-4e75w.7
created: 2026-09-01
status: frozen
---

# RDR-200 Phase 1 Frozen Question Set

FROZEN at initial commit. Per the pre-registration's revision rule
(prereg §1): any later change to this set — question text, stratum
assignment, membership — is permitted only BEFORE any arm runs, and only
as a dated entry under a `## Revision History` section appended to this
file. Nothing here is ever revised after Phase 1 results exist.

## Assembly method (executed 2026-09-01, bead nexus-4e75w.7)

- **Client tree at assembly:** develop checkout at commit
  `936bd94d64e45e8d9ae5c0b6f275da3e64017973`.
- **Candidate pool (32):** real question texts mined from the `answer_runs`
  history (`uv run nx answer-runs --limit 300 --json`, 214 rows, engine
  api.conexus-nexus.com release 0.1.93) — the two shapes the prereg names:
  12 paper-corpus questions (the May paper-corpus era plus its Jul-Aug
  successors over the same knowledge collections) and 20 RDR-research /
  cross-corpus questions. Every candidate is a verbatim history question;
  provenance below names the `answer_runs` row id and date. No question
  was invented for this set.
- **Answerability screen (before scoring):** subjects verified present in
  today's indexed corpora by targeted `search` probes. History questions
  EXCLUDED as unanswerable (sources not indexed today): the Grossberg-paper
  questions (ids 57/59/117/119 — no Grossberg papers in any knowledge
  collection), Parmar 2026 5-primitive workflow-engine questions (154-156),
  the SEP-1865/SEP-1686/MCP-spec verbatim-quote questions (157-161 — the
  spec text itself is not indexed; only secondhand exploration docs),
  Adams permutohedral-lattice (167), qwen-coprocessor-stack RDR-002
  questions (148-150), and the stored-review-document question (163, TTL
  risk). Honesty note: the May paper-corpus era therefore contributes its
  SFC/mesh cluster (Knapp/Burstedde-Holke/p4est, all verified present in
  `knowledge__dt-papers`) rather than its full breadth; the paper-corpus
  shape was filled out with the Jul-Aug organic paper-corpus questions
  (semantic-operators / Delos / agent-memory collections), which are the
  same shape over the same corpus family.
- **Crowding procedure (prereg §2, verbatim):** for each candidate, ONE
  plain flat `search` MCP-tool call, default corpus fan-out
  (`knowledge,code,docs` — note `rdr__` collections are NOT in the default
  fan-out), default threshold (0.65), `limit=10`, query = the question text
  verbatim. All 32 calls returned a full 10 results. The 10 results (rank,
  distance, collection, source, snippet as rendered by the tool) were then
  labeled relevant/irrelevant by the judge model in ONE `claude -p
  --model opus` dispatch per question with a strict-JSON schema, blind (the
  judge saw only the question and the 10 results — no arm or stratum
  context). Crowding score = irrelevant fraction of the top-10; crowded =
  score >= 0.5 (OQ-7 pin, T2 [23922]).
- **Judge model id (exact, from every dispatch envelope's `modelUsage`):**
  `claude-opus-5` (alias `opus` passed to `claude -p`). 32/32 dispatches
  succeeded; 0 retries needed.
- **Judge instruction (verbatim relevance definition):** a result is
  RELEVANT if the document it comes from plausibly contains material that
  helps answer the specific question as asked — same subject matter and a
  source actually capable of answering it (a question about what a paper
  says needs the paper or a document quoting it, not merely code that
  implements similar ideas; a question about how a codebase works needs
  that codebase's code/docs). IRRELEVANT = adjacent topic, different
  project/domain, vocabulary overlap, or coincidental term match.
- **Selection rule (pre-committed before scores were parsed):** up to 12
  per stratum in candidate-id order; extend the pool only if a stratum
  falls below 8. Final pool split 19 crowded / 13 clean, so no extension
  was needed; the frozen set is 12 crowded + 12 clean. The 8 scored-but-
  unselected candidates are listed at the bottom as the audited overflow
  pool (first resort, in id order, if a frozen question is ever invalidated
  via a dated revision).

## Prereg §7 residuals discharged at set-freeze

- **Per-operator model table re-verified** against
  `src/nexus/operators/model_tiers.py` at commit `936bd94d6` (assembly
  HEAD): UNCHANGED from the prereg §3 transcription at `3c3c10321`.
  `FLIPPED_OPERATORS` = {extract, filter, groupby, rank, check, verify} ->
  cheap alias `haiku`; aggregate / summarize / generate / compare ->
  `STRONG_DEFAULT_ALIAS` = `opus`; bundles pinned to `opus`
  unconditionally. No prereg Revision History entry required (no drift).
- **Arm session-model id (continuation-reducer / caller-only-reasoner):**
  TO-RECORD-AT-ARM-RUN — the orchestrator session that runs the arms must
  record its exact session-model id as a dated Revision History entry in
  the prereg BEFORE the first arm runs (prereg §7(b)). Not recordable at
  set-assembly: the assembly agent's session is not the arm session.

## Frozen set — stratum summary

- **Crowded** (score >= 0.5): 12 questions (q01-q12).
- **Clean** (score < 0.5): 12 questions (q13-q24).
- Score distribution across the full 32-candidate pool:
  0.0: 2, 0.1: 4, 0.2: 1, 0.3: 3, 0.4: 3, 0.5: 6, 0.6: 1, 0.7: 1, 0.9: 1, 1.0: 10
- Shape mix of the frozen set: 12 paper-corpus + 12 rdr-research overall;
  the crowded stratum is 11 paper-corpus + 1 rdr-research and the clean
  stratum 1 paper-corpus + 11 rdr-research. That confound is the corpus's
  own structure on the default fan-out (paper questions are what flat
  search drowns in code), not a selection artifact; it is recorded here so
  the gate report can discuss it rather than discover it.

## Crowded stratum (q01-q12)

### q01 [crowded, score 1.0] (pool id c01)

**Text (verbatim, frozen):** In the Knapp 2026 pyramid SFC paper section 3, what is the explicit pyramid Morton index encoding? Specifically, how are 6 bits per level split between child id (or coordinates) and type? Cite equation numbers verbatim. Also explain min_tet_level field and what it short-circuits.

**Provenance:** answer_runs row id 170, asked 2026-05-28; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0885] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/SimpleTMIndex.java:1-13 | irrelevant | Java code, not Knapp paper section 3 |
| 2 | [0.1057] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:117-142 | irrelevant | Code citing Knapp Table 3.1, not paper text |
| 3 | [0.1075] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/PyramidKeyCodec.java:145-165 | irrelevant | Codec implementation; question asks paper equations |
| 4 | [0.1110] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/MinTetLevelReinjectionTest.java:154-169 | irrelevant | Test code touching minTetLevel, not paper |
| 5 | [0.1167] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/PyramidTetLeaf12DOPTest.java:45-59 | irrelevant | Test fixture code, no paper content |
| 6 | [0.1179] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:84-115 | irrelevant | t8code child-type tables, not paper section 3 |
| 7 | [0.1197] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:230-266 | irrelevant | Face connectivity tables, unrelated to encoding equations |
| 8 | [0.1199] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/PyramidKeyCodec.java:128-144 | irrelevant | Codec code mentioning minTetLevel, not paper |
| 9 | [0.1221] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/Pyramid.java:169-197 | irrelevant | Pyramid parent code, not paper equations |
| 10 | [0.1236] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/ExtendedTetreeKey.java:23-46 | irrelevant | Tetree key bit layout code, not paper |

### q02 [crowded, score 1.0] (pool id c02)

**Text (verbatim, frozen):** In Knapp 2026 Section 4, what are the explicit algorithms and complexities for pyramid parent, child, and face_neighbor operations? Cite algorithm numbers and tables. Explain the min_tet_level field role. How does branching by type (6/7 + tet 0/3) work?

**Provenance:** answer_runs row id 171, asked 2026-05-28; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1076] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/HybridFaceNeighborTest.java:68-84 | irrelevant | Java test code, not Knapp paper Section 4 |
| 2 | [0.1116] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/T8codeDpyramidTetBoundaryOracleTest.java:196-226 | irrelevant | Test oracle code, not the paper text |
| 3 | [0.1165] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/T8codeDpyramidTetBoundaryOracleTest.java:227-249 | irrelevant | Test oracle code, cannot cite paper algorithms |
| 4 | [0.1231] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/PyramidAddNeighboringNodesTest.java:301-331 | irrelevant | Test helper code, not paper content |
| 5 | [0.1238] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/HybridTraversalIntegrationTest.java:64-85 | irrelevant | Integration test, not paper Section 4 |
| 6 | [0.1269] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:230-266 | irrelevant | Connectivity table code, not paper text |
| 7 | [0.1285] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/Pyramid.java:169-197 | irrelevant | Implementation of parent(), not the paper |
| 8 | [0.1324] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/tetree/TetParentElementTest.java:43-65 | irrelevant | Test code, not paper algorithms or tables |
| 9 | [0.1341] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/T8codeDpyramidFaceOracleTest.java:270-295 | irrelevant | Test oracle code, not paper source |
| 10 | [0.1341] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:117-142 | irrelevant | Code cites Table 3.1, not Section 4 text |

### q03 [crowded, score 1.0] (pool id c03)

**Text (verbatim, frozen):** In Burstedde Wilcox Ghattas 2011 p4est paper, what is the forest contract for partition, 2:1 balance, and ghost layer? Describe the algorithms at a high level — what guarantees do they provide?

**Provenance:** answer_runs row id 173, asked 2026-05-28; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0982] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ParallelBalancer.java:46-70 | irrelevant | Java balancer code, not p4est paper |
| 2 | [0.1072] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ParallelBalancer.java:24-45 | irrelevant | Different project's balancing interface, not paper |
| 3 | [0.1258] code__1-28 \| tutorials/general/t8_step4.h:1-30 | irrelevant | t8code tutorial header, not p4est paper |
| 4 | [0.1326] code__1-28 \| tutorials/general/t8_step4_partition_balance_ghost.cxx:123-124 | irrelevant | t8code implementation, not the paper's contract |
| 5 | [0.1329] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ParallelBalancer.java:131-167 | irrelevant | Java distributed forest API, coincidental vocabulary |
| 6 | [0.1349] code__1-28 \| src/t8_forest/t8_forest_balance.h:1-28 | irrelevant | t8code balance header, not p4est paper text |
| 7 | [0.1379] code__1-28 \| src/t8_forest/t8_forest_balance.cxx:225-254 | irrelevant | t8code balance implementation, not paper description |
| 8 | [0.1386] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/DefaultParallelBalancerPhase1Test.java:29-70 | irrelevant | Java test imports, unrelated to paper |
| 9 | [0.1388] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/DefaultParallelBalancerPhase1Test.java:289-312 | irrelevant | Java ghost extraction test, not paper content |
| 10 | [0.1389] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/MultiPartitionIntegrationTest.java:31-45 | irrelevant | Java integration tests, different codebase |

### q04 [crowded, score 1.0] (pool id c04)

**Text (verbatim, frozen):** In Knapp 2026 paper §3, what is the geometric definition of pyramid types 6 and 7? How does type 7 relate to type 6 (180-degree rotation?). What does the pyramidal subdivision yield - 6 pyramidal children + 4 tetrahedral children = 10 children total, with what positions? Cite explicit text from §3.

**Provenance:** answer_runs row id 174, asked 2026-05-28; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1069] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:84-115 | irrelevant | Java child-type tables, not Knapp paper text |
| 2 | [0.1116] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:117-142 | irrelevant | Code citing Table 3.1; not paper §3 text |
| 3 | [0.1401] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:230-266 | irrelevant | Face-childid lookup code, no paper prose |
| 4 | [0.1471] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:144-166 | irrelevant | Code for §4.4 face neighbors, not §3 text |
| 5 | [0.1488] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:167-191 | irrelevant | Implementation table, not quotable §3 paper text |
| 6 | [0.1497] code__1-28 \| src/t8_schemes/t8_default/t8_default_pyramid/t8_dpyramid_connectivity.c:80-113 | irrelevant | t8code C source, different project, no paper text |
| 7 | [0.1499] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/Pyramid.java:169-197 | irrelevant | Java parent computation code, not paper §3 |
| 8 | [0.1588] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/PyramidNavigationTest.java:52-78 | irrelevant | Test code comparing to t8code, not paper |
| 9 | [0.1607] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/tetree/PyramidConnectivityTest.java:1-20 | irrelevant | Test file header/imports, no §3 content |
| 10 | [0.1615] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/PyramidNavigationTest.java:1-27 | irrelevant | Test file boilerplate, no paper definitions |

### q05 [crowded, score 0.6] (pool id c05)

**Text (verbatim, frozen):** Compare Burstedde+Holke 2016 (tetrahedral SFC for nonconforming adaptive meshes) and Knapp 2026 (pyramid SFC). What does Knapp 2026 REUSE unchanged from Burstedde+Holke 2016 (tet types 0-5, Bey refinement, TM-index)? What does Knapp 2026 ADD (pyramid types 6-7, 10-child subdivision, min_tet_level field, 6D Morton embedding)?

**Provenance:** answer_runs row id 175, asked 2026-05-28; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 6/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0817] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:84-115 | relevant | Bey child-type table with explicit t8code/tet-type provenance note |
| 2 | [0.1027] code__1-28 \| src/t8_schemes/t8_default/t8_default_pyramid/t8_dpyramid_connectivity.c:80-113 | relevant | Pyramid types 6-7 connectivity, Knapp scheme reference implementation |
| 3 | [0.1030] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:117-142 | relevant | Cites Knapp Table 3.1; 10-child pyramid subdivision |
| 4 | [0.1043] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:230-266 | irrelevant | Face-child inside table; not reuse/addition comparison |
| 5 | [0.1130] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/HybridFaceNeighborTest.java:68-84 | irrelevant | Neighbor-finding test; no scheme-provenance content |
| 6 | [0.1151] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/pyramid/PyramidSubdivisionStrategy.java:1-33 | irrelevant | License header and imports only |
| 7 | [0.1190] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/pyramid/PyramidAddNeighboringNodesTest.java:301-331 | irrelevant | Test helper for neighbor search, unrelated to comparison |
| 8 | [0.1209] code__1-28 \| src/t8_schemes/t8_default/t8_default_tet/t8_dtet_connectivity.c:38-82 | relevant | Canonical tet types 0-5 Bey refinement table |
| 9 | [0.1222] code__1-28 \| src/t8_schemes/t8_default/t8_default_pyramid/t8_dpyramid_connectivity.c:1-38 | irrelevant | t8code file license boilerplate, no substantive content |
| 10 | [0.1230] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/tetree/TetreeConnectivity.java:193-213 | irrelevant | Hybrid boundary-element typing; adjacent vocabulary only |

### q06 [crowded, score 0.7] (pool id c07)

**Text (verbatim, frozen):** What techniques improve memory efficiency in LLM agent systems?

**Provenance:** answer_runs row id 178, asked 2026-05-30; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 7/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.2433] knowledge__agent-memory \| Memory in the LLM Era  Modular Architectures and Strategies in a Unified Framework | relevant | LLM-era memory architectures survey, directly on topic |
| 2 | [0.2522] code__1-2 \| chatsome-modules/chatsome-language/src/test/java/com/hellblazer/art/chatsome/language/nlp/benchmark/grossberg/PerformanceBenchmarkTest.java:197-199 | irrelevant | Java NLP benchmark test, unrelated domain |
| 3 | [0.2529] knowledge__agent-memory \| Memory in the LLM Era  Modular Architectures and Strategies in a Unified Framework | relevant | Same LLM agent memory survey paper |
| 4 | [0.2588] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/sfc/OctreeVsSFCArrayBenchmark.java:273-273 | irrelevant | Octree SFC benchmark memory usage, unrelated |
| 5 | [0.2613] code__1-15 \| samples/agent/adk/custom-components-example/agent.py:145-182 | irrelevant | ADK runner wiring, no memory-efficiency technique |
| 6 | [0.2629] code__1-15 \| samples/agent/adk/mcp_app_proxy/agent.py:173-216 | irrelevant | ADK proxy agent plumbing, not memory efficiency |
| 7 | [0.2649] knowledge__dt-papers \| MemForest  An Efficient Agent Memory System with Hierarchical Temporal Indexing | relevant | MemForest efficient agent memory system, directly answers |
| 8 | [0.2667] code__1-15 \| samples/agent/adk/orchestrator/agent.py:187-193 | irrelevant | Orchestrator agent construction, unrelated to memory efficiency |
| 9 | [0.2706] code__1-2 \| art-modules/art-performance/src/main/java/com/hellblazer/art/performance/algorithms/VectorizedSalienceART.java:225-247 | irrelevant | ART sparse-mode metric, different domain entirely |
| 10 | [0.2716] code__1-2 \| art-modules/art-core/src/test/java/com/hellblazer/art/test/convergence/ScalabilityTest.java:132-134 | irrelevant | ART scalability test, unrelated to LLM agents |

### q07 [crowded, score 0.5] (pool id c08)

**Text (verbatim, frozen):** Compare the pipeline-compilation approach of NL2Pipe with semantic caching approaches for LLM query planning

**Provenance:** answer_runs row id 371, asked 2026-07-14; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 5/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1724] docs__1-1 \| papers/2512.11001.pdf:page-6 | relevant | Paper text on semantic caching for agent invocations |
| 2 | [0.1754] docs__1-1 \| papers/2512.11001.pdf:page-5 | relevant | Same paper's semantic caching related-work discussion |
| 3 | [0.1838] code__1-27 \| src/arcaneum/search/embedder.py:1-8 | irrelevant | Embedding cache code; vocabulary overlap only |
| 4 | [0.1964] knowledge__semantic-operators \| Rethinking Query Optimization for Multi-Agent Systems [Vision] | relevant | Vision paper section on semantic caching across pipelines |
| 5 | [0.1967] knowledge__dt-papers \| CacheRAG  A Semantic Caching System for Retrieval Augmented Generation in Knowledge Graph Question Answering | relevant | Semantic caching system with LLM planner routing |
| 6 | [0.2009] code__1-1 \| tests/test_document_lookup_seed_plans.py:1-25 | irrelevant | Nexus plan-seeding tests, not the papers |
| 7 | [0.2014] code__1-1 \| tests/test_document_lookup_seed_plans.py:149-168 | irrelevant | Nexus test file about synthesis question shapes |
| 8 | [0.2037] knowledge__semantic-operators \| Bridge the Last Mile Gap to Semantic Analytics  Compiling Natural Language Queries into Semantic Operator Pipelines | relevant | This is the NL2Pipe compilation paper itself |
| 9 | [0.2051] code__1-1 \| tests/test_document_lookup_seed_plans.py:192-216 | irrelevant | Nexus test guard code, different subject |
| 10 | [0.2061] code__1-1 \| scripts/spikes/spike_rdr090_5q.py:334-343 | irrelevant | Local spike script on plan-library routing |

### q08 [crowded, score 0.5] (pool id c09)

**Text (verbatim, frozen):** Which indexed papers discuss intermediate representations for compiling natural-language queries?

**Provenance:** answer_runs row id 372, asked 2026-07-25; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 5/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.2593] knowledge__semantic-operators \| Natural Language to What  A Vision for Intermediate Representations in NL to X Querying | relevant | The paper on IRs for NL querying itself |
| 2 | [0.2715] code__1-2 \| chatsome-modules/chatsome-language/src/main/java/com/hellblazer/art/chatsome/language/sovereign/SemanticQueryRouter.java:25-25 | irrelevant | Unrelated Java code, vocabulary overlap only |
| 3 | [0.2840] code__1-1 \| scripts/spikes/spike_rdr090_5q.py:95-127 | irrelevant | Spike query fixture, not a paper |
| 4 | [0.2850] code__1-1 \| scripts/spikes/spike_b_queries.py:202-238 | irrelevant | Test query fixtures, unrelated project code |
| 5 | [0.2867] knowledge__semantic-operators \| Natural Language to What  A Vision for Intermediate Representations in NL to X Querying | relevant | Same paper; states NL query translation target |
| 6 | [0.2915] code__1-2 \| chatsome-modules/chatsome-vision/src/test/java/com/hellblazer/art/chatsome/vision/integration/testdata/TextQueryGenerator.java:1-17 | irrelevant | Vision test data generator, coincidental term overlap |
| 7 | [0.2926] knowledge__semantic-operators \| Natural Language to What  A Vision for Intermediate Representations in NL to X Querying | relevant | Same paper's abstract/keywords on intermediate representations |
| 8 | [0.2987] code__1-1 \| tests/test_document_lookup_seed_plans.py:149-168 | irrelevant | Repo test file about plan composition |
| 9 | [0.3059] knowledge__semantic-operators \| Natural Language to What  A Vision for Intermediate Representations in NL to X Querying | relevant | Same paper discussing NL query target definition |
| 10 | [0.3064] knowledge__semantic-operators \| Natural Language to What  A Vision for Intermediate Representations in NL to X Querying | relevant | References chunk of the same relevant paper |

### q09 [crowded, score 1.0] (pool id c10)

**Text (verbatim, frozen):** How do the distributed systems papers in the corpus handle membership churn and failure detection during reconfiguration?

**Provenance:** answer_runs row id 410, asked 2026-08-21; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1974] code__1-3 \| witness-service/src/test/java/com/hellblazer/delos/witness/integration/Phase1CIntegrationTest.java:271-273 | irrelevant | Java test code, not a distributed systems paper |
| 2 | [0.2229] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/ViewAssembly.java:1-36 | irrelevant | Source file license header, not paper content |
| 3 | [0.2241] code__1-3 \| fireflies/src/test/java/com/hellblazer/delos/fireflies/ViewChangeStressTest.java:250-253 | irrelevant | Implementation stress test, not a paper |
| 4 | [0.2270] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeTest.java:1-35 | irrelevant | Test file header; no paper material |
| 5 | [0.2364] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/ViewAssembly.java:37-47 | irrelevant | Code javadoc on reconfiguration, not paper text |
| 6 | [0.2448] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeValidationTest.java:1-37 | irrelevant | Test license header, not scholarly source |
| 7 | [0.2472] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/support/ReconfigurationCoordinator.java:1-26 | irrelevant | Implementation code, not a corpus paper |
| 8 | [0.2487] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeIdentityValidationTest.java:1-34 | irrelevant | Test file header, no paper content |
| 9 | [0.2502] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/ViewCoordinator.java:1-10 | irrelevant | Code header only; not a paper |
| 10 | [0.2438] code__1-3 \| fireflies/src/test/java/com/hellblazer/delos/fireflies/ChurnTest.java:36-59 | irrelevant | Churn test imports; code, not paper |

### q10 [crowded, score 1.0] (pool id c11)

**Text (verbatim, frozen):** How do the Delos papers handle membership churn and reconfiguration (adding/removing servers, view changes, log sealing)?

**Provenance:** answer_runs row id 383, asked 2026-08-21; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1632] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeTest.java:1-35 | irrelevant | Codebase test file, not Delos papers |
| 2 | [0.1750] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeValidationTest.java:1-37 | irrelevant | Codebase test file, not papers |
| 3 | [0.1761] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/ViewAssembly.java:1-36 | irrelevant | Implementation code, not paper content |
| 4 | [0.1794] code__1-3 \| choam/src/test/java/com/hellblazer/delos/choam/CommitteeIdentityValidationTest.java:1-34 | irrelevant | Test code, not paper material |
| 5 | [0.1843] code__1-3 \| model/src/main/java/com/hellblazer/delos/model/ProcessDomain.java:1-35 | irrelevant | Implementation code, not papers |
| 6 | [0.1868] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/support/ReconfigurationCoordinator.java:1-26 | irrelevant | Implementation code, not paper text |
| 7 | [0.1884] code__1-3 \| model/src/test/java/com/hellblazer/delos/model/FireFliesTest.java:1-35 | irrelevant | Test code, unrelated to papers |
| 8 | [0.1926] code__1-3 \| choam/src/main/java/com/hellblazer/delos/choam/ViewAssembly.java:37-47 | irrelevant | Code comment on reconfiguration, not paper |
| 9 | [0.1879] code__1-3 \| witness-service/src/test/java/com/hellblazer/delos/witness/integration/Phase1CIntegrationTest.java:271-273 | irrelevant | Integration test, not paper content |
| 10 | [0.2064] code__1-3 \| witness-service/src/test/java/com/hellblazer/delos/witness/WitnessViewAdapterTest.java:1-33 | irrelevant | Test code, not paper material |

### q11 [crowded, score 1.0] (pool id c12)

**Text (verbatim, frozen):** What do the semantic operator papers say about batching and cascading LLM calls to reduce cost?

**Provenance:** answer_runs row id 411, asked 2026-08-21; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.2615] code__1-1 \| scripts/bench/synthesis_tier_study.py:181-212 | irrelevant | Local benchmark script, not paper content |
| 2 | [0.2693] code__1-1 \| scripts/bench/operator_proxy_variance.py:71-102 | irrelevant | Dispatch variance bench code, not paper claims |
| 3 | [0.2785] code__1-1 \| scripts/validate/01-mcp-core.py:298-333 | irrelevant | MCP validation script, coincidental operator vocabulary |
| 4 | [0.2789] code__1-1 \| tests/test_plan_cost_estimate.py:231-244 | irrelevant | Cost-estimate test code, not paper source |
| 5 | [0.2791] code__1-1 \| conexus/hooks/scripts/subagent-start.sh:258-283 | irrelevant | Hook script about sequential thinking; unrelated |
| 6 | [0.2795] code__1-1 \| tests/test_operator_pipelines_dispatch.py:86-120 | irrelevant | Dispatch tests in codebase, not paper text |
| 7 | [0.2837] code__1-1 \| scripts/bundle_sandbox_probe.py:1-47 | irrelevant | Bundle sandbox probe implementation, not paper discussion |
| 8 | [0.2879] code__1-1 \| tests/test_operator_proxy_ab.py:30-59 | irrelevant | A/B test fixture code, not paper claims |
| 9 | [0.2909] code__1-1 \| src/nexus/mcp/operator_requests.py:27-39 | irrelevant | MCP request schema code, no paper content |
| 10 | [0.2920] code__1-1 \| scripts/spikes/spike_b_aggregate_leakage.py:35-67 | irrelevant | Spike script on aggregate leakage, not paper |

### q12 [crowded, score 1.0] (pool id c13)

**Text (verbatim, frozen):** What tradeoffs did RDR-104 (incremental catalog projection rebuild) accept that RDR-101 (catalog T3 metadata design / event-sourcing) did not, and vice versa? Compare the explicit tradeoffs documented in each RDR.

**Provenance:** answer_runs row id 97, asked 2026-05-06; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 10/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1743] code__1-1 \| tests/test_kmo9h_catalog_gate_census.py:39-59 | irrelevant | catalog gate census test; no RDR tradeoff content |
| 2 | [0.1788] code__1-1 \| src/nexus/catalog/catalog_protocol.py:22-30 | irrelevant | RDR-152 protocol whitelist, different RDR |
| 3 | [0.1876] code__1-1 \| tests/test_catalog_event_sourced_mutators.py:1-26 | irrelevant | RDR-101 implementation test, not documented tradeoffs |
| 4 | [0.1897] code__1-1 \| tests/test_traverse_step.py:283-292 | irrelevant | traverse step tests, unrelated topic |
| 5 | [0.1938] code__1-1 \| src/nexus/catalog/catalog_protocol.py:2-22 | irrelevant | RDR-168 protocol contract, different RDR |
| 6 | [0.1952] code__1-1 \| tests/test_catalog_event_sourced_register.py:1-34 | irrelevant | RDR-101 gate-parsing test, not the RDR document |
| 7 | [0.1985] code__1-1 \| tests/db/test_i711w_gap_contracts.py:2-28 | irrelevant | i711w exit-criteria contracts, unrelated |
| 8 | [0.2115] code__1-1 \| tests/catalog/test_catalog_protocol_fidelity.py:1-25 | irrelevant | RDR-168 protocol fidelity guards, different RDR |
| 9 | [0.2166] code__1-1 \| src/nexus/metadata_schema.py:117-139 | irrelevant | code comment on RDR-101 field drop, not tradeoffs |
| 10 | [0.2184] code__1-1 \| tests/test_rdr137_followup_reader_sigs.py:1-36 | irrelevant | RDR-137 reader shims, different RDR |

## Clean stratum (q13-q24)

### q13 [clean, score 0.1] (pool id c06)

**Text (verbatim, frozen):** In Knapp 2026, what does Algorithm 5.1 (the parallel partitioning of a hybrid pyramid/tet/hex forest) specify step by step? How are per-shape element weights N_shape(level) (N_pyramid = 2·8^l − 6^l) used to compute cumulative offsets and assign SFC ranges to ranks? What is the exact partition rule?

**Provenance:** answer_runs row id 177, asked 2026-05-30; shape: paper-corpus.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 1/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0736] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightPartitioner.java:17-38 | relevant | Cites Knapp 2026 Alg 5.1; cumulative-offset partition rule |
| 2 | [0.0966] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightProvider.java:1-27 | relevant | Defines per-shape element-count weights N_shape |
| 3 | [0.0990] code__1-20 \| lucien-distributed/src/test/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightedPartitionBootstrapTest.java:21-33 | irrelevant | Distributed bootstrap consumer test, not the algorithm specification |
| 4 | [0.1111] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightPartitionerTest.java:1-26 | relevant | Test document for the Alg 5.1 partitioner |
| 5 | [0.1122] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/forest/HybridMeshIntegrationDemoTest.java:159-182 | relevant | Asserts N_pyramid = 2·8^l − 6^l partition weighting |
| 6 | [0.1470] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightPartitioner.java:175-189 | relevant | Shape-aware weight partition rule across ranks |
| 7 | [0.1511] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightProviderTest.java:58-85 | relevant | Quotes Knapp Eq 5.1 with golden weight values |
| 8 | [0.1600] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightPartitioner.java:205-218 | relevant | Maps SFC-ordered weighted trees to partition ranks |
| 9 | [0.1676] code__1-20 \| lucien/src/main/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightPartitioner.java:98-112 | relevant | Per-rank SFC cut points from cumulative offsets |
| 10 | [0.1806] code__1-20 \| lucien/src/test/java/com/hellblazer/luciferase/lucien/balancing/ShapeWeightProviderTest.java:1-22 | relevant | Same weight-provider test document verifying N_shape formulas |

### q14 [clean, score 0.0] (pool id c15)

**Text (verbatim, frozen):** How does the nexus catalog auto-linker work from store_put through link-context scratch consumption to links table insertion?

**Provenance:** answer_runs row id 99, asked 2026-05-06; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 0/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0414] code__1-1 \| tests/test_auto_linker.py:299-299 | relevant | Integration test of _catalog_auto_link via store_put |
| 2 | [0.1002] code__1-1 \| tests/test_auto_linker.py:302-343 | relevant | Full pipeline scratch link-context to catalog link |
| 3 | [0.1299] code__1-1 \| tests/test_auto_linker.py:345-379 | relevant | store_put auto-link path without scratch context |
| 4 | [0.1447] code__1-1 \| tests/test_auto_linker.py:382-420 | relevant | Link-context coverage per store_put in session |
| 5 | [0.1463] code__1-1 \| src/nexus/catalog/auto_linker.py:92-118 | relevant | Auto-linker creates links from contexts, link_if_absent |
| 6 | [0.1497] code__1-1 \| src/nexus/catalog/auto_linker.py:120-162 | relevant | Auto-linker loop parsing target tumblers into links |
| 7 | [0.1573] code__1-1 \| src/nexus/catalog/auto_linker.py:1-29 | relevant | Auto-linker module docstring: scratch link-context to catalog |
| 8 | [0.1580] code__1-1 \| tests/test_auto_linker.py:1-39 | relevant | Auto-linker test imports and fixtures |
| 9 | [0.1662] code__1-1 \| tests/test_catalog_e2e.py:351-351 | relevant | E2E test section for store_put to catalog |
| 10 | [0.1672] code__1-1 \| tests/test_auto_linker.py:218-247 | relevant | AutoLinkResult semantics of auto_link return counts |

### q15 [clean, score 0.4] (pool id c16)

**Text (verbatim, frozen):** How does the t3_collection_name resolver in nexus/corpus.py handle bare prefix inputs?

**Provenance:** answer_runs row id 118, asked 2026-05-06; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 4/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1423] code__1-1 \| tests/test_corpus.py:361-393 | relevant | t3_collection_name tests including prefix/default resolution |
| 2 | [0.1470] code__1-1 \| tests/test_corpus.py:506-514 | relevant | directly tests bare prefix multi-match tie-break |
| 3 | [0.1478] code__1-1 \| tests/test_corpus.py:419-444 | relevant | parametrized bare-prefix unique-match resolution tests |
| 4 | [0.1523] code__1-1 \| tests/test_corpus.py:396-418 | relevant | bare prefix fall-through when multiple collections |
| 5 | [0.1543] code__1-1 \| src/nexus/catalog/recovery_bundle.py:363-381 | irrelevant | different resolver: recovery bundle target collection |
| 6 | [0.1906] code__1-1 \| tests/test_corpus.py:327-360 | relevant | same resolver's test file, argument resolution semantics |
| 7 | [0.1990] code__1-1 \| tests/test_corpus.py:517-534 | relevant | bare knowledge prefix legacy-default fall-through guard |
| 8 | [0.2007] code__1-1 \| src/nexus/doctor_search.py:84-120 | irrelevant | doctor search probe, unrelated RDR resolver |
| 9 | [0.2169] code__1-1 \| src/nexus/commands/catalog_cmds/collections.py:159-187 | irrelevant | CLI command for conformant name, not corpus resolver |
| 10 | [0.2191] code__1-1 \| tests/test_registry.py:223-246 | irrelevant | registry repo-collection fallback, different module |

### q16 [clean, score 0.4] (pool id c18)

**Text (verbatim, frozen):** How does nexus's plan-match-first gate decide between executing a matched plan and dispatching the inline planner?

**Provenance:** answer_runs row id 153, asked 2026-05-10; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 4/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1780] code__1-1 \| tests/test_plan_match_category_route_probes.py:67-84 | relevant | Tests plan matcher routing/category decisions in nexus |
| 2 | [0.1921] code__1-1 \| tests/test_force_dynamic.py:1-26 | relevant | force_dynamic bypass of plan_match gate; directly on-topic |
| 3 | [0.1990] code__1-1 \| tests/test_document_lookup_seed_plans.py:273-310 | irrelevant | Seed-plan document lookup tests, not gate decision logic |
| 4 | [0.2033] code__1-1 \| tests/test_force_dynamic.py:102-129 | relevant | Asserts plan_match skipped and inline planner fires |
| 5 | [0.2069] code__1-1 \| tests/integration/test_rdr_088_operator_pipelines.py:88-112 | irrelevant | Operator pipeline tests; only wraps Match for plan_run |
| 6 | [0.2078] code__1-1 \| tests/test_nx_answer_plan_choice.py:67-69 | relevant | Exercises nx_answer Step-1 plan-choice path on match hit |
| 7 | [0.2122] code__1-1 \| scripts/spikes/spike_b_queries.py:239-266 | irrelevant | Query-ambiguity spike script, not gate logic |
| 8 | [0.2126] code__1-1 \| tests/integration/test_nx_answer_step_telemetry_mvv.py:72-98 | irrelevant | Step telemetry test with hand-built plan; not gate |
| 9 | [0.2145] code__1-1 \| tests/test_plan_match_binding_satisfiability.py:224-261 | relevant | Tests matcher gate dropping unrunnable plans, FTS5 fallback path |
| 10 | [0.2169] code__1-1 \| tests/test_document_lookup_seed_plans.py:257-267 | relevant | Covers plan_match classification and nx_answer fast-path dispatch |

### q17 [clean, score 0.0] (pool id c22)

**Text (verbatim, frozen):** How does the nexus T1 scratch session lifecycle couple to MCP server lifecycle? What problems arose from session-ID coupling to MCP child process lifecycle — stale sessions, orphaned MCP children, reload/reconnect leakage, parent-pid tracking, pidfiles, watchdogs, heartbeat probes? What strategies were tried (T1 keyed by session vs by ppid vs by transport, supervisor reaping, hook-based cleanup) and which won?

**Provenance:** answer_runs row id 166, asked 2026-05-21; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 0/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0823] code__1-1 \| src/nexus/hooks.py:143-164 | relevant | Session id handoff to MCP sibling for T1 scope |
| 2 | [0.1282] code__1-1 \| tests/test_session_end_t1_ownership.py:1-32 | relevant | SessionEnd T1 ownership regression, session coupling |
| 3 | [0.1309] code__1-1 \| src/nexus/_session_end_launcher.py:27-50 | relevant | SessionEnd launcher, watchdog sidecar, T1 flush |
| 4 | [0.1373] code__1-1 \| tests/test_hooks.py:196-216 | relevant | MCP sibling pid ancestry gate test |
| 5 | [0.1379] code__1-1 \| tests/test_session_end_t1_ownership.py:35-84 | relevant | Scratch store session binding/clearing test |
| 6 | [0.1396] code__1-1 \| src/nexus/_session_end_launcher.py:2-27 | relevant | Fork-first SessionEnd daemonizer, hook lifecycle |
| 7 | [0.1451] code__1-1 \| tests/test_hooks.py:123-143 | relevant | SessionStart hook test with claude/sibling pids |
| 8 | [0.1193] code__1-1 \| src/nexus/hooks.py:339-364 | relevant | SessionEnd stdio EOF race window on transport |
| 9 | [0.1531] code__1-1 \| tests/test_session_sweep_orphan_trackers.py:1-37 | relevant | Orphaned MCP child resource trackers after shutdown |
| 10 | [0.1540] code__1-1 \| tests/test_ppid_chain_hypothesis.py:1-36 | relevant | PPID chain walk for T1 session propagation |

### q18 [clean, score 0.1] (pool id c23)

**Text (verbatim, frozen):** What is Nexus's current Claude Code plugin distribution architecture? What plugins does it ship (nx, sn)? How is it packaged for the marketplace?

**Provenance:** answer_runs row id 168, asked 2026-05-23; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 1/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1779] code__1-1 \| tests/test_plugin_name_drift.py:1-38 | relevant | Nexus plugin naming/distribution, nx→conexus rename |
| 2 | [0.2000] code__1-1 \| tests/e2e/upgrade-shakeout.sh:596-623 | relevant | Nexus plugin install drift shakeout, distribution behavior |
| 3 | [0.2161] code__1-1 \| tests/test_sn_plugin.py:99-124 | relevant | sn plugin marketplace source, git-subdir tag pinning |
| 4 | [0.2229] code__1-1 \| tests/test_sn_plugin.py:1-15 | relevant | Tests describing the sn plugin structure |
| 5 | [0.2317] code__1-1 \| tests/hooks/test_version_lockstep_hook.py:257-293 | relevant | Marketplace.json plugin-channel cut version lockstep |
| 6 | [0.2357] code__1-1 \| tests/test_plugin_name_drift.py:41-74 | relevant | nx vs conexus plugin naming and install guidance |
| 7 | [0.2423] code__1-1 \| conexus/hooks/scripts/auto-approve-nx-mcp.sh:10-42 | irrelevant | Hook MCP tool allowlist, not distribution architecture |
| 8 | [0.2434] code__1-1 \| scripts/cut_plugin_release.py:42-72 | relevant | Plugin release cutting script, packaging mechanics |
| 9 | [0.2439] code__1-1 \| tests/test_plugin_install.py:1-21 | relevant | Simulates Claude Code installing conexus plugin from GitHub |
| 10 | [0.2458] code__1-1 \| scripts/plugin_channel.py:128-153 | relevant | Plugin channel packaging, what ships to installed users |

### q19 [clean, score 0.3] (pool id c24)

**Text (verbatim, frozen):** RDR-070 (Incremental Taxonomy and Clustered Search) for nexus: what did it design and ship, and which of its capabilities are underutilized or not wired into the retrieval path today? Specifically: does it produce per-collection summary embeddings or only per-topic centroids; is clustered-search / topic-scoped prefiltering wired into nx_answer and search_cross_corpus by default or only behind explicit flags; and which components consume the taxonomy outputs versus leaving them dormant?

**Provenance:** answer_runs row id 169, asked 2026-05-27; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 3/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0853] code__1-1 \| tests/test_search_clustering_integration.py:1-41 | irrelevant | RDR-056 cluster search test, different RDR |
| 2 | [0.1036] code__1-1 \| tests/db/test_taxonomy_compute.py:1-38 | relevant | taxonomy compute tests, RDR-070 lineage capabilities |
| 3 | [0.1041] code__1-1 \| src/nexus/db/t2/taxonomy_compute.py:1-30 | relevant | taxonomy compute core implementing RDR-070 topic discovery |
| 4 | [0.1041] code__1-1 \| src/nexus/db/t2/taxonomy_compute.py:58-103 | relevant | HDBSCAN clustering internals of taxonomy compute |
| 5 | [0.1067] code__1-1 \| src/nexus/db/t2/taxonomy_compute.py:31-57 | relevant | Explicitly cites RDR-070 topic discovery dependency |
| 6 | [0.1089] code__1-1 \| src/nexus/commands/search_cmd.py:367-389 | relevant | Shows RDR-070 topic boost wiring in search path |
| 7 | [0.1104] code__1-1 \| service/src/test/java/dev/nexus/service/TaxonomyCentroidRepositoryTest.java:31-54 | relevant | Taxonomy centroid store: per-topic centroids question |
| 8 | [0.1148] code__1-1 \| service/src/main/java/dev/nexus/service/vectors/TaxonomyCentroidRepository.java:55-78 | relevant | Centroid repository dims; centroid-vs-summary-embedding evidence |
| 9 | [0.1164] code__1-1 \| src/nexus/db/t2/taxonomy_compute.py:106-160 | irrelevant | RDR-077 hub detection, different feature |
| 10 | [0.1190] code__1-1 \| scripts/spikes/spike_rdr090_5q.py:66-94 | irrelevant | RDR-090 evaluation spike queries, unrelated |

### q20 [clean, score 0.1] (pool id c25)

**Text (verbatim, frozen):** What are pgvector 0.8 HNSW iterative_scan tradeoffs and recall behavior under metadata filters?

**Provenance:** answer_runs row id 182, asked 2026-06-07; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 1/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1575] code__1-46 \| spikes/pgvector-recall/run_recall.py:1-48 | relevant | Recall harness for pgvector HNSW iterative-scan under filters |
| 2 | [0.1923] code__1-46 \| spikes/pgvector-recall/run_recall.py:233-257 | relevant | Stress sweep measuring filtered recall degradation vs ef_search |
| 3 | [0.1941] docs__1-46 \| spikes/pgvector-recall/DESIGN.md:chunk-4 | relevant | Design doc: pgvector 0.8 HNSW method, metadata WHERE filters |
| 4 | [0.2254] code__1-1 \| service/src/test/java/dev/nexus/service/db/HnswServingGucParityTest.java:1-30 | irrelevant | Serving GUC parity test; config plumbing, not recall tradeoffs |
| 5 | [0.2320] code__1-46 \| spikes/pgvector-recall/load_pg.py:1-38 | relevant | Loads pgvector with metadata columns defining the filter setup |
| 6 | [0.2337] code__1-46 \| deploy/tests/test_smoke.py:233-256 | relevant | Proves iterative_scan=relaxed_order behavior under filtered KNN |
| 7 | [0.2373] code__1-1 \| service/src/test/java/dev/nexus/service/HybridSelectiveGateTest.java:178-189 | relevant | HNSW max_scan_tuples budget under selective filter routing |
| 8 | [0.2379] code__1-46 \| spikes/pgvector-recall/test_pass_bar.py:1-30 | relevant | Pass bar asserting pgvector filtered recall@10 thresholds |
| 9 | [0.2426] code__1-46 \| spikes/pgvector-recall/run_recall.py:156-175 | relevant | Same recall harness computing filtered recall metrics |
| 10 | [0.2498] code__1-46 \| spikes/pgvector-recall/ground_truth.py:1-30 | relevant | Exact filtered top-10 ground truth for recall measurement |

### q21 [clean, score 0.1] (pool id c26)

**Text (verbatim, frozen):** How does nexus's plan runner work? What are the typed operators and how does the plan DAG execute? What is the plan-match-first gate?

**Provenance:** answer_runs row id 184, asked 2026-06-26; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 1/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.2313] code__1-1 \| tests/integration/test_rdr_196_p2c_ab_measurement.py:132-164 | relevant | nexus nx_answer/plan_save integration test, plan runner behavior |
| 2 | [0.2378] code__1-1 \| tests/test_builtin_plan_operator_arg_census_lint.py:45-76 | relevant | imports plans.runner operator tool maps directly |
| 3 | [0.2424] code__1-1 \| tests/test_builtin_plan_operator_arg_census_lint.py:2-27 | relevant | builtin plan operator arg census, runner operator resolution |
| 4 | [0.2531] code__1-1 \| tests/test_builtin_plan_operator_arg_names.py:1-32 | relevant | builtin plan steps and resolved MCP operators |
| 5 | [0.2552] code__1-1 \| src/nexus/operators/model_tiers.py:67-73 | relevant | nexus operator model tiers, enumerates typed operators |
| 6 | [0.2581] code__1-1 \| tests/integration/test_nx_answer_equivalence.py:207-230 | relevant | nx_answer plan retrieval vs operator steps equivalence |
| 7 | [0.2660] code__1-1 \| scripts/bench/synthesis_tier_study.py:234-265 | irrelevant | benchmark script for synthesis tiering, not runner mechanics |
| 8 | [0.2670] code__1-1 \| tests/test_plan_match_category_route_probes.py:67-84 | relevant | directly about plan-match gate routing decisions |
| 9 | [0.2705] code__1-1 \| tests/integration/test_rdr_088_operator_pipelines.py:208-246 | relevant | plan_run DAG execution with operator pipeline steps |
| 10 | [0.2716] code__1-1 \| tests/integration/test_rdr_088_operator_pipelines.py:133-170 | relevant | plan_run multi-step DAG with traverse and search |

### q22 [clean, score 0.3] (pool id c28)

**Text (verbatim, frozen):** Is the nx-mcp / nx CLI client (the 'conexus' PyPI/uv package, providing mcp__plugin_conexus_nexus__* tools) part of the conexus project or the separate nexus engine repo? How does it authenticate to the conexus cloud edge for a tenant's data-path calls today?

**Provenance:** answer_runs row id 370, asked 2026-07-12; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 3/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1693] code__1-1 \| src/nexus/db/managed_endpoint.py:31-55 | relevant | Cloud-mode managed endpoint config used by nx client |
| 2 | [0.1702] code__1-46 \| deploy/gate/relay_post.py:36-71 | irrelevant | Deploy gate relay poster, not client data-path auth |
| 3 | [0.1739] code__1-1 \| src/nexus/engine_version.py:232-252 | irrelevant | Engine version/deployment gate notes, not client packaging or auth |
| 4 | [0.1789] code__1-46 \| control-plane/src/main/java/dev/conexus/controlplane/auth/package-info.java:65-65 | relevant | conexus control-plane auth package documents edge authentication |
| 5 | [0.1794] code__1-1 \| src/nexus/db/managed_endpoint.py:1-30 | relevant | Cloud-mode endpoint config and capability probe docstring |
| 6 | [0.1821] code__1-1 \| tests/e2e/cloud-client-path-gate.sh:25-47 | relevant | Client-path gate covers authenticated vs unauthenticated public edge |
| 7 | [0.1839] code__1-1 \| tests/e2e/cloud-client-path-gate.sh:1-24 | relevant | Real client code contract against public conexus edge |
| 8 | [0.1856] code__1-1 \| src/nexus/mcp_client/__init__.py:1-11 | irrelevant | Outbound MCP client to external servers; vocabulary overlap only |
| 9 | [0.2010] code__1-1 \| tests/e2e/data-token-cli-gate.sh:27-49 | relevant | Tenant-scoped data-token credential minted for CLI calls |
| 10 | [0.2027] code__1-1 \| tests/e2e/cloud-client-path-gate.sh:152-159 | relevant | Authenticated edge probe shape via conexus relay |

### q23 [clean, score 0.2] (pool id c29)

**Text (verbatim, frozen):** Which nexus client code paths call catalog manifest write_many and do they supply the collection field per manifest row after RDR-191 GATE-2?

**Provenance:** answer_runs row id 377, asked 2026-08-14; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 2/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.0701] code__1-1 \| tests/db/test_c2_manifest_null_collection_engine.py:162-187 | relevant | RDR-191 write_manifest non-blank collection contract test |
| 2 | [0.0811] code__1-1 \| tests/test_d9fwj_ghost_doc_retraction.py:2-29 | relevant | client call site: retraction calls write_manifest, GATE-2 |
| 3 | [0.0846] code__1-1 \| tests/db/test_c2_manifest_null_collection_engine.py:121-143 | relevant | client requires non-blank collection on manifest write |
| 4 | [0.0883] code__1-1 \| tests/test_o8dil2_prune_misclassified_manifest_guard.py:96-118 | relevant | manifest write protocol double with per-call collection kwarg |
| 5 | [0.0924] code__1-1 \| service/src/test/java/dev/nexus/service/CatalogHandlerManifestEnvelopeTest.java:246-268 | relevant | RDR-191 per-row caller-supplied collection, service side |
| 6 | [0.0928] code__1-1 \| src/nexus/catalog/catalog_protocol.py:219-247 | relevant | catalog protocol file defining manifest write signatures |
| 7 | [0.1002] code__1-1 \| tests/test_o8dil2_prune_misclassified_manifest_guard.py:286-317 | relevant | manifest write routing between writer and catalog |
| 8 | [0.1012] code__1-1 \| tests/test_o8dil45_indexer_delete_count.py:105-136 | irrelevant | indexer delete counting, not manifest write collection |
| 9 | [0.1027] code__1-1 \| tests/test_superseded_vector_sweep.py:445-474 | irrelevant | catalog read path, not manifest write_many |
| 10 | [0.1057] code__1-1 \| tests/test_o8dil2_prune_misclassified_manifest_guard.py:231-255 | relevant | tests which writer receives manifest write calls |

### q24 [clean, score 0.4] (pool id c30)

**Text (verbatim, frozen):** What is the chunk identity convention used for T3 chunks?

**Provenance:** answer_runs row id 412, asked 2026-08-21; shape: rdr-research.

**Crowding audit trail** (judge `claude-opus-5`, one blind dispatch; irrelevant 4/10):

| rank | result (distance, collection, source) | label | judge reason |
|---|---|---|---|
| 1 | [0.1581] code__1-1 \| tests/test_collection_health.py:146-146 | irrelevant | chunk counting health test, not identity convention |
| 2 | [0.2061] code__1-1 \| src/nexus/db/t3_reidentify.py:38-77 | relevant | T3 re-identification module, chunk id convention |
| 3 | [0.2113] code__1-1 \| tests/test_orphan_backfill.py:341-349 | relevant | states cid is full chash, no truncation |
| 4 | [0.2328] code__1-1 \| service/src/main/java/dev/nexus/service/http/PipelineHandler.java:211-211 | irrelevant | Java pipeline handler, coincidental chunks comment |
| 5 | [0.2359] code__1-1 \| tests/catalog/test_chash_citation_resolution.py:145-151 | relevant | chash derivation identity, producer/grammar agreement |
| 6 | [0.2379] code__1-1 \| tests/test_metadata_consistency.py:257-290 | relevant | chunk chash keyset coverage on T3 puts |
| 7 | [0.2433] code__1-1 \| src/nexus/chunk_identity.py:1-29 | relevant | canonical chash derivation module, definitive source |
| 8 | [0.2474] code__1-1 \| tests/test_service_mode_cli_real_client.py:158-158 | irrelevant | t3 gc CLI test, not identity convention |
| 9 | [0.2493] code__1-72 \| packages/llm/llm/src/assembler.ts:48-48 | irrelevant | different project, streaming chunk types |
| 10 | [0.2505] code__1-1 \| tests/test_chunk_identity.py:1-36 | relevant | tests for canonical chunk natural-ID helper |

## Scored, unselected overflow pool (not part of the frozen set)

| pool id | score | stratum | question (truncated) | provenance |
|---|---|---|---|---|
| c14 | 0.5 | crowded | What are the tradeoffs between T1 nx scratch, T2 nx memory, and T3 nx store for sharing findings between subag | answer_runs id 98, 2026-05-06 |
| c17 | 0.5 | crowded | how does T1 scratch persist across MCP reconnects in nexus | answer_runs id 128, 2026-05-07 |
| c19 | 1.0 | crowded | In RDR-110 semantic tuple space, does the read() API filter on registered dimensions only, or on any field inc | answer_runs id 162, 2026-05-11 |
| c20 | 1.0 | crowded | What prior design work exists on the nexus "surface" concept — portals, compositor, cell-based rendering, recu | answer_runs id 164, 2026-05-18 |
| c21 | 0.5 | crowded | How could a2ui be used within nx? What's the existing thinking on a2ui-nexus synthesis and cockpit mapping? | answer_runs id 165, 2026-05-19 |
| c27 | 0.5 | crowded | What is the design intent and prior discussion of the pgvector 40P01 deadlock fix (chash sort + retry belt) fo | answer_runs id 369, 2026-07-05 |
| c31 | 0.3 | clean | What is the INLINE-VS-BIND rule for jOOQ DSL conversions in the nexus service layer? | answer_runs id 413, 2026-08-21 |
| c32 | 0.9 | crowded | What prior findings exist about jitterbug LGA contact ribbon aliasing, member quanta stride thresholds, or the | answer_runs id 375, 2026-08-10 |

Full per-result labels for the overflow rows are preserved in the
assembly record (T2 `nexus/question-set-rdr200-phase1-frozen-2026-09`
names the artifact chain).

## Labeling spend (actual)

- 32 opus labeling dispatches, total **$13.5887** (mean $0.4246/call;
  first ~6 calls ~$0.86-0.93 cache-cold, remainder ~$0.30-0.35).
- Envelope source: each `claude -p --output-format json` result's
  `total_cost_usd`, summed.
