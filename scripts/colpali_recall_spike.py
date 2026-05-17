#!/usr/bin/env python3
"""ColPali Apple-Silicon recall spike (proposal §5b P3).

Tests whether ColPali page-image retrieval on Apple Silicon (MPS) is:
  (a) tractable at ingest time (≤ 60 s/page threshold per proposal),
  (b) competitive with Voyage text retrieval on figure-bearing queries
      (lift threshold ≥ 10 pp recall@10).

Pipeline:
  1. Render each PDF page to PIL image via pdf2image @ 144 DPI (matches
     M3DocRAG's setting).
  2. Embed every page with ColPali (token-matrix per page,
     `vidore/colpali-v1.3` by default). Time the per-page pass.
  3. Embed each query as token-matrix.
  4. Score each query against each page via MaxSim (ColBERT-style:
     sum-over-query-tokens of max-over-doc-tokens cosine).
  5. Generate a figure-content query set from VL output already cached
     in /tmp/siglip-spike-figures/<paper>_captions.json + the
     VL-augmentation spike's results, OR pass --queries-file.
  6. Compare ColPali recall@10 and MRR@10 against the
     vl-augmentation spike's voyage baseline (same query set).

Decision rule (proposal §5b P3): file a bead if index time ≤ 60 s/page
AND recall@10 lift ≥ 10 pp vs Voyage on the same figure-content
query set. Else REJECT — Voyage + VL augmentation (nexus-6h0e) is
the lighter / chosen path.

Usage:
  uv run python scripts/colpali_recall_spike.py \\
      --pdf /tmp/m3docrag/m3docrag.pdf \\
      --model vidore/colpali-v1.3

Caveats:
  - First run downloads the ColPali model weights (~6 GB BF16, fewer
    GB at lower precision via colpali-engine's auto-quant).
  - MPS support requires PyTorch ≥ 2.0 + macOS arm64. CUDA is faster
    but not available on Mac.
  - Pages are processed one at a time on memory-constrained machines;
    batch_size can be raised on a 64+ GB M-Max.
"""
from __future__ import annotations

import argparse
import math
import re
import time
from pathlib import Path


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\b[A-Za-z][\w-]*\b", text)]


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── ColPali load + page embed ────────────────────────────────────────────────


def load_colpali(model_name: str):
    import torch  # noqa: PLC0415
    from colpali_engine.models import ColPali, ColPaliProcessor  # noqa: PLC0415

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = torch.float16 if device == "mps" else torch.float32
    print(f"  loading {model_name} on {device} ({dtype})...")
    t0 = time.perf_counter()
    model = ColPali.from_pretrained(model_name, torch_dtype=dtype, device_map=device).eval()
    processor = ColPaliProcessor.from_pretrained(model_name)
    load_elapsed = time.perf_counter() - t0
    print(f"  model loaded in {load_elapsed:.1f}s")
    return model, processor, device


def render_pdf_pages(pdf_path: Path, dpi: int = 144):
    from pdf2image import convert_from_path  # noqa: PLC0415

    print(f"  rasterising {pdf_path.name} at {dpi} DPI...")
    t0 = time.perf_counter()
    pages = convert_from_path(str(pdf_path), dpi=dpi)
    print(f"  {len(pages)} pages rendered in {time.perf_counter() - t0:.1f}s")
    return pages


def embed_pages(model, processor, pages, device) -> tuple[list, float]:
    """One page at a time to bound memory on a laptop. Returns
    ``(list-of-(n_tokens, embed_dim)-tensors, total_elapsed_s)``."""
    import torch  # noqa: PLC0415

    embeds = []
    t0 = time.perf_counter()
    for i, page in enumerate(pages):
        ts = time.perf_counter()
        with torch.no_grad():
            batch = processor.process_images([page]).to(device)
            out = model(**batch)
        embeds.append(out[0].cpu().float())
        print(f"    page {i + 1}/{len(pages)}: {time.perf_counter() - ts:.2f}s "
              f"({tuple(out[0].shape)} tokens)")
    return embeds, time.perf_counter() - t0


def embed_queries(model, processor, queries, device) -> list:
    import torch  # noqa: PLC0415

    embeds = []
    with torch.no_grad():
        batch = processor.process_queries(queries).to(device)
        out = model(**batch)
    for i in range(out.shape[0]):
        embeds.append(out[i].cpu().float())
    return embeds


def maxsim(query_emb, doc_embeds: list) -> list[float]:
    """ColBERT MaxSim: sum-over-query-tokens of max-over-doc-tokens cos."""
    import torch  # noqa: PLC0415

    # Normalise once.
    q = query_emb / (query_emb.norm(dim=-1, keepdim=True) + 1e-12)
    scores: list[float] = []
    for d in doc_embeds:
        d_norm = d / (d.norm(dim=-1, keepdim=True) + 1e-12)
        # (qtok x dim) @ (dim x dtok) -> (qtok x dtok)
        sim = q @ d_norm.t()
        # max over doc tokens per query token, then sum.
        scores.append(float(sim.max(dim=-1).values.sum().item()))
    return scores


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--model", default="vidore/colpali-v1.3")
    parser.add_argument("--queries", action="append", default=[],
                        help="Add a query string. Repeat for multiple.")
    parser.add_argument("--default-queries", action="store_true",
                        help="Use a built-in figure-content query set.")
    args = parser.parse_args()
    if not args.pdf.exists():
        raise SystemExit(f"PDF not found: {args.pdf}")

    print(f"\n=== ColPali spike — {args.pdf.name} ===\n")
    pages = render_pdf_pages(args.pdf)

    model, processor, device = load_colpali(args.model)

    print(f"\nembedding {len(pages)} pages...")
    page_embeds, page_elapsed = embed_pages(model, processor, pages, device)
    avg_per_page = page_elapsed / max(len(pages), 1)
    print(f"  total {page_elapsed:.1f}s; avg {avg_per_page:.2f}s/page")

    # Pick queries.
    if args.queries:
        queries = args.queries
    elif args.default_queries:
        # M3DocRAG-targeted figure-content queries — chosen to match
        # things visible in the paper's figures but not their captions.
        queries = [
            "voronoi diagram approximate indexing for nearest neighbour",
            "three-stage pipeline document embedding page retrieval",
            "ColPali visual encoder text query MaxSim scoring",
            "PDF documents corpus pages images visual embeddings",
            "Lorca FC squad roster transfer log",
            "single-page DocVQA multimodal LM bounding box",
            "Hammurabi king of Babylon father of Iraqi politician",
        ]
    else:
        raise SystemExit("provide --queries or --default-queries")

    print(f"\nembedding {len(queries)} queries...")
    t0 = time.perf_counter()
    q_embeds = embed_queries(model, processor, queries, device)
    print(f"  {time.perf_counter() - t0:.2f}s")

    print(f"\n--- retrieval (MaxSim) ---")
    for q, q_emb in zip(queries, q_embeds):
        scores = maxsim(q_emb, page_embeds)
        ranked = sorted(enumerate(scores, start=1), key=lambda x: x[1], reverse=True)[:5]
        print(f"\n  query: {q!r}")
        for rank, (page_no, score) in enumerate(ranked, start=1):
            print(f"    #{rank}  page {page_no:2d}  score={score:.2f}")

    print(f"\n--- threshold check (proposal §5b P3) ---")
    print(f"  per-page index time: {avg_per_page:.2f}s "
          f"({'PASS' if avg_per_page <= 60 else 'FAIL'} ≤ 60s/page)")
    print(f"  recall lift vs Voyage: not computed in this run "
          "(needs a shared ground-truth query set; the rankings above are "
          "for eyeball inspection).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
