# SPDX-License-Identifier: AGPL-3.0-or-later
"""``MetadataMergingCatalog``'s ``on_request`` hook (RDR-223, nexus-z0o2p.11 / .15).

The hook tells a caller that the writer may have written chunks, so that a caller which would undo
a fresh registration on failure (``index_pdf``'s rollback of a document it minted) does not
tombstone a document whose chunks landed. It must not over-report: a request the engine (or the
network) refused CLEANLY wrote nothing, and a freshly minted document whose first request was
refused is a phantom registration that has to be rolled back.

* reported: the request came back (a response), or died in a way that leaves the outcome open
  (a read timeout, a 5xx, a dropped connection mid-request, an unparseable answer);
* not reported: the request never reached the engine (connect error, pool timeout, the client's
  own argument checks) or the engine refused it with a 4xx; or it answered that the document's
  transaction failed (``failed_doc_ids``), which rolled the whole request back.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.catalog.metadata_merging_catalog import MetadataMergingCatalog
from nexus.catalog.write_outcome import PreSendArgumentError
from nexus.errors import CombinedWriteEmbedTimeoutError

_REQ = httpx.Request("POST", "http://engine/v1/catalog/manifest/write_many")


def _status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(f"{code}", request=_REQ, response=httpx.Response(code, request=_REQ))


class _Inner:
    def __init__(self, result=None, raises: BaseException | None = None) -> None:
        self.result, self.raises = result, raises

    def write_manifest_many(self, docs, *a, **kw):
        if self.raises is not None:
            raise self.raises
        return self.result

    def append_manifest_chunks(self, doc_id, rows, **kw):
        if self.raises is not None:
            raise self.raises
        return self.result


#: exception -> does the request count as possibly written?
_OUTCOMES = {
    # never reached the engine
    "connect-error": (httpx.ConnectError("connection refused"), False),
    "connect-timeout": (httpx.ConnectTimeout("connect timed out"), False),
    "pool-timeout": (httpx.PoolTimeout("no connection free"), False),
    "argument-check": (PreSendArgumentError("write_manifest_many: 'collection' is required"), False),
    # refused by the engine
    "400": (_status(400), False),
    "409": (_status(409), False),
    "422": (_status(422), False),
    "429": (_status(429), False),
    # the outcome is open
    "408": (_status(408), True),
    "500": (_status(500), True),
    "502": (_status(502), True),
    "503": (_status(503), True),
    "504": (_status(504), True),
    "read-timeout": (httpx.ReadTimeout("read timed out"), True),
    "read-error": (httpx.ReadError("connection dropped"), True),
    "write-error": (httpx.WriteError("broken pipe mid-request"), True),
    "protocol-error": (httpx.RemoteProtocolError("server disconnected"), True),
    "embed-timeout": (CombinedWriteEmbedTimeoutError(collection="c", chunk_count=3, original="x"), True),
    "ack-mismatch": (RuntimeError("write_many ack mismatch: no 'chunks_written'"), True),
    "unparseable-answer": (json.JSONDecodeError("Expecting value", "", 0), True),
    # a failure while reading the answer is NOT a pre-send check: the engine already committed
    # (the full matrix, run against every caller, is tests/catalog/test_write_outcome_matrix.py)
    "plain-value-error": (ValueError("invalid literal for int()"), True),
    "type-error": (TypeError("bad argument"), True),
}


@pytest.mark.parametrize("which", ["write_manifest_many", "append_manifest_chunks"])
@pytest.mark.parametrize("name", sorted(_OUTCOMES))
def test_a_failed_request_is_reported_as_possibly_written_only_when_the_outcome_is_open(name, which) -> None:
    exc, reported = _OUTCOMES[name]
    seen: list[str] = []
    cat = MetadataMergingCatalog(_Inner(raises=exc), "docs__c", [], on_request=lambda: seen.append("r"))
    args = ([("1.1.1", [])],) if which == "write_manifest_many" else ("1.1.1", [])
    with pytest.raises(type(exc)):
        getattr(cat, which)(*args, collection="docs__c")
    assert seen == (["r"] if reported else []), name


@pytest.mark.parametrize("which", ["write_manifest_many", "append_manifest_chunks"])
def test_a_response_is_reported_after_it_came_back(which) -> None:
    seen: list[str] = []
    cat = MetadataMergingCatalog(
        _Inner(result={"chunks_written": 2, "failed_doc_ids": []}), "docs__c", [],
        on_request=lambda: seen.append("r"))
    args = ([("1.1.1", [])],) if which == "write_manifest_many" else ("1.1.1", [])
    getattr(cat, which)(*args, collection="docs__c")
    assert seen == ["r"]


def test_a_request_the_engine_rolled_back_for_every_document_is_not_reported() -> None:
    """``failed_doc_ids`` naming the only document: its per-document transaction rolled back, so none
    of the request landed, and the registration that preceded it is a phantom."""
    seen: list[str] = []
    cat = MetadataMergingCatalog(
        _Inner(result={"failed_doc_ids": ["1.1.1"]}), "docs__c", [], on_request=lambda: seen.append("r"))
    cat.write_manifest_many([("1.1.1", [])], collection="docs__c")
    assert seen == []


def test_a_request_with_a_surviving_document_is_reported_even_if_another_failed() -> None:
    seen: list[str] = []
    cat = MetadataMergingCatalog(
        _Inner(result={"failed_doc_ids": ["1.1.2"]}), "docs__c", [], on_request=lambda: seen.append("r"))
    cat.write_manifest_many([("1.1.1", []), ("1.1.2", [])], collection="docs__c")
    assert seen == ["r"]


def test_no_hook_is_no_problem() -> None:
    cat = MetadataMergingCatalog(_Inner(result={}), "docs__c", [])
    cat.write_manifest_many([("1.1.1", [])], collection="docs__c")


# ── a retry's failure carries the first attempt's as its context ────────────────────────────


def _lost_then(second: BaseException) -> BaseException:
    """What ``RefreshableHttpStoreMixin._send`` raises when attempt 1 was reset after the engine
    committed and its one retry, made inside the ``except`` block, failed too: only the SECOND error
    propagates, with the first as its ``__context__``."""
    try:
        raise httpx.ReadError("connection reset after the commit")
    except httpx.ReadError:
        try:
            raise second
        except BaseException as out:
            return out


_RETRY_SECOND_ERRORS = {
    "connect-error": lambda: httpx.ConnectError("connection refused on the retry"),
    "connect-timeout": lambda: httpx.ConnectTimeout("connect timed out on the retry"),
    "401": lambda: _status(401),
    "409": lambda: _status(409),
}


@pytest.mark.parametrize("which", ["write_manifest_many", "append_manifest_chunks"])
@pytest.mark.parametrize("name", sorted(_RETRY_SECOND_ERRORS))
def test_a_retry_that_failed_cleanly_after_a_dropped_first_attempt_is_reported_as_possibly_written(
    name, which,
) -> None:
    """The first attempt may have committed (its response was reset); the retry's clean refusal says
    nothing about it. Judging the outermost exception alone reads "wrote nothing", and a caller that
    rolls back a fresh registration on that reading deletes a document whose chunks landed."""
    exc = _lost_then(_RETRY_SECOND_ERRORS[name]())
    assert isinstance(exc.__context__, httpx.ReadError), "non-vacuity: the chain holds attempt 1"
    seen: list[str] = []
    cat = MetadataMergingCatalog(_Inner(raises=exc), "docs__c", [], on_request=lambda: seen.append("r"))
    args = ([("1.1.1", [])],) if which == "write_manifest_many" else ("1.1.1", [])
    with pytest.raises(type(exc)):
        getattr(cat, which)(*args, collection="docs__c")
    assert seen == ["r"], name


def test_a_chain_of_clean_refusals_only_is_still_not_reported() -> None:
    """The control: a 401 whose retry was refused to connect wrote nothing on either attempt."""
    try:
        raise _status(401)
    except httpx.HTTPStatusError:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as out:
            exc = out
    seen: list[str] = []
    cat = MetadataMergingCatalog(_Inner(raises=exc), "docs__c", [], on_request=lambda: seen.append("r"))
    with pytest.raises(httpx.ConnectError):
        cat.write_manifest_many([("1.1.1", [])], collection="docs__c")
    assert seen == []


def test_a_cause_link_is_followed_as_well_as_a_context_link() -> None:
    exc = httpx.ConnectError("refused")
    exc.__cause__ = httpx.ReadError("reset")
    seen: list[str] = []
    cat = MetadataMergingCatalog(_Inner(raises=exc), "docs__c", [], on_request=lambda: seen.append("r"))
    with pytest.raises(httpx.ConnectError):
        cat.write_manifest_many([("1.1.1", [])], collection="docs__c")
    assert seen == ["r"]
