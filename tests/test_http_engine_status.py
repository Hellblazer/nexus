# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-s71lr, deliverable 2/3 — client for GET /v1/status."""
from unittest.mock import MagicMock, patch

import httpx

from nexus.db.http_engine_status import fetch_engine_status, format_engine_activity_line


def test_fetch_engine_status_returns_none_when_endpoint_unresolvable():
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        side_effect=RuntimeError("no lease"),
    ):
        assert fetch_engine_status() is None


def test_fetch_engine_status_returns_none_on_transport_error():
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", "tok"),
    ), patch("nexus.db.http_engine_status.httpx.get", side_effect=httpx.ConnectError("refused")):
        assert fetch_engine_status() is None


def test_fetch_engine_status_returns_none_on_404_pre_s71lr_engine():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404),
    )
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", "tok"),
    ), patch("nexus.db.http_engine_status.httpx.get", return_value=resp):
        assert fetch_engine_status() is None


def test_fetch_engine_status_returns_parsed_body_on_success():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "embedding_mode": "onnx-local",
        "local_embed_activity": {
            "active": True, "chunks_done_total": 128, "sub_batches_total": 8,
            "last_chunks_per_sec": 7.7, "last_activity_age_ms": 230,
            "queue_depth": 0, "thread_width": 4,
        },
    }
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", "tok"),
    ), patch("nexus.db.http_engine_status.httpx.get", return_value=resp) as mock_get:
        status = fetch_engine_status(timeout=3.0)
    assert status["embedding_mode"] == "onnx-local"
    assert status["local_embed_activity"]["chunks_done_total"] == 128
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "http://127.0.0.1:1/v1/status"
    assert mock_get.call_args.kwargs["timeout"] == 3.0


def test_fetch_engine_status_returns_none_on_non_dict_body():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = ["not", "a", "dict"]
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", "tok"),
    ), patch("nexus.db.http_engine_status.httpx.get", return_value=resp):
        assert fetch_engine_status() is None


# ── format_engine_activity_line ─────────────────────────────────────────────


def test_format_engine_activity_line_unknown_when_status_none():
    line = format_engine_activity_line(None)
    assert line.startswith("Engine activity: UNKNOWN")


def test_format_engine_activity_line_cloud_mode_no_local_activity():
    line = format_engine_activity_line({"embedding_mode": "voyage", "local_embed_activity": None})
    assert "mode=voyage" in line
    assert "not tracked" in line


def test_format_engine_activity_line_local_mode_active():
    status = {
        "embedding_mode": "onnx-local",
        "local_embed_activity": {
            "active": True, "chunks_done_total": 128, "sub_batches_total": 8,
            "last_chunks_per_sec": 7.7, "last_activity_age_ms": 230,
            "queue_depth": 0, "thread_width": 4,
        },
    }
    line = format_engine_activity_line(status)
    assert "mode=onnx-local" in line
    assert "active" in line
    assert "chunks_done=128" in line
    assert "rate=7.7/s" in line
    assert "last_activity=230ms ago" in line
    assert "queue_depth=0" in line
    assert "thread_width=4" in line


def test_format_engine_activity_line_local_mode_idle_omits_negative_sentinels():
    status = {
        "embedding_mode": "onnx-local",
        "local_embed_activity": {
            "active": False, "chunks_done_total": 0, "sub_batches_total": 0,
            "last_chunks_per_sec": 0.0, "last_activity_age_ms": -1,
            "queue_depth": -1, "thread_width": -1,
        },
    }
    line = format_engine_activity_line(status)
    assert "idle" in line
    assert "queue_depth=" not in line
    assert "thread_width=" not in line
    assert "last_activity=" not in line
