# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from nexus.config import MineruSkewInfo
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


_MINERU_CFG = {"pdf": {"mineru_server_url": "http://127.0.0.1:8010"}}
_MINERU_CFG_NO_TABLE = {"pdf": {"mineru_server_url": "http://127.0.0.1:8010", "mineru_table_enable": False}}


def _patch_config(cfg: dict = _MINERU_CFG_NO_TABLE):
    return patch("nexus.config.load_config", return_value=cfg)


def _mock_post_ok(response: dict | None = None):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = response or _server_response()
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _mock_post_error(status: int, message: str):
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        message, request=MagicMock(), response=mock_resp,
    )
    return mock_resp


# ── _mineru_server_available ─────────────────────────────────────────────────


class TestMineruServerAvailable:

    @pytest.mark.parametrize("side_effect,expected", [
        (None, True),
        (httpx.ConnectError("refused"), False),
        (httpx.TimeoutException("timeout"), False),
    ], ids=["health_200", "connect_error", "timeout"])
    def test_health_check(self, extractor: PDFExtractor, side_effect, expected) -> None:
        mock_resp = MagicMock(status_code=200)
        kwargs = {"return_value": mock_resp} if side_effect is None else {"side_effect": side_effect}
        with patch("nexus.pdf_extractor.httpx.get", **kwargs), _patch_config(_MINERU_CFG):
            assert extractor._mineru_server_available() is expected

    def test_cached_per_instance(self, extractor: PDFExtractor) -> None:
        mock_resp = MagicMock(status_code=200)
        with (
            patch("nexus.pdf_extractor.httpx.get", return_value=mock_resp) as mock_get,
            _patch_config(_MINERU_CFG),
        ):
            for _ in range(5):
                extractor._mineru_server_available()
            assert mock_get.call_count == 1


# ── nexus-eti1v: a KNOWN skew must skip straight to subprocess ───────────────
#
# Measured 2026-09-02 on one 54-page PDF: mineru_server_version_skew logged
# 63 times (once per health poll), then the client dialled the hardcoded
# default :8010 (refused), "rediscovered" the same :8010, waited the full
# mineru_ensure_health_timeout (120s), then fell back to subprocess -- ~2.5
# minutes spent on a server it had already decided not to use. On a
# get_mineru_server_url() sentinel (None), the extractor must not dial
# anything, must not wait, and must log exactly once with both versions.


_SKEW = MineruSkewInfo(
    registered_version="3.4.5", our_version="3.1.11",
    pid=12345, port=61425, registered_python="/some/python",
)


class TestMineruServerAvailableSkew:

    def test_skips_straight_to_subprocess_no_http_dial(self, extractor: PDFExtractor) -> None:
        with (
            patch("nexus.config.get_mineru_server_url", return_value=None),
            patch("nexus.config.get_mineru_skew_info", return_value=_SKEW),
            patch("nexus.pdf_extractor.httpx.get") as mock_get,
        ):
            assert extractor._mineru_server_available() is False
            mock_get.assert_not_called()

    def test_logs_once_with_both_versions_and_remedy(self, extractor: PDFExtractor) -> None:
        # The fallback log is INFO-level (bead nexus-eti1v: it is expected
        # behavior on a known skew, not itself a warning condition — the
        # config-layer mineru_server_version_skew WARNING already covers
        # that). The suite's default structlog level is WARNING, so raise
        # it for this assertion the same way test_nx_answer_plan_choice.py
        # does for its own INFO-level event assertions.
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with (
            patch("nexus.config.get_mineru_server_url", return_value=None),
            patch("nexus.config.get_mineru_skew_info", return_value=_SKEW),
            capture_logs() as logs,
        ):
            extractor._mineru_server_available()

        fallback_events = [
            e for e in logs if e.get("event") == "mineru_skew_subprocess_fallback"
        ]
        assert len(fallback_events) == 1, (
            f"expected exactly one mineru_skew_subprocess_fallback log, "
            f"got {len(fallback_events)}: {fallback_events}"
        )
        event = fallback_events[0]
        assert event["registered_version"] == "3.4.5"
        assert event["our_version"] == "3.1.11"
        assert "nx mineru restart" in event["remedy"]

    def test_no_health_wait_no_ensure_mineru_running_call(self, extractor: PDFExtractor) -> None:
        """The bug's core cost: a full mineru_ensure_health_timeout wait
        for a server already known unusable. On skew, ensure_mineru_running
        (the autostart/health-wait path) must never be reached."""
        with (
            patch("nexus.config.get_mineru_server_url", return_value=None),
            patch("nexus.config.get_mineru_skew_info", return_value=_SKEW),
            patch(
                "nexus.daemon.mineru_lifecycle.ensure_mineru_running",
            ) as mock_ensure,
        ):
            extractor._mineru_server_available()
            mock_ensure.assert_not_called()

    def test_result_cached_per_instance(self, extractor: PDFExtractor) -> None:
        with (
            patch("nexus.config.get_mineru_server_url", return_value=None) as mock_url,
            patch("nexus.config.get_mineru_skew_info", return_value=_SKEW),
        ):
            for _ in range(5):
                assert extractor._mineru_server_available() is False
            assert mock_url.call_count == 1


