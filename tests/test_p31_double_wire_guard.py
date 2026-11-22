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


def _send(body: dict) -> None:
    import nexus.db.http_vector_client as hvc

    with pytest.raises(Exception):  # noqa: B017 — there is no engine here; the request fails after the guard has looked
        hvc._request("POST", "/v1/vectors/upsert-chunks", tenant="t", timeout=1, body=body)


def test_vectors_on_the_engine_wire_after_a_double_was_built_are_recorded():
    suite.make_vector_test_client()

    _send({"collection": "c", "ids": ["a"], "documents": ["d"], "embeddings": [[0.1, 0.2]]})

    assert suite._DOUBLE_WIRE_STATE["leaks"] == ["/v1/vectors/upsert-chunks"]
    suite._DOUBLE_WIRE_STATE["leaks"] = []  # the guard's teardown would otherwise fail this test, as designed


def test_a_request_without_vectors_is_not_a_leak():
    suite.make_vector_test_client()

    _send({"collection": "c", "ids": ["a"], "documents": ["d"]})

    assert suite._DOUBLE_WIRE_STATE["leaks"] == []


def test_a_test_that_built_no_double_is_not_watched():
    _send({"collection": "c", "ids": ["a"], "documents": ["d"], "embeddings": [[0.1, 0.2]]})

    assert suite._DOUBLE_WIRE_STATE["leaks"] == []
