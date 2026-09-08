# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8ooxn / nexus-hcy4w: a service error body that echoes the rejected
credential is redacted before it reaches a structlog line. The doctor's
printed output already omitted it; the log line did not, and under xdist
the worker's console renderer landed that line in the CliRunner output.
"""
from __future__ import annotations

from nexus.health import redact_credentials


def test_api_key_value_is_redacted() -> None:
    assert redact_credentials("HTTP 401: invalid api_key SUPERSECRET") == "HTTP 401: invalid api_key [redacted]"
    assert "SUPERSECRET" not in redact_credentials("token=SUPERSECRET; retry")
    assert redact_credentials("Bearer abc.def.ghi expired") == "Bearer [redacted] expired"


def test_text_without_credentials_is_unchanged() -> None:
    msg = "connection refused: 127.0.0.1:26901"
    assert redact_credentials(msg) == msg


def test_prose_that_merely_uses_a_key_word_is_unchanged() -> None:
    """Review of 4c092e40a: the first regex swallowed the word after any key
    word, so the managed-service remedy ("invalid token\\nthe managed nexus
    service ...") lost its own next word. Prose survives; values do not."""
    remedy = "HTTP 401: invalid token\nthe managed nexus service refused the request"
    assert redact_credentials(remedy) == remedy
    assert redact_credentials("token endpoint returned 404 for /oauth/token") == (
        "token endpoint returned 404 for /oauth/token"
    )
    assert redact_credentials("secret sauce") == "secret sauce"


def test_client_error_message_is_redacted_at_the_source(monkeypatch) -> None:
    """Critique of 4c092e40a: the credential entered VectorServiceError's
    message in http_vector_client, so every renderer of that message (the
    doctor's log line, ``nx search``, ``nx doc``) echoed it. Redaction now
    happens where the body enters the message."""
    import io
    import urllib.error

    import pytest

    import nexus.db.http_vector_client as hv

    body = b'{"error":"invalid api_key sk-live-ABCDEF1234567890","detail":"token=sk-live-ABCDEF1234567890 rejected"}'

    def _raise(*a, **k):
        raise urllib.error.HTTPError(url="http://svc/v1/x", code=401, msg="err", hdrs={}, fp=io.BytesIO(body))

    monkeypatch.setattr(hv, "_request", _raise)
    monkeypatch.setattr(hv, "_managed_remedy", lambda: None)
    monkeypatch.setattr(hv, "_local_voyage_restart_remedy", lambda code, text: None)
    with pytest.raises(hv.VectorServiceError) as excinfo:
        hv._post("/v1/vectors/query", {"q": 1})
    assert "sk-live" not in str(excinfo.value), str(excinfo.value)
    assert "api_key [redacted]" in str(excinfo.value)
    with pytest.raises(hv.VectorServiceError) as excinfo:
        hv._get("/v1/vectors/collections")
    assert "sk-live" not in str(excinfo.value), str(excinfo.value)