# ── _mineru_run_via_server ───────────────────────────────────────────────────


class TestMineruRunViaServer:

    def test_posts_correct_form_params(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok()) as mock_post,
            _patch_config(),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, 5)

        data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1].get("data", {})
        expected = {
            "backend": "pipeline", "formula_enable": "true", "return_md": "true",
            "return_middle_json": "true", "return_content_list": "true",
            "parse_method": "auto", "lang_list": "en",
            "start_page_id": "0", "end_page_id": "5",
        }
        for k, v in expected.items():
            assert data[k] == v

    def test_table_enable_true_sent_as_string(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        cfg = {"pdf": {"mineru_server_url": "http://127.0.0.1:8010", "mineru_table_enable": True}}
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok()) as mock_post,
            _patch_config(cfg),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, 5)

        data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1].get("data", {})
        assert data["table_enable"] == "true"

    def test_end_none_sends_sentinel(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok()) as mock_post,
            _patch_config(),
        ):
            extractor._mineru_run_via_server(dummy_pdf, 0, None)

        data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1].get("data", {})
        assert data["end_page_id"] == "99999"

    def test_key_miss_falls_back_to_single_result(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        response = {
            "results": {
                "normalized_paper": {
                    "md_content": "# Recovered",
                    "content_list": json.dumps([]),
                    "middle_json": json.dumps({"pdf_info": []}),
                },
            },
        }
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok(response)),
            _patch_config(),
        ):
            md, _, _ = extractor._mineru_run_via_server(dummy_pdf, 0, None)
        assert md == "# Recovered"

    def test_key_miss_multiple_results_raises(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        response = {
            "results": {
                "other_paper": {"md_content": "# Wrong"},
                "another_paper": {"md_content": "# Also wrong"},
            },
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = response
        mock_resp.raise_for_status = MagicMock()
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=mock_resp),
            _patch_config(),
        ):
            with pytest.raises(RuntimeError, match="missing key"):
                extractor._mineru_run_via_server(dummy_pdf, 0, None)

    def test_empty_md_content_returns_empty(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok(_server_response(md_content=""))),
            _patch_config(),
        ):
            md, _, _ = extractor._mineru_run_via_server(dummy_pdf, 0, None)
            assert md == ""

    def test_json_fields_are_parsed(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        cl_data = [{"type": "text", "text": "parsed"}]
        mj_data = {"pdf_info": [{"page_idx": 0, "para_blocks": [{"type": "equation"}]}]}
        resp = _server_response(content_list=cl_data, middle_json=mj_data)
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok(resp)),
            _patch_config(),
        ):
            md, content_list, pdf_info = extractor._mineru_run_via_server(dummy_pdf, 0, None)
        assert content_list == cl_data
        assert pdf_info == mj_data["pdf_info"]

    def test_url_none_mid_extraction_raises_connect_error_not_unsupported_protocol(
        self, extractor: PDFExtractor, dummy_pdf: Path,
    ) -> None:
        """substantive-critic (T2 critique-index-path-four-beads-2026-09-02):
        this method re-resolves ``get_mineru_server_url()`` FRESH on every
        call, independent of ``_mineru_server_available()``'s once-per-
        instance cache. A skew that appears between the two (another
        process runs `nx mineru restart` onto a differently-versioned
        server mid-extraction) makes THIS call see the ``None`` sentinel
        eti1v introduced -- pre-fix, an unguarded f-string turned that into
        the literal URL "None/file_parse", which httpx refuses with
        UnsupportedProtocol: NOT one of the exception types
        ``_mineru_run_isolated``'s caller catches
        (ConnectError/TimeoutException/RemoteProtocolError/HTTPStatusError),
        so it propagated uncaught instead of triggering the existing
        "server crashed -- invalidate cache, try restart, fall back to
        subprocess" recovery path. Must now raise ``httpx.ConnectError``
        (caught by that existing recovery machinery) and must never reach
        ``httpx.post`` at all."""
        with (
            patch("nexus.pdf_extractor.httpx.post") as mock_post,
            patch("nexus.config.get_mineru_server_url", return_value=None),
            _patch_config(),
        ):
            with pytest.raises(httpx.ConnectError):
                extractor._mineru_run_via_server(dummy_pdf, 0, 5)
        mock_post.assert_not_called()

    @pytest.mark.parametrize("status,message", [
        (409, "Conflict"),
        (503, "Service Unavailable"),
    ], ids=["http_409", "http_503"])
    def test_http_error_raises(self, extractor: PDFExtractor, dummy_pdf: Path, status, message) -> None:
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_error(status, message)),
            _patch_config(),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                extractor._mineru_run_via_server(dummy_pdf, 0, None)

    def test_filename_with_spaces_and_unicode(self, tmp_path: Path) -> None:
        pdf = tmp_path / "my paper (2024) r\u00e9sum\u00e9.pdf"
        pdf.write_bytes(b"dummy")
        resp = _server_response(stem=pdf.stem)
        ext = PDFExtractor()
        with (
            patch("nexus.pdf_extractor.httpx.post", return_value=_mock_post_ok(resp)),
            _patch_config(),
        ):
            md, _, _ = ext._mineru_run_via_server(pdf, 0, None)
        assert md == "# Title\nSome text"


