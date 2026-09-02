---
title: "RDR-200 Phase 1c Held-Out Question Set (committed AFTER the arms and judging finished)"
parent_prereg: docs/rdr/rdr-200-phase1c-prereg.md
created: 2026-09-02
status: frozen
kind: companion
---

# RDR-200 Phase 1c Held-Out Question Set

Committed only after all arms ran and all verdicts were written, per the prereg (sha256 of the text list is recorded there). Assembled 2026-09-01: 35 authored, 1 from answer_runs history (id 172); each anchored to a verified indexed document. Crowding labeled by claude-opus-5 over 36 candidates, $14.04.

### q01 [crowded, score 0.6] (pool h01, paper-corpus)

**Text (verbatim, frozen):** In Holke's 2018 PhD thesis, state Theorem 3.5 — the SFC index definition / existence-uniqueness theorem for tetrahedral Morton index. What does it claim about the TM-index being a unique identifier?

**Anchor:** knowledge__dt-papers / Scalable Algorithms for Parallel Tree-based Adaptive Mesh Refinement with General Element Types (Holke 2018 thesis; indexed under its DEVONthink PDF path)
**Provenance:** answer_runs row id 172, asked 2026-05-28 (heldout_pool_raw.json); shape: paper-corpus. Re-verified: thesis chapter 4 'The Tetrahedral Morton Index' surfaces at rank 2 in knowledge__dt-papers.

**Verdict:** vs headless other; vs caller-only other

### q02 [crowded, score 0.7] (pool h02, paper-corpus)

