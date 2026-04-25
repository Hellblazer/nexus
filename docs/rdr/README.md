**Reading order:** [Overview](../rdr-overview.md) | [Workflow](../rdr-workflow.md) | [Nexus Integration](../rdr-nexus-integration.md) | [Templates](../rdr-templates.md) | RDR Index (this page)

---

# RDR Index

An RDR (Research-Design-Review) is a short document that records a technical decision: the problem, what was found, what was chosen, and what was rejected. They exist so decisions are reproducible, searchable, and useful as agent context. Each RDR is written once and never deleted — closing an RDR means updating its status, not removing it.

**New to RDR?** Start with the [Overview](../rdr-overview.md) — it covers when to write one, how to right-size it, and evidence classification. Then read the [Workflow](../rdr-workflow.md) for the full lifecycle.

## Process Documentation

| Doc | What it covers |
|-----|---------------|
| [Overview](../rdr-overview.md) | What RDRs are, when to write one, evidence classification, the iterative pattern |
| [Workflow](../rdr-workflow.md) | Create → Research → Gate → Accept → Close, with worked example |
| [Nexus Integration](../rdr-nexus-integration.md) | How storage tiers (T2/T3) and agents amplify RDRs |
| [Templates](../rdr-templates.md) | Minimal and full RDR examples, post-mortem template, drift taxonomy |

---

## All RDRs

