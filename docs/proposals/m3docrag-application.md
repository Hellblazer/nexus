# Proposal: Applying "M3DocRAG" Findings to Nexus

**Status:** draft — proposal + scoped spike (no production code change)
**Author:** synthesis of /nx:research run 2026-05-17
**Source paper:** Cho, Bansal, Yoon et al., *M3DocRAG: Multi-modal Retrieval is What You Need for Multi-page Multi-document Understanding*, arXiv:2411.04952 (2024, NeurIPS-era)
**Source code:** local clone at `/Users/hal.hildebrand/git/m3docrag`, indexed in `code__1-13__voyage-code-3__v1`
**Paper indexed at:** `knowledge__dt-papers__voyage-context-3__v1`, tumbler `1.14.1`
**Research finding:** T3 docs `e003ad4edd1088e8242fa72423ffba5a` + `781129dbd239bdc71835792563f6e4d2` (`knowledge__knowledge__voyage-context-3__v1`)
**Sibling proposals:** `beyond-similarity-search-application.md`, `open-ontologies-application.md`

This document is a **proposal**, not an in-place edit of architecture or RDRs. The accompanying text-only spike (`scripts/colbert_recall_spike.py`) is a measurement scaffold — it does not change retrieval behavior in production.

**Revision 2026-05-17 (a):** the initial draft rejected the multi-modal path as "GPU-only." §5 was rewritten to quantify hardware requirements component-by-component. The generator (Qwen2-VL 7B) is GPU-mandatory; ColPali retrieval alone is feasible on Apple Silicon via MPS / MLX and is **reconsidered** for a scoped spike (§5b).

**Revision 2026-05-17 (b):** the qwen-coprocessor survey in §5c was wrong. The qwentescence backend already runs Qwen 3.6-35B-A3B with a BF16 vision projector (image-text-to-text); accessible from nexus via OpenAI-compatible HTTP. The lead spike is no longer ColBERT or ColPali — it is **ingest-time VL text augmentation** (§5c) that closes M3DocRAG §5.1's visual-evidence gap by turning the visual signal into text in the chunk stream. Retrieval architecture stays single-vector Voyage; no new pipe, no new generator path. The two earlier-promoted retrieval-only spikes drop in priority.

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
| Figure-only embeddings via SigLIP | **Spike PASSED, cross-paper validated, Docling investigation cleared — bead nexus-8siy** | Single-paper (M3DocRAG): recall@10 = 1.0000, MRR@10 = 0.85. Cross-paper (3 dt-papers): recall@10 = 1.0000, MRR@10 = 0.875, top-1 same-paper match 100 % (6/6). Docling under-detection hypothesis **REFUTED** by `scripts/docling_figure_yield_probe.py`: the per-paper yield (M3DocRAG 7, BSS 1, OO 0) reflects actual paper content, not classification errors. BSS has 1 figure + 4 result tables + SQL; OO has zero figures (the A/B/C/D tool-access ablation we cited is a table, not a figure). Docling correctly classifies them. The figure-image index is genuinely useful only on visually rich papers. No Docling prerequisite needed. |

## 5c. Qwen coprocessor as a local VL backend — REVISED 2026-05-17

**Initial survey conclusion was wrong.** The first draft of this section concluded the qwen-coprocessor was text-only and irrelevant to M3DocRAG. Then the qwentescence operator reported the actual deployment:

> Qwen 3.6-35B-A3B vision-language model — open-weight, Apache-2.0, released 2026-04-24 by the Qwen team as an image-text-to-text model. Hybrid Gated DeltaNet + Gated Attention backbone (3:1), 35B total / 3B active, 256-expert MoE, 262K native context. Running the unsloth `Qwen3.6-35B-A3B-UD-Q4_K_XL` GGUF (~21 GB, ~98 % of Q8 quality at ~1.5× throughput) on an AMD Strix Halo box (128 GB UMA, GPU offload 99 layers, flash-attn, q8_0 KV cache, 131K context). BF16 vision projector `mmproj-Qwen3.6-35B-A3B-BF16.gguf` (~0.85 GB) is on disk and being wired into the launch config. llama-server `/v1/chat/completions` accepts standard OpenAI image content arrays once the projector is loaded. Latency: 1k–5k prefill tokens per high-res image → ~5–15 s of prefill per image + ~30–50 tok/s text generation. Pure image-in / structured-text-out works; mid-loop tool-use during image processing is not wired.

