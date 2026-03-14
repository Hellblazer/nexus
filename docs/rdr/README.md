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
| [RDR-034](rdr-034-mcp-server-agent-storage.md) | MCP Server for Agent Storage Operations | Architecture | Accepted | 2026-03-11 |
| [RDR-035](rdr-035-plugin-agent-mcp-tool-access.md) | Fix Plugin Agent MCP Tool Access | Bugfix | Closed | 2026-03-12 |
| [RDR-036](rdr-036-post-accept-planning-workflow.md) | Post-Accept Planning Workflow | Enhancement | Accepted | 2026-03-12 |
| [RDR-037](rdr-037-t3-database-consolidation.md) | T3 Database Consolidation | Enhancement | Accepted | 2026-03-14 |

---

**Reading order:** [Overview](../rdr-overview.md) | [Workflow](../rdr-workflow.md) | [Nexus Integration](../rdr-nexus-integration.md) | [Templates](../rdr-templates.md) | RDR Index (this page)
