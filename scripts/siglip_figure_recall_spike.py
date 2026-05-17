#!/usr/bin/env python3
"""SigLIP figure-only retrieval spike — measurement scaffold.

Per ``docs/proposals/m3docrag-application.md`` §5b. Tests whether a
figure-only image index, built from a PDF's extracted figures, can be
retrieved by figure-caption queries.

Pipeline:
  1. Extract figures from a PDF via Docling (bounding boxes + caption
     text are already produced by ``DocumentConverter``).
  2. Render each figure's bbox region to PIL image.
  3. Embed each figure image via SigLIP image encoder.
  4. Embed each figure's caption via SigLIP text encoder.
  5. For each caption (query), retrieve top-10 figures by cosine
     similarity in SigLIP space. The ground-truth relevant figure is
     the one the caption belongs to.
  6. Report recall@10 + MRR@10.

This is NOT a comparison against Voyage text retrieval. It's a
viability check on the figure-only path: if SigLIP can't retrieve its
own captions' figures at recall@10 ≥ 0.7, the whole figure-image
adjunct is unviable and the proposal's §5b P2 spike fails.

Decision rule (proposal §5b): file a bead for an opt-in figure-image
index IF SigLIP recall@10 ≥ 0.7 on the caption→figure self-retrieval
task AND figure extraction time per page ≤ 5 s on Apple Silicon.

Usage:
  uv run python scripts/siglip_figure_recall_spike.py \\
      --pdf /tmp/m3docrag/m3docrag.pdf \\
      --model google/siglip-base-patch16-224

Optional deps:
  pip install sentence-transformers docling pdf2image pillow

Limitations:
  - Self-retrieval ground truth (caption matches its own figure). Real
    nexus retrieval is caption-text-from-the-paper → figure. Both rely
    on the caption being descriptive enough; this spike measures the
    second link only.
  - Caption length and quality vary; for figures with no caption
    Docling emits an empty string and the query is dropped.
  - SigLIP base (~400 MB) is the smallest reasonable image encoder.
    CLIP ViT-B/32 is an alternative the script also accepts via
    --model.
"""
from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Figure:
    """One figure extracted from a PDF."""

    figure_id: str
    caption: str
    image_path: Path
    page_number: int
    bbox: tuple[float, float, float, float] | None


# ── Figure extraction via Docling ────────────────────────────────────────────


def extract_figures(pdf_path: Path, out_dir: Path) -> list[Figure]:
    """Run Docling on the PDF, write each picture region to PNG, and
    return ``Figure`` records.

    Docling's ``DocumentConverter`` produces a ``DoclingDocument`` whose
    ``pictures`` collection holds ``PictureItem`` entries with
    ``prov.bbox`` (page coordinates) and optional ``caption_text``.
    Rendering uses ``picture.image`` when Docling has already rasterised
    the region (default with ``PdfPipelineOptions.images_scale > 0``).
    """
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)

    # Enable picture rasterisation so picture.image is populated.
    pipeline = PdfPipelineOptions()
    pipeline.images_scale = 2.0
    pipeline.generate_picture_images = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
        }
    )
    print(f"  running Docling on {pdf_path.name}...")
    t0 = time.perf_counter()
    result = converter.convert(str(pdf_path))
    convert_elapsed = time.perf_counter() - t0
    print(f"  Docling done in {convert_elapsed:.1f}s")

    doc = result.document
    figures: list[Figure] = []
    for i, pic in enumerate(doc.pictures):
        # Caption text — Docling stitches caption refs together.
        caption = ""
        for cap_ref in getattr(pic, "captions", []) or []:
            cap_item = cap_ref.resolve(doc) if hasattr(cap_ref, "resolve") else None
            if cap_item is not None and hasattr(cap_item, "text"):
                caption += cap_item.text + " "
        caption = caption.strip()

        # Resolve the rasterised image. Newer Docling exposes
        # ``picture.image.pil_image``; older versions use ``image`` PIL
        # directly. Tolerate both.
        pil = None
        img_obj = getattr(pic, "image", None)
        if img_obj is not None:
            pil = getattr(img_obj, "pil_image", None) or img_obj
        if pil is None or not hasattr(pil, "save"):
            print(f"  picture {i}: no rasterised image, skipping")
            continue

        page_no = 0
        bbox = None
        prov = getattr(pic, "prov", None) or []
        if prov:
            first = prov[0]
            page_no = getattr(first, "page_no", 0)
            bb = getattr(first, "bbox", None)
            if bb is not None:
                bbox = (bb.l, bb.t, bb.r, bb.b)

        image_path = out_dir / f"figure_{i:03d}.png"
        pil.save(image_path)
        figures.append(
            Figure(
                figure_id=f"fig_{i:03d}",
                caption=caption,
                image_path=image_path,
                page_number=page_no,
                bbox=bbox,
            )
        )
    return figures


# ── SigLIP embedding ─────────────────────────────────────────────────────────


