# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction with auto-detect math routing.

Extraction backends (three tiers, selected by ``extractor`` param):
1. Docling — neural layout model for multi-column academic PDFs, Type3 fonts,
   and complex tables.  Enriched mode enables formula detection via FormulaItem.
2. MinerU — math-aware extraction (optional ``mineru`` extra).  Used when auto
   mode detects formulas in the Docling pass.
3. PyMuPDF normalized — final fallback for all extraction failures.

Auto mode (default): Docling pass → if formulas detected → try MinerU → fallback
to Docling → fallback to PyMuPDF normalized.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re
import subprocess
import sys
import tempfile

import httpx
import structlog

try:
    from mineru.cli.common import do_parse
except ImportError:
    do_parse = None  # type: ignore[assignment]


# Inline script executed in a child Python process for memory isolation.
# Uses os._exit to force-terminate without waiting for daemon threads / worker
# pools that MinerU's pipeline may leave running.
_MINERU_WORKER_SCRIPT = '''
import json, sys, os
from pathlib import Path
from mineru.cli.common import do_parse

pdf_path, result_dir, start, end_str = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
end = None if end_str == "none" else int(end_str)
do_parse(
    result_dir,
    pdf_file_names=[Path(pdf_path).name],
    pdf_bytes_list=[Path(pdf_path).read_bytes()],
    p_lang_list=["en"],
    formula_enable=True,
    table_enable=True,
    start_page_id=start,
    end_page_id=end,
)
os._exit(0)
'''

_log = structlog.get_logger(__name__)