**Text (verbatim, frozen):** Compare how Rapid (ATC'18) and Fireflies (TOCS) detect member failure and reach agreement on membership changes: what does Rapid's expander-graph monitoring plus multi-process cut detection buy over Fireflies' accusation/rebuttal rings, and what does each give up?

**Anchor:** knowledge__distributed-systems / rapid atc18 (plus fireflies tocs)
**Provenance:** authored 2026-09-01, anchored to knowledge__distributed-systems/rapid atc18 (plus fireflies tocs), verified by search probe 'Rapid membership service multi-process cut detection expander graph almost-everywhere agreement'

**Verdict:** vs headless other; vs caller-only continuation

### q03 [crowded, score 0.8] (pool h03, paper-corpus)

**Text (verbatim, frozen):** How does Zanzibar prevent the 'new enemy' problem? Explain what a zookie encodes, how snapshot reads at a zookie timestamp give external consistency, and what staleness the design deliberately tolerates in exchange.

**Anchor:** knowledge__distributed-systems / zanzibar
**Provenance:** authored 2026-09-01, anchored to knowledge__distributed-systems/zanzibar, verified by search probe 'Zanzibar zookie snapshot read external consistency new enemy problem'

**Verdict:** vs headless other; vs caller-only other

### q04 [crowded, score 0.6] (pool h04, paper-corpus)

**Text (verbatim, frozen):** In the Aleph BFT paper, how does ordering emerge from the DAG of units, and what role does the randomness beacon play in guaranteeing liveness under asynchrony? Contrast with the partially-synchronous assumptions the corpus's lightweight SMR paper relies on.

**Anchor:** knowledge__distributed-systems / aleph bft
**Provenance:** authored 2026-09-01, anchored to knowledge__distributed-systems/aleph bft, verified by search probe 'Aleph BFT DAG ordering asynchronous randomness beacon common coin'

**Verdict:** vs headless continuation; vs caller-only other

### q05 [crowded, score 0.8] (pool h05, paper-corpus)

**Text (verbatim, frozen):** What convergence guarantees does the Dolev/Hoch/van Renesse self-stabilizing Byzantine-tolerant overlay paper prove (its Theorems on time to a working overlay), and how does it position itself relative to Fireflies / S-Fireflies?

**Anchor:** knowledge__distributed-systems / self stabilizing bft overlay
**Provenance:** authored 2026-09-01, anchored to knowledge__distributed-systems/self stabilizing bft overlay, verified by search probe 'self-stabilizing Byzantine fault tolerant overlay network convergence from arbitrary state'

**Verdict:** vs headless continuation; vs caller-only other

### q06 [crowded, score 0.6] (pool h06, paper-corpus)

**Text (verbatim, frozen):** Compare the distributed bloom filter paper and the hex bloom paper on set reconciliation: how does each handle hash-function cost and collision probability, and which is better suited to gossip-based anti-entropy?

**Anchor:** knowledge__distributed-systems / distributed bloom filter (plus hex bloom)
**Provenance:** authored 2026-09-01, anchored to knowledge__distributed-systems/distributed bloom filter (plus hex bloom), verified by search probe 'hex bloom filter hierarchical set reconciliation gossip anti-entropy'

**Verdict:** vs headless continuation; vs caller-only other

### q07 [crowded, score 0.6] (pool h07, paper-corpus)

**Text (verbatim, frozen):** Compare MemForest's hierarchical temporal indexing (forest recall then tree browse) with Mandol's agglomerative SemanticMap/SemanticGraph structure: what retrieval failure does each paper say it fixes, and how do their index-maintenance costs differ?

**Anchor:** knowledge__dt-papers / MemForest: An Efficient Agent Memory System with Hierarchical Temporal Indexing (plus Mandol)
**Provenance:** authored 2026-09-01, anchored to knowledge__dt-papers/MemForest: An Efficient Agent Memory System with Hierarchical Temporal Indexing (plus Mandol), verified by search probe 'MemForest hierarchical temporal indexing memory retrieval'

**Verdict:** vs headless other; vs caller-only other

### q08 [crowded, score 0.7] (pool h08, paper-corpus)

**Text (verbatim, frozen):** What are the four core memory mechanisms in the unified modular framework of 'Memory in the LLM Era', and which opportunities (O1..On) does the paper argue existing hierarchical memory systems leave unaddressed?

**Anchor:** knowledge__agent-memory / Memory in the LLM Era: Modular Architectures and Strategies in a Unified Framework
**Provenance:** authored 2026-09-01, anchored to knowledge__agent-memory/Memory in the LLM Era: Modular Architectures and Strategies in a Unified Framework, verified by search probe 'memory operations taxonomy consolidation retrieval forgetting unified framework'

**Verdict:** vs headless continuation; vs caller-only other

### q09 [crowded, score 0.8] (pool h09, paper-corpus)

**Text (verbatim, frozen):** In 'Verbalizable Representations Form a Global Workspace in Language Models', what is the Jacobian lens (J-space) and what experiments support the claim that verbalizable representations act as a global workspace rather than being involved in routine processing?

**Anchor:** knowledge__interpretability / Verbalizable Representations Form a Global Workspace in Language Models
**Provenance:** authored 2026-09-01, anchored to knowledge__interpretability/Verbalizable Representations Form a Global Workspace in Language Models, verified by search probe 'verbalizable representations global workspace evidence experiments'

**Verdict:** vs headless continuation; vs caller-only other

### q10 [crowded, score 0.6] (pool h10, paper-corpus)

**Text (verbatim, frozen):** According to the HRNN paper, what two limitations make approximate reverse k-nearest-neighbor search harder than kNN on high-dimensional vectors, and how does its hybrid graph index address candidate generation and verification cost?

**Anchor:** knowledge__vector-search / HRNN: A Hybrid Graph Index for Approximate Reverse k Nearest Neighbor Search on High Dimensional Vectors
**Provenance:** authored 2026-09-01, anchored to knowledge__vector-search/HRNN: A Hybrid Graph Index for Approximate Reverse k Nearest Neighbor Search on High Dimensional Vectors, verified by search probe 'reverse k nearest neighbor hybrid graph index why harder than kNN'

**Verdict:** vs headless continuation; vs caller-only other

### q11 [crowded, score 0.7] (pool h14, paper-corpus)

**Text (verbatim, frozen):** How does CacheRAG bound LLM-driven knowledge-graph traversal, and how does it classify cache outcomes (hit, miss, hallucination)? What accuracy, truthfulness, and miss-rate improvements does it report over StructGPT-style baselines?

**Anchor:** knowledge__dt-papers / CacheRAG: A Semantic Caching System for Retrieval Augmented Generation in Knowledge Graph Question Answering
**Provenance:** authored 2026-09-01, anchored to knowledge__dt-papers/CacheRAG: A Semantic Caching System for Retrieval Augmented Generation in Knowledge Graph Question Answering, verified by search probe 'CacheRAG cache hit decision similarity threshold false hit precision recall'

**Verdict:** vs headless continuation; vs caller-only other

### q12 [crowded, score 0.6] (pool h15, paper-corpus)

**Text (verbatim, frozen):** What associative-recall gap between attention and gated-convolution models does Zoology measure, why does it attribute the gap to input-independent convolution filters, and what input-dependent remedies does it evaluate?

**Anchor:** knowledge__dt-papers / Zoology: Measuring and Improving Recall in Efficient Language Models
**Provenance:** authored 2026-09-01, anchored to knowledge__dt-papers/Zoology: Measuring and Improving Recall in Efficient Language Models, verified by search probe 'Zoology recall gap gated convolution attention associative recall'

**Verdict:** vs headless continuation; vs caller-only other

### q13 [clean, score 0.0] (pool h11, paper-corpus)

**Text (verbatim, frozen):** In the NOMA vision paper ('Rethinking Query Optimization for Multi-Agent Systems'), which assumptions of classical optimize-then-execute query optimization does it argue multi-agent pipelines invalidate, and what research directions does it propose in response?

**Anchor:** knowledge__semantic-operators / Rethinking Query Optimization for Multi-Agent Systems [Vision]
**Provenance:** authored 2026-09-01, anchored to knowledge__semantic-operators/Rethinking Query Optimization for Multi-Agent Systems [Vision], verified by search probe 'multi-agent system query optimization opportunities compared to classical database optimizer'

**Verdict:** vs headless other; vs caller-only other

### q14 [clean, score 0.0] (pool h12, paper-corpus)

**Text (verbatim, frozen):** What systems vocabulary does 'Natural Language to What?' introduce for NL-to-X querying (fixed target vs answer-object family vs constructed target), and what does it argue an intermediate representation must provide that direct NL-to-SQL/Cypher translation cannot?

**Anchor:** knowledge__semantic-operators / Natural Language to What? A Vision for Intermediate Representations in NL to X Querying
**Provenance:** authored 2026-09-01, anchored to knowledge__semantic-operators/Natural Language to What? A Vision for Intermediate Representations in NL to X Querying, verified by search probe 'desirable properties of an intermediate representation for natural language querying'

**Verdict:** vs headless other; vs caller-only continuation

### q15 [clean, score 0.1] (pool h13, paper-corpus)

**Text (verbatim, frozen):** Describe NL2Pipe's three-phase compilation workflow and its validation/repair loop. Why does the paper argue reflection-style code repair (e.g. CodeTree) is insufficient for semantic errors, and what accuracy gains does it report over fixed-structure baselines?

**Anchor:** knowledge__semantic-operators / Bridge the Last Mile Gap to Semantic Analytics: Compiling Natural Language Queries into Semantic Operator Pipelines
**Provenance:** authored 2026-09-01, anchored to knowledge__semantic-operators/Bridge the Last Mile Gap to Semantic Analytics: Compiling Natural Language Queries into Semantic Operator Pipelines, verified by search probe 'NL2Pipe compiler validation errors semantic operator pipeline evaluation accuracy'

**Verdict:** vs headless n/a; vs caller-only other

### q16 [clean, score 0.1] (pool h19, rdr-research)

**Text (verbatim, frozen):** Why did RDR-191 collapse the per-dimension chunks_384/768/1024 tables into one nexus.chunks table with an exactly-one CHECK over typed embedding columns, what did that make expressible for the manifest FK, and what documented tradeoff (e.g. manifest_verify's cross-dim presence check) did it accept?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md (plus service ManifestVerifyTest / src/nexus/db/chash_tables.py)
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md (plus service ManifestVerifyTest / src/nexus/db/chash_tables.py), verified by search probe 'RDR-191 unify per-dimension chunk tables single nexus.chunks exactly-one CHECK manifest foreign key'

**Verdict:** vs headless other; vs caller-only other

### q17 [clean, score 0.1] (pool h20, rdr-research)

**Text (verbatim, frozen):** What lifecycle problems (discovery, single-writer election, self-heal, version skew, restart-race fencing) does RDR-149's unified ServiceRegistry solve for T1/T2/T3, and how does the conformance suite plus the lifecycle gate test keep tier-specific copies from drifting?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / src/nexus/daemon/service_registry.py + tests/daemon/test_rdr149_lifecycle_conformance.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/src/nexus/daemon/service_registry.py + tests/daemon/test_rdr149_lifecycle_conformance.py, verified by search probe 'RDR-149 unified service registry discovery single-writer self-heal version skew lifecycle conformance'

**Verdict:** vs headless continuation; vs caller-only other

### q18 [clean, score 0.0] (pool h21, rdr-research)

**Text (verbatim, frozen):** How does the RDR-163 aspect-queue backoff/retry ladder work end to end: who chooses the backoff interval, how does the service stamp next_retry_at, and why was the ladder verified through transaction-mode PgBouncer?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / service/src/main/java/dev/nexus/service/db/AspectRepository.java + src/nexus/aspect_worker.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/service/src/main/java/dev/nexus/service/db/AspectRepository.java + src/nexus/aspect_worker.py, verified by search probe 'RDR-163 aspect queue backoff retry ladder design'

**Verdict:** vs headless continuation; vs caller-only other

### q19 [clean, score 0.0] (pool h22, rdr-research)

**Text (verbatim, frozen):** What did RDR-188 server-side rerank design and ship? Explain the Reranker interface and CrossEncoderReranker in the engine, how search_cross_corpus plumbs a rerank request, and what happens when no Voyage key is configured.

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / service/src/main/java/dev/nexus/service/vectors/CrossEncoderReranker.java + tests/test_search_engine_rerank.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/service/src/main/java/dev/nexus/service/vectors/CrossEncoderReranker.java + tests/test_search_engine_rerank.py, verified by search probe 'RDR-188 server-side rerank cross-encoder design client path'

**Verdict:** vs headless other; vs caller-only other

### q20 [clean, score 0.0] (pool h24, rdr-research)

**Text (verbatim, frozen):** How does RDR-196's predicted plan-cost estimation choose among plan-match candidates in nx_answer: what is the confidence band, why is the prefix contiguous, how is cost predicted from step shape rather than recorded medians, and what does the plan_choice log carry?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / src/nexus/plans/cost_estimate.py + tests/test_nx_answer_plan_choice.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/src/nexus/plans/cost_estimate.py + tests/test_nx_answer_plan_choice.py, verified by search probe 'RDR-196 cost-aware nx_answer plan choice confidence band predicted cost estimate'

**Verdict:** vs headless other; vs caller-only other

### q21 [clean, score 0.1] (pool h25, rdr-research)

**Text (verbatim, frozen):** What are invariants R and W of the RDR-197 plugin-only release channel, how does scripts/cut_plugin_release.py enforce them and derive the anchored plugin-v{X.Y.Z}-{n} tag, and why is there no stored counter?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / scripts/plugin_channel.py + scripts/cut_plugin_release.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/scripts/plugin_channel.py + scripts/cut_plugin_release.py, verified by search probe 'RDR-197 plugin-only release channel invariants anchored tag plugin-v cut script'

**Verdict:** vs headless other; vs caller-only other

### q22 [clean, score 0.2] (pool h26, rdr-research)

**Text (verbatim, frozen):** Compare RDR-185 (single convergent upgrade ladder) with RDR-176 (survivable managed migration readiness): what does each treat as the unit of migration, how do stateless preconditions relate to ladder rungs, and what failure class did each RDR exist to close?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / src/nexus/upgrade_ladder/ + docs/rdr/rdr-176-survivable-managed-migration-readiness.md
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/src/nexus/upgrade_ladder/ + docs/rdr/rdr-176-survivable-managed-migration-readiness.md, verified by search probe 'RDR-185 single ladder convergent upgrade versus RDR-176 survivable managed migration readiness'

**Verdict:** vs headless n/a; vs caller-only other

### q23 [clean, score 0.1] (pool h27, rdr-research)

**Text (verbatim, frozen):** Why does RDR-160 route every local-mode collection through the Java service's bge-768 ONNX embedder, how is the standard ONNX provisioned at nx init, and how is Python/Java embedding parity verified?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / service/src/main/java/dev/nexus/service/vectors/Bge768Embedder.java + src/nexus/db/service_bge_model.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/service/src/main/java/dev/nexus/service/vectors/Bge768Embedder.java + src/nexus/db/service_bge_model.py, verified by search probe 'RDR-160 bge-768 local service embedder ONNX bundled rationale'

**Verdict:** vs headless continuation; vs caller-only other

### q24 [clean, score 0.0] (pool h28, rdr-research)

**Text (verbatim, frozen):** How does the nexus generation install layout work (nexus-utpuw): what does the shim resolve at spawn time, how does flipping 'current' interact with live holders, how does GC decide a generation is reapable, and how does health.py surface a broken layout?

**Anchor:** rdr__1-1,code__1-1,docs__1-1 / src/nexus/install_layout.py + src/nexus/health.py
**Provenance:** authored 2026-09-01, anchored to rdr__1-1,code__1-1,docs__1-1/src/nexus/install_layout.py + src/nexus/health.py, verified by search probe 'generation install layout shim current pointer garbage collection live holder'

**Verdict:** vs headless continuation; vs caller-only other

