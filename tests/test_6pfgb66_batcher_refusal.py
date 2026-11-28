# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66 fix round 1, item 1: a chunk-key refusal is not bisected.

``ChunkBatcher`` bisects any non-throttle flush failure to isolate a poisoned file. A
``chunk_metadata_key_refused`` 422 is about the writer's keys, which every half carries, so bisecting a
batch of F files costs up to 2F-1 refused requests. The refusal fails the whole batch once, like a
throttle, and the typed error is kept per file so the run summary can name the keys.

The flush here is the real transport: ``HttpCatalogClient.write_manifest_many`` over an httpx
``MockTransport`` that answers the 422, so the typed error arrives the way it does in a run.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.chunk_batcher import ChunkBatcher
from nexus.collection_errors import ChunkMetadataKeyRefusedError

_COLLECTION = "code__6pfgb66-1-1__bge-base-en-v15-768__v1"

_BODY = {
    "reason": "chunk_metadata_key_refused",
    "error": "chunk metadata names keys a chunk row does not carry: title",
    "route": "/v1/catalog/manifest/write_many",
    "collection": _COLLECTION,
    "refused": [{"key": "title", "class": "DOCUMENT", "home": "the catalog document"}],
    "refused_chunks": 3,
    "requested_count": 3,
}


@pytest.fixture(autouse=True)
def _registration_stubs(monkeypatch: pytest.MonkeyPatch):
    import nexus.corpus as corpus  # noqa: PLC0415 — test-local import

    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: None)
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.fixture(autouse=True)
def _no_write_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.t2._refreshable_client.guard_production_write", lambda _url: None)


class _Engine:
    """A catalog client whose manifest writes the engine answers with *answer*; counts every request."""

    def __init__(self, answer: httpx.Response) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local import

        self.writes = 0

        def handler(request: httpx.Request) -> httpx.Response:
            if "/manifest/" in request.url.path:
                self.writes += 1
                return answer
            if request.url.path.endswith("/collections/get"):
                return httpx.Response(200, json={"name": _COLLECTION, "superseded_by": ""})
            return httpx.Response(200, json={})

        self.client = HttpCatalogClient(base_url="http://engine.test", tenant="t", _token="x")
        self.client._client = httpx.Client(transport=httpx.MockTransport(handler))

    def flush(self, collection, ids, docs, metas, file_contexts) -> None:
        self.client.write_manifest_many(
            [(f"1.1.{i}", [{"position": 0, "chash": c, "chunk_index": 0}]) for i, c in enumerate(ids)],
            chunks=[{"chash": c, "text": d, "metadata": m} for c, d, m in zip(ids, docs, metas, strict=True)],
            collection=collection,
        )


def _run(engine: _Engine, files: int) -> tuple[ChunkBatcher, list[tuple[str, str]]]:
    failed: list[tuple[str, str]] = []
    batcher = ChunkBatcher(
        flush=engine.flush, on_file_complete=lambda p, c=None: None,
        on_file_failed=lambda p, e, c=None: failed.append((p, e)), max_chunks=300,
    )
    for i in range(files):
        h = f"{i:064x}"
        batcher.add(f"f{i}.py", _COLLECTION, [h], [f"text {i}"], [{"title": "t"}])
    batcher.drain()
    return batcher, failed


def test_a_refused_batch_is_sent_once_not_bisected() -> None:
    engine = _Engine(httpx.Response(422, json=_BODY))
    batcher, failed = _run(engine, files=4)
    assert engine.writes == 1, "bisecting would send 2F-1 = 7 refused requests"
    assert sorted(p for p, _ in failed) == [f"f{i}.py" for i in range(4)]


def test_the_typed_error_itself_is_kept_for_every_file_of_the_batch() -> None:
    engine = _Engine(httpx.Response(422, json=_BODY))
    batcher, _ = _run(engine, files=3)
    refused = batcher.refused_files
    assert sorted(refused) == ["f0.py", "f1.py", "f2.py"]
    assert all(isinstance(e, ChunkMetadataKeyRefusedError) for e in refused.values())
    assert [k.key for k in refused["f0.py"].refused] == ["title"]
    assert sorted(batcher.failed_files) == sorted(refused), "a refused file is also a failed file"
    assert batcher.throttled_files == {}


def test_another_422_still_bisects_to_isolate_the_poisoned_file() -> None:
    engine = _Engine(httpx.Response(422, json={"error": "nope", "reason": "something_else"}))
    batcher, _ = _run(engine, files=4)
    assert engine.writes > 1, "an unrelated failure keeps the isolate-by-halving behaviour"
    assert batcher.refused_files == {}


def test_a_refusal_wrapped_in_another_error_is_still_recognised() -> None:
    """The flush callback may wrap the typed error; the carve-out reads the cause chain."""
    sent: list[int] = []

    def flush(collection, ids, docs, metas, file_contexts) -> None:
        sent.append(len(ids))
        inner = ChunkMetadataKeyRefusedError(collection=collection)
        raise RuntimeError("flush failed") from inner

    batcher = ChunkBatcher(flush=flush, on_file_complete=lambda p, c=None: None, on_file_failed=lambda p, e, c=None: None,
                           max_chunks=300)
    for i in range(3):
        batcher.add(f"f{i}.py", _COLLECTION, [f"{i:064x}"], ["t"], [{}])
    batcher.drain()
    assert sent == [3]
    assert sorted(batcher.refused_files) == ["f0.py", "f1.py", "f2.py"]


# ── the chain a run follows: real batcher -> run stats -> the summary text ───


def test_a_real_refused_run_names_the_keys_once_in_the_summary_and_promises_no_retry() -> None:
    from nexus.collection_errors import chunk_key_refusal_report, chunk_key_refusal_stats  # noqa: PLC0415 — test-local import

    engine = _Engine(httpx.Response(422, json=_BODY))
    batcher, _ = _run(engine, files=4)
    stats = chunk_key_refusal_stats(batcher.refused_files)
    assert stats["chunk_flush_refused_files"] == 4
    assert stats["chunk_flush_refused_paths"] == [f"f{i}.py" for i in range(4)]
    assert stats["chunk_flush_refused_keys"] == [{"key": "title", "class": "DOCUMENT", "home": "the catalog document"}]
    warning, error = chunk_key_refusal_report(stats, 9)
    assert "4/9 file(s) refused by the engine" in warning
    assert warning.count("'title'") == 1, "four files refused for one key name it once"
    assert "the catalog document" in warning
    assert "refused the same way" in warning
    assert "Upgrade nx if it is older than the engine; otherwise report this message" in warning
    for bad in ("retried automatically", "zero chunks landed", "nothing was indexed", "WARNING line", "Nothing was written"):
        assert bad not in warning and bad not in error
    assert "refused the same way" in error
