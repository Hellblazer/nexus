# Proposal: Applying "Open Ontologies" Findings to Nexus

**Status:** draft — proposal only, no code or doc changes yet
**Author:** synthesis of /nx:research run 2026-05-17
**Source paper:** Fabio Rovai, *Open Ontologies: Tool-Augmented Ontology Engineering with Stable Matching Alignment*, arXiv:2512.05594 (2026)
**Indexed at:** `knowledge__dt-papers__voyage-context-3__v1`, tumbler `1.12.2`
**Source repo:** https://github.com/fabio-rovai/open-ontologies (Rust, ~17.4 k LOC, MIT)
**Research finding:** T3 doc `743c3e484617712304878893ca3fcc9c` (`knowledge__knowledge__voyage-context-3__v1`)
**Sibling proposal:** `docs/proposals/beyond-similarity-search-application.md`

This document is a **proposal**, not an in-place edit of `docs/architecture.md`, RDR-088, RDR-101, or any other existing artifact. Accepted recommendations become beads; bead work happens on its own branches.

---

## 1. What the paper claims

The paper presents Open Ontologies, a Rust + MCP system exposing 43 narrowly-typed tools (`onto_validate`, `onto_align`, `onto_diff`, `onto_search`, `onto_similarity`, …) across 14 functional categories. Core ideas:

1. **Tool decomposition over raw context.** Compared to passing OWL files into an LLM, exposing typed tools that the LLM composes step-by-step dramatically improves task accuracy.
2. **Stable 1-to-1 matching with a label penalty.** Alignment is a weighted six-signal similarity score followed by Gale-Shapley stable matching that downweights structurally unsupported matches.
3. **Quantitative results** (OAEI Anatomy F1 0.182 → 0.832; OntoAxiom F1 conditions):
   - A: unaided LLM = **0.431**
   - B: LLM + typed MCP tools = **0.717** (+66 % vs A)
   - C: LLM + structured summary in prompt = 0.430 (no benefit vs A)
   - D: LLM + raw OWL file in context = **0.323** (*worse* than unaided)
4. **Weight irrelevance.** Five signal-weight configurations under stable matching produce F1 0.830–0.834 (<0.004 spread). The matching constraint dominates over weight tuning.

LLM used: Claude Opus 4 (`claude-opus-4-20250514`) — same model family nexus runs against, so portability of LLM-side findings is higher than usual.

## 2. What's portable to nexus

### 2.1 Operator decomposition is the right pattern — independent validation

Condition D below is the headline. **Raw OWL in context (F1 0.323) is worse than no file at all (F1 0.431).** Typed MCP tools (F1 0.717) recover and exceed the unaided baseline. The mechanism is decomposition: the LLM is forced into typed, verifiable steps instead of free-running over a large structured blob.

