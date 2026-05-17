# Proposal: Applying "M3DocRAG" Findings to Nexus

**Status:** draft — proposal + scoped spike (no production code change)
**Author:** synthesis of /nx:research run 2026-05-17
**Source paper:** Cho, Bansal, Yoon et al., *M3DocRAG: Multi-modal Retrieval is What You Need for Multi-page Multi-document Understanding*, arXiv:2411.04952 (2024, NeurIPS-era)
**Source code:** local clone at `/Users/hal.hildebrand/git/m3docrag`, indexed in `code__1-13__voyage-code-3__v1`
**Paper indexed at:** `knowledge__dt-papers__voyage-context-3__v1`, tumbler `1.14.1`
**Research finding:** T3 docs `e003ad4edd1088e8242fa72423ffba5a` + `781129dbd239bdc71835792563f6e4d2` (`knowledge__knowledge__voyage-context-3__v1`)
**Sibling proposals:** `beyond-similarity-search-application.md`, `open-ontologies-application.md`

This document is a **proposal**, not an in-place edit of architecture or RDRs. The accompanying text-only spike (`scripts/colbert_recall_spike.py`) is a measurement scaffold — it does not change retrieval behavior in production.

**Revision 2026-05-17:** the initial draft rejected the multi-modal path as "GPU-only." §5 has been rewritten to quantify hardware requirements component-by-component. The generator (Qwen2-VL 7B) is GPU-mandatory and still rejected; ColPali retrieval alone is feasible on Apple Silicon via MPS / MLX and is **reconsidered** for a scoped spike (§5b). A lighter figure-only path via SigLIP is added as a third spike candidate.

---

## 1. What the paper claims

M3DocRAG is a multi-modal RAG pipeline for visually rich documents. Three stages:

1. Convert each PDF page to an image (`pdf2image` @ 144 DPI).
2. Embed each page as a token-matrix with ColPali (Faysse et al. 2024). Index the token vectors in Faiss IVFFlat. Retrieval scores pages via ColBERT-style MaxSim across query and page tokens.
3. Feed retrieved page images plus the user query to Qwen2-VL 7B to generate the answer.

Headline results: SOTA on MP-DocVQA; best overall F1 on MMLongBench-Doc; IVFFlat brings open-domain retrieval over 41,005 pages from ~21 s to <2 s with minimal accuracy loss.

