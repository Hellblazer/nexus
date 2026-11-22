# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): the shared vector double never sends vectors to the engine.

The double (``make_vector_test_client``) embeds with MiniLM whatever model its collection names carry, and
``double_topology_mints_profile_model`` names them for the tenant's bge profile. That label is harmless only while
the vectors stay in the process, so the suite's autouse guard fails a test that built a double and then put
``embeddings`` on a ``/v1/vectors`` request. Needs no engine.
"""
from __future__ import annotations

import pytest

from tests import conftest as suite


_PATH = "/v1/vectors/search"  # the guard watches the whole /v1/vectors prefix; a route that is not an ownerless write route


class _NoEngine(Exception):
    """Raised by the stubbed transport: nothing here may reach storage."""


def _send(body: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.db.http_vector_client as hvc

    def _refuse(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise _NoEngine

    # The suite's wrapper has already looked at the request when ``_request`` reaches its transport, so stubbing
    # the transport keeps the request off storage (no tenant, no engine) without hiding what the guard saw.
    monkeypatch.setattr(hvc, "_request_once", _refuse)
    with pytest.raises(_NoEngine):
        hvc._request("POST", _PATH, tenant="t", timeout=1, body=body)


def test_vectors_on_the_engine_wire_after_a_double_was_built_are_recorded(monkeypatch):
    suite.make_vector_test_client()

    _send({"collection": "c", "ids": ["a"], "documents": ["d"], "embeddings": [[0.1, 0.2]]}, monkeypatch)

    assert suite._DOUBLE_WIRE_STATE["leaks"] == [_PATH]
    suite._DOUBLE_WIRE_STATE["leaks"] = []  # the guard's teardown would otherwise fail this test, as designed


def test_a_request_without_vectors_is_not_a_leak(monkeypatch):
    suite.make_vector_test_client()

    _send({"collection": "c", "ids": ["a"], "documents": ["d"]}, monkeypatch)

    assert suite._DOUBLE_WIRE_STATE["leaks"] == []


def test_a_test_that_built_no_double_is_not_watched(monkeypatch):
    _send({"collection": "c", "ids": ["a"], "documents": ["d"], "embeddings": [[0.1, 0.2]]}, monkeypatch)

    assert suite._DOUBLE_WIRE_STATE["leaks"] == []