# ── _mineru_run_isolated fallback logic ──────────────────────────────────────


class TestMineruRunIsolatedFallback:

    def test_uses_server_when_available(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        server_result = ("# Server", [], [{"page_idx": 0}])
        with (
            patch.object(extractor, "_mineru_server_available", return_value=True),
            patch.object(extractor, "_mineru_run_via_server", return_value=server_result) as mock_server,
            patch.object(extractor, "_mineru_run_subprocess") as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)
        mock_server.assert_called_once_with(dummy_pdf, 0, None)
        mock_sub.assert_not_called()
        assert result == server_result

    def test_falls_back_to_subprocess_when_unavailable(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        sub_result = ("# Subprocess", [], [])
        with (
            patch.object(extractor, "_mineru_server_available", return_value=False),
            patch.object(extractor, "_mineru_run_via_server") as mock_server,
            patch.object(extractor, "_mineru_run_subprocess", return_value=sub_result) as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)
        mock_server.assert_not_called()
        mock_sub.assert_called_once_with(dummy_pdf, 0, None)
        assert result == sub_result

    def test_falls_back_to_subprocess_on_http_error(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        sub_result = ("# Subprocess fallback", [], [])
        with (
            patch.object(extractor, "_mineru_server_available", return_value=True),
            patch.object(extractor, "_mineru_run_via_server",
                         side_effect=httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())),
            patch.object(extractor, "_mineru_run_subprocess", return_value=sub_result) as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)
        mock_sub.assert_called_once_with(dummy_pdf, 0, None)
        assert result == sub_result

    def test_server_crash_invalidates_cache(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        sub_result = ("# Subprocess", [], [])
        extractor._mineru_server_checked = True
        extractor._mineru_server_up = True

        with (
            patch.object(extractor, "_mineru_run_via_server", side_effect=httpx.ConnectError("refused")),
            patch.object(extractor, "_restart_mineru_server", return_value=False),
            patch.object(extractor, "_mineru_run_subprocess", return_value=sub_result),
        ):
            extractor._mineru_run_isolated(dummy_pdf, 0, None)
        assert extractor._mineru_server_up is False

        with (
            patch.object(extractor, "_mineru_run_via_server") as mock_server,
            patch.object(extractor, "_mineru_run_subprocess", return_value=sub_result),
        ):
            extractor._mineru_run_isolated(dummy_pdf, 1, 2)
        mock_server.assert_not_called()

    def test_server_crash_triggers_restart(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        server_result = ("# Server recovered", [], [])
        extractor._mineru_server_checked = True
        extractor._mineru_server_up = True
        call_count = 0

        def via_server_side_effect(path, start, end):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            return server_result

        with (
            patch.object(extractor, "_mineru_run_via_server", side_effect=via_server_side_effect),
            patch.object(extractor, "_restart_mineru_server", return_value=True),
            patch.object(extractor, "_mineru_run_subprocess") as mock_sub,
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)
        assert result == server_result
        mock_sub.assert_not_called()
        assert extractor._mineru_server_restarts == 0

    def test_restart_budget_exhausted(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        sub_result = ("# Subprocess", [], [])
        extractor._mineru_server_checked = True
        extractor._mineru_server_up = True
        extractor._mineru_server_restarts = 2

        with (
            patch.object(extractor, "_mineru_run_via_server", side_effect=httpx.ConnectError("refused")),
            patch.object(extractor, "_restart_mineru_server", return_value=False) as mock_restart,
            patch.object(extractor, "_mineru_run_subprocess", return_value=sub_result),
        ):
            result = extractor._mineru_run_isolated(dummy_pdf, 0, None)
        assert result == sub_result
        mock_restart.assert_called_once()


# ── Adaptive page ranges + OOM retry ─────────────────────────────────────────


def _mock_pymupdf(page_count: int):
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

    @pytest.mark.parametrize("pages,batch,expected_calls,expected_ranges", [
        (5, 1, 5, [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]),
        (10, 5, 2, [(0, 5), (5, 10)]),
    ], ids=["single_page_batches", "multi_page_batches"])
    def test_batch_sizing(self, extractor: PDFExtractor, dummy_pdf: Path,
                          pages, batch, expected_calls, expected_ranges) -> None:
        isolated_return = ("text", [], [{"page_idx": 0}])
        with (
            _mock_pymupdf(pages), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=batch),
            patch.object(extractor, "_mineru_run_isolated", return_value=isolated_return) as mock_iso,
        ):
            extractor._extract_with_mineru(dummy_pdf)
        assert mock_iso.call_count == expected_calls
        assert [c.args[1:] for c in mock_iso.call_args_list] == expected_ranges

    def test_oom_retry_bisects_to_single_pages(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        # RDR-148 Gap 6 batch//2 ladder: any multi-page range OOMs; only single
        # pages succeed, so the ladder bisects all the way to 1-page
        # granularity and every page is ultimately extracted.
        def mock_isolated(path, start, end):
            rng_end = end if end is not None else 5
            if rng_end - start > 1:
                raise RuntimeError("MinerU subprocess exited with code -9")
            return ("text", [], [])

        with (
            _mock_pymupdf(5), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated", side_effect=mock_isolated) as mock_iso,
        ):
            result = extractor._extract_with_mineru(dummy_pdf)
        single_calls = [
            c for c in mock_iso.call_args_list
            if ((c.args[2] if c.args[2] is not None else 5) - c.args[1]) == 1
        ]
        assert len(single_calls) == 5  # all 5 pages reached at 1-page granularity
        assert result.metadata["extraction_method"] == "mineru"

    def test_oom_retry_bisects_not_straight_to_single(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        # When the halves succeed, the ladder STOPS at batch//2 — it does not
        # drop straight to single pages (that was the pre-Gap-6 behavior).
        def mock_isolated(path, start, end):
            if start == 0 and end is None:  # the full 5-page batch
                raise RuntimeError("MinerU subprocess exited with code -9")
            return ("text", [], [])

        with (
            _mock_pymupdf(5), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated", side_effect=mock_isolated) as mock_iso,
        ):
            extractor._extract_with_mineru(dummy_pdf)
        ranges = [c.args[1:] for c in mock_iso.call_args_list]
        assert ranges == [(0, None), (0, 2), (2, 5)]  # bisected once, halves OK

    def test_oom_retry_single_page_propagates(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        with (
            _mock_pymupdf(3), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=1),
            patch.object(extractor, "_mineru_run_isolated",
                         side_effect=RuntimeError("MinerU subprocess exited with code -9")),
            patch.object(extractor, "_extract_with_docling",
                         side_effect=RuntimeError("Docling fallback also failed")),
        ):
            with pytest.raises(RuntimeError):
                extractor._extract_with_mineru(dummy_pdf)

    def test_oom_retry_structlog_event(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
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
            patch.object(extractor, "_mineru_run_isolated", side_effect=mock_isolated),
            patch("nexus.pdf_extractor._log") as mock_log,
        ):
            extractor._extract_with_mineru(dummy_pdf)
        warning_calls = [c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "mineru_oom_retry"]
        assert len(warning_calls) == 1

    def test_oom_multi_batch_only_retries_failed_range(self, extractor: PDFExtractor, dummy_pdf: Path) -> None:
        # Only the first 5-page batch OOMs; it bisects into (0,2)+(2,5). The
        # healthy (5,10) batch runs exactly once and is never retried.
        def mock_isolated(path, start, end):
            if start == 0 and end == 5:
                raise RuntimeError("OOM")
            return ("text", [], [])

        with (
            _mock_pymupdf(10), _mock_do_parse(),
            patch("nexus.config.get_mineru_page_batch", return_value=5),
            patch.object(extractor, "_mineru_run_isolated", side_effect=mock_isolated) as mock_iso,
        ):
            result = extractor._extract_with_mineru(dummy_pdf)
        ranges = [c.args[1:] for c in mock_iso.call_args_list]
        assert ranges == [(0, 5), (0, 2), (2, 5), (5, 10)]  # failed range bisected only
        assert ranges.count((5, 10)) == 1  # healthy batch never retried
        assert result.metadata["extraction_method"] == "mineru"