Released artifacts: code (MIT, https://github.com/bloomberg/m3docrag), the M3DocVQA benchmark (2,441 multi-hop questions over 3,368 PDFs). Trained weights are HuggingFace `vidore/` (ColPali) and HuggingFace `Qwen/Qwen2-VL-7B-Instruct` — not shipped in the repo.

## 2. Paper vs code — close match

Read both. The pipeline described in §2.1–2.3 is faithfully implemented:

| Component | Paper claim | Code reality | Verdict |
|---|---|---|---|
| Page rasterisation | 144 DPI | `src/m3docrag/utils/pdfs.py:24` `convert_from_path(pdf, dpi=dpi_resolution)` with default 144 | match |
| ColPali embeddings | per-page token matrix | `src/m3docrag/retrieval/colpali.py:64–129` `encode_images` returns `(n_pages, n_tokens, emb_dim)` | match |
| Index | Faiss IVFFlat | `examples/run_indexing_m3docvqa.py:56–114` `IndexIVFFlat(quantizer, d=128, ncentroids=1024)` + `token2pageuid` list + `faiss.write_index()` | match; paper omits `ncentroids=1024` |
| MaxSim scoring | ColBERT-style | `src/m3docrag/rag/base.py:82–143` — iterate query tokens, nearest doc tokens via Faiss, max-score per page per query, sum, sort | match |
| Generator | Qwen2-VL 7B, up to 4 page-images | `src/m3docrag/vqa/qwen2.py:26–100` Flash-Attention 2, BF16, `max_new_tokens=128`, ≤4 images | match |
| Embedding dimension | (not stated) | 128-dim (Faiss `d=128`) | paper omits; code reveals |
| Model weight paths | HuggingFace `vidore/` | hard-coded `/job/model/` cluster paths at `colpali.py:29` (README clarifies) | UX divergence only |
| `return_doclens` feature | working | `colpali.py:127` missing `return` keyword — returns `None` | silent bug; does not affect main pipeline |

No major structural divergences. The repo is a usable reference implementation. The benchmark eval ships in `m3docvqa/`; weights and data are downloaded separately.

## 3. What's novel

From most to least novel:

1. **The M3DocVQA benchmark.** 2,441 multi-hop questions over 3,368 PDFs / 41,005 pages, derived from MultimodalQA. A genuine contribution independent of the pipeline.
2. **Empirical demonstration** that page-image retrieval + multi-modal LM beats text RAG on visually rich documents, with the gap concentrated in chart/figure evidence (§5.1).
3. **Pipeline engineering.** Standard three-stage RAG composing existing components — ColPali (Faysse 2024) for retrieval, Faiss for indexing, Qwen2-VL for generation. The IVFFlat 21 s → <2 s latency claim is standard Faiss applied to ColPali embeddings, not a M3DocRAG contribution.

**The pipeline's wins are mostly ColPali's wins.** Without ColPali, M3DocRAG is a standard RAG system.

## 4. What's portable to nexus

### 4.1 ColBERT-style late interaction — INVESTIGATE (this proposal's only adoption candidate)

Nexus retrieves with single-vector dense embeddings (Voyage `voyage-context-3`, 1024-dim per chunk). Late-interaction retrieval encodes query and document as token matrices (typically 128-dim per token) and scores via MaxSim — `max` over doc-token similarity per query token, summed across query tokens. Pro: better recall for multi-faceted queries and vocabulary mismatch. Con: larger index (one vector per token, not per chunk), more compute at retrieval time.

M3DocRAG's MaxSim implementation in `rag/base.py:82–143` is a clean reference. The ColPali variant is GPU-only; the text-only ColBERT variant is CPU-compatible and is what is portable to nexus.

**Adoption shape (if the spike supports it):** additive index alongside the existing Voyage collection. Pick a high-value subset (RDR collection, code collection) where vocabulary mismatch is most likely to hurt single-vector recall. Expose as an optional reranker layer or as a parallel retriever whose results merge into the existing reranker.

This finding cross-links with the **Beyond Similarity Search** sibling (`24c5da96…`): both papers argue for multi-signal retrieval. Nexus's reranker (`search_engine.py:628–658`) already does post-retrieval signal mixing (topic + salience boosts). Late interaction would add a pre-retrieval signal class that the current pipeline doesn't have. Likewise the **Open Ontologies** sibling (`743c3e48…`) recommended adding signals, not retuning weights — late interaction is exactly such a signal class.

### 4.2 Cite §5.1 in nexus docs — ACCEPT (low cost, high confidence)

M3DocRAG §5.1 is the cleanest external evidence for the text-vs-multimodal RAG gap, and concentrates the gap in visual evidence with no textual description. This is the single best citation for nexus's text-first PDF strategy. Adding one sentence to `docs/architecture.md` and the PDF section of `docs/cli-reference.md` (the user-facing ingest doc) costs nothing and grounds a design choice readers might otherwise question.

## 5. Hardware requirements — quantified, not dismissed

The initial synthesis rejected the multi-modal path as "CUDA-only." That was overstated. The M3DocRAG repo's `pyproject.toml` does pin `flash-attn==2.5.8` + `bitsandbytes==0.43.1`, but those pins are for the **generator** (Qwen2-VL 7B), not for ColPali retrieval. The actual decomposition:

| Component | Model | Memory (BF16) | Apple Silicon? | Notes |
|---|---|---|---|---|
| ColPali retrieval (index time) | PaliGemma-3B base | ~6 GB | **Yes (MPS)** | Transformers supports MPS for PaliGemma. INT4 via `mlx-llm` ~1.5 GB. Throughput 5–20× slower than H100; for nexus's small corpus (~10s of papers, ~100s of pages) that's ~5–10 minutes per paper on M2 Max — acceptable for index-time work. |
| ColPali retrieval (query time) | (token-level Faiss MaxSim) | proportional to index size | **CPU sufficient** | Once the index is built, retrieval is a Faiss IVFFlat lookup; the per-token MaxSim accumulation runs fine on CPU. |
| Qwen2-VL 7B (generator) | Qwen2-VL 7B | ~14 GB BF16 / ~4 GB INT4 | Possible on M-Max class | **Nexus doesn't need this.** Synthesis is done by the calling LLM (Claude in MCP). |
| `flash-attn` | — | — | **CUDA-only** | Only required by Qwen2-VL inference, not by ColPali. |
| `bitsandbytes` 0.43.1 | — | — | **CUDA-only** | Only used for INT4/INT8 quantization in the M3DocRAG repo's generator path. Apple Silicon uses MLX instead. |

So the deployability picture is more nuanced:

- **Full M3DocRAG (retrieval + Qwen2-VL generator)** — still GPU-mandatory for the generator. **Reject.**
- **ColPali retrieval alone, no generator** — feasible on Apple Silicon (transformers + MPS, or MLX with INT4). Index-time slow; query-time fast. **Worth quantifying with a small Apple-Silicon benchmark before deciding.**
- **Figure-only embeddings via SigLIP/CLIP** — a much lighter path: extract figure bounding boxes from PDFs (already in Docling's output), embed each figure with SigLIP-base (~400 MB, runs comfortably on CPU/MPS), and store the figure embeddings as a parallel index. Captures most of M3DocRAG §5.1's "visual-only signal" wins without the PaliGemma footprint. **Worth a separate spike.**

The text-only ColBERT spike (§6) is unchanged in scope — it's about late-interaction text retrieval, independent of figures. But the figure-image path deserves its own measurement.

## 5a. What to reject

| Item | Why reject |
|---|---|
| Full M3DocRAG pipeline (with Qwen2-VL in answer path) | Qwen2-VL is GPU-mandatory at usable quality, and nexus has no generation path — synthesis happens in the calling LLM. Adding a second LM in-loop also requires MCP-protocol changes. |
| `flash-attn` / `bitsandbytes` dependencies | Apple Silicon equivalents are MLX-native; do not adopt the CUDA-pinned deps. |
| Per-page image indexing as the *default* retrieval unit | Nexus's sub-page text chunking (`pdf_chunker.py: _DEFAULT_CHUNK_CHARS=1500`) gives better granularity for text-only queries. Page-image retrieval is additive when it lands, not a replacement. |
| M3DocVQA as nexus retrieval benchmark | Wrong task (reading-comprehension QA over scanned PDFs), wrong corpus. Use it as a sanity-check that the ColPali retriever works on a known dataset, nothing more. |

## 5b. What to reconsider (was rejected, now scoped)

| Item | Status | Spike scope |
|---|---|---|
| ColPali retrieval on Apple Silicon (no generator) | **Reconsider** | Index a 20-PDF subset with ColPali via transformers+MPS. Measure: per-page index time, query latency, recall@10 vs Voyage on figure-heavy queries. File a bead if index time ≤ 60s/page and recall@10 lift on figure-bearing queries ≥ 10pp. |
| Figure-only embeddings via SigLIP | **New, deferred** | Use Docling's figure bounding boxes (already extracted); embed via `sentence-transformers/clip-ViT-B-32` or SigLIP base; store as a parallel index per PDF. Test on the same figure-heavy query set. Lighter footprint (~400 MB model, CPU OK). |

## 6. The spike

This proposal ships a measurement scaffold, not a production change: `scripts/colbert_recall_spike.py`.

What it does:

- Loads a configurable sample (default 200) of chunks from a nexus T3 collection (default `rdr__nexus`).
- Generates a synthetic query set by extracting salient bigrams/trigrams from chunk titles and section headings, OR uses an externally provided query file.
- For each query, computes ground-truth relevance by string overlap against source chunk text (held-out chunk is the relevant one).
- Runs two retrieval pipelines on the same chunks:
  1. **Baseline:** Voyage embeddings (already in T3) → cosine similarity.
  2. **Candidate:** Late-interaction via the `pylate` library (CPU-compatible ColBERT) over the same chunk text.
- Reports recall@10 and MRR@10 for both, plus index size and per-query latency.

Decision rule: if late interaction shows **≥5 % absolute recall@10 lift** on RDR or code collections, file a bead to add an opt-in late-interaction index as a parallel retriever. Otherwise reject as not worth the index-size cost.

Why this is a spike and not adoption:

- New optional dependency (`pylate`, model weights ~250 MB).
- Adds ~8× index size in the worst case (one vector per token at 128 dims vs one per chunk at 1024 dims) — a real cost.
- Single-user nexus has plenty of compute headroom but Mac index size is still a constraint.

Run instructions are in the script's docstring. The script is gated behind `--collection` and reads from existing T3; it does not modify state.

## 7. Cross-links with sibling proposals

- **Beyond Similarity Search** (`beyond-similarity-search-application.md`): the retrieval layer's split-tier consistency problem. Late interaction is orthogonal — it changes *what* is retrieved, not *where* the manifest authority lives.
- **Open Ontologies** (`open-ontologies-application.md`): operator decomposition for LLM-callable tools, plus stable matching for catalog link generation. Independent surfaces from this proposal.
- **All three sibling papers agree** on one architectural claim: multi-signal retrieval beats single-signal. M3DocRAG adds the strongest specific candidate (late interaction); Beyond Similarity Search adds the general framing; Open Ontologies' weight-irrelevance result reinforces that signal diversity matters more than signal weighting.

## 8. Decisions asked

1. **Accept architecture.md + cli-reference.md citation** (§4.2)? — yes / no / modify.
2. **Run the text-only ColBERT spike and decide on the +5 % rule** (§6)? — yes / no / defer.
3. **If the spike passes the threshold, file a bead for an opt-in late-interaction parallel retriever**? — yes / conditional / defer.
4. **Run the ColPali-on-Apple-Silicon retrieval spike** (§5b)? — yes / no / defer. Scope and threshold spelled out in the table; no Qwen2-VL involved.
5. **Run the figure-only SigLIP spike** (§5b)? — yes / no / defer. Lighter footprint than ColPali; uses Docling's existing figure extraction.
6. **Promote to RDR?** — only if any spike supports adoption; the proposal + spikes alone don't need one.

No code changes from this document. The text-only spike script (`scripts/colbert_recall_spike.py`) is measurement-only. The two new figure/page-image spikes (§5b) are not yet scaffolded — they're scoped here so the conversation can choose whether to invest in scaffolding them.