That changes the picture materially. The qwen-coprocessor stack is not the gating piece — **a multi-modal Qwen 3.6 backend is operational (or imminent) at `qwentescence`**, addressable from nexus directly over OpenAI-compatible HTTP. The supervisor doesn't need to know about it: nexus can POST to the backend itself for one-off image-in / text-out calls.

This is bigger than the M3DocRAG question. The original M3DocRAG critique was "Qwen2-VL generator is GPU-mandatory and nexus has no generator." We don't need to add a *generator* — we can use the VL backend for **ingest-time text augmentation**: read figures out of PDFs (Docling already produces bounding boxes), POST each figure + its caption + surrounding prose to the VL backend, get back a structured text description, inject that into the chunk stream. The visual signal becomes text. Retrieval stays single-vector Voyage. No new retrieval pipe, no new generator pipe — just an enrichment hook.

### Revised spike priority

The retrieval-only spikes (§5b) remain valid but **drop in priority** relative to a much simpler path:

| Priority | Spike | Why this priority |
|---|---|---|
| **NEW P0** | **VL-augmented chunk enrichment at ingest** | Closes M3DocRAG §5.1's visual-evidence gap by turning visual signal into text. No retrieval-architecture change. Leverages existing qwentescence backend. Smallest blast radius of any spike here. |
| P1 | Text-only ColBERT (§6) | Independent of VL; addresses vocabulary mismatch on text corpora. Scaffold shipped. |
| P2 | Figure-only SigLIP (§5b) | Lighter than ColPali; an alternative to VL-augmentation IF the VL endpoint is unavailable. Lower priority because VL gives richer text than image embeddings give vector matches. **Spike PASSED 2026-05-17; bead nexus-8siy filed.** |
| P3 | ColPali on Apple Silicon (§5b) | Heaviest spike; only relevant if both VL-augmentation and SigLIP underperform on figure-heavy queries. |

### NEW P0 spike scope

**Hypothesis:** for visually rich PDFs (architecture diagrams, charts, complex tables), VL-generated text descriptions added to the chunk stream meaningfully improve text-retrieval recall on figure-bearing queries vs the existing MinerU/Docling text-only extraction.

**Implementation sketch (proposal-only, not coded yet):**

1. Configuration: new `vl_backend` block in `.nexus.yml` / `~/.config/nexus/config.yml` — URL, model name, on/off flag, timeout, retries. No new dependency; uses `httpx` already in the tree.
2. New post-extract hook: after Docling/MinerU produces page-level structured output, iterate figures and tables with bounding boxes, render each region to PNG (`pdf2image` already a dep), POST to the VL backend with a structured prompt requesting: (a) one-sentence description; (b) for charts: axes, units, trend summary; (c) for tables: row/column labels and any salient values.
3. Output handling: append the returned text to the chunk that owns the figure's bounding-box region. Tag with `vl_augmented: true` metadata so retrieval can prefer / dispreference these chunks if needed. The augmentation text counts toward the chunk's byte budget so we don't exceed `MAX_DOCUMENT_BYTES`.
4. Idempotency: deduplicate by figure chash (`sha256` of the rendered PNG) so re-indexing the same paper doesn't re-spend prefill cost.
5. Failure mode: if the VL backend is unreachable or times out, the chunk lands without augmentation — fail-soft, log structured event.
6. Cost / latency: 5–15 s per image × ~5–20 figures per paper = 25 s – 5 min per paper of one-time prefill cost at ingest. Acceptable for a background queue. Throughput bottleneck is the backend's single-image prefill, not nexus.

**Decision rule:** index 5–10 figure-heavy papers from `knowledge__dt-papers` both with and without VL augmentation, build a query set targeting figure content ("which model has the highest F1 on OAEI Anatomy"; "what are the axes of Figure 3 in M3DocRAG"), measure recall@10. File a bead for a configurable VL-enrichment hook if recall@10 lift ≥ 10 pp on figure-bearing queries.

**Constraints to surface in the eventual bead:**
- VL backend is remote (qwentescence); nexus's enrichment must tolerate occasional unreachability and not block ingest.
- The qwentescence operator flagged that tool-use during image processing is not wired. The proposed enrichment path is one-shot image-in / structured-text-out, which matches the supported mode. Do not design a mid-loop agentic flow.
- Output should be JSON-schema-constrained via `response_format` so the structured-text payload is parseable without retries.

### Three earlier "relevance paths" — restated under the new info

