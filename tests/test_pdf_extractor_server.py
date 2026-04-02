# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for MinerU server-backed extraction in pdf_extractor.py.

TDD — defines expected behavior for _mineru_server_available(),
_mineru_run_via_server(), and the fallback path in _mineru_run_isolated().
Bead: nexus-rkgn, Epic: nexus-5f2b (RDR-046 Phase 2).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.pdf_extractor import PDFExtractor


@pytest.fixture
def extractor() -> PDFExtractor:
    return PDFExtractor()


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"dummy pdf bytes")
    return p


def _server_response(
    stem: str = "paper",
    md_content: str = "# Title\nSome text",
    content_list: list | None = None,
    middle_json: dict | None = None,
) -> dict:
    """Build a mock /file_parse response matching mineru-api format."""
    cl = content_list if content_list is not None else [{"type": "text", "text": "hello"}]
    mj = middle_json if middle_json is not None else {"pdf_info": [{"page_idx": 0}]}
    return {
        "results": {
            stem: {
                "md_content": md_content,
                "content_list": json.dumps(cl),
                "middle_json": json.dumps(mj),
            },
        },
    }


# ── _mineru_server_available ─────────────────────────────────────────────────


class TestMineruServerAvailable:
    """_mineru_server_available() checks GET /health on the configured server."""

    def test_returns_true_on_health_200(self, extractor: PDFExtractor) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("nexus.pdf_extractor.httpx.get", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010"},
            }),
        ):
            assert extractor._mineru_server_available() is True

    def test_returns_false_on_connect_error(self, extractor: PDFExtractor) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.get",
                  side_effect=httpx.ConnectError("refused")),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010"},
            }),
        ):
            assert extractor._mineru_server_available() is False

    def test_returns_false_on_timeout(self, extractor: PDFExtractor) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.get",
                  side_effect=httpx.TimeoutException("timeout")),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010"},
            }),
        ):
            assert extractor._mineru_server_available() is False

    def test_cached_per_instance(self, extractor: PDFExtractor) -> None:
        """Health check is cached: 5 calls → GET /health called exactly once."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("nexus.pdf_extractor.httpx.get", return_value=mock_resp) as mock_get,
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010"},
            }),
        ):
            for _ in range(5):
                extractor._mineru_server_available()

            assert mock_get.call_count == 1


# ── _mineru_run_via_server ───────────────────────────────────────────────────


class TestMineruRunViaServer:
    """_mineru_run_via_server() POSTs to /file_parse and parses the response."""

    def test_posts_correct_form_params(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Verifies all required form params sent to /file_parse."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _server_response()
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp) as mock_post,
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, 5)

        # Verify the POST was made
        call_kwargs = mock_post.call_args
        data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
        assert data["backend"] == "pipeline"
        assert data["formula_enable"] == "true"
        assert data["return_md"] == "true"
        assert data["return_middle_json"] == "true"
        assert data["return_content_list"] == "true"
        assert data["parse_method"] == "auto"
        assert data["lang_list"] == "en"
        assert data["start_page_id"] == 0
        assert data["end_page_id"] == 5

    def test_table_enable_true_sent_as_string(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """table_enable=True in config → 'true' in POST data."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _server_response()
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp) as mock_post,
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": True},
            }),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, 5)

        call_kwargs = mock_post.call_args
        data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
        assert data["table_enable"] == "true"

    def test_end_none_sends_sentinel(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """end=None → end_page_id=99999 sentinel for 'all remaining pages'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _server_response()
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp) as mock_post,
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, None)

        call_kwargs = mock_post.call_args
        data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
        assert data["end_page_id"] == 99999

    def test_key_miss_falls_back_to_single_result(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """When stem doesn't match results key, falls back to first result."""
        response = {
            "results": {
                "normalized_paper": {  # server normalized the filename
                    "md_content": "# Recovered",
                    "content_list": json.dumps([]),
                    "middle_json": json.dumps({"pdf_info": []}),
                },
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            md, cl, pi = extractor._mineru_run_via_server(dummy_pdf, 0, None)

        assert md == "# Recovered"

    def test_empty_md_content_raises(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Empty md_content raises RuntimeError."""
        response = _server_response(md_content="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            with pytest.raises(RuntimeError, match="empty md_content"):
                extractor._mineru_run_via_server(dummy_pdf, 0, None)

    def test_json_fields_are_parsed(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """content_list and middle_json are JSON strings — json.loads() applied."""
        cl_data = [{"type": "text", "text": "parsed"}]
        mj_data = {"pdf_info": [{"page_idx": 0, "para_blocks": [{"type": "equation"}]}]}
        response = _server_response(content_list=cl_data, middle_json=mj_data)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            md, content_list, pdf_info = extractor._mineru_run_via_server(
                dummy_pdf, 0, None,
            )

        assert content_list == cl_data
        assert pdf_info == mj_data["pdf_info"]

    def test_http_error_raises(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """HTTP 409 (extraction failure) raises — not a silent skip."""
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Conflict", request=MagicMock(), response=mock_resp,
        )

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                extractor._mineru_run_via_server(dummy_pdf, 0, None)

    def test_http_503_raises(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """HTTP 503 (server temporarily unavailable) raises to trigger fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Service Unavailable", request=MagicMock(), response=mock_resp,
        )

        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                extractor._mineru_run_via_server(dummy_pdf, 0, None)

    def test_filename_with_spaces_and_unicode(self, tmp_path: Path) -> None:
        """Filenames with spaces, parens, and unicode work — stem lookup succeeds."""
        pdf = tmp_path / "my paper (2024) résumé.pdf"
        pdf.write_bytes(b"dummy")
        stem = pdf.stem  # "my paper (2024) résumé"

        response = _server_response(stem=stem)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        mock_resp.raise_for_status = MagicMock()

        extractor = PDFExtractor()
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={
                "pdf": {"mineru_server_url": "http://127.0.0.1:8010",
                        "mineru_table_enable": False},
            }),
        ):
            md, cl, pi = extractor._mineru_run_via_server(pdf, 0, None)

        assert md == "# Title\nSome text"


# ── _mineru_run_isolated fallback logic ──────────────────────────────────────


class TestMineruRunIsolatedFallback:
    """_mineru_run_isolated() dispatches to server or subprocess."""

    def test_uses_server_when_available(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Calls _mineru_run_via_server when server is available."""
        server_result = ("# Server", [], [{"page_idx": 0}])

        with (
            patch.object(extractor, "_mineru_server_available", return_value=True),
            patch.object(extractor, "_mineru_run_via_server",
                         return_value=server_result) as mock_server,
            patch.object(extractor, "_mineru_run_subprocess") as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)

        mock_server.assert_called_once_with(dummy_pdf, 0, None)
        mock_sub.assert_not_called()
        assert result == server_result

    def test_falls_back_to_subprocess_when_unavailable(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Calls _mineru_run_subprocess when server is not available."""
        sub_result = ("# Subprocess", [], [])

        with (
            patch.object(extractor, "_mineru_server_available", return_value=False),
            patch.object(extractor, "_mineru_run_via_server") as mock_server,
            patch.object(extractor, "_mineru_run_subprocess",
                         return_value=sub_result) as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)

        mock_server.assert_not_called()
        mock_sub.assert_called_once_with(dummy_pdf, 0, None)
        assert result == sub_result

    def test_falls_back_to_subprocess_on_http_error(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Server returns HTTP 503 → falls back to subprocess, not propagated."""
        sub_result = ("# Subprocess fallback", [], [])

        with (
            patch.object(extractor, "_mineru_server_available", return_value=True),
            patch.object(extractor, "_mineru_run_via_server",
                         side_effect=httpx.HTTPStatusError(
                             "503", request=MagicMock(), response=MagicMock())),
            patch.object(extractor, "_mineru_run_subprocess",
                         return_value=sub_result) as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)

        mock_sub.assert_called_once_with(dummy_pdf, 0, None)
        assert result == sub_result


# ── Adaptive page ranges + OOM retry (Phase 3) ──────────────────────────────


def _mock_pymupdf(page_count: int):
    """Return a patch context for pymupdf.open that reports page_count."""
    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=page_count)
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_pymupdf = MagicMock()
    mock_pymupdf.open = MagicMock(return_value=mock_doc)
    return patch.dict("sys.modules", {"pymupdf": mock_pymupdf})


def _mock_do_parse():
    return patch("nexus.pdf_extractor.do_parse", MagicMock())


class TestAdaptivePageRanges:
    """Adaptive page-range sizing and OOM retry in _extract_with_mineru."""

    def test_default_1page_ranges(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Default mineru_page_batch=1: 5-page PDF → 5 single-page calls."""
        isolated_return = ("text", [], [{"page_idx": 0}])

        with (
            _mock_pymupdf(5), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=1),
            patch.object(extractor, "_mineru_run_isolated",
                         return_value=isolated_return) as mock_iso,
        ):
            extractor._extract_with_mineru(dummy_pdf)

        assert mock_iso.call_count == 5
        calls = [c.args[1:] for c in mock_iso.call_args_list]
        assert calls == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]

    def test_configurable_batch_size(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """pdf.mineru_page_batch=5 with 10 pages → 2 calls: (0,5), (5,10)."""
        isolated_return = ("text", [], [])

        with (
            _mock_pymupdf(10), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated",
                         return_value=isolated_return) as mock_iso,
        ):
            extractor._extract_with_mineru(dummy_pdf)

        assert mock_iso.call_count == 2
        calls = [c.args[1:] for c in mock_iso.call_args_list]
        assert calls == [(0, 5), (5, 10)]

    def test_oom_retry_splits_to_single_pages(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """OOM on a multi-page batch → retry at 1-page granularity for that range."""
        ok_return = ("text", [], [])

        # First call (0,5) raises RuntimeError (OOM), then per-page retries succeed
        call_count = 0

        def mock_isolated(path, start, end):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First batch (0,5) fails with OOM
                raise RuntimeError("MinerU subprocess exited with code -9")
            return ok_return

        with (
            _mock_pymupdf(5), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated",
                         side_effect=mock_isolated) as mock_iso,
        ):
            result = extractor._extract_with_mineru(dummy_pdf)

        # 1 failed batch + 5 per-page retries = 6 calls total
        assert mock_iso.call_count == 6
        assert result.metadata["extraction_method"] == "mineru"

    def test_oom_retry_single_page_propagates(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """Already at batch_size=1 and fails → RuntimeError propagates (no infinite loop)."""
        def mock_isolated(path, start, end):
            raise RuntimeError("MinerU subprocess exited with code -9")

        with (
            _mock_pymupdf(3), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=1),
            patch.object(extractor, "_mineru_run_isolated",
                         side_effect=mock_isolated),
        ):
            with pytest.raises(RuntimeError, match="code -9"):
                extractor._extract_with_mineru(dummy_pdf)

    def test_oom_retry_structlog_event(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """OOM retry logs mineru_oom_retry event with batch range."""
        ok_return = ("text", [], [])
        call_count = 0

        def mock_isolated(path, start, end):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("OOM")
            return ok_return

        with (
            _mock_pymupdf(5), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated",
                         side_effect=mock_isolated),
            patch("nexus.pdf_extractor._log") as mock_log,
        ):
            extractor._extract_with_mineru(dummy_pdf)

        # Find the mineru_oom_retry log call
        warning_calls = [
            c for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "mineru_oom_retry"
        ]
        assert len(warning_calls) == 1

    def test_oom_multi_batch_only_retries_failed_range(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """With 10 pages at batch=5: first batch (0,5) fails, retried as 5 singles.
        Second batch (5,10) succeeds normally."""
        ok_return = ("text", [], [])
        call_count = 0

        def mock_isolated(path, start, end):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and start == 0 and end == 5:
                raise RuntimeError("OOM")
            return ok_return

        with (
            _mock_pymupdf(10), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated",
                         side_effect=mock_isolated) as mock_iso,
        ):
            result = extractor._extract_with_mineru(dummy_pdf)

        # 1 failed (0,5) + 5 retries (0,1)..(4,5) + 1 success (5,10) = 7 calls
        assert mock_iso.call_count == 7
        assert result.metadata["extraction_method"] == "mineru"
