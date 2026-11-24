# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 R7 (engine 0.1.202+): a request by label that names a collection retired with no live successor.

The engine answers the by-id body for that row: ``{error, reason: "collection_superseded", collection, collection_id}``
with NO ``superseded_by`` and NO ``successor_collection_id`` (a retired collection whose successor was deleted names
none). Read routes by label used to serve the tombstone with 200. ``nx collection re-embed`` is separate: its plan and
start answer a plain 409 ``{error: "collection X was retired and has no successor; ...", collection}`` with no reason.
The client turns each into one message (the collection, why, what to do), keyed on the body and never on the status
alone, never retries, and shows no traceback. Other 409s, and the with-successor ``collection_superseded``, are unchanged.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.collection_errors import RetiredNoSuccessorError, SupersededCollectionWriteError
from nexus.commands.collection import _reembed_job_error
from nexus.db import http_vector_client as hv
from nexus.db.http_vector_client import VectorServiceError
from tests._cli_tree import missing_commands, named_commands

NAME = "docs__papers__bge-base-en-v15-768__v1"
RETIRED = {"error": "collection_superseded", "reason": "collection_superseded", "collection": NAME, "collection_id": 41,
           "message": f"collection_id 41 ('{NAME}') is retired and names no live successor, so a request that names it is refused"}
WITH_SUCCESSOR = {**RETIRED, "superseded_by": "docs__papers__bge-base-en-v15-768__v2", "successor_collection_id": 42}


def _assert_the_one_message(text: str, name: str = NAME) -> None:
    out = " ".join(text.split())
    assert f"collection '{name}' was retired and has no successor" in out, out
    assert "nothing to read or write under that label" in out, out
    assert "`nx collection list`" in out and "index their files into a live collection" in out, out
    assert "Nothing was changed" in out and "Traceback" not in text and "HTTPStatusError" not in text, out
    assert "retry" not in out.lower()
    assert named_commands(out) and missing_commands(out) == [], out


def _urllib_409(body: dict):
    def raiser(*_a, **_k):
        raise urllib.error.HTTPError("http://engine/x", 409, "conflict", {}, io.BytesIO(json.dumps(body).encode()))
    return raiser


@pytest.fixture(autouse=True)
def _no_prod_write_guard():
    with patch("nexus.db.service_endpoint.guard_production_write"):
        yield


# ── the vector client (search, query, get, store get/list, taxonomy, aspects ride _post and _get) ──


@pytest.mark.parametrize("call", ["post", "get"], ids=["POST search/get/list routes", "GET routes"])
def test_the_vector_client_turns_the_retired_no_successor_409_into_the_typed_error_once(call) -> None:
    fn = hv._post if call == "post" else hv._get
    args = ({"collection": NAME},) if call == "post" else ()
    with patch.object(hv, "_resolve_endpoint", return_value=("http://engine", None)), \
         patch.object(hv, "_request", side_effect=_urllib_409(RETIRED)) as request, \
         pytest.raises(RetiredNoSuccessorError) as raised:
        fn("/v1/vectors/search", *args)
    assert raised.value.name == NAME and raised.value.collection_id == 41
    assert request.call_count == 1, "never resent"
    _assert_the_one_message(str(raised.value))


def test_a_collection_superseded_409_with_a_successor_is_still_the_write_error() -> None:
    with patch.object(hv, "_resolve_endpoint", return_value=("http://engine", None)), \
         patch.object(hv, "_request", side_effect=_urllib_409(WITH_SUCCESSOR)), \
         pytest.raises(SupersededCollectionWriteError) as raised:
        hv._post("/v1/vectors/upsert-chunks", {"collection": NAME})
    assert raised.value.successor == "docs__papers__bge-base-en-v15-768__v2"


