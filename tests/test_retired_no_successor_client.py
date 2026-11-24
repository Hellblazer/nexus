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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.catalog_spans import resolve_span_text_for_entry
from nexus.catalog.rows.resolve import Unreadable, resolve_collection
from nexus.cli import main
from nexus.collection_errors import (
    RetiredNoSuccessorError,
    SupersededCollectionWriteError,
    retired_no_successor_error_from_body,
)
from nexus.commands.catalog import _span_text_or_marker
from nexus.commands.collection import _reembed_job_error
from nexus.db import http_vector_client as hv
from nexus.db.gateway_backoff import _GATEWAY_RETRY_CODES
from nexus.db.http_vector_client import VectorServiceError
from nexus.errors import SearchEmbeddingProfileMismatchError
from nexus.search_engine import SearchDiagnostics, _is_permanent_poisoning_error, search_cross_corpus
from tests._cli_tree import missing_commands, named_commands
from tests.conftest import catalog_row_for_collection_name

NAME = "docs__papers__bge-base-en-v15-768__v1"
RETIRED = {"error": "collection_superseded", "reason": "collection_superseded", "collection": NAME, "collection_id": 41,
           "message": f"collection_id 41 ('{NAME}') is retired and names no live successor, so a request that names it is refused"}
WITH_SUCCESSOR = {**RETIRED, "superseded_by": "docs__papers__bge-base-en-v15-768__v2", "successor_collection_id": 42}


def _assert_the_one_message(text: str, name: str = NAME) -> None:
    out = " ".join(text.split())
    assert f"collection '{name}' was retired and has no successor" in out, out
    assert "nothing to read or write under that label" in out, out
    assert "`nx collection list`" in out and "index their files into a live collection" in out, out
    assert "`nx index repo <path>`" not in out and "`nx index repo` is refused while" in out, "repo indexing is not the way out here"
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
    # what the generic 409 VectorServiceError was before the type existed, so every handler written for it recovers
    assert isinstance(raised.value, VectorServiceError)
    assert raised.value.code == 409 and raised.value.reason == "collection_superseded"
    assert raised.value.engine_body == RETIRED and raised.value.edge_refusal is False


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
    if body.get("superseded_by") and body.get("reason") == "collection_superseded":
        assert isinstance(raised.value, SupersededCollectionWriteError), body  # the with-successor path
    else:
        assert type(raised.value) is VectorServiceError and raised.value.code == 409, (body, raised.value)  # the generic handler


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
    # still the httpx status error this 409 was (a store that recovers from it by catching that keeps recovering), with
    # its response, and a VectorServiceError for the handlers written for the vector client's 409
    assert isinstance(raised.value, httpx.HTTPStatusError) and raised.value.response.status_code == 409
    assert isinstance(raised.value, VectorServiceError) and raised.value.code == 409 and raised.value.engine_body == RETIRED


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


# ── (a) a retired name in a multi-collection search is one failed collection, not a failed search ──


class _BatchingT3:
    """A T3 stand-in whose combined search is refused whole when it names a retired collection (the engine's 409)."""

    _voyage_client = "fake-voyage"

    def __init__(self, rows: dict[str, list[dict]], retired: set[str]) -> None:
        self._rows, self._retired = rows, retired
        self.calls: list[list[str]] = []

    def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include"):
        self.calls.append(list(collection_names))
        for name in collection_names:
            if name in self._retired:
                raise RetiredNoSuccessorError(name, 41)
        return [{**r, "collection": name} for name in collection_names for r in self._rows.get(name, [])]

    def _resolve_collection_rows(self, names):
            return {n: {"content_type": catalog_row_for_collection_name(n)["content_type"]} for n in names}


def test_a_search_over_a_live_and_a_retired_collection_returns_the_live_hits_and_names_the_retired_one_failed() -> None:
    live, dead = "code__nexus", "knowledge__gone__bge-base-en-v15-768__v1"
    t3 = _BatchingT3({live: [{"id": "a", "content": "hit", "distance": 0.3}]}, retired={dead})
    diags: list[SearchDiagnostics] = []
    results = search_cross_corpus("q", [live, dead], 10, t3, diagnostics_out=diags, sensitivity="include")

    assert {r.id for r in results} == {"a"}, "the live collection's hits still return"
    assert list(diags[0].failed_collections) == [dead]
    message = diags[0].failed_collections[dead]
    assert "was retired and has no successor" in message
    assert "dim" not in message.lower(), "the failure text must not read as a dimension mismatch"


def test_a_search_over_only_a_retired_collection_fails_loud_naming_it() -> None:
    dead = "knowledge__gone__bge-base-en-v15-768__v1"
    with pytest.raises(Exception) as raised:
        search_cross_corpus("q", [dead], 10, _BatchingT3({}, retired={dead}), sensitivity="include")
    assert dead in str(raised.value) and "failed" in str(raised.value).lower()


# ── (b) a span in a retired collection is its own unresolvable reason, never "span not found" ──


def test_a_span_in_a_retired_collection_raises_it_and_nx_catalog_show_prints_its_own_marker() -> None:
    entry = SimpleNamespace(physical_collection=NAME, file_path="", meta={"doc_id": "d1"}, tumbler="1.2.3")

    class _Retired:
        def get_or_create_collection(self, name):
            raise RetiredNoSuccessorError(name, 41)

        get_collection = get_or_create_collection

    with patch("nexus.db.make_t3", lambda: _Retired()), pytest.raises(RetiredNoSuccessorError):
        resolve_span_text_for_entry(entry, "3:10-40", catalog=None)


