#!/usr/bin/env python3
"""VL-augmentation retrieval spike — measurement scaffold (proposal §5c, P0).

Tests the hypothesis: adding VL-generated structured descriptions to the
chunk that owns a figure improves retrieval recall on figure-content
queries, vs the original caption-only chunk.

Pipeline:
  1. Extract figures from a PDF via Docling (uses
     ``scripts/siglip_figure_recall_spike.py``'s extractor pattern).
  2. For each figure, POST {image, structured prompt} to the qwentescence
     llama-server endpoint (OpenAI-compatible /v1/chat/completions) with
     ``chat_template_kwargs: {enable_thinking: false}`` to bypass Qwen
     3.6's default reasoning mode.
  3. Build two parallel "chunk" pools:
        baseline[i]  = Docling caption for figure i (caption-only)
        augmented[i] = caption + " " + VL_description (caption + VL text)
  4. Synthesize content-bearing queries by extracting n-grams that appear
     in VL_description but NOT in the original caption. Each query's
     ground-truth target is figure i.
  5. Embed both pools (Voyage voyage-context-3 via the existing nexus
     CCE pathway). For each query, score cosine vs all baseline_chunks
     and against all augmented_chunks; measure recall@K and MRR@K.

Decision rule (proposal §5c): file a bead for an opt-in VL-enrichment
hook IF augmented recall@10 lifts ≥ 10 pp over baseline on the
figure-content query set.

Usage:
  uv run python scripts/vl_augmentation_recall_spike.py \\
      --pdf /tmp/m3docrag/m3docrag.pdf \\
      --backend http://qwentescence:1234/v1 \\
      --vl-model qwen3.6-35b-a3b

Notes:
  - Reuses figures already extracted to /tmp/siglip-spike-figures/ when
    present, skipping the Docling pass. Pass ``--re-extract`` to force.
  - The figure-content query set is derived from VL output, so this is
    a "does VL add retrievable signal" test, NOT a human-labelled
    relevance benchmark. The bias is acknowledged: VL describes the
    figure, queries are derived from VL, so retrieval to VL-augmented
    chunks is easier *by construction*. The honest read is the DELTA
    vs baseline — does the caption alone retrieve the same content?
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FigureWithDesc:
    figure_id: str
    caption: str
    image_path: Path
    vl_description: str
    vl_elapsed_s: float


# ── Figure extraction (reuses cached PNGs when available) ────────────────────


def extract_or_reuse_figures(pdf_path: Path, out_dir: Path) -> list[tuple[str, str, Path]]:
    """Return ``[(figure_id, caption, image_path)]``.

    If ``out_dir`` already has PNGs for this PDF (from the SigLIP spike),
    reuse them and try to find a sidecar caption file; otherwise run
    Docling fresh.
    """
    # The SigLIP spike wrote figures as ``<paper_slug>_figure_NNN.png``.
    slug = pdf_path.stem[:16]
    cached = sorted(out_dir.glob(f"{slug}_figure_*.png"))
    captions_file = out_dir / f"{slug}_captions.json"

    if cached and captions_file.exists():
        captions = json.loads(captions_file.read_text())
        figures: list[tuple[str, str, Path]] = []
        for img_path in cached:
            cap = captions.get(img_path.name, "")
            figures.append((img_path.stem, cap, img_path))
        return figures

    # Cold path: run Docling.
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    pipeline = PdfPipelineOptions()
    pipeline.images_scale = 2.0
    pipeline.generate_picture_images = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)},
    )
    print(f"  running Docling on {pdf_path.name}...")
    t0 = time.perf_counter()
    result = converter.convert(str(pdf_path))
    print(f"  Docling done in {time.perf_counter() - t0:.1f}s")

    doc = result.document
    figures = []
    captions_map: dict[str, str] = {}
    for i, pic in enumerate(doc.pictures):
        cap = ""
        for cap_ref in getattr(pic, "captions", []) or []:
            cap_item = cap_ref.resolve(doc) if hasattr(cap_ref, "resolve") else None
            if cap_item is not None and hasattr(cap_item, "text"):
                cap += cap_item.text + " "
        cap = cap.strip()
        img_obj = getattr(pic, "image", None)
        pil = getattr(img_obj, "pil_image", None) or img_obj
        if pil is None or not hasattr(pil, "save"):
            continue
        img_path = out_dir / f"{slug}_figure_{i:03d}.png"
        pil.save(img_path)
        figures.append((img_path.stem, cap, img_path))
        captions_map[img_path.name] = cap
    captions_file.write_text(json.dumps(captions_map, indent=2))
    return figures


# ── VL description via qwentescence ──────────────────────────────────────────


_VL_PROMPT = (
    "Describe this figure in 2-4 sentences. If it is a chart, name the "
    "axes and units and summarise the trend. If it is a diagram or "
    "system overview, name the components and their relationships. Use "
    "concrete vocabulary; avoid filler like 'the figure shows'."
)


def get_vl_description(
    image_path: Path,
    backend: str,
    model: str,
    max_tokens: int = 400,
    timeout: int = 180,
) -> tuple[str, float]:
    """POST one image+prompt to the VL backend. Return ``(text, elapsed_s)``."""
    img_bytes = image_path.read_bytes()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    # Best-effort MIME inference.
    suffix = image_path.suffix.lstrip(".").lower() or "png"
    mime = {"jpg": "jpeg"}.get(suffix, suffix)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VL_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{mime};base64,{img_b64}"}},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        # nexus-qw21-class lesson: Qwen 3.6 defaults to thinking mode and
        # emits reasoning_content; set enable_thinking=False to get
        # plain content out.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        backend.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    elapsed = time.perf_counter() - t0
    msg = body["choices"][0]["message"]
    text = msg.get("content") or ""
    if not text:
        # Fall back to reasoning content if content is empty — shouldn't
        # happen with enable_thinking=False but be defensive.
        text = msg.get("reasoning_content", "")
    return text.strip(), elapsed


# ── Query synthesis: pull n-grams unique to VL text ──────────────────────────

_WORD_RE = re.compile(r"\b[A-Za-z][\w-]*\b")
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with",
    "by", "is", "are", "was", "were", "be", "been", "being", "as", "at",
    "from", "that", "this", "it", "its", "into", "but", "not", "no",
    "shows", "depicts", "illustrates", "presents", "figure", "diagram",
    "chart", "table", "image",
}


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text)]


def _content_ngrams(text: str, n: int) -> set[str]:
    toks = _tokens(text)
    out: set[str] = set()
    for i in range(len(toks) - n + 1):
        gram = toks[i : i + n]
        if any(t in _STOPWORDS or len(t) <= 3 for t in gram):
            continue
        out.add(" ".join(gram))
    return out


def build_queries_from_vl(
    figures: list[FigureWithDesc],
    per_figure: int = 2,
    n: int = 3,
) -> list[tuple[str, int]]:
    """For each figure, pick up to ``per_figure`` n-grams that appear in
    its VL description but NOT in its caption. The relevant figure is
    the source index.
    """
    queries: list[tuple[str, int]] = []
    for idx, f in enumerate(figures):
        caption_grams = _content_ngrams(f.caption, n)
        vl_grams = _content_ngrams(f.vl_description, n)
        unique = sorted(vl_grams - caption_grams)
        for gram in unique[:per_figure]:
            queries.append((gram, idx))
    # Dedupe identical queries pointing at different figures.
    seen: dict[str, int] = {}
    for q, _ in queries:
        seen[q] = seen.get(q, 0) + 1
    return [(q, idx) for q, idx in queries if seen[q] == 1]


# ── Voyage CCE embedding (queries + chunks) ──────────────────────────────────


def embed_voyage_cce(texts: list[str], input_type: str) -> list[list[float]]:
    import voyageai  # noqa: PLC0415

    api_key = os.environ.get("VOYAGE_API_KEY") or os.environ.get("VOYAGE_API_KEY_2")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY must be set")
    client = voyageai.Client(api_key=api_key, max_retries=2)
    embeds: list[list[float]] = []
    for t in texts:
        # One-text-per-inner-list = no cross-chunk context propagation,
        # matches nexus's _cce_embed contract.
        resp = client.contextualized_embed(
            inputs=[[t]], model="voyage-context-3", input_type=input_type,
        )
        embeds.append(resp.results[0].embeddings[0])
    return embeds


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Metrics:
    label: str
    n_queries: int
    recall_at_10: float
    mrr_at_10: float
    mean_rank_of_target: float


def score(
    query_embeds: list[list[float]],
    pool_embeds: list[list[float]],
    relevant_ids: list[int],
    label: str,
) -> Metrics:
    hits = 0
    mrr_sum = 0.0
    rank_sum = 0
    for q_emb, relevant in zip(query_embeds, relevant_ids):
        scored = sorted(
            [(i, _cosine(q_emb, c_emb)) for i, c_emb in enumerate(pool_embeds)],
            key=lambda x: x[1], reverse=True,
        )
        top_ids = [i for i, _ in scored]
        target_rank = top_ids.index(relevant) + 1
        rank_sum += target_rank
        if target_rank <= 10:
            hits += 1
            mrr_sum += 1.0 / target_rank
    n = len(query_embeds)
    return Metrics(
        label=label,
        n_queries=n,
        recall_at_10=hits / max(n, 1),
        mrr_at_10=mrr_sum / max(n, 1),
        mean_rank_of_target=rank_sum / max(n, 1),
    )


def _print(m: Metrics) -> None:
    print(f"  {m.label}")
    print(f"    n_queries          : {m.n_queries}")
    print(f"    recall@10          : {m.recall_at_10:.4f}")
    print(f"    mrr@10             : {m.mrr_at_10:.4f}")
    print(f"    mean target rank   : {m.mean_rank_of_target:.2f}")


# ── Driver ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", action="append", default=[], type=Path,
                        help="Path to a PDF. Repeat for multi-paper pool.")
    parser.add_argument("--backend", default="http://qwentescence:1234/v1")
    parser.add_argument("--vl-model", default="qwen3.6-35b-a3b")
    parser.add_argument(
        "--out", default=Path("/tmp/siglip-spike-figures"), type=Path,
    )
    args = parser.parse_args()
    if not args.pdf:
        raise SystemExit("at least one --pdf is required")
    for p in args.pdf:
        if not p.exists():
            raise SystemExit(f"PDF not found: {p}")

    label = "cross-paper" if len(args.pdf) > 1 else args.pdf[0].name
    print(f"\n=== VL-augmentation spike — {label} ===")
    print(f"  backend: {args.backend}  model: {args.vl_model}\n")

    figs: list[tuple[str, str, Path]] = []
    for pdf in args.pdf:
        per_pdf = extract_or_reuse_figures(pdf, args.out)
        print(f"  {pdf.name}: {len(per_pdf)} figures, "
              f"{sum(1 for _, c, _ in per_pdf if c)} captioned")
        figs.extend(per_pdf)
    captioned = [(fid, cap, p) for fid, cap, p in figs if cap]
    print(f"loaded {len(figs)} figures total; {len(captioned)} have captions")
    if not captioned:
        return 0

    print("\nfetching VL descriptions...")
    vl_results: list[FigureWithDesc] = []
    for i, (fid, cap, p) in enumerate(captioned):
        text, elapsed = get_vl_description(p, args.backend, args.vl_model)
        vl_results.append(FigureWithDesc(
            figure_id=fid, caption=cap, image_path=p,
            vl_description=text, vl_elapsed_s=elapsed,
        ))
        print(f"  [{i + 1}/{len(captioned)}] {fid} ({elapsed:.1f}s, "
              f"{len(text)} chars): {text[:80]!r}")

    total_vl_time = sum(f.vl_elapsed_s for f in vl_results)
    print(f"\nVL pass: {total_vl_time:.1f}s total, "
          f"{total_vl_time / len(vl_results):.1f}s avg per figure")

    print("\nbuilding chunk pools and query set...")
    baseline_chunks = [f.caption for f in vl_results]
    augmented_chunks = [f"{f.caption} {f.vl_description}".strip() for f in vl_results]
    queries = build_queries_from_vl(vl_results, per_figure=3, n=3)
    print(f"  {len(queries)} unique figure-content queries (3-grams from VL "
          f"text, absent in caption)")
    if not queries:
        print("  no unique VL-only queries; cannot measure delta.")
        return 0

    print("\nembedding with voyage-context-3...")
    query_texts = [q for q, _ in queries]
    relevant_ids = [idx for _, idx in queries]
    t0 = time.perf_counter()
    q_embeds = embed_voyage_cce(query_texts, input_type="query")
    base_embeds = embed_voyage_cce(baseline_chunks, input_type="document")
    aug_embeds = embed_voyage_cce(augmented_chunks, input_type="document")
    embed_elapsed = time.perf_counter() - t0
    print(f"  embed pass: {embed_elapsed:.1f}s for "
          f"{len(query_texts) + len(baseline_chunks) + len(augmented_chunks)} "
          f"items")

    print("\n--- metrics ---\n")
    m_base = score(q_embeds, base_embeds, relevant_ids, "baseline (caption-only)")
    _print(m_base)
    print()
    m_aug = score(q_embeds, aug_embeds, relevant_ids, "augmented (caption + VL)")
    _print(m_aug)

    print("\n--- delta ---")
    d_recall = m_aug.recall_at_10 - m_base.recall_at_10
    d_mrr = m_aug.mrr_at_10 - m_base.mrr_at_10
    d_rank = m_base.mean_rank_of_target - m_aug.mean_rank_of_target
    print(f"  recall@10 lift     : {d_recall:+.4f} ({d_recall * 100:+.1f} pp)")
    print(f"  mrr@10 lift        : {d_mrr:+.4f}")
    print(f"  mean rank improved : {d_rank:+.2f} (positive = better)")

    print("\n--- decision (proposal §5c) ---")
    if d_recall >= 0.10:
        print(f"  recall@10 lift ≥ 10 pp. PASSED. File a bead for an "
              "opt-in VL-enrichment ingest hook.")
    else:
        print(f"  recall@10 lift {d_recall * 100:.1f} pp < 10 pp threshold.")
        print(f"  Reject by the proposal's threshold, but mrr/mean-rank may "
              "still tell a different story; review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