Nexus arrived at the same conclusion from the AgenticScholar paper (RDR-088). The operator suite at `src/nexus/mcp/core.py:2206–2832` (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`, `operator_filter`, `operator_check`, `operator_verify`, `operator_groupby`, `operator_aggregate`) is the nexus equivalent of the Open Ontologies tool suite, plus a composition layer (`nx_answer` at `core.py:3479`) that the paper's system does not have. Convergent design from two independent domains.

The conclusion is: **the pattern is right; the divergence to watch is structural drift.** Specifically, there is no convention preventing a nexus caller from passing a raw T3 dump to `operator_generate` and recreating Condition D inside nexus. `chroma_quotas.py:MAX_DOCUMENT_BYTES = 16384` is a quota cap, not a usage convention.

### 2.2 Stable matching has exactly one credible nexus application

The paper's stable-matching algorithm applies to any "pick a deterministic 1-to-1 assignment between two fuzzy-matched sets" problem. In nexus that means: where do we currently have a many-to-many or silent-failure matching problem that should be 1-to-1?

**Candidate: `src/nexus/catalog/link_generator.py:61–66` — fuzzy bib-ID bridge.** Today the citation auto-linker matches references by exact string within a single ID space (S2 paperIds or OpenAlex W-IDs). The code comment is explicit:

> cross-backend references (a paper enriched by OpenAlex referencing one enriched only by S2) won't match — that's the correct conservative behavior, since the two ID spaces are distinct and we don't have a DOI bridge yet.

A small 3-signal similarity (title Jaro-Winkler + year delta + DOI suffix) followed by Gale-Shapley would build that DOI bridge cleanly. Each reference maps to at most one catalog entry. 1-to-1 cardinality is exactly the constraint Gale-Shapley enforces.

Other candidates fail the 1-to-1 test: plan→bead matching is asymmetric; chash dedupe is already exact; `relates` links are intentionally many-to-many.

### 2.3 Multi-signal similarity is already covered

The paper's six signals are: label similarity (Jaro-Winkler + token Jaccard), property overlap, parent overlap, instance overlap, semantic embedding, structural embedding. Signals 2–4 are ontology-specific.

Nexus's reranker (`src/nexus/search_engine.py`) uses: vector distance, catalog prefilter (`:303`), topic boost (`:628`), salience boost (`:646`). Different signal classes for a different domain, but the same multi-signal pattern.

The **operational lesson from the paper's weight-irrelevance result** is: don't spend engineering effort on reranker weight tuning. Spend it on signal diversity. The sibling Beyond Similarity Search finding agrees on this point from a different angle.

## 3. What to reject

| Idea | Why reject |
|---|---|
| OWL / triple store / SHACL / OWL-RL reasoner | Catalog link types are a closed enum of seven types (`docs/catalog-link-types.md`). Consistency checks are two-line SQL against T2. Graph is O(thousands of edges), not millions. Raw-SQL invariant per `CLAUDE.md` excludes ORMs and rules out a new storage engine here. |
| Adopting Open Ontologies as a dependency | Rust binary, ontology-specific. Nexus's domain is knowledge retrieval, not ontology engineering. Zero callers. |
| Passing raw catalog or T3 dump as single LLM context | Condition D from the paper: actively worse than unaided. The right move is decomposition via operators, which nexus already has. |
| Reranker weight-tuning sprints | Paper's weight irrelevance + Beyond Similarity Search's portability skepticism both argue this is low-leverage. |

## 4. Recommendations

| Priority | Recommendation | Confidence | Next step |
|---|---|---|---|
| **P0** | One-paragraph note in `docs/architecture.md` recording Open Ontologies' Condition A/B/D result as external validation of operator decomposition over raw-dump. Cite arXiv:2512.05594. | HIGH | doc-only, file bead |
| **P1** | Spike Gale-Shapley fuzzy stable matching in `generate_citation_links()` for cross-backend bib-ID bridging. 3 signals (title Jaro-Winkler + year delta + DOI suffix). Behind a feature flag; measure false-positive rate against a held-out set of known same-paper-different-backend pairs. | MEDIUM | file bead, target `link_generator.py` |
| **P2** | Add a note in RDR-101 § Further Work: reranker weight tuning is low-leverage per Open Ontologies + Beyond Similarity Search converging evidence. Future work should add signals, not retune weights. | MEDIUM | doc-only |
| **P3** | Doc convention in `docs/mcp-servers.md` or `CLAUDE.md`: do not invoke MCP operators with raw T3 / catalog dumps that bypass decomposition. Cite paper Condition D. | LOW | doc-only |
| (reject) | No OWL / triple store / SHACL adoption. | HIGH | nothing |

P0 and P3 are doc-only and small. P1 is the only real engineering candidate from this paper; it is **investigate, not adopt** — start with a measurement spike before any production change. P2 is one paragraph.

## 5. Cross-link with the sibling proposal

The Beyond Similarity Search proposal (`docs/proposals/beyond-similarity-search-application.md`) addressed the **retrieval layer**: T2/T3 split consistency, manifest-first contract, hot/warm/cold framing. This Open Ontologies proposal addresses the **interaction layer**: how LLMs use nexus's tool surface, and how the catalog graph is constructed.

Where the two interact:

- **Multi-signal ranking.** Both papers converge: signal diversity matters, weight tuning does not. RDR-101 § Further Work should record this as joint evidence.
- **Tool surface discipline.** Beyond Similarity Search argues for cross-tier consistency; Open Ontologies argues for cross-tool discipline. Both point at the same broader principle: typed contracts beat shape-free dumps.

## 6. Evidence quality

This paper has a **stronger** evidence base than the Beyond Similarity Search sibling:

- Code released and substantive (~17.4 k LOC Rust, 43 tools, real Tauri studio, clinical Arrow/Parquet support — verified from the GitHub repo as primary source, not inferred from paper alone).
- Ablations present (`ablations_present: true` per the extracted aspect record).
- Real OAEI benchmarks with published baselines (LogMap F1=0.67, BERTMap F1=0.71) — not a wholly synthetic corpus.
- Same LLM family (Claude Opus 4) as nexus, improving result portability for the tool-design claims.

Remaining cautions:

- LLM A/B/C/D conditions were single-run; the +66 % F1 claim has no variance bound.
- Condition D simulated "raw file in prompt" via tool injection. The paper acknowledges this may slightly inflate the degradation.
- The domain (OWL alignment) is distant from nexus (knowledge retrieval). Generic tool-design lessons transfer; specific benchmarks do not.
- arXiv preprint, no peer review, zero external citations (DOI `10.48550/arXiv.2512.05594`, fresh as of 2026-05-17).

Net: structurally portable conclusions are stronger than the sibling paper. Quantitative claims should still be treated as directional.

## 7. Decisions asked

1. **Accept P0** (architecture.md paragraph on operator decomposition validation)? — yes / no / modify.
2. **Accept P1** (Gale-Shapley bib-ID bridge spike) as a bead? — yes / no / defer.
3. **Accept P2 / P3** (RDR-101 + mcp-servers.md doc notes)? — yes / no / batch with P0.
4. **Promote to RDR?** — only if any of P0–P3 trigger non-trivial code changes. P1 alone might warrant one if its scope expands. — yes / no / defer.

No code changes from this document.
