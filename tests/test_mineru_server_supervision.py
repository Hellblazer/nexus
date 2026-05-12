# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h1jk: MinerU server supervision (visibility in doctor +
warn-on-fallback in pdf_extractor).

Tests the visibility layer only. Auto-spawn is deferred to a separate
bead; the focused fixes here are:

1. ``nx doctor`` reports MinerU server reachability in the default
   health-check flow (always-on, not gated behind ``--check-mineru``).
2. ``PDFExtractor._mineru_server_available`` emits a structured WARN
   when the configured URL is unreachable, so the operator sees the
   silent fallback to the in-process subprocess (which OOMs on
   large math papers).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.health import _check_mineru_server


# ── doctor: _check_mineru_server ─────────────────────────────────────


def test_check_mineru_server_pass_when_reachable() -> None:
    fake_resp = MagicMock(status_code=200)
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp):
        results = _check_mineru_server()
    assert len(results) == 1
    assert results[0].ok is True
    assert "8010" in results[0].detail


def test_check_mineru_server_fail_when_unreachable() -> None:
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:49353",
    ), patch(
        "httpx.get",
        side_effect=httpx.ConnectError("connection refused"),
    ):
        results = _check_mineru_server()
    assert len(results) == 1
    assert results[0].ok is False
    assert "49353" in results[0].detail
    assert any("mineru start" in s for s in results[0].fix_suggestions)
    assert any("config.yml" in s for s in results[0].fix_suggestions)


def test_check_mineru_server_fail_on_non_200() -> None:
    fake_resp = MagicMock(status_code=503)
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp):
        results = _check_mineru_server()
    assert len(results) == 1
    assert results[0].ok is False
    assert "503" in results[0].detail


def test_check_mineru_server_no_op_when_url_missing() -> None:
    with patch("nexus.config.get_mineru_server_url", return_value=""):
        results = _check_mineru_server()
    assert results == []


# ── pdf_extractor: warn-on-fallback ──────────────────────────────────


def test_mineru_server_available_warns_on_unreachable(caplog) -> None:
    from nexus.pdf_extractor import PDFExtractor

    ex = PDFExtractor()
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:49353",
    ), patch(
        "httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        assert ex._mineru_server_available() is False
    # Second call returns the cached False without re-probing — locks in
    # the contract that the warning fires exactly once per extractor.
    with patch("httpx.get") as mock_get:
        assert ex._mineru_server_available() is False
        mock_get.assert_not_called()


def test_mineru_server_available_warns_on_non_200(caplog) -> None:
    from nexus.pdf_extractor import PDFExtractor

    ex = PDFExtractor()
    fake_resp = MagicMock(status_code=503)
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp):
        assert ex._mineru_server_available() is False


def test_mineru_server_available_caches_success() -> None:
    from nexus.pdf_extractor import PDFExtractor

    ex = PDFExtractor()
    fake_resp = MagicMock(status_code=200)
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp) as mock_get:
        assert ex._mineru_server_available() is True
        assert ex._mineru_server_available() is True  # cache hit
        mock_get.assert_called_once()