@pytest.mark.parametrize("body", [
    {"error": "collection 'x' is held by 2 live rows", "reason": "collection_label_ambiguous", "collection": "x"},
    {"error": "the label 'x' is reserved", "reason": "label_reserved", "label": "x"},
    {"error": "re-embed job is running", "collection": NAME, "job_id": "j1", "state": "running"},
    {"error": "collection_superseded", "reason": "collection_superseded", "collection_id": 41},        # names no collection
    {"error": "collection_superseded", "reason": "collection_superseded", "collection": ""},
    {**RETIRED, "superseded_by": "docs__x"},                                                            # names a successor
    {**RETIRED, "successor_collection_id": 5},
    {"error": "collection_superseded", "collection": NAME, "collection_id": 41},                       # no reason
])
def test_every_other_409_is_left_to_its_own_handling(body) -> None:
    with patch.object(hv, "_resolve_endpoint", return_value=("http://engine", None)), \
         patch.object(hv, "_request", side_effect=_urllib_409(body)):
        with pytest.raises(Exception) as raised:
            hv._post("/v1/vectors/search", {"collection": NAME})
    assert not isinstance(raised.value, RetiredNoSuccessorError), body
    if "reason" in body and body["reason"] == "collection_label_ambiguous":
        assert isinstance(raised.value, VectorServiceError) and raised.value.code == 409


def test_a_409_that_is_not_409_is_not_taken_for_it() -> None:
    def err(*_a, **_k):
        raise urllib.error.HTTPError("http://engine/x", 500, "boom", {}, io.BytesIO(json.dumps(RETIRED).encode()))

    with patch.object(hv, "_resolve_endpoint", return_value=("http://engine", None)), \
         patch.object(hv, "_request", side_effect=err), pytest.raises(VectorServiceError) as raised:
        hv._post("/v1/vectors/search", {"collection": NAME})
    assert raised.value.code == 500


# ── the shared httpx transport (catalog and the T2 stores) ──


def _catalog(body: dict, status: int = 409) -> HttpCatalogClient:
    sent: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(status, json=body)

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handle))
    cat.sent = sent  # type: ignore[attr-defined]
    return cat


def test_the_shared_transport_turns_the_retired_no_successor_409_into_the_typed_error_once() -> None:
    cat = _catalog(RETIRED)
    with pytest.raises(RetiredNoSuccessorError) as raised:
        cat.list_by_collection(NAME)
    _assert_the_one_message(str(raised.value))
    assert len(cat.sent) == 1, "one request, never resent"  # type: ignore[attr-defined]


def test_the_shared_transport_keeps_the_with_successor_and_other_409s() -> None:
    with pytest.raises(SupersededCollectionWriteError):
        _catalog(WITH_SUCCESSOR).list_by_collection(NAME)
    with pytest.raises(httpx.HTTPStatusError):
        _catalog({"error": "x", "reason": "collection_label_ambiguous", "collection": NAME}).list_by_collection(NAME)
    with pytest.raises(httpx.HTTPStatusError):
        _catalog(RETIRED, status=422).list_by_collection(NAME)


# ── the command line boundary ──


def test_the_cli_boundary_renders_it_as_one_message_exit_1_no_traceback() -> None:
    @click.command("zz-read")
    def zz_read() -> None:
        raise RetiredNoSuccessorError(NAME, 41)

    main.add_command(zz_read)
    try:
        result = CliRunner().invoke(main, ["zz-read"])
    finally:
        main.commands.pop("zz-read", None)
    assert result.exit_code == 1, result.output
    _assert_the_one_message(result.output)
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ── nx collection re-embed: a plain 409 with no reason, matched by its text ──


@pytest.mark.parametrize("suffix", ["; a retired collection is not re-embedded", "; it cannot be this job's target"])
def test_a_reembed_refusal_of_a_retired_collection_is_the_one_message(suffix) -> None:
    body = {"error": f"collection {NAME} was retired and has no successor{suffix}", "collection": NAME}
    err = _reembed_job_error(VectorServiceError("HTTP 409", code=409, engine_body=body))
    _assert_the_one_message(err.message)


@pytest.mark.parametrize("code,body", [
    (409, {"error": f"source collection {NAME} is already superseded by docs__y; a superseded collection is not re-embedded",
           "collection": NAME, "superseded_by": "docs__y"}),
    (409, {"error": f"collection {NAME} was retired and has no successor; x", "reason": "something_else"}),
    (409, {"error": f"collection {NAME} was retired and has no successor; x", "job_id": "j1", "state": "done"}),
    (422, {"error": f"collection {NAME} was retired and has no successor; x", "collection": NAME}),
    (409, {"error": "the tenant has no embedding profile for content type docs"}),
])
def test_other_reembed_refusals_keep_their_own_text(code, body) -> None:
    err = _reembed_job_error(VectorServiceError("HTTP", code=code, engine_body=body))
    assert "nothing to read or write under that label" not in err.message
