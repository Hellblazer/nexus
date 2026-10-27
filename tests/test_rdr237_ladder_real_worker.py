# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.1): the systemic-failure rule through the REAL worker process.

``tests/test_rdr237_ladder.py`` drives the ladder with a fake MinerU. This one runs the real
``_mineru_run_subprocess``: a child interpreter starts, imports ``mineru`` and dies on an
``ImportError`` from a shim package placed first on the CHILD's ``PYTHONPATH`` (read at interpreter
startup, so the parent's own imports are untouched). Three pages each fail both rungs with one
class, and the document stops with the class and the import error's line named.

Default unit tier: no MinerU and no model weights are needed. The test FAILS, not skips, if the
shim is not what the worker imported: a returncode must have been recorded, and the shim's marker
line must be in the class key.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from nexus.pdf_extractor import MineruSystemicFailure, PDFExtractor

MARKER = "rdr237-shim-marker: libshim.so.1: cannot open shared object file"


def _install_shim(base: Path) -> Path:
    """A ``mineru`` package whose import raises, ahead of any real one on the child's path."""
    pkg = base / "mineru"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"raise ImportError({MARKER!r})\n")
    return base


def _make_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"page {n + 1} has a text layer")
    doc.save(str(path))
    doc.close()
    return path


def test_three_pages_failing_both_rungs_in_a_real_worker_trip_the_breaker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(_install_shim(tmp_path / "shim")))
    pdf = _make_pdf(tmp_path / "doc.pdf", 3)
    ext = PDFExtractor()
    delivered: list[int] = []

    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch.object(ext, "_mineru_server_available", return_value=False), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch("nexus.config.get_pdf_ladder_breaker", return_value=3), \
         patch("nexus.config.get_mineru_page_timeout_s", return_value=120), \
         patch("nexus.config.get_mineru_memory_ceiling_mb", return_value=0):
        with pytest.raises(MineruSystemicFailure) as info:
            ext._extract_with_mineru(pdf, on_page=lambda i, t, m: delivered.append(i))

    exc = info.value
    # Non-vacuity: the worker process ran and exited (a return code is in the class), and what it
    # printed is the shim's line, not some other failure of the harness.
    assert exc.failure_class.startswith("exit:1:"), exc.failure_class
    assert MARKER in exc.failure_class, exc.failure_class
    assert "ImportError" in exc.failure_class, exc.failure_class
    assert exc.failure_class in str(exc) and MARKER in str(exc)
    assert delivered == [0, 1], (
        "pages 1 and 2 settled on their text layer; page 3 tripped the rule before its rung 3"
    )