def embed_with_siglip(
    figures: list[Figure],
    model_name: str,
) -> tuple[list[list[float]], list[list[float]], int]:
    """Return (image_embeddings, caption_embeddings, embed_dim).

    Uses HuggingFace transformers directly (not sentence-transformers,
    whose .encode() doesn't accept PIL images for SigLIP). The CLIP/
    SigLIP family exposes get_image_features + get_text_features which
    share a vector space designed for cross-modal cosine retrieval.
    """
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    from transformers import AutoModel, AutoProcessor  # noqa: PLC0415

    print(f"  loading {model_name}...")
    t0 = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name)
    model.eval()
    # Honour Apple Silicon when available; CPU otherwise.
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = model.to(device)
    load_elapsed = time.perf_counter() - t0
    print(f"  model loaded in {load_elapsed:.1f}s on {device}")

    images = [Image.open(f.image_path).convert("RGB") for f in figures]
    captions = [f.caption for f in figures]

    print(f"  embedding {len(images)} images + {len(captions)} captions...")
    t1 = time.perf_counter()
    with torch.no_grad():
        img_inputs = processor(images=images, return_tensors="pt").to(device)
        img_feats = model.get_image_features(**img_inputs)
        img_feats = img_feats / img_feats.norm(dim=-1, keepdim=True)

        txt_inputs = processor(
            text=captions, padding="max_length", truncation=True, return_tensors="pt",
        ).to(device)
        txt_feats = model.get_text_features(**txt_inputs)
        txt_feats = txt_feats / txt_feats.norm(dim=-1, keepdim=True)
    embed_elapsed = time.perf_counter() - t1
    print(f"  embeddings produced in {embed_elapsed:.1f}s")

    dim = img_feats.shape[-1]
    return (
        img_feats.cpu().tolist(),
        txt_feats.cpu().tolist(),
        int(dim),
    )


# ── Retrieval scoring ────────────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # normalize_embeddings → dot is cosine


@dataclass
class Metrics:
    n_queries: int
    recall_at_10: float
    mrr_at_10: float
    notes: str = ""


def measure_caption_to_figure(
    figure_ids: list[str],
    image_embeds: list[list[float]],
    caption_embeds: list[list[float]],
    captions: list[str],
) -> Metrics:
    """For each non-empty caption, retrieve top-10 figures by cosine
    similarity. The relevant figure is the one whose own caption is
    being queried with — index of the caption == index of the relevant
    figure.
    """
    hits = 0
    mrr_sum = 0.0
    n_valid = 0
    for i, (cap, q_emb) in enumerate(zip(captions, caption_embeds)):
        if not cap:
            continue
        n_valid += 1
        scored = [
            (j, _cosine(q_emb, img_emb))
            for j, img_emb in enumerate(image_embeds)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        top10 = [j for j, _ in scored[:10]]
        if i in top10:
            hits += 1
            rank = top10.index(i) + 1
            mrr_sum += 1.0 / rank
    return Metrics(
        n_queries=n_valid,
        recall_at_10=hits / max(n_valid, 1),
        mrr_at_10=mrr_sum / max(n_valid, 1),
        notes=f"{len(captions) - n_valid} figures had empty captions and were dropped",
    )


# ── Driver ──────────────────────────────────────────────────────────────────


def _print(m: Metrics, label: str) -> None:
    print(f"  pipeline   : {label}")
    print(f"  n_queries  : {m.n_queries}")
    print(f"  recall@10  : {m.recall_at_10:.4f}")
    print(f"  mrr@10     : {m.mrr_at_10:.4f}")
    if m.notes:
        print(f"  notes      : {m.notes}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", required=True, type=Path,
        help="Path to a PDF to extract figures from.",
    )
    parser.add_argument(
        "--model", default="google/siglip-base-patch16-224",
        help="SigLIP / CLIP model id for sentence-transformers.",
    )
    parser.add_argument(
        "--out", default=Path("/tmp/siglip-spike-figures"), type=Path,
        help="Directory to write extracted figure PNGs.",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        raise SystemExit(f"PDF not found: {args.pdf}")

    print(f"\n=== SigLIP figure-only spike — {args.pdf.name} ===\n")

    t_total = time.perf_counter()
    figures = extract_figures(args.pdf, args.out)
    extract_elapsed = time.perf_counter() - t_total
    print(f"\nextracted {len(figures)} figures in {extract_elapsed:.1f}s")
    if not figures:
        print("no figures extracted; nothing to measure.")
        return 0

    n_with_caption = sum(1 for f in figures if f.caption)
    print(f"  {n_with_caption} of {len(figures)} figures have a caption")
    if n_with_caption == 0:
        print("no captions; cannot run caption→figure retrieval.")
        return 0

    image_embeds, caption_embeds, dim = embed_with_siglip(figures, args.model)
    print(f"  embedding dim: {dim}")

    print("\n--- caption → figure retrieval ---")
    m = measure_caption_to_figure(
        figure_ids=[f.figure_id for f in figures],
        image_embeds=image_embeds,
        caption_embeds=caption_embeds,
        captions=[f.caption for f in figures],
    )
    _print(m, f"SigLIP self-retrieval ({args.model})")

    print("\n--- decision ---")
    if m.recall_at_10 >= 0.7:
        print(f"  recall@10 = {m.recall_at_10:.4f} ≥ 0.7. Viable. "
              "Per proposal §5b, file a bead for an opt-in figure-image index.")
    else:
        print(f"  recall@10 = {m.recall_at_10:.4f} < 0.7. Viability "
              "below threshold; figure-only path likely doesn't pay for "
              "itself on this PDF. Consider running on more PDFs / a "
              "stronger image encoder before final reject.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
