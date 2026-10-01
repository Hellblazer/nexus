# SPDX-License-Identifier: AGPL-3.0-or-later
"""One matrix of failed-write exceptions, run against EVERY caller that asks "did this request
possibly reach and change the engine" (RDR-223 Phase 2 gate, nexus-z0o2p.35, finding I2).

Three hand-rolled classifiers disagreed on 6 of 21 inputs while their own suites stayed green,
because each suite pinned its own table and nothing compared them. This file is the comparison: one
table of exception chains, each with the shape it must have, asserted against

* the shared classifier (:mod:`nexus.catalog.write_outcome`) directly,
* ``note_write``'s shape (:func:`nexus.catalog.note_write._classify`) that decides ``put_note``'s
  outcome,
* ``MetadataMergingCatalog``'s ``on_request`` latch that decides whether a per-file writer keeps or
  rolls back a freshly minted document.

The rule is one sentence: a request "may have written" exactly when it is in flight under the
classifier. Add a new exception class to the table and every caller is held to it.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.catalog.metadata_merging_catalog import MetadataMergingCatalog
from nexus.errors import CombinedWriteEmbedTimeoutError, EngineOlderThanClientError

_REQ = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")

UNSENT, REFUSED, IN_FLIGHT = "unsent", "refused", "in-flight"


def _status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(f"{code}", request=_REQ, response=httpx.Response(code, request=_REQ))


def _chained(first: Exception, second: Exception) -> Exception:
    """*second* as the refreshable client raises it: inside the ``except`` for *first*."""
    try:
        try:
            raise first
        except Exception:
            raise second
    except Exception as caught:
        return caught


def _connect() -> Exception:
    return httpx.ConnectError("connection refused")


def _client_refusal(name: str) -> Exception:
    from nexus.collection_errors import SupersededCollectionWriteError
    from nexus.corpus import EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError

    if name == "superseded":
        return SupersededCollectionWriteError("docs__old", "docs__new")
    cls = {"profile": EmbeddingProfileMismatchError, "voyage-key": LocalVoyageCredentialMissingError}[name]
    exc = cls.__new__(cls)
    RuntimeError.__init__(exc, f"{name} refusal")
    return exc


def _pre_send_argument_check() -> Exception:
    from nexus.catalog.write_outcome import PreSendArgumentError

    return PreSendArgumentError("write_manifest_many: 'collection' is required")


#: name -> (factory, shape). A factory so each caller gets a fresh exception (tracebacks mutate).
_MATRIX: dict[str, tuple] = {
    # never reached the engine
    "connect-error": (_connect, UNSENT),
    "connect-timeout": (lambda: httpx.ConnectTimeout("connect timed out"), UNSENT),
    "pool-timeout": (lambda: httpx.PoolTimeout("no connection free"), UNSENT),
    "pre-send-argument-check": (_pre_send_argument_check, UNSENT),
    "client-refusal-profile-mismatch": (lambda: _client_refusal("profile"), UNSENT),
    "client-refusal-voyage-key": (lambda: _client_refusal("voyage-key"), UNSENT),
    "client-refusal-superseded": (lambda: _client_refusal("superseded"), UNSENT),
    "connect-then-connect": (lambda: _chained(_connect(), _connect()), UNSENT),
    "connect-then-connect-timeout": (lambda: _chained(_connect(), httpx.ConnectTimeout("slow")), UNSENT),
    # refused by the engine
    "400": (lambda: _status(400), REFUSED),
    "401": (lambda: _status(401), REFUSED),
    "409": (lambda: _status(409), REFUSED),
    "422": (lambda: _status(422), REFUSED),
    "429": (lambda: _status(429), REFUSED),
    "connect-then-409": (lambda: _chained(_connect(), _status(409)), REFUSED),
    "409-then-connect": (lambda: _chained(_status(409), _connect()), REFUSED),
    # the outcome is open
    "408": (lambda: _status(408), IN_FLIGHT),
    "500": (lambda: _status(500), IN_FLIGHT),
    "502": (lambda: _status(502), IN_FLIGHT),
    "503": (lambda: _status(503), IN_FLIGHT),
    "504": (lambda: _status(504), IN_FLIGHT),
    "read-timeout": (lambda: httpx.ReadTimeout("read timed out"), IN_FLIGHT),
    "read-error": (lambda: httpx.ReadError("connection dropped"), IN_FLIGHT),
    "write-error": (lambda: httpx.WriteError("broken pipe mid-request"), IN_FLIGHT),
    "protocol-error": (lambda: httpx.RemoteProtocolError("server disconnected"), IN_FLIGHT),
    "embed-timeout": (lambda: CombinedWriteEmbedTimeoutError(collection="c", chunk_count=3, original="x"), IN_FLIGHT),
    "ack-mismatch": (lambda: RuntimeError("write_many ack mismatch: no 'chunks_written'"), IN_FLIGHT),
    "engine-older-than-client": (lambda: EngineOlderThanClientError("write_manifest_many: no echo"), IN_FLIGHT),
    # a parse or typing failure AFTER a 2xx: the engine already committed
    "json-decode-after-200": (lambda: json.JSONDecodeError("Expecting value", "", 0), IN_FLIGHT),
    "unicode-decode-after-200": (lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), IN_FLIGHT),
    "value-error-after-200": (lambda: ValueError("invalid literal for int() with base 10: 'x'"), IN_FLIGHT),
    "type-error-after-200": (lambda: TypeError("'NoneType' object is not subscriptable"), IN_FLIGHT),
    # chains: any in-flight node of the chain makes the request in flight
    "read-error-then-connect": (lambda: _chained(httpx.ReadError("reset"), _connect()), IN_FLIGHT),
    "connect-then-read-error": (lambda: _chained(_connect(), httpx.ReadError("reset")), IN_FLIGHT),
    "read-error-then-401": (lambda: _chained(httpx.ReadError("reset"), _status(401)), IN_FLIGHT),
    "401-then-read-error": (lambda: _chained(_status(401), httpx.ReadError("reset")), IN_FLIGHT),
    "500-then-connect": (lambda: _chained(_status(500), _connect()), IN_FLIGHT),
    "embed-timeout-then-connect": (
        lambda: _chained(CombinedWriteEmbedTimeoutError(collection="c", chunk_count=1, original="x"), _connect()),
        IN_FLIGHT),
    "protocol-error-then-client-refusal": (
        lambda: _chained(httpx.RemoteProtocolError("dropped"), _client_refusal("profile")), IN_FLIGHT),
}


class _Inner:
    def __init__(self, raises: BaseException) -> None:
        self.raises = raises

    def write_manifest_many(self, docs, *a, **kw):
        raise self.raises

    def append_manifest_chunks(self, doc_id, rows, **kw):
        raise self.raises


def _shape_shared(exc: BaseException) -> str:
    from nexus.catalog.write_outcome import judge

    return judge(exc)[0]


def _written_shared(exc: BaseException) -> bool:
    from nexus.catalog.write_outcome import may_have_written

    return may_have_written(exc)


def _shape_note_write(exc: BaseException) -> str:
    from nexus.catalog.note_write import _classify

    return _classify(exc)


def _written_merging_catalog(exc: BaseException, method: str) -> bool:
    seen: list[str] = []
    cat = MetadataMergingCatalog(_Inner(exc), "docs__c", [], on_request=lambda: seen.append("r"))
    args = ([("1.1.1", [])],) if method == "write_manifest_many" else ("1.1.1", [])
    with pytest.raises(type(exc)):
        getattr(cat, method)(*args, collection="docs__c")
    return bool(seen)


@pytest.mark.parametrize("name", sorted(_MATRIX))
class TestEveryCallerAgrees:
    def test_the_shared_classifier_gives_the_shape(self, name):
        factory, shape = _MATRIX[name]
        assert _shape_shared(factory()) == shape

    def test_may_have_written_is_exactly_in_flight(self, name):
        factory, shape = _MATRIX[name]
        assert _written_shared(factory()) is (shape == IN_FLIGHT)

    def test_note_write_gives_the_same_shape(self, name):
        factory, shape = _MATRIX[name]
        assert _shape_note_write(factory()) == shape

    @pytest.mark.parametrize("method", ["write_manifest_many", "append_manifest_chunks"])
    def test_the_per_file_writer_keeps_a_registration_exactly_when_in_flight(self, name, method):
        factory, shape = _MATRIX[name]
        assert _written_merging_catalog(factory(), method) is (shape == IN_FLIGHT), (
            f"{name}: MetadataMergingCatalog.{method} disagrees with the shared classifier")


def test_the_matrix_names_every_client_refusal_the_note_writer_knows() -> None:
    """The table cannot silently miss a client-side refusal: it names each class
    ``note_write`` treats as a connection never made."""
    from nexus.catalog.write_outcome import client_side_refusals

    named = {type(factory()) for name, (factory, shape) in _MATRIX.items()
             if name.startswith("client-refusal-") or name == "pre-send-argument-check"}
    assert set(client_side_refusals()) <= named


def test_no_caller_keeps_its_own_copy_of_the_rule() -> None:
    """``_may_have_written`` and ``_judge`` are gone: the callers import the one classifier."""
    import nexus.catalog.metadata_merging_catalog as m
    import nexus.catalog.note_write as n
    from nexus.catalog import write_outcome as w

    assert not hasattr(m, "_attempt_may_have_written")
    assert not hasattr(m, "_may_have_written")
    assert not hasattr(n, "_judge")
    assert m.may_have_written is w.may_have_written
    assert n.judge is w.judge