| ID | Title | Type | Status | Created |
| -- | ----- | ---- | ------ | ------- |
| [RDR-001](rdr-001-rdr-process-validation.md) | RDR Process Validation | Architecture | Closed | 2026-02-27 |
| [RDR-002](rdr-002-t2-status-synchronization.md) | T2 Status Synchronization | Technical Debt | Closed | 2026-02-27 |
| [RDR-004](rdr-004-four-store-architecture.md) | Four-Store T3 Architecture | Architecture | Closed | 2026-02-28 |
| [RDR-005](rdr-005-chromadb-cloud-quota-enforcement.md) | ChromaDB Cloud Quota Enforcement | Architecture | Closed | 2026-02-28 |
| [RDR-006](rdr-006-chunk-size-configuration.md) | File-Size Scoring Penalty for Code Search | Feature | Closed | 2026-02-28 |
| [RDR-007](rdr-007-claude-adoption-session-context-and-search-guidance.md) | Claude Adoption: Session Context Gaps and Search Tool Guidance | Feature | Closed | 2026-02-28 |
| [RDR-008](rdr-008-nx-workflow-integration.md) | nx Workflow Integration: Protocol Standardization and Knowledge Accumulation | Architecture | Closed | 2026-03-01 |
| [RDR-009](rdr-009-remove-agentic-and-answer-flags.md) | Remove --agentic and --answer Flags from nx search | Technical Debt | Closed | 2026-03-01 |
| [RDR-010](rdr-010-t1-scratch-persistent-bounded-store.md) | T1 Scratch: Cross-Process Session Sharing via ChromaDB Server + PPID Chain | Architecture | Closed | 2026-03-01 |
| [RDR-011](rdr-011-pdf-ingest-test-coverage.md) | PDF Ingest Test Coverage: Unit, Subsystem, and E2E with Local ChromaDB | Testing | Closed | 2026-03-01 |
| [RDR-012](rdr-012-pdfplumber-extraction-tier.md) | pdfplumber Extraction Tier for Complex-Table PDFs | Architecture | Closed | 2026-03-01 |
| [RDR-013](rdr-013-remove-nx-pm-layer.md) | Remove nx pm Layer — Use T2 Memory Directly | Architecture | Closed | 2026-03-01 |
| [RDR-014](rdr-014-knowledge-base-retrieval-quality.md) | Knowledge Base Retrieval Quality: Code Context and Docs Deduplication | Bug | Closed | 2026-03-02 |
| [RDR-015](rdr-015-indexing-pipeline-rethink.md) | Indexing Pipeline Rethink: Align Nexus with Arcaneum's Battle-Tested Implementation | Enhancement | Closed | 2026-03-02 |
| [RDR-016](rdr-016-ast-chunk-line-range-bug.md) | AST Chunk Line Range Bug: CodeSplitter Returns Empty Metadata, Breaking Context Prefix | Bug | Closed | 2026-03-03 |
| [RDR-017](rdr-017-indexing-progress-reporting.md) | Indexing Progress Reporting: tqdm-Based Progress Bar for nx index | Enhancement | Closed | 2026-03-03 |
| [RDR-018](rdr-018-replace-serve-with-git-hooks.md) | Replace nx serve Polling Server with Git Hooks | Refactor | Closed | 2026-03-03 |
| [RDR-019](rdr-019-chromadb-transient-retry.md) | ChromaDB Transient HTTP Error Retry | Bug Fix | Closed | 2026-03-04 |
| [RDR-020](rdr-020-voyage-chromadb-read-timeout.md) | Voyage AI and ChromaDB Client Read Timeouts | Bug Fix | Closed | 2026-03-05 |
| [RDR-021](rdr-021-docling-pdf-extraction.md) | Replace 3-Tier PDF Extraction Stack with Docling | Enhancement | Closed | 2026-03-05 |
| [RDR-022](rdr-022-memory-delete-command.md) | Add delete subcommand to nx memory, nx store, nx scratch | Enhancement | Closed | 2026-03-05 |
| [RDR-023](rdr-023-agent-tool-permissions-audit.md) | Agent Tool Permissions Audit and Remediation | Enhancement | Closed | 2026-03-07 |
| [RDR-024](rdr-024-rdr-process-guardrails.md) | RDR Process Guardrails: Prevent Implementation Before Gate/Accept | Enhancement | Closed | 2026-03-07 |
| [RDR-025](rdr-025-language-agnostic-agents.md) | Generalize Java Agents to Language-Agnostic Developer/Debugger/Architect-Planner | Enhancement | Closed | 2026-03-08 |
| [RDR-026](rdr-026-hybrid-search-fusion.md) | Hybrid Search — Exact-Match Score Boosting | Feature | Closed | 2026-03-08 |
| [RDR-027](rdr-027-search-results-ux.md) | Search Results UX — Context Lines and Syntax Highlighting | Feature | Closed | 2026-03-08 |
| [RDR-028](rdr-028-code-search-recall.md) | Code Search Recall — Language Registry Unification | Enhancement | Closed | 2026-03-08 |
| [RDR-029](rdr-029-pipeline-versioning.md) | Pipeline Versioning — Force Reindex and Collection Version Stamping | Enhancement | Closed | 2026-03-08 |
| [RDR-030](rdr-030-reliability-hardening.md) | Reliability Hardening — Silent Error Audit and Logging Policy | Enhancement | Closed | 2026-03-08 |
| [RDR-031](rdr-031-collection-portability.md) | Collection Portability — Export/Import for T3 Backup and Migration | Feature | Closed | 2026-03-08 |
| [RDR-032](rdr-032-indexer-decomposition.md) | Indexer Module Decomposition and Configuration Externalization | Technical Debt | Closed | 2026-03-08 |
| [RDR-033](rdr-033-pdf-agent-nx-index-alignment.md) | PDF Processing Agent Should Delegate to nx index pdf | Architecture | Closed | 2026-03-08 |
| [RDR-034](rdr-034-mcp-server-agent-storage.md) | MCP Server for Agent Storage Operations | Architecture | Closed | 2026-03-11 |
| [RDR-035](rdr-035-plugin-agent-mcp-tool-access.md) | Fix Plugin Agent MCP Tool Access | Bugfix | Closed | 2026-03-12 |
| [RDR-036](rdr-036-post-accept-planning-workflow.md) | Post-Accept Planning Workflow | Enhancement | Closed | 2026-03-12 |
| [RDR-037](rdr-037-t3-database-consolidation.md) | T3 Database Consolidation | Enhancement | Closed | 2026-03-14 |
| [RDR-038](rdr-038-local-t3-backend.md) | Local T3 Backend | Architecture | Closed | 2026-03-14 |
| [RDR-039](rdr-039-claude-code-framework-alignment.md) | Claude Code Framework Alignment (v2.1.72–v2.1.81) | Technical Debt | Closed | 2026-03-21 |
| [RDR-040](rdr-040-developer-agent-circuit-breaker.md) | Developer Agent Circuit Breaker for Test Failure Escalation | Architecture | Closed | 2026-03-25 |
| [RDR-041](rdr-041-t1-scratch-inter-agent-context.md) | T1 Scratch Inter-Agent Context Sharing | Architecture | Closed | 2026-03-26 |
| [RDR-042](rdr-042-agenticscholar-enhancements.md) | AgenticScholar-Inspired Enhancements | Architecture | Closed | 2026-03-29 |
| [RDR-043](rdr-043-plan-enricher-scope.md) | Widen Plan-Enricher Scope | Enhancement | Closed | 2026-03-30 |
| [RDR-044](rdr-044-math-aware-pdf-extraction.md) | Math-Aware PDF Extraction | Bug | Closed | 2026-03-31 |
| [RDR-045](rdr-045-post-implementation-verification.md) | Post-Implementation Verification Gate | Architecture | Closed | 2026-04-01 |
| [RDR-046](rdr-046-mineru-server-backed-extraction.md) | MinerU Server-Backed PDF Extraction | Architecture | Closed | 2026-04-02 |
| [RDR-047](rdr-047-large-pdf-extraction-resilience.md) | Large PDF Extraction Resilience | Architecture | Closed | 2026-04-05 |
| [RDR-048](rdr-048-streaming-pdf-pipeline.md) | Streaming PDF Pipeline | Architecture | Closed | 2026-04-03 |
| [RDR-049](rdr-049-git-backed-catalog.md) | Git-Backed Xanadu-Inspired Catalog for T3 | Architecture | Closed | 2026-04-05 |
| [RDR-050](rdr-050-knowledge-graph-query-planning.md) | Knowledge Graph and Catalog-Aware Query Planning | Architecture | Closed | 2026-04-05 |
| [RDR-051](rdr-051-link-lifecycle.md) | Link Lifecycle: Full CRUD, Queryable Links, Bulk Operations | Architecture | Closed | 2026-04-05 |
| [RDR-052](rdr-052-catalog-first-query-routing.md) | Catalog-First Query Routing — Push Planning into MCP | Architecture | Closed | 2026-04-05 |
| [RDR-053](rdr-053-xanadu-fidelity.md) | Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed Spans | Architecture | Closed | 2026-04-05 |
| [RDR-054](rdr-054-chunk-boundary-equation-splitting.md) | Chunk Boundary Equation Splitting — Information Loss at Chunk Boundaries | Bug | Closed | 2026-04-07 |
| [RDR-055](rdr-055-section-type-metadata-quality-scoring.md) | Section-Type Metadata and Quality Scoring for Knowledge Collections | Feature | Closed | 2026-04-07 |
| [RDR-056](rdr-056-search-robustness-result-clustering.md) | Search Robustness and Result Clustering | Feature | Closed | 2026-04-07 |
| [RDR-057](rdr-057-progressive-formalization-memory-tiers.md) | Progressive Formalization Across Memory Tiers | Feature | Closed | 2026-04-07 |
| [RDR-058](rdr-058-pipeline-orchestration-plan-reuse.md) | Pipeline Orchestration and Plan Reuse | Feature | Closed | 2026-04-07 |
| [RDR-059](rdr-059-code-search-embedding-mismatch.md) | Code Search Embedding Model Mismatch | Bug | Closed | 2026-04-07 |
| [RDR-060](rdr-060-catalog-path-rationalization-link-usability.md) | Catalog Path Rationalization and Link Graph Usability | Feature | Closed | 2026-04-08 |
| [RDR-061](rdr-061-literature-grounded-search-knowledge-enhancement.md) | Literature-Grounded Search and Knowledge Enhancement Roadmap | Design | Closed | 2026-04-09 |
| [RDR-062](rdr-062-mcp-interface-tiering.md) | MCP Interface Tiering — Core + Catalog Server Split | Architecture | Closed | 2026-04-09 |
| [RDR-063](rdr-063-t2-domain-split.md) | T2 Domain Split — Separating Memory, Plans, Catalog, and Telemetry | Architecture | Closed | 2026-04-10 |
| [RDR-064](rdr-064-nx-console-embedded-web-ui.md) | nx console — Process Monitor for Agentic Nexus | Feature | Closed (implemented) | 2026-04-11 |
| [RDR-065](rdr-065-close-time-funnel-hardening.md) | Close-Time Funnel Hardening Against Silent Scope Reduction | Process | Closed | 2026-04-11 |
| [RDR-066](rdr-066-composition-smoke-probe-at-coordinator-beads.md) | Composition Smoke Probe at Coordinator Beads | Process | Closed | 2026-04-11 |
| [RDR-067](rdr-067-cross-project-rdr-audit-loop.md) | Cross-Project RDR Audit Loop | Process | Closed | 2026-04-11 |
| [RDR-068](rdr-068-dimensional-contracts-at-enrichment.md) | Dimensional Contracts at Enrichment | Process | Closed | 2026-04-11 |
| [RDR-069](rdr-069-automatic-substantive-critic-at-close.md) | Automatic Substantive-Critic at Close | Process | Closed | 2026-04-11 |
| [RDR-070](rdr-070-incremental-taxonomy-clustered-search.md) | Incremental Taxonomy & Clustered Search | Feature | Closed | 2026-04-12 |
| [RDR-071](rdr-071-query-sanitizer-permanence-mode.md) | Query Sanitizer | Feature | Closed | 2026-04-13 |
| [RDR-072](rdr-072-progressive-context-loading.md) | Progressive Context Loading | Feature | Closed | 2026-04-13 |
| [RDR-073](rdr-073-temporal-entity-knowledge-graph.md) | Temporal Entity Knowledge Graph | Feature | Deferred | 2026-04-13 |
| [RDR-074](rdr-074-permanence-mode.md) | Permanence Mode | Feature | Deferred | 2026-04-13 |
| [RDR-075](rdr-075-cross-collection-topic-projection.md) | Cross-Collection Topic Projection | Feature | Closed (implemented) | 2026-04-13 |
| [RDR-076](rdr-076-idempotent-upgrade-mechanism.md) | Idempotent Upgrade Mechanism | Architecture | Closed (implemented) | 2026-04-13 |
| [RDR-077](rdr-077-projection-quality-similarity-icf.md) | Projection Quality — Similarity Scores and ICF Hub Detection | Feature | Closed (implemented) | 2026-04-14 |
| [RDR-078](rdr-078-unified-context-graph-and-retrieval.md) | Plan-Centric Retrieval — Semantic Plan Matching, Typed-Graph Traversal, Scenario Plans | Feature | Closed (implemented) | 2026-04-14 |
| [RDR-079](rdr-079-operator-dispatch-and-execution.md) | Operator Dispatch + Plan Execution End-to-End | Feature | Abandoned | 2026-04-15 |
| [RDR-079 P5](rdr-079-calibration.md) | min_confidence calibration (ROC measurement spun off from RDR-079) | Calibration artifact | Closed | 2026-04-15 |
| [RDR-080](rdr-080-retrieval-layer-consolidation.md) | Retrieval Layer Consolidation — `nx_answer` + Agent/Skill Pruning | Feature | Closed (implemented) | 2026-04-15 |
| [RDR-081](rdr-081-authoring-trust-cli.md) | Stale-Reference Validator (`nx taxonomy validate-refs`) | Feature | Closed (implemented) | 2026-04-15 |
| [RDR-082](rdr-082-doc-render-tokens.md) | Doc-Build Token Resolution — `nx doc render` with Bead and RDR Tokens | Feature | Closed (implemented) | 2026-04-15 |
| [RDR-083](rdr-083-chunk-grounded-citations.md) | Corpus-Evidence Tokens — `chash:` Spans, Grounding Validator, `nx-anchor` | Feature | Closed (implemented) | 2026-04-15 |
| [RDR-084](rdr-084-plan-library-growth.md) | Plan Library Growth — Auto-Save Successful Ad-Hoc Plans | Feature | Closed (implemented) | 2026-04-16 |
| [RDR-085](rdr-085-glossary-aware-labeler.md) | Glossary-Aware Topic Labeler — Project Vocabulary via `claude_dispatch` | Feature | Closed (implemented) | 2026-04-16 |
| [RDR-086](rdr-086-chash-span-resolution.md) | Chash Span Surface — Authoring, Resolution, and Verification | Feature | Closed (shipped in v4.7.0) | 2026-04-16 |
| [RDR-087](rdr-087-collection-observability-surfaces.md) | Collection Observability and Curation Surfaces | Feature | Accepted | 2026-04-17 |
| [RDR-088](rdr-088-agenticscholar-operator-completion.md) | AgenticScholar Operator-Set Completion | Feature | Closed (Phases 1+2 implemented; Phase 3 closed by spike) | 2026-04-17 |
| [RDR-089](rdr-089-structured-aspect-extraction-at-ingest.md) | Structured Aspect Extraction at Ingest | Feature | Draft | 2026-04-17 |
| [RDR-090](rdr-090-realistic-agenticscholar-benchmark.md) | Realistic AgenticScholar Benchmark | Feature | Draft | 2026-04-17 |
| [RDR-091](rdr-091-scope-aware-plan-matching.md) | Scope-Aware Plan Matching (nexus-zs1d Phase 2) | Feature | Closed (implemented) | 2026-04-23 |
| [RDR-092](rdr-092-plan-match-text-from-dimensional-identity.md) | Plan Match-Text from Dimensional Identity | Feature | Closed (implemented) | 2026-04-23 |
| [RDR-093](rdr-093-groupby-aggregate-operators.md) | GroupBy and Aggregate Operators | Feature | Accepted | 2026-04-24 |
| [RDR-094](rdr-094-mcp-owned-t1-chroma-lifecycle.md) | MCP-Owned T1 Chroma Lifecycle | Architecture | Closed | 2026-04-24 |
| [RDR-095](rdr-095-post-store-hook-batch-contract.md) | Post-Store Hook Framework: Batch Contract | Architecture | Closed (implemented) | 2026-04-25 |

---

**Reading order:** [Overview](../rdr-overview.md) | [Workflow](../rdr-workflow.md) | [Nexus Integration](../rdr-nexus-integration.md) | [Templates](../rdr-templates.md) | RDR Index (this page)
