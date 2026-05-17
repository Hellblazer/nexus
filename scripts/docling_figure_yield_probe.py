#!/usr/bin/env python3
"""Probe Docling's per-PDF item classification to explain the figure-
extraction yield gap surfaced by the SigLIP spike (nexus-8siy).

Per ``docs/proposals/m3docrag-application.md`` §5b, the cross-paper
SigLIP run measured:
  M3DocRAG                 : 7 figures via Docling PictureItem
  Beyond Similarity Search : 1 (paper has ~4 architecture diagrams)
  Open Ontologies          : 0 (paper has multiple algorithm + arch diagrams)

This probe walks each PDF's DoclingDocument and tallies every item by
class name. Where ``PictureItem`` is empty we expect to see those
figures classified somewhere else (TableItem, TextItem, etc.) — that
identifies the misclassification surface.

Output:
  - Per-PDF item-type histogram
  - For TableItem and SectionHeaderItem (the likely figure-impersonator
    classes), the first 80 chars of label/text + bbox + page number,
    so we can eyeball whether each is genuinely a figure mis-labelled
    or a true non-figure.
  - For PictureItem entries, the caption + bbox so we can verify those
    we did catch.

Usage:
  uv run python scripts/docling_figure_yield_probe.py \\
      --pdf <path> [--pdf <path> ...]
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path


def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def probe(pdf_path: Path) -> None:
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    pipeline = PdfPipelineOptions()
    pipeline.images_scale = 2.0
    pipeline.generate_picture_images = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
        }
    )
    print(f"\n=== {pdf_path.name} ===")
    result = converter.convert(str(pdf_path))
    doc = result.document

    # Tally every item type.
    item_types: Counter[str] = Counter()
    items_by_type: dict[str, list] = {}
    for item, _level in doc.iterate_items():
        cls = type(item).__name__
        item_types[cls] += 1
        items_by_type.setdefault(cls, []).append(item)

    print("\nitem-type histogram:")
    for cls, count in item_types.most_common():
        print(f"  {cls:24s} {count:4d}")

    # PictureItem detail — what did we catch?
    pics = items_by_type.get("PictureItem", [])
    print(f"\nPictureItem detail ({len(pics)} caught):")
    for i, pic in enumerate(pics):
        caption_parts = []
        for cap_ref in getattr(pic, "captions", []) or []:
            cap_item = cap_ref.resolve(doc) if hasattr(cap_ref, "resolve") else None
            if cap_item is not None and hasattr(cap_item, "text"):
                caption_parts.append(cap_item.text)
        caption = " ".join(caption_parts)
        page_no = 0
        bbox_str = ""
        prov = getattr(pic, "prov", None) or []
        if prov:
            first = prov[0]
            page_no = getattr(first, "page_no", 0)
            bb = getattr(first, "bbox", None)
            if bb is not None:
                bbox_str = f"l={bb.l:.0f} t={bb.t:.0f} r={bb.r:.0f} b={bb.b:.0f}"
        print(f"  [{i:2d}] p{page_no} {bbox_str:36s} cap={_truncate(caption)!r}")

    # TableItem detail — common impersonator for diagrams.
    tables = items_by_type.get("TableItem", [])
    if tables:
        print(f"\nTableItem detail ({len(tables)} found — suspected diagram impersonators):")
        for i, tbl in enumerate(tables):
            caption_parts = []
            for cap_ref in getattr(tbl, "captions", []) or []:
                cap_item = cap_ref.resolve(doc) if hasattr(cap_ref, "resolve") else None
                if cap_item is not None and hasattr(cap_item, "text"):
                    caption_parts.append(cap_item.text)
            caption = " ".join(caption_parts)
            page_no = 0
            bbox_str = ""
            prov = getattr(tbl, "prov", None) or []
            if prov:
                first = prov[0]
                page_no = getattr(first, "page_no", 0)
                bb = getattr(first, "bbox", None)
                if bb is not None:
                    bbox_str = f"l={bb.l:.0f} t={bb.t:.0f} r={bb.r:.0f} b={bb.b:.0f}"
            # First row of the table for sanity
            row_preview = ""
            data = getattr(tbl, "data", None)
            if data is not None and getattr(data, "table_cells", None):
                cells = data.table_cells[:5]
                row_preview = " | ".join(c.text for c in cells if hasattr(c, "text"))
            print(f"  [{i:2d}] p{page_no} {bbox_str:36s} cap={_truncate(caption)!r}")
            if row_preview:
                print(f"       first-cells: {_truncate(row_preview, 100)!r}")

    # GroupItem / KeyValueItem / other classes that occasionally hold figures.
    for cls in ("GroupItem", "KeyValueItem", "FormItem", "ListItem"):
        items = items_by_type.get(cls, [])
        if not items:
            continue
        print(f"\n{cls} ({len(items)} found):")
        for i, it in enumerate(items[:5]):
            text = getattr(it, "text", "") or ""
            page_no = 0
            prov = getattr(it, "prov", None) or []
            if prov:
                page_no = getattr(prov[0], "page_no", 0)
            print(f"  [{i:2d}] p{page_no} text={_truncate(text)!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", action="append", default=[], type=Path,
        help="Path to a PDF. Repeat for multi-PDF probe.",
    )
    args = parser.parse_args()
    if not args.pdf:
        raise SystemExit("at least one --pdf is required")
    for p in args.pdf:
        if not p.exists():
            raise SystemExit(f"PDF not found: {p}")
        probe(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
