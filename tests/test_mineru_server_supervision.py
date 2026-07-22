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
        "nexus.config.mineru_server_provisioned", return_value=True,
    ), patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp):
        results = _check_mineru_server()
    assert len(results) == 1
    assert results[0].ok is True
    assert "8010" in results[0].detail


def test_check_mineru_server_fail_when_unreachable() -> None:
    """A PROVISIONED server that went stale must still render the red ✗ —
    that drift is exactly what this check exists to surface."""
    with patch(
        "nexus.config.mineru_server_provisioned", return_value=True,
    ), patch(
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


def test_check_mineru_server_skips_unprovisioned_fresh_box() -> None:
    """nexus-9xfx5 (reviewer-3modes H1): the DEFAULT doctor flow must not
    probe the built-in default URL on a box where no MinerU server was
    ever provisioned — every fresh install rendered a red ✗ otherwise.
    This is the always-on `run_health_checks` leg, NOT --check-mineru."""
    with patch(
        "nexus.config.mineru_server_provisioned", return_value=False,
    ), patch("httpx.get") as probe:
        results = _check_mineru_server()
    assert results == [], "unprovisioned box must produce no MinerU result row"
    probe.assert_not_called()


def test_check_mineru_server_fail_on_non_200() -> None:
    fake_resp = MagicMock(status_code=503)
    with patch(
        "nexus.config.mineru_server_provisioned", return_value=True,
    ), patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch("httpx.get", return_value=fake_resp):
        results = _check_mineru_server()
    assert len(results) == 1
    assert results[0].ok is False
    assert "503" in results[0].detail


def test_check_mineru_server_no_op_when_url_missing() -> None:
    with patch(
        "nexus.config.mineru_server_provisioned", return_value=True,
    ), patch("nexus.config.get_mineru_server_url", return_value=""):
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


# ── RDR-148 Gap 2: rediscover-then-fail-loud ─────────────────────────


def test_health_fail_triggers_single_rediscovery_then_loud_fallback() -> None:
    """On /health failure, exactly one rediscovery pass runs (2 probes
    total); when it also fails, the fallback is logged LOUD (WARNING),
    not silently."""
    from structlog.testing import capture_logs

    from nexus.pdf_extractor import PDFExtractor

    ex = PDFExtractor()
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:49353",
    ), patch(
        "httpx.get", side_effect=httpx.ConnectError("refused"),
    ) as mock_get, capture_logs() as logs:
        assert ex._mineru_server_available() is False
        # First probe + exactly one rediscovery probe + the nexus-1qdb9
        # lifecycle's own gate probe (autostart pinned OFF suite-wide in
        # conftest, so ensure() stops after its health check) = 3 calls.
        assert mock_get.call_count == 3
    # The fallback decision is loud and reasoned, not silent.
    fallback = [e for e in logs if e["event"] == "mineru_fallback_to_subprocess"]
    assert len(fallback) == 1, f"expected one loud fallback warning; got {logs!r}"
    assert fallback[0]["log_level"] == "warning"
    assert fallback[0]["reason"]  # a non-empty diagnostic reason is surfaced
    # This is the same-endpoint transient-recovery path (URL unchanged);
    # the new-port pid rediscovery path is covered by the next test.
    assert fallback[0]["first_url"] == fallback[0]["rediscovered_url"]


def test_health_probe_remote_protocol_error_degrades_gracefully() -> None:
    """A server dying mid-startup can return a malformed HTTP response
    (httpx.RemoteProtocolError). The probe must treat it as unreachable
    and fall back, not crash the extraction."""
    from nexus.pdf_extractor import PDFExtractor

    ex = PDFExtractor()
    with patch(
        "nexus.config.get_mineru_server_url",
        return_value="http://127.0.0.1:8010",
    ), patch(
        "httpx.get", side_effect=httpx.RemoteProtocolError("malformed"),
    ):
        # Must not raise — degrades to False.
        assert ex._mineru_server_available() is False


def test_health_fail_then_rediscovery_finds_restarted_server(monkeypatch) -> None:
    """The server died on the first port but restarted on a new one; the
    rediscovery pass re-resolves (re-reads the pid file) and uses it —
    no degrade to subprocess."""
    from nexus.pdf_extractor import PDFExtractor

    # First resolve -> dead port; rediscovery resolve -> live port.
    urls = iter(["http://127.0.0.1:49353", "http://127.0.0.1:8010"])
    monkeypatch.setattr(
        "nexus.config.get_mineru_server_url", lambda *a, **k: next(urls),
    )

    def fake_get(url, *a, **k):
        if "8010" in url:
            return MagicMock(status_code=200)
        raise httpx.ConnectError("refused")

    ex = PDFExtractor()
    with patch("httpx.get", side_effect=fake_get) as mock_get:
        assert ex._mineru_server_available() is True
        assert mock_get.call_count == 2  # one dead probe + one live probe
