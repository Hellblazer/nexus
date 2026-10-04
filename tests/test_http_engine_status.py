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
    # nexus-grzai: /v1/status sits under the auth-gated /v1 prefix -- the public
    # edge answers 403 to a bare GET and 200 with the resolved token. The
    # resolver hands the token over; the probe must send it.
    assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


def _manager_minting(bearer):
    manager = MagicMock()
    manager.bearer_for.return_value = bearer
    return manager


def test_fetch_engine_status_sends_no_auth_header_without_a_token():
    """No static token AND no mint credential (bearer_for -> None): a plain
    local box. The probe sends no bearer, as before."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"embedding_mode": "onnx-local"}
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", None),
    ), patch(
        "nexus.db.data_token.get_data_token_manager", return_value=_manager_minting(None),
    ), patch("nexus.db.http_engine_status.httpx.get", return_value=resp) as mock_get:
        assert fetch_engine_status() is not None
    assert "Authorization" not in mock_get.call_args.kwargs["headers"]


def test_fetch_engine_status_mints_a_data_token_when_the_resolver_returns_none():
    """nexus-z0o2p.40: a mint-armed cloud box resolves an EMPTY static token
    (nexus-xzeml), and the public edge answers 403 to a bare GET on /v1/status,
    so the doctor's Ownerless writes row read "could not be read" on every
    cloud box. The probe must mint the data token the way the stores and
    tests/e2e/cloud-client-path-gate.sh do, for the resolved base_url and
    the default tenant."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ownerless_write_mode": "log-only"}
    manager = _manager_minting("minted-data-token")
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("https://edge.example", ""),
    ), patch(
        "nexus.db.data_token.get_data_token_manager", return_value=manager,
    ), patch("nexus.db.http_engine_status.httpx.get", return_value=resp) as mock_get:
        status = fetch_engine_status()
    assert status == {"ownerless_write_mode": "log-only"}
    manager.bearer_for.assert_called_once_with("https://edge.example", "default")
    assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer minted-data-token"}


def test_fetch_engine_status_returns_none_when_the_mint_fails():
    """A mint failure is 'unprobeable', never a crash and never a bare GET
    that the edge would 403 anyway."""
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("https://edge.example", ""),
    ), patch(
        "nexus.db.data_token.get_data_token_manager",
        side_effect=RuntimeError("mint round trip failed"),
    ), patch("nexus.db.http_engine_status.httpx.get") as mock_get:
        assert fetch_engine_status() is None
    mock_get.assert_not_called()


def test_fetch_engine_status_does_not_mint_when_a_static_token_resolves():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"embedding_mode": "voyage"}
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("https://edge.example", "static"),
    ), patch("nexus.db.data_token.get_data_token_manager") as mock_mgr, patch(
        "nexus.db.http_engine_status.httpx.get", return_value=resp,
    ) as mock_get:
        assert fetch_engine_status() is not None
    mock_mgr.assert_not_called()
    assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer static"}


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


def test_format_engine_activity_line_cloud_mode_no_activity_at_all():
    """Pre-nexus-s71lr-pass-3 engine: local_embed_activity null AND no
    embedder_activity map (key absent entirely) -- the honest "nothing
    tracked" message, not a KeyError."""
    line = format_engine_activity_line({"embedding_mode": "voyage", "local_embed_activity": None})
    assert "mode=voyage" in line
    assert "no embedder activity reported" in line


def test_format_engine_activity_line_cloud_mode_falls_back_to_embedder_activity():
    """Pass 3 (T2 [24547]): the majority-posture fix -- cloud mode reports
    real activity via embedder_activity when local_embed_activity is null.

    Neutral model token on purpose (RDR-109 mode lint): "model-code" is
    a LABEL in the status dict this pure formatter renders; no real
    embedder call is made.
    """
    status = {
        "embedding_mode": "voyage",
        "local_embed_activity": None,
        "embedder_activity": {
            "model-code": {
                "active": True, "chunks_done_total": 64, "sub_batches_total": 4,
                "last_chunks_per_sec": 3.2, "last_activity_age_ms": 150,
                "queue_depth": -1, "thread_width": -1,
            },
        },
    }
    line = format_engine_activity_line(status)
    assert "mode=voyage" in line
    assert "embedder=model-code" in line
    assert "active" in line
    assert "chunks_done=64" in line
    assert "rate=3.2/s" in line
    assert "last_activity=150ms ago" in line
    # No LocalOnnxAdmission-equivalent for cloud -- -1 sentinels stay omitted.
    assert "queue_depth=" not in line
    assert "thread_width=" not in line


def test_format_engine_activity_line_picks_the_busiest_embedder_and_names_the_rest():
    """Neutral model tokens on purpose (RDR-109 mode lint): same reason
    as the sibling test above -- pure display LABELS, no embedder call."""
    status = {
        "embedding_mode": "voyage",
        "local_embed_activity": None,
        "embedder_activity": {
            "model-code": {
                "active": False, "chunks_done_total": 10, "sub_batches_total": 1,
                "last_chunks_per_sec": 1.0, "last_activity_age_ms": 9000,
                "queue_depth": -1, "thread_width": -1,
            },
            "model-ctx": {
                "active": True, "chunks_done_total": 90, "sub_batches_total": 9,
                "last_chunks_per_sec": 9.0, "last_activity_age_ms": 50,
                "queue_depth": -1, "thread_width": -1,
            },
        },
    }
    line = format_engine_activity_line(status)
    # The fresher (50ms ago) entry wins over the stale (9000ms ago) one.
    assert "embedder=model-ctx" in line
    assert "chunks_done=90" in line
    assert "(+1 other embedder(s) tracked)" in line


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


def test_format_engine_activity_line_local_mode_prefers_local_embed_activity_over_map():
    """local_embed_activity, when non-null, always wins -- the
    embedder_activity map is a fallback for when it is null, never
    consulted when it isn't (even if the map ALSO carries a bge768 entry
    for uniformity, per StatusHandler's own docstring)."""
    status = {
        "embedding_mode": "onnx-local",
        "local_embed_activity": {
            "active": True, "chunks_done_total": 5, "sub_batches_total": 1,
            "last_chunks_per_sec": 1.0, "last_activity_age_ms": 10,
            "queue_depth": 0, "thread_width": 4,
        },
        "embedder_activity": {
            "bge-base-en-v15-768": {
                "active": True, "chunks_done_total": 999, "sub_batches_total": 99,
                "last_chunks_per_sec": 99.0, "last_activity_age_ms": 1,
                "queue_depth": 0, "thread_width": 4,
            },
        },
    }
    line = format_engine_activity_line(status)
    assert "chunks_done=5" in line
    assert "chunks_done=999" not in line
    assert "embedder=" not in line


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