def test_catalog_show_preview_prints_the_retired_marker_the_degraded_one_and_the_text(capsys) -> None:
    cat = MagicMock()
    cat.resolve_span_text.side_effect = RetiredNoSuccessorError(NAME, 41)
    assert _span_text_or_marker(cat, "1.1.2", "3:10-40") is None
    out = capsys.readouterr().out
    assert f"[span unavailable — collection {NAME!r} was retired and has no successor]" in out
    assert "vector service degraded" not in out

    cat.resolve_span_text.side_effect = VectorServiceError("boom")
    assert _span_text_or_marker(cat, "1.1.2", "3:10-40") is None
    assert "vector service degraded" in capsys.readouterr().out

    cat.resolve_span_text.side_effect = None
    cat.resolve_span_text.return_value = "the passage"
    assert _span_text_or_marker(cat, "1.1.2", "3:10-40") == "the passage"
    assert capsys.readouterr().out == ""


# ── (c) the manifest span fill: tests/upgrade/test_manifest_span_fill_rung.py (retired_no_successor case) ──


# ── (d) the converter, directly ──


@pytest.mark.parametrize("status,body,expect", [
    (409, RETIRED, True),
    (409, {**RETIRED, "collection_id": None}, True),
    (409, {**RETIRED, "superseded_by": None, "successor_collection_id": None}, True),
    (409, {**RETIRED, "superseded_by": ""}, True),
    (409, {**RETIRED, "superseded_by": "docs__x"}, False),          # a successor named: the with-successor path's
    (409, {**RETIRED, "successor_collection_id": 5}, False),
    (409, {**RETIRED, "reason": "label_reserved"}, False),
    (409, {k: v for k, v in RETIRED.items() if k != "reason"}, False),
    (409, {**RETIRED, "collection": ""}, False),
    (409, {**RETIRED, "collection": None}, False),
    (409, {k: v for k, v in RETIRED.items() if k != "collection"}, False),
    (404, RETIRED, False),
    (500, RETIRED, False),
    (None, RETIRED, False),
    (409, "collection_superseded", False),
    (409, None, False),
    (409, [RETIRED], False),
])
def test_the_converter_keys_on_status_reason_name_and_the_absence_of_a_successor(status, body, expect) -> None:
    got = retired_no_successor_error_from_body(status, body)
    assert (got is not None) is expect, (status, body)
    if expect:
        assert got.name == RETIRED["collection"]


def test_a_non_int_or_bool_collection_id_is_dropped_not_trusted() -> None:
    assert retired_no_successor_error_from_body(409, {**RETIRED, "collection_id": True}).collection_id is None
    assert retired_no_successor_error_from_body(409, {**RETIRED, "collection_id": "41"}).collection_id is None
    assert retired_no_successor_error_from_body(409, RETIRED).collection_id == 41


# ── it is a 409 VectorServiceError: the handlers written for the engine's 409 recover as they did ──


def test_a_real_handler_of_vector_service_error_recovers_from_the_retired_409() -> None:
    """``resolve_collection`` turns a ``VectorServiceError`` from the row read into ``Unreadable`` (the name's own
    derivation); the retired 409 reaches it as the same 409 ``VectorServiceError`` and recovers the same way."""
    with patch("nexus.mcp_infra.get_collection_row", side_effect=RetiredNoSuccessorError(NAME, 41)):
        got = resolve_collection(NAME)
    assert isinstance(got, Unreadable) and got.reason == "listing"
    assert isinstance(got.cause, RetiredNoSuccessorError)


def test_a_vector_service_error_handler_that_branches_on_code_409_takes_the_same_branch() -> None:
    generic = VectorServiceError("POST /v1/vectors/search → HTTP 409: collection_superseded", code=409,
                                 reason="collection_superseded", engine_body=RETIRED)
    retired = retired_no_successor_error_from_body(409, RETIRED)
    for exc in (generic, retired):
        assert (exc.code, exc.reason, exc.engine_body, exc.edge_refusal) == (409, "collection_superseded", RETIRED, False)


def test_the_retried_classes_do_not_include_it() -> None:
    """The gateway ladder retries 502/503/504 and a 409 only for ``request_in_flight``; neither matches this 409."""
    exc = retired_no_successor_error_from_body(409, RETIRED)
    assert exc.code not in _GATEWAY_RETRY_CODES
    assert (exc.engine_body or {}).get("error") != "request_in_flight"


# ── a retired collection's name can contain "dim"; it must not read as a dimension mismatch or poison a model ──


def test_a_retired_collection_whose_name_contains_dim_is_a_failed_collection_not_a_dimension_mismatch() -> None:
    dimname = "knowledge__dimensions__bge-base-en-v15-768__v1"
    live = "code__nexus"
    t3 = _BatchingT3({live: [{"id": "a", "content": "hit", "distance": 0.3}]}, retired={dimname})
    diags: list[SearchDiagnostics] = []
    results = search_cross_corpus("q", [live, dimname], 10, t3, diagnostics_out=diags, sensitivity="include")
    assert {r.id for r in results} == {"a"} and list(diags[0].failed_collections) == [dimname]
    assert _is_permanent_poisoning_error(RetiredNoSuccessorError(dimname)) is False, "no model is memoized as poisoned"
    # all-retired with "dim" in the name: loud failure, but never the embedding-profile mismatch error
    with pytest.raises(Exception) as raised:
        search_cross_corpus("q", [dimname], 10, _BatchingT3({}, retired={dimname}), sensitivity="include")
    assert not isinstance(raised.value, SearchEmbeddingProfileMismatchError)
