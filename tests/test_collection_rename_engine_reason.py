# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-02454: ``nx collection rename`` shows the engine's reason for a refusal, not only the status line.

The engine answers a refused rename with ``{"error": <message>}`` (a 400 for the label rule and other bad input, a
409 for a conflict). ``str(HTTPStatusError)`` is only httpx's status line, so before this fix a label the engine
refused read as ``400 Bad Request`` with no reason. The 409 ``collection_model_mismatch`` keeps its own text
(nexus-ucagi, tests/test_ucagi_rename_embedding_model_wire.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import httpx
import pytest

from nexus.collection_rename import rename_collection_data_plane

_NEW = "docs__1-1__voyage-context-3__v1"


def _t3() -> MagicMock:
    t3 = MagicMock()
    t3.collection_exists = MagicMock(side_effect=lambda n: n == "docs__old")
    return t3


def _refusal(status: int, content: bytes) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://engine/v1/catalog/collections/rename")
    resp = httpx.Response(status, content=content, request=req)
    return httpx.HTTPStatusError(f"Client error '{status}' for url '{req.url}'", request=req, response=resp)


def _message(exc: Exception) -> str:
    client = MagicMock()
    client.rename_collection_cascade = MagicMock(side_effect=exc)
    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane("docs__old", _NEW, t3_db=_t3(), catalog=client)
    return str(ei.value.message)


@pytest.mark.parametrize("status", [400, 409, 422])
def test_an_engine_refusal_shows_the_engines_reason_and_keeps_the_status(status: int) -> None:
    reason = "collection label 'notes\\u2028x' holds a control character (U+2028)"
    msg = _message(_refusal(status, ('{"error":"' + reason + '"}').encode()))
    assert "holds a control character (U+2028)" in msg, msg
    assert str(status) in msg, msg
    assert "unchanged" in msg


@pytest.mark.parametrize("content", [b"", b"not json", b"[1, 2]", b'{"reason":"x"}', b'{"error":""}'])
def test_a_body_with_no_reason_keeps_the_status_line_alone(content: bytes) -> None:
    msg = _message(_refusal(400, content))
    assert "400" in msg and "unchanged" in msg


def test_a_server_error_body_is_not_shown_as_a_reason() -> None:
    """A 5xx body is the engine's own failure, which the server log carries; the client keeps the status line."""
    msg = _message(_refusal(500, b'{"error":"java.lang.NullPointerException at ..."}'))
    assert "NullPointerException" not in msg
    assert "500" in msg