1. **Post-retrieval text enrichment via the text supervisor** — still text-only, still not the M3DocRAG answer. Continue as the active text-offload integration target (per `qwen-offload-transition-plan.md`).
2. **Parallel Qwen2-VL llama.cpp backend, called directly** — **operational at qwentescence (or imminent), not hypothetical.** This is the NEW P0 spike's backing infrastructure.
3. **Adding VL support to the supervisor** — still upstream-dependent (`@qwen-code/sdk` has no image content type), still a future change in the coprocessor stack. Not needed for the P0 spike because direct HTTP works.

### Stance — revised

The qwen-coprocessor's text supervisor remains text-only. **But the backing inference infrastructure (llama-server on Strix Halo / qwentescence) already serves a vision-language Qwen 3.6 model that nexus can call directly via OpenAI-compatible HTTP, bypassing the supervisor entirely.** That's the path. Closing the M3DocRAG §5.1 visual-evidence gap via ingest-time VL text augmentation is now the highest-priority spike in this proposal.

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

### First run — 2026-05-17, RDR corpus

| Pipeline | recall@10 | MRR@10 | latency | index floats |
|---|---|---|---|---|
| voyage-context-3 cosine | **0.8000** | 0.5404 | 160 ms | 204,800 |
| ColBERT `colbertv2.0` | 0.6000 | **0.5692** | 15 ms | 3,478,784 (17×) |
| Delta | **−20.0 pp** | +0.029 | — | 17× |

Repro:
```
NX_STORAGE_MODE=direct uv run python scripts/colbert_recall_spike.py \
  --collection rdr__1-1__voyage-context-3__v1 \
  --sample-size 200 --n-queries 50
```

Threshold not met. **Rejected on RDR corpus.** Caveats:

- Synthetic queries are salient n-grams extracted verbatim from chunk text, biasing toward exact-string match, which favours single-vector cosine over late interaction.
- `colbertv2.0` is a generic English ColBERT, not domain-tuned for RDR prose.
- 200 chunks / 50 queries is a small sample; baseline recall@10 = 0.80 leaves thin headroom for improvement.
- MRR@10 is marginally higher for ColBERT, suggesting better ranking when a hit lands, but the hit rate itself is lower.

A second data point on `code__nexus*` may be worth running because vocabulary mismatch is higher in code than in RDR prose. Lower priority than the §5c VL-augmentation P0 spike.

## 7. Cross-links with sibling proposals

- **Beyond Similarity Search** (`beyond-similarity-search-application.md`): the retrieval layer's split-tier consistency problem. Late interaction is orthogonal — it changes *what* is retrieved, not *where* the manifest authority lives.
- **Open Ontologies** (`open-ontologies-application.md`): operator decomposition for LLM-callable tools, plus stable matching for catalog link generation. Independent surfaces from this proposal.
- **All three sibling papers agree** on one architectural claim: multi-signal retrieval beats single-signal. M3DocRAG adds the strongest specific candidate (late interaction); Beyond Similarity Search adds the general framing; Open Ontologies' weight-irrelevance result reinforces that signal diversity matters more than signal weighting.

## 8. Decisions asked

1. **NEW P0: run the VL-augmented chunk-enrichment spike** (§5c)? — yes / no / defer. Closes M3DocRAG §5.1's visual-evidence gap via ingest-time text augmentation; uses the qwentescence Qwen 3.6-35B-A3B VL backend over OpenAI-compatible HTTP. Smallest blast radius of any spike in this proposal. Decision rule: recall@10 lift ≥ 10 pp on figure-bearing queries → file bead.
2. **Accept architecture.md + cli-reference.md citation** (§4.2)? — yes / no / modify.
3. **Run the text-only ColBERT spike and decide on the +5 % rule** (§6)? — yes / no / defer. Independent of VL; addresses vocabulary mismatch on text corpora.
4. **If §6 ColBERT passes the threshold, file a bead for an opt-in late-interaction parallel retriever**? — yes / conditional / defer.
5. **Run the figure-only SigLIP spike** (§5b)? — yes / no / defer. Lighter than ColPali but lower priority than VL-augmentation (VL gives richer text than vector matches).
6. **Run the ColPali-on-Apple-Silicon retrieval spike** (§5b)? — yes / no / defer. Heaviest spike; only worth running if both VL-augmentation and SigLIP underperform.
7. **Promote to RDR?** — likely yes if the §5c P0 spike clears the bar, given the ingest-pipeline change is non-trivial and crosses configuration / extraction / chunking layers.

No code changes from this document. The text-only spike script (`scripts/colbert_recall_spike.py`) is measurement-only. The three figure / page-image / VL-augmentation spikes (§5b, §5c) are not yet scaffolded — they're scoped here so the conversation can choose where to invest.