def _normalize_whitespace_edge_cases(text: str) -> str:
    """Normalize whitespace variants not covered by basic normalization.

    - Replace tab characters with a single space.
    - Collapse Unicode non-breaking and exotic whitespace to a single space.
    - Collapse 4+ consecutive newlines to three (preserving intentional breaks).
    """
    text = text.replace("\t", " ")
    text = re.sub(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PDFExtractor:
    """Extract PDF text via Docling with PyMuPDF normalized fallback.

    Docling uses a neural layout model to handle multi-column academic PDFs,
    producing structured markdown with headings and correct reading order.
    Falls back to PyMuPDF normalized extraction on any Docling failure.
    """

    def __init__(self) -> None:
        self._converter = None  # lazy init — fast mode (no formula enrichment)
        self._converter_enriched = None  # lazy init — enriched mode (formula enrichment)
        self._mineru_server_checked: bool = False
        self._mineru_server_up: bool = False
        self._mineru_server_restarts: int = 0

    def extract(self, pdf_path: Path, *, extractor: str = "auto") -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult.

        *extractor* selects the backend:
        - ``"auto"`` — Docling pass (enriched, to detect formulas); if
          formulas found, try MinerU then fall back to PyMuPDF normalized.
        - ``"docling"`` — Docling with PyMuPDF normalized fallback.
        - ``"mineru"`` — MinerU directly (no fallback).
        """
        if extractor not in ("auto", "docling", "mineru"):
            raise ValueError(
                f"extractor must be 'auto', 'docling', or 'mineru'; got {extractor!r}"
            )

        if extractor == "docling":
            try:
                return self._extract_with_docling(pdf_path)
            except Exception:
                _log.warning(
                    "docling extraction failed; falling back to pymupdf_normalized",
                    exc_info=True,
                )
                return self._extract_normalized(pdf_path)

        if extractor == "mineru":
            return self._extract_with_mineru(pdf_path)

        # extractor == "auto"
        try:
            fast_result = self._extract_with_docling(pdf_path)
        except Exception:
            _log.warning(
                "docling fast pass failed; falling back to pymupdf_normalized",
                exc_info=True,
            )
            return self._extract_normalized(pdf_path)

        formula_count = fast_result.metadata.get("formula_count", 0)
        if formula_count == 0:
            return fast_result

        # Math paper detected — try MinerU
        try:
            return self._extract_with_mineru(pdf_path, formula_count=formula_count)
        except Exception:
            _log.warning(
                "mineru_extraction_failed; returning docling result",
                exc_info=True,
            )
            return fast_result

    # ── internal extraction methods ───────────────────────────────────────────

    def _get_converter(self, enriched: bool = False):
        """Lazily initialise the Docling DocumentConverter.

        *enriched* enables ``do_formula_enrichment`` for LaTeX extraction.
        Two converters are cached independently so callers can switch modes
        without re-creating the converter each time.
        """
        attr = "_converter_enriched" if enriched else "_converter"
        converter = getattr(self, attr)
        if converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions

            opts = PdfPipelineOptions()
            opts.do_ocr = False                 # digital PDFs have embedded text
            opts.do_table_structure = True      # TableFormer for table detection
            opts.generate_page_images = False
            opts.generate_picture_images = False
            opts.do_formula_enrichment = enriched
            converter = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
            setattr(self, attr, converter)
        return converter

    def _extract_with_docling(self, pdf_path: Path) -> ExtractionResult:
        """Extract per-page markdown via Docling."""
        result = self._get_converter(enriched=True).convert(str(pdf_path))
        doc = result.document
        page_count = doc.num_pages()

        page_texts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        for p in range(1, page_count + 1):
            page_md = doc.export_to_markdown(page_no=p).strip()
            if page_md:
                page_boundaries.append(
                    {
                        "page_number": p,
                        "start_char": current_pos,
                        # +1 includes the \n separator from "\n".join so that
                        # _page_for ranges are contiguous (same convention as the
                        # former _extract_markdown implementation).
                        "page_text_length": len(page_md) + 1,
                    }
                )
                page_texts.append(page_md)
                current_pos += len(page_md) + 1

        text = "\n".join(page_texts)
        if not text.strip():
            raise RuntimeError("docling produced empty output")

        # Collect TableItem regions and count FormulaItem (duck-typed, single pass)
        table_regions: list[dict] = []
        formula_count = 0
        for item, _ in doc.iterate_items():
            item_type = type(item).__name__
            if item_type == "FormulaItem":
                formula_count += 1
            elif item_type == "TableItem":
                prov = getattr(item, "prov", [])
                page_no = prov[0].page_no if prov else 0
                html = ""
                if callable(getattr(item, "export_to_html", None)):
                    try:
                        html = item.export_to_html(doc=doc)
                    except Exception as exc:
                        _log.debug("table_html_export_failed", page=page_no, error=str(exc))
                        html = ""
                table_regions.append({"page": page_no, "html": html})

        if formula_count > 0:
            _log.warning("formula_content_detected", formula_count=formula_count, path=str(pdf_path))

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "docling",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
                "table_regions": table_regions,
                "formula_count": formula_count,
                "docling_title": self._extract_title(doc),
                "pdf_title": "",  # XMP metadata not exposed by Docling
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

    # Maximum pages per MinerU batch.  Large formula-dense PDFs (e.g. 108-page
    # Grossberg 1986) OOM during MFR prediction when processed as a single pass.
    # Splitting into page ranges keeps peak memory bounded.  Small batches add
    # only ~5s model-init overhead each — negligible vs. the cost of an OOM kill.
    MINERU_PAGE_BATCH = 1

    def _extract_with_mineru(
        self, pdf_path: Path, *, formula_count: int = 0,
    ) -> ExtractionResult:
        """Extract text via MinerU (math-aware, optional dependency).

        Each page-range batch runs in a **subprocess** so that MinerU's
        GPU/model memory is fully reclaimed between batches.  Without this,
        memory accumulates across in-process ``do_parse`` calls and large
        formula-dense PDFs get OOM-killed.

        OOM retry: if a multi-page batch fails, retries at 1-page granularity.
        Single-page failures propagate immediately (no infinite retry).
        """
        if do_parse is None:
            raise ImportError(
                "MinerU is not installed. Install with: uv pip install 'conexus[mineru]'"
            )

        import pymupdf  # lightweight — only used for page count

        with pymupdf.open(pdf_path) as doc:
            total_pages = len(doc)

        from nexus.config import get_mineru_page_batch
        batch_size = get_mineru_page_batch()

        batches: list[tuple[int, int | None]] = []
        if total_pages <= batch_size:
            batches.append((0, None))
        else:
            _log.info(
                "mineru_splitting_large_pdf",
                total_pages=total_pages,
                batch_size=batch_size,
                path=str(pdf_path),
            )
            for start in range(0, total_pages, batch_size):
                batches.append((start, min(start + batch_size, total_pages)))

        md_parts: list[str] = []
        all_content_list: list[dict] = []
        all_pdf_info: list[dict] = []

        for start, end in batches:
            label = f"{start + 1}–{end}" if end is not None else f"{start + 1}–{total_pages}"
            _log.info("mineru_batch", pages=label, path=str(pdf_path))
            try:
                md, content_list, pdf_info = self._mineru_run_isolated(pdf_path, start, end)
            except RuntimeError:
                # OOM or subprocess failure — retry at 1-page granularity
                span = (end or total_pages) - start
                if span <= 1:
                    raise  # already single-page, no retry possible
                _log.warning(
                    "mineru_oom_retry",
                    pages=label,
                    path=str(pdf_path),
                    original_batch=span,
                )
                for page in range(start, end or total_pages):
                    md, content_list, pdf_info = self._mineru_run_isolated(
                        pdf_path, page, page + 1,
                    )
                    md_parts.append(md)
                    all_content_list.extend(content_list)
                    all_pdf_info.extend(pdf_info)
                continue
            md_parts.append(md)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)

        md_text = "\n".join(md_parts)
        return self._mineru_build_result(
            pdf_path, md_text, all_content_list, all_pdf_info,
        )

    def _mineru_server_available(self) -> bool:
        """Check if the MinerU API server is reachable.

        Result cached for the lifetime of this PDFExtractor instance —
        a False result is never retried. Create a new instance to re-check.
        """
        if self._mineru_server_checked:
            return self._mineru_server_up

        from nexus.config import get_mineru_server_url
        url = f"{get_mineru_server_url()}/health"
        try:
            resp = httpx.get(url, timeout=2)
            self._mineru_server_up = resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            self._mineru_server_up = False

        self._mineru_server_checked = True
        return self._mineru_server_up

    def _mineru_run_via_server(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Extract via MinerU HTTP server (POST /file_parse)."""
        from nexus.config import get_mineru_server_url, get_mineru_table_enable

        url = f"{get_mineru_server_url()}/file_parse"
        with pdf_path.open("rb") as f:
            resp = httpx.post(
                url,
                files=[("files", (pdf_path.name, f, "application/pdf"))],
                data={
                    "backend": "pipeline",
                    "start_page_id": str(start),
                    "end_page_id": str(end if end is not None else 99999),
                    "formula_enable": "true",
                    "table_enable": str(get_mineru_table_enable()).lower(),
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_content_list": "true",
                    "parse_method": "auto",
                    "lang_list": "en",
                },
                timeout=300,
            )
        resp.raise_for_status()
        data = resp.json()

        all_results = data.get("results", {})
        stem = pdf_path.stem
        results = all_results.get(stem)
        if results is None:
            if len(all_results) == 1:
                results = next(iter(all_results.values()))
            else:
                raise RuntimeError(
                    f"Server results missing key {stem!r}; "
                    f"available keys: {list(all_results.keys())}"
                )

        md = results.get("md_content", "")
        if not md:
            raise RuntimeError(
                f"Server returned empty md_content for {pdf_path.name}"
            )

        raw_cl = results.get("content_list")
        raw_mj = results.get("middle_json")
        if raw_mj is None:
            _log.warning("mineru_server_no_middle_json", path=str(pdf_path))
        content_list = json.loads(raw_cl) if raw_cl else []
        middle = json.loads(raw_mj) if raw_mj else {}
        return md, content_list, middle.get("pdf_info", [])

    _MINERU_MAX_RESTARTS: int = 2

    def _restart_mineru_server(self) -> bool:
        """Attempt to restart the MinerU server after a crash.

        Returns True if the server was restarted and is healthy.
        Limited to _MINERU_MAX_RESTARTS per PDFExtractor instance.
        """
        if self._mineru_server_restarts >= self._MINERU_MAX_RESTARTS:
            _log.warning("mineru_restart_budget_exhausted",
                         restarts=self._mineru_server_restarts)
            return False

        self._mineru_server_restarts += 1
        _log.info("mineru_server_restarting",
                  attempt=self._mineru_server_restarts)

        from nexus.commands.mineru import (
            _is_process_alive,
            _pid_file_path,
            _read_pid_file,
        )

        # Clean up stale PID file if the server is dead
        info = _read_pid_file()
        if info is not None and not _is_process_alive(info["pid"]):
            _pid_file_path().unlink(missing_ok=True)

        # Start a new server via the same logic as `nx mineru start`
        import subprocess as _sp
        import time as _time
        from nexus.commands.mineru import (
            _HEALTH_POLL_INTERVAL,
            _find_free_port,
            _server_env,
        )
        from nexus.config import set_config_value

        port = _find_free_port()
        cmd = ["mineru-api", "--host", "127.0.0.1", "--port", str(port)]
        try:
            proc = _sp.Popen(
                cmd, env=_server_env(),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            _log.warning("mineru_restart_failed", reason="mineru-api not found")
            return False

        # Poll health for up to 60s (models already cached in memory by OS)
        url = f"http://127.0.0.1:{port}/health"
        deadline = _time.monotonic() + 60
        while _time.monotonic() < deadline:
            if proc.poll() is not None:
                _log.warning("mineru_restart_failed", reason="process exited")
                return False
            try:
                resp = httpx.get(url, timeout=2)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            _time.sleep(_HEALTH_POLL_INTERVAL)
        else:
            _log.warning("mineru_restart_failed", reason="health timeout")
            return False

        # Write PID file and update config
        import json as _json
        from datetime import datetime, timezone
        pid_path = _pid_file_path()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(_json.dumps({
            "pid": proc.pid, "port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }))
        set_config_value("pdf.mineru_server_url", f"http://127.0.0.1:{port}")

        # Reset availability cache
        self._mineru_server_checked = True
        self._mineru_server_up = True
        _log.info("mineru_server_restarted", pid=proc.pid, port=port)
        return True

    def _mineru_run_isolated(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Dispatch to server or subprocess based on server availability."""
        if self._mineru_server_available():
            try:
                return self._mineru_run_via_server(pdf_path, start, end)
            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError) as exc:
                # Server crashed — invalidate cache, try restart
                self._mineru_server_checked = True
                self._mineru_server_up = False
                _log.warning("mineru_server_lost", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                if self._restart_mineru_server():
                    # Retry this page on the new server
                    try:
                        return self._mineru_run_via_server(pdf_path, start, end)
                    except Exception:
                        pass  # fall through to subprocess
                return self._mineru_run_subprocess(pdf_path, start, end)
            except httpx.HTTPStatusError as exc:
                _log.warning("mineru_server_error", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                return self._mineru_run_subprocess(pdf_path, start, end)
        return self._mineru_run_subprocess(pdf_path, start, end)

    def _mineru_run_subprocess(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Run MinerU in a fresh OS process for full memory isolation.

        Uses ``subprocess.run`` with an inline Python script so the child
        loads MinerU models independently.  When the child exits, all
        GPU/model memory is reclaimed by the OS — no leaks across batches.
        """
        result_dir = tempfile.mkdtemp()
        try:
            import os as _os
            import signal

            proc = subprocess.Popen(
                [
                    sys.executable, "-c", _MINERU_WORKER_SCRIPT,
                    str(pdf_path), result_dir,
                    str(start), "none" if end is None else str(end),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # own process group
            )
            try:
                returncode = proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                _os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise RuntimeError(
                    f"MinerU subprocess timed out after 180s "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
            if returncode != 0:
                # Clean up any orphaned children in the process group
                try:
                    _os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _log.error(
                    "mineru_subprocess_failed",
                    returncode=returncode,
                    pages=f"{start}–{end}",
                    path=str(pdf_path),
                )
                raise RuntimeError(
                    f"MinerU subprocess exited with code {returncode} "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
            # Kill any lingering workers in the process group
            try:
                _os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

            pdf_name = pdf_path.name
            base = Path(result_dir) / pdf_name / "auto"
            md = (base / f"{pdf_name}.md").read_text(encoding="utf-8")
            content_list: list[dict] = json.loads(
                (base / f"{pdf_name}_content_list.json").read_text(encoding="utf-8")
            )
            middle: dict = json.loads(
                (base / f"{pdf_name}_middle.json").read_text(encoding="utf-8")
            )
            return md, content_list, middle.get("pdf_info", [])
        finally:
            import shutil
            shutil.rmtree(result_dir, ignore_errors=True)

    @staticmethod
    def _mineru_build_result(
        pdf_path: Path, md_text: str,
        content_list: list[dict], pdf_info: list[dict],
    ) -> ExtractionResult:
        """Assemble an ExtractionResult from (merged) MinerU outputs."""
        display_count = sum(1 for e in content_list if e.get("type") == "equation")

        inline_count = 0
        for page in pdf_info:
            for block in page.get("para_blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") == "inline_equation":
                            inline_count += 1

        formula_count = display_count + inline_count
        page_count = len(pdf_info)
        total_len = len(md_text)

        page_boundaries: list[dict] = []
        if page_count > 0 and total_len > 0:
            chars_per_page = total_len / page_count
            for i in range(page_count):
                start = int(i * chars_per_page)
                length = int(chars_per_page) + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": start,
                    "page_text_length": length,
                })

        if formula_count > 0:
            _log.info(
                "mineru_formulas_extracted",
                formula_count=formula_count,
                path=str(pdf_path),
            )

        return ExtractionResult(
            text=md_text,
            metadata={
                "extraction_method": "mineru",
                "page_count": page_count,
                "format": "markdown",
                "formula_count": formula_count,
                "page_boundaries": page_boundaries,
                "table_regions": [],
                "docling_title": "",
                "pdf_title": "",
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

    def _extract_title(self, doc) -> str:
        """Extract a paper title from Docling document items on page 1.

        Algorithm (verified on 19 corpus PDFs, 17/19 correct):
        1. Iterate page-1 items, skip section labels (abstract, introduction, keywords).
        2. Return first item with label containing 'title' or 'section_header'.
        3. Fallback: first text-labelled item on page 1 with 10 ≤ len < 120.
        """
        _SKIP = {"abstract", "introduction", "1 introduction", "keywords"}

        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if not text or len(text) < 10:
                continue
            lower = text.lower()
            if lower in _SKIP:
                continue
            if lower.startswith("abstract") and len(text) > 100:
                continue
            label = str(getattr(item, "label", ""))
            if "title" in label or "section_header" in label:
                return text

        # Fallback: first short text block on page 1
        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if text and 10 <= len(text) < 120:
                return text

        return ""

    def _extract_normalized(self, pdf_path: Path) -> ExtractionResult:
        """Extract via raw PyMuPDF with whitespace normalization."""
        import pymupdf  # lazy

        text_parts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        with pymupdf.open(pdf_path) as doc:
            page_count = len(doc)
            doc_meta = doc.metadata or {}
            for page_num, page in enumerate(doc):
                raw: str = page.get_text(sort=True)
                # Normalize per-page so page_boundaries match character positions
                # in the final joined text (global normalization after the fact
                # would shift boundaries unpredictably).
                page_text = re.sub(r" +", " ", raw)
                page_text = re.sub(r"\n{3,}", "\n\n", page_text)
                page_text = "\n".join(line.rstrip() for line in page_text.split("\n")).strip()
                page_text = _normalize_whitespace_edge_cases(page_text)
                if page_text:
                    page_boundaries.append(
                        {
                            "page_number": page_num + 1,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join (same
                            # rationale as _extract_with_docling: contiguous ranges).
                            "page_text_length": len(page_text) + 1,
                        }
                    )
                    text_parts.append(page_text)
                    current_pos += len(page_text) + 1

        text = "\n".join(text_parts)

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "pymupdf_normalized",
                "page_count": page_count,
                "format": "normalized",
                "page_boundaries": page_boundaries,
                "docling_title": "",
                "pdf_title": doc_meta.get("title", ""),
                "pdf_author": doc_meta.get("author", ""),
                "pdf_subject": doc_meta.get("subject", ""),
                "pdf_keywords": doc_meta.get("keywords", ""),
                "pdf_creator": doc_meta.get("creator", ""),
                "pdf_producer": doc_meta.get("producer", ""),
                "pdf_creation_date": doc_meta.get("creationDate", ""),
                "pdf_mod_date": doc_meta.get("modDate", ""),
                "formula_count": 0,
            },
        )
