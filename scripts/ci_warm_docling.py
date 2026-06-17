#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI pre-fetch / warm-and-verify probe for the docling PDF-extraction models.

nexus-c7gnx: docling loads its layout + TableFormer models lazily at convert()
time, so merely building the converter does NOT download them. This runs a REAL
extraction on a tiny generated PDF and exits non-zero unless docling actually
performed it (extraction_method == 'docling'). The CI step retries this a few
times and HARD-FAILS, so a cold HuggingFace cache plus a transient HF outage
fails loudly here instead of silently falling back to PyMuPDF mid-suite and
producing confusing extraction_method=='docling' assertion failures.

Exit codes: 0 = docling extraction succeeded (models warm); 1 = otherwise.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile


def main() -> int:
    import pymupdf

    from nexus.pdf_extractor import PDFExtractor

    with tempfile.TemporaryDirectory() as td:
        probe = pathlib.Path(td) / "warm.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "docling warm probe")
        doc.save(str(probe))
        doc.close()

        method = PDFExtractor().extract(probe).metadata.get("extraction_method")
        print(f"extraction_method={method}")
        return 0 if method == "docling" else 1


if __name__ == "__main__":
    sys.exit(main())
