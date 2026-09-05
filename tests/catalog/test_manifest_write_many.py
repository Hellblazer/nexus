# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batched manifest replace (nexus-u2kwq): write_manifest_many pages at
1000 docs via /manifest/write_many; _manifest_write_loop uses it for
multi-doc flush-grain batches with 404 fallback to the per-doc path.

GH #1371: _manifest_write_loop retries transient connection errors and,
on persistent failure, records the doc_id in the module-level failure
collector instead of only logging a WARNING (see TestManifestWriteFailureSurfacing).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.errors import CombinedWriteEmbedTimeoutError
from nexus.mcp_infra import (
    _manifest_write_loop,
    get_manifest_write_failures,
    reset_manifest_write_failures,
)
from nexus.retry import _is_connectivity_error

# RDR-191 (Hal ruling 2026-08-12): every manifest writer call in this file
# now requires an explicit collection. A single shared constant keeps the
# many call sites below from inventing their own plausible-looking values.
_COLLECTION = "knowledge__manifest-write-many-test__voyage-context-3__v1"


def _client() -> HttpCatalogClient:
    return HttpCatalogClient.__new__(HttpCatalogClient)


class TestWriteManifestMany:
    def test_single_post_and_row_shape(self, monkeypatch) -> None:
        c = _client()
        posts: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: posts.append((path, body)) or {"docs": 2, "rows": 3, "failed_doc_ids": []},
            raising=False,
        )
        result = c.write_manifest_many([
            ("1.15.1", [{"chash": "a" * 64, "position": 0}]),
            ("1.15.2", [{"chash": "b" * 64, "position": 0}, {"chash": "c" * 64, "position": 1}]),
        ], collection=_COLLECTION)
        assert result["failed_doc_ids"] == []
        assert len(posts) == 1
        path, body = posts[0]
        assert path == "/manifest/write_many"
        assert [d["doc_id"] for d in body["docs"]] == ["1.15.1", "1.15.2"]
        assert len(body["docs"][1]["rows"]) == 2

    def test_pages_at_1000_docs(self, monkeypatch) -> None:
        c = _client()
        posts: list[dict] = []
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: posts.append(body) or {"docs": len(body["docs"]), "rows": 0, "failed_doc_ids": []},
            raising=False,
        )
        docs = [(f"1.9.{i}", [{"chash": "a" * 64, "position": 0}]) for i in range(1500)]
        c.write_manifest_many(docs, collection=_COLLECTION)
        assert [len(p["docs"]) for p in posts] == [1000, 500]

    def test_failed_doc_ids_surface(self, monkeypatch) -> None:
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {"docs": 1, "rows": 1, "failed_doc_ids": ["1.9.7"]},
            raising=False,
        )
        result = c.write_manifest_many(
            [("1.9.7", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION)
        assert result["failed_doc_ids"] == ["1.9.7"]

    def test_failed_reasons_logged(self, monkeypatch) -> None:
        # nexus-fhhwf: engine v0.1.33+ returns {failed:[{doc_id, reason,
        # sqlstate}]} alongside the bare id list — the client must surface
        # the reason in its log so a failed doc is diagnosable without
        # server access (the v0.1.24 three-iteration lesson).
        import structlog.testing

        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {
                "docs": 0, "rows": 0,
                "failed_doc_ids": ["1.9.8"],
                "failed": [{
                    "doc_id": "1.9.8",
                    "reason": "check constraint violation [catalog_document_chunks_chash_len_check]",
                    "sqlstate": "23514",
                }],
            },
            raising=False,
        )
        with structlog.testing.capture_logs() as logs:
            result = c.write_manifest_many(
                [("1.9.8", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION,
            )
        assert result["failed_doc_ids"] == ["1.9.8"]
        events = [l for l in logs if l["event"] == "manifest_write_many_doc_failed"]
        assert len(events) == 1
        assert events[0]["doc_id"] == "1.9.8"
        assert "chash_len_check" in events[0]["reason"]
        assert events[0]["sqlstate"] == "23514"

    def test_pre_reason_engine_response_still_works(self, monkeypatch) -> None:
        # Back-compat: an older engine without the "failed" field must not
        # break the client (no KeyError, ids still returned).
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {"docs": 0, "rows": 0, "failed_doc_ids": ["1.9.9"]},
            raising=False,
        )
        assert c.write_manifest_many(
            [("1.9.9", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION,
        )["failed_doc_ids"] == ["1.9.9"]

    def test_no_chunks_no_sweep_body_unchanged(self, monkeypatch) -> None:
        # RDR-191 (Hal ruling 2026-08-12) superseded the old "byte-for-byte
        # pre-wxjr6 body" backward-compat claim this docstring made: the
        # engine no longer infers `collection` from chunk membership, so it
        # now rides EVERY page's body unconditionally, chunks or not — that
        # is the whole point of the fix (previously `collection` was sent
        # only on the chunks-carrying page, which was itself the bug). An
        # absent `chunks` field and sweep=False (the default) still omit
        # `chunks`, `sweep`, and `force_re_embed` — only `collection` is now
        # always present.
        c = _client()
        posts: list[dict] = []
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: posts.append(body) or {"docs": 1, "rows": 1, "failed_doc_ids": []},
            raising=False,
        )
        c.write_manifest_many(
            [("1.9.10", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION)
        assert set(posts[0]) == {"docs", "collection"}
        assert posts[0]["collection"] == _COLLECTION


class TestWriteManifestManyCombined:
    """nexus-wxjr6: the client half of the kl2z6 combined write."""

    def test_chunks_requires_collection(self) -> None:
        # RDR-191: collection is unconditionally required now (not only
        # when chunks is provided), so the omission itself has become a
        # TypeError at the Python call boundary (a required keyword-only
        # arg with no default) rather than reaching this method's own
        # ValueError. Passing an explicit blank collection still exercises
        # the client-side refusal this test is pinning.
        c = _client()
        with pytest.raises(ValueError, match="collection.*required"):
            c.write_manifest_many(
                [("1.9.11", [{"chash": "a" * 64, "position": 0}])],
                chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
                collection="",
            )

    def test_combined_wire_shape(self, monkeypatch) -> None:
        c = _client()
        posts: list[dict] = []
        chunks = [{"chash": "a" * 64, "text": "hi", "metadata": {"k": "v"}}]
        monkeypatch.setattr(
            c, "_post",
            lambda path, body, **kw: posts.append(body) or {
                "docs": 1, "rows": 1, "failed_doc_ids": [], "chunks_written": 1,
            },
            raising=False,
        )
        result = c.write_manifest_many(
            [("1.9.12", [{"chash": "a" * 64, "position": 0}])],
            sweep=True,
            chunks=chunks,
            collection="code__nexus-1-1__voyage-code-3__v1",
            force_re_embed=True,
        )
        assert len(posts) == 1
        body = posts[0]
        assert body["collection"] == "code__nexus-1-1__voyage-code-3__v1"
        assert body["chunks"] == chunks
        assert body["sweep"] is True
        assert body["force_re_embed"] is True
        assert result["chunks_written"] == 1

    def test_ack_echo_raises_when_chunks_written_absent(self, monkeypatch) -> None:
        # design memo §5.2: the ONLY runtime capability signal — an old
        # engine silently drops the unknown `chunks` field with no error,
        # so absence of `chunks_written` must RAISE, not warn-and-proceed
        # (the upsert_chunks ack-mismatch precedent, not the
        # complete_refused_count_mismatch WARN fallback).
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body, **kw: {"docs": 1, "rows": 1, "failed_doc_ids": []},
            raising=False,
        )
        with pytest.raises(RuntimeError, match="ack mismatch"):
            c.write_manifest_many(
                [("1.9.13", [{"chash": "a" * 64, "position": 0}])],
                chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
                collection="code__nexus-1-1__voyage-code-3__v1",
            )

    def test_no_chunks_sent_never_checks_ack(self, monkeypatch) -> None:
        # A call that never sends `chunks` must not raise even though the
        # response also lacks `chunks_written` — that key is only
        # meaningful (and only checked) when this call actually carried
        # content.
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {"docs": 1, "rows": 1, "failed_doc_ids": []},
            raising=False,
        )
        result = c.write_manifest_many(
            [("1.9.14", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION,
        )
        assert "chunks_written" not in result

    def test_sweep_fields_forwarded(self, monkeypatch) -> None:
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {
                "docs": 1, "rows": 1, "failed_doc_ids": [],
                "swept": 2, "sweep_skipped": 1,
                "sweep_detail": [{"doc_id": "1.9.15", "errored": True, "reason": "sweep_failed"}],
            },
            raising=False,
        )
        result = c.write_manifest_many(
            [("1.9.15", [{"chash": "a" * 64, "position": 0}])], sweep=True,
            collection=_COLLECTION,
        )
        assert result["swept"] == 2
        assert result["sweep_skipped"] == 1
        assert result["sweep_detail"][0]["reason"] == "sweep_failed"


class TestWriteManifestManyCombinedTimeout:
    """nexus-y9t08: the combined write (``chunks=`` present) performs a
    SYNCHRONOUS server-side embed inside the request. The v7.5.0 regression
    let this call inherit ``RefreshableHttpStoreMixin``'s
    ``_DEFAULT_TIMEOUT_S`` (30.0s, a control-plane default) instead of an
    embed-appropriate budget — the predecessor path
    (``HttpVectorClient.upsert_chunks`` -> ``/v1/vectors/upsert-chunks``)
    passed ``timeout=600`` explicitly for the identical synchronous-embed
    shape (``src/nexus/db/http_vector_client.py:1408``).

    Scoping decision (bead ask (a) vs (b)): option (a), a PER-REQUEST
    override on the chunk-carrying POST only — not a client-wide timeout
    bump (option (b), which would make every genuinely-hung ordinary
    catalog call (registration, linking, listing) take 10 minutes to
    surface). The plain (no ``chunks``) branch of ``write_manifest_many``
    must keep the 30s default; that is what
    ``test_plain_write_never_gets_the_embed_timeout`` pins.
    """

    def test_combined_write_gets_an_embed_grade_timeout(self, monkeypatch) -> None:
        c = _client()
        calls: list[dict[str, Any]] = []

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            calls.append(kw)
            return {"docs": 1, "rows": 1, "failed_doc_ids": [], "chunks_written": 1}

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        c.write_manifest_many(
            [("1.9.16", [{"chash": "a" * 64, "position": 0}])],
            chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
            collection="code__nexus-1-1__voyage-code-3__v1",
        )

        assert len(calls) == 1
        # Non-vacuity: must be an EXPLICIT override, not just "present" —
        # and it must differ from the T2 control-plane default this
        # regression accidentally inherited.
        from nexus.db.t2._refreshable_client import _DEFAULT_TIMEOUT_S

        assert calls[0].get("timeout") is not None
        assert calls[0]["timeout"] != _DEFAULT_TIMEOUT_S
        assert calls[0]["timeout"] == 600.0

    def test_plain_write_never_gets_the_embed_timeout(self, monkeypatch) -> None:
        """The narrowness guard for scoping decision (a): a manifest write
        with no ``chunks`` (the overwhelming majority of catalog traffic —
        registration, linking, plain manifest replace) must NOT receive the
        embed-grade override. A blanket bump (option (b)) would make this
        assertion fail."""
        c = _client()
        calls: list[dict[str, Any]] = []

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            calls.append(kw)
            return {"docs": 1, "rows": 1, "failed_doc_ids": []}

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        c.write_manifest_many(
            [("1.9.17", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION)

        assert len(calls) == 1
        assert "timeout" not in calls[0]


class TestCombinedWriteReadTimeoutNotRetried:
    """nexus-y9t08 CRITICAL 1 (post-127f2dbc correction): the 127f2dbc
    comment claimed the retained retry-on-timeout "matches the OLD path's
    own accepted behavior" — FALSE. The predecessor
    (``HttpVectorClient.upsert_chunks``) path's retry classifier catches
    only ``(urllib.error.URLError, ConnectionRefusedError,
    ConnectionResetError)`` and explicitly excludes ALL timeouts
    (``src/nexus/db/http_vector_client.py:779-784`` — "TimeoutError is
    intentionally NOT in this retry classifier ... it propagates straight
    to the _get/_post handler"). The old path fails ONCE on a timeout, by
    design.

    A ReadTimeout on the chunk-carrying POST must behave the same way:
    exactly ONE POST, no retry at either the self-heal layer
    (``RefreshableHttpStoreMixin._send`` — pinned directly in
    ``tests/db/test_refreshable_client.py``'s
    ``TestReadTimeoutRetryOptOut``) or the outer
    ``nexus.retry._manifest_write_with_retry`` layer that wraps this
    exact call from ``indexer.py``'s combined-write flush path. Ordinary
    connectivity blips (``ConnectionResetError`` et al., GH #1371) must
    still propagate un-converted so they retry as before at BOTH layers.
    """

    def test_read_timeout_raises_converted_type_after_exactly_one_post(
        self, monkeypatch
    ) -> None:
        c = _client()
        calls: list[dict[str, Any]] = []

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            calls.append(kw)
            raise httpx.ReadTimeout("hang mid-read")

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        with pytest.raises(CombinedWriteEmbedTimeoutError):
            c.write_manifest_many(
                [("1.9.21", [{"chash": "a" * 64, "position": 0}])],
                chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
                collection="code__nexus-1-1__voyage-code-3__v1",
            )

        # Exactly one POST at this boundary — write_manifest_many itself
        # never loops/retries on this exception.
        assert len(calls) == 1
        # The chunk-carrying POST must opt OUT of the mixin's self-heal
        # retry-on-ReadTimeout too (proven end-to-end in
        # tests/db/test_refreshable_client.py's TestReadTimeoutRetryOptOut).
        assert calls[0].get("retry_read_timeout") is False

    def test_converted_exception_is_not_classified_as_connectivity(
        self, monkeypatch
    ) -> None:
        """Proves the OUTER nexus.retry._manifest_write_with_retry layer
        (indexer.py's wrapper around this exact call) would not retry
        either — that classifier inspects exception type + chained
        __cause__/__context__ only, with no visibility into which inner
        POST failed."""
        c = _client()

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            raise httpx.ReadTimeout("hang mid-read")

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        try:
            c.write_manifest_many(
                [("1.9.22", [{"chash": "a" * 64, "position": 0}])],
                chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
                collection="code__nexus-1-1__voyage-code-3__v1",
            )
        except CombinedWriteEmbedTimeoutError as exc:
            assert _is_connectivity_error(exc) is False
        else:
            pytest.fail("expected CombinedWriteEmbedTimeoutError")

    def test_connection_reset_still_propagates_unconverted_for_retry(
        self, monkeypatch
    ) -> None:
        """The connectivity-blip carve-out: a ConnectionResetError on the
        SAME chunk-carrying call must NOT be converted or swallowed — it
        propagates as the original type so both retry layers still see a
        genuine connectivity error and retry it (GH #1371 must not
        regress for this call)."""
        c = _client()
        calls: list[dict[str, Any]] = []

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            calls.append(kw)
            raise ConnectionResetError("reset by peer")

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        with pytest.raises(ConnectionResetError):
            c.write_manifest_many(
                [("1.9.23", [{"chash": "a" * 64, "position": 0}])],
                chunks=[{"chash": "a" * 64, "text": "hi", "metadata": {}}],
                collection="code__nexus-1-1__voyage-code-3__v1",
            )

        assert len(calls) == 1
        assert calls[0].get("retry_read_timeout") is False
        assert _is_connectivity_error(ConnectionResetError("reset by peer")) is True

    def test_other_catalog_calls_unaffected(self, monkeypatch) -> None:
        """Narrowness guard: a plain (no ``chunks``) write never passes
        ``retry_read_timeout`` at all, so it rides
        ``HttpCatalogClient._post``'s own default (True) — identical to
        every other one of the ~100+ non-combined-write catalog calls
        through this client. Mirrors
        ``TestWriteManifestManyCombinedTimeout.test_plain_write_never_gets_the_embed_timeout``'s
        shape for the retry-opt-out kwarg specifically."""
        c = _client()
        calls: list[dict[str, Any]] = []

        def _fake_post(path: str, body: dict, **kw: Any) -> dict:
            calls.append(kw)
            return {"docs": 1, "rows": 1, "failed_doc_ids": []}

        monkeypatch.setattr(c, "_post", _fake_post, raising=False)

        c.write_manifest_many(
            [("1.9.24", [{"chash": "a" * 64, "position": 0}])], collection=_COLLECTION)

        assert len(calls) == 1
        assert "retry_read_timeout" not in calls[0]


class _FakeCat:
    """Catalog test double capturing which write path the loop takes."""

    def __init__(self, many: bool = True, many_404: bool = False) -> None:
        self.many_calls: list[list] = []
        self.replace_calls: list[str] = []
        self.resync_calls: list[str] = []
        self.many_404 = many_404
        if not many:
            # simulate a writer without the capability (legacy proxy)
            self.write_manifest_many = None  # type: ignore[assignment]

    # nexus-kgos1: the sweep's two READS. Present and empty so the per-doc
    # path's sweep is exercised and provably no-ops on an empty `before` —
    # rather than dying on AttributeError and no-opping by accident, which is
    # what this file did before `reader` became required.
    def get_chunk_chashes(self, doc_id):
        return []

    def docs_for_chashes(self, chashes):
        return {}

    # nexus-tgrgs: the fast branch's batched "before" read. Empty for every
    # doc_id by default (matches "nothing dropped" — the tests in this file
    # exercise write-path ROUTING, not sweep behaviour; see
    # TestFastBranchSweep below for the sweep-specific fakes).
    def get_manifests(self, doc_ids):
        return {}

    def write_manifest_many(self, docs, complete=None, *, collection):  # type: ignore[no-redef]
        assert collection, "write_manifest_many called with a blank collection"
        if self.many_404:
            err: Any = RuntimeError("HTTP 404")
            err.code = 404
            raise err
        self.many_calls.append(list(docs))
        return []

    def atomic_manifest_replace(self, doc_id, chunks, *, collection, **kw):
        assert collection, "atomic_manifest_replace called with a blank collection"
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        self.resync_calls.append(doc_id)


def _by_doc(n_docs: int) -> dict:
    return {
        f"1.9.{d}": [(i, {"chunk_text_hash": f"{d:02d}{i:02d}" + "e" * 60,
                          "chunk_index": i}) for i in range(2)]
        for d in range(n_docs)
    }


class TestManifestWriteLoopBatching:
    def test_multi_doc_uses_write_many_once(self) -> None:
        cat = _FakeCat()
        _manifest_write_loop(cat, _by_doc(3), _COLLECTION, reader=cat)
        assert len(cat.many_calls) == 1
        assert len(cat.many_calls[0]) == 3
        assert cat.replace_calls == []

    def test_404_falls_back_to_per_doc_with_chunk_count_resync(self) -> None:
        cat = _FakeCat(many_404=True)
        _manifest_write_loop(cat, _by_doc(2), _COLLECTION, reader=cat)
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]
        # chunk_count parity (critique Critical): HTTP replace does not
        # sync documents.chunk_count — the fallback must resync per doc.
        assert sorted(cat.resync_calls) == ["1.9.0", "1.9.1"]

    def test_single_doc_batch_takes_write_many(self) -> None:
        # critique Critical: len(by_doc)==1 must STILL use write_many —
        # it is the only HTTP path that folds chunk_count in.
        cat = _FakeCat()
        _manifest_write_loop(cat, _by_doc(1), _COLLECTION, reader=cat)
        assert len(cat.many_calls) == 1
        assert [d for d, _ in cat.many_calls[0]] == ["1.9.0"]
        assert cat.replace_calls == []

    def test_all_continuation_batch_still_appends(self) -> None:
        # critique Significant: every doc lacking position 0 must fall
        # through to the append path, not vanish in an early return.
        cat = _FakeCat()
        cat.append_calls: list[str] = []
        cat.append_manifest_chunks = lambda doc_id, chunks, collection: cat.append_calls.append(doc_id)
        by_doc = {
            f"1.9.{d}": [(i, {"chunk_text_hash": "a" * 64, "chunk_index": 3 + i})
                         for i in range(2)]
            for d in range(2)
        }
        _manifest_write_loop(cat, by_doc, _COLLECTION, reader=cat)
        assert cat.many_calls == []
        assert sorted(cat.append_calls) == ["1.9.0", "1.9.1"]
        assert cat.replace_calls == []

    def test_continuation_doc_without_position0_never_replaced(self) -> None:
        # Review Important #1: a doc whose slice lacks position 0 is a
        # continuation — REPLACE would delete its earlier rows. It must
        # route to the per-doc append path, never write_many.
        cat = _FakeCat()
        cat.append_calls: list[str] = []
        cat.append_manifest_chunks = lambda doc_id, chunks, collection: cat.append_calls.append(doc_id)
        cat.resync_chunk_count_cache = lambda doc_id: None
        by_doc = _by_doc(2)  # both have position 0 via chunk_index 0
        # make one doc a continuation slice (positions 5,6)
        by_doc["1.9.1"] = [
            (i, {"chunk_text_hash": "f" * 64, "chunk_index": 5 + i})
            for i in range(2)
        ]
        _manifest_write_loop(cat, by_doc, _COLLECTION, reader=cat)
        assert len(cat.many_calls) == 1
        assert [d for d, _ in cat.many_calls[0]] == ["1.9.0"]
        assert cat.replace_calls == []  # continuation NOT replaced
        assert cat.append_calls == ["1.9.1"]  # appended instead

    def test_writer_without_capability_uses_per_doc(self) -> None:
        cat = _FakeCat(many=False)
        _manifest_write_loop(cat, _by_doc(2), _COLLECTION, reader=cat)
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]


def _continuation_by_doc(doc_id: str, start_position: int) -> dict:
    # No position 0 in this batch -> a continuation slice, same shape as
    # a ChunkBatcher flush after the first for one large document.
    return {
        doc_id: [
            (i, {"chunk_text_hash": "a" * 64, "chunk_index": start_position + i})
            for i in range(2)
        ]
    }


def _reroute_structlog_to_caplog() -> None:
    """Reroute structlog to stdlib logging so ``caplog`` can see events
    below the session-wide WARNING floor (conftest.py's ``pytest_configure``
    sets ``wrapper_class=make_filtering_bound_logger(WARNING)`` for the
    whole run — ``structlog.testing.capture_logs()`` only swaps
    *processors*, so it never bypasses that floor and silently sees
    nothing for an INFO/DEBUG event). Same pattern as
    ``test_5xn3k_fence_ordering.py``'s complete-refused tests; the
    autouse ``_restore_structlog_after_test`` fixture in conftest.py
    restores the original config after the test.
    """
    import structlog

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


class TestManifestWriteManyPartialDocSkippedLogging:
    """nexus-gup3b: a multi-batch document's every flush after the first
    lacks position 0 and lands in the continuation bucket by design —
    normal for any document spanning more than one ChunkBatcher flush
    (~16 chunks), not a defect. Before this fix each flush fired an
    unconditional WARNING; a live re-index of one large document logged
    it 23 times for that single document, once per flush, drowning
    genuine warnings."""

    def test_three_batch_document_logs_one_info_line_not_three(self, caplog) -> None:
        import logging

        from nexus.mcp_infra import reset_manifest_partial_doc_skips

        _reroute_structlog_to_caplog()
        reset_manifest_partial_doc_skips()
        cat = _FakeCat()
        cat.append_manifest_chunks = lambda doc_id, chunks, collection: None

        with caplog.at_level(logging.DEBUG):
            for flush in range(3):
                _manifest_write_loop(
                    cat,
                    _continuation_by_doc("1.9.0", start_position=2 + flush * 2),
                    _COLLECTION, reader=cat,
                )

        info_records = [r for r in caplog.records if r.message == "manifest_write_many_partial_doc_skipped"]
        repeat_records = [r for r in caplog.records if r.message == "manifest_write_many_partial_doc_skipped_repeat"]
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]

        # ONE line, not three — the whole point of this bead.
        assert len(info_records) == 1, [r.message for r in caplog.records]
        assert info_records[0].levelname == "INFO"
        assert info_records[0].doc_ids == ["1.9.0"]
        # The count stays visible: 2 later flushes for the same doc this
        # run are reported at debug, not silently dropped.
        assert len(repeat_records) == 2, repeat_records
        assert all(r.levelname == "DEBUG" for r in repeat_records)
        assert repeat_records[0].counts == {"1.9.0": 2}
        assert repeat_records[1].counts == {"1.9.0": 3}
        assert warning_records == []

    def test_different_docs_each_get_their_own_first_info_line(self, caplog) -> None:
        import logging

        from nexus.mcp_infra import reset_manifest_partial_doc_skips

        _reroute_structlog_to_caplog()
        reset_manifest_partial_doc_skips()
        cat = _FakeCat()
        cat.append_manifest_chunks = lambda doc_id, chunks, collection: None
        by_doc = {
            **_continuation_by_doc("1.9.0", start_position=2),
            **_continuation_by_doc("1.9.1", start_position=2),
        }

        with caplog.at_level(logging.DEBUG):
            _manifest_write_loop(cat, by_doc, _COLLECTION, reader=cat)

        info_records = [r for r in caplog.records if r.message == "manifest_write_many_partial_doc_skipped"]
        assert len(info_records) == 1
        assert sorted(info_records[0].doc_ids) == ["1.9.0", "1.9.1"]
        assert info_records[0].count == 2

    def test_reset_gives_a_later_run_its_own_fresh_info_line(self, caplog) -> None:
        import logging

        from nexus.mcp_infra import reset_manifest_partial_doc_skips

        _reroute_structlog_to_caplog()
        cat = _FakeCat()
        cat.append_manifest_chunks = lambda doc_id, chunks, collection: None

        reset_manifest_partial_doc_skips()
        _manifest_write_loop(cat, _continuation_by_doc("1.9.0", 2), _COLLECTION, reader=cat)

        # A fresh CLI run resets the collector -> the same doc_id gets a
        # NEW first-flush INFO line rather than staying suppressed forever
        # for the life of the long-lived MCP server process.
        reset_manifest_partial_doc_skips()
        with caplog.at_level(logging.DEBUG):
            _manifest_write_loop(cat, _continuation_by_doc("1.9.0", 2), _COLLECTION, reader=cat)

        info_records = [r for r in caplog.records if r.message == "manifest_write_many_partial_doc_skipped"]
        assert len(info_records) == 1


class _FlakyThenOkCat:
    """Fails the first N calls to a given write op with a transient
    connection error, then succeeds. Used to prove _manifest_write_loop
    retries transient connection failures instead of losing the write."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.replace_calls: list[str] = []
        self.resync_calls: list[str] = []
        self.write_manifest_many = None  # type: ignore[assignment]

    def atomic_manifest_replace(self, doc_id, chunks, *, collection, **kw):
        assert collection, "atomic_manifest_replace called with a blank collection"
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("connection refused")
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        self.resync_calls.append(doc_id)


class _AlwaysDownCat:
    """Every write raises a persistent (non-connection) error — proves the
    hook still swallows the failure (contract: never propagate) and
    records it for the CLI summary."""

    def __init__(self) -> None:
        self.write_manifest_many = None  # type: ignore[assignment]

    def atomic_manifest_replace(self, doc_id, chunks, *, collection, **kw):
        raise ValueError("FK violation: doc_id not found")

    def resync_chunk_count_cache(self, doc_id):
        pass


class _AlwaysDownManyCat:
    """write_manifest_many always raises a persistent connection error —
    proves the write_many path does not crash the caller and records
    every doc in the failed batch."""

    def __init__(self) -> None:
        self.replace_calls: list[str] = []

    def write_manifest_many(self, docs, complete=None, *, collection):
        raise httpx.ConnectError("connection refused")

    def atomic_manifest_replace(self, doc_id, chunks, *, collection, **kw):
        assert collection, "atomic_manifest_replace called with a blank collection"
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        pass


class TestManifestWriteFailureSurfacing:
    """GH #1371: retry transient connection errors; surface persistent
    failures via the module-level collector instead of only a log line."""

    def setup_method(self) -> None:
        reset_manifest_write_failures()

    def teardown_method(self) -> None:
        reset_manifest_write_failures()

    def test_transient_connect_error_recovers_without_recording_failure(self) -> None:
        cat = _FlakyThenOkCat(fail_times=2)
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(1), _COLLECTION, reader=cat)
        assert cat.replace_calls == ["1.9.0"]
        assert get_manifest_write_failures() == []

    def test_persistent_per_doc_failure_is_swallowed_and_recorded(self) -> None:
        cat = _AlwaysDownCat()
        with patch("nexus.retry.time.sleep"):
            # Contract: must never raise out of the hook.
            _manifest_write_loop(cat, _by_doc(2), _COLLECTION, reader=cat)
        assert sorted(get_manifest_write_failures()) == ["1.9.0", "1.9.1"]

    def test_persistent_write_many_failure_is_swallowed_and_recorded(self) -> None:
        cat = _AlwaysDownManyCat()
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(2), _COLLECTION, reader=cat)
        assert sorted(get_manifest_write_failures()) == ["1.9.0", "1.9.1"]
        # Not re-attempted per-doc after the batch path exhausted retries.
        assert cat.replace_calls == []

    def test_reset_clears_prior_run_failures(self) -> None:
        cat = _AlwaysDownCat()
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(1), _COLLECTION, reader=cat)
        assert get_manifest_write_failures() == ["1.9.0"]
        reset_manifest_write_failures()
        assert get_manifest_write_failures() == []


class TestManifestNeverOutrunsConfirmedChunks:
    """nexus-tp8yk D1 consequence, pinned at this file's own layer (memo
    §5 TDD plan): ``_manifest_write_loop`` — and therefore the whole
    ``manifest_write_batch_hook`` chain this file otherwise drives
    directly — must be STRUCTURALLY UNREACHABLE for a batch
    ``doc_indexer._upsert_skip_reembed`` could not confirm landed. The
    gate itself lives one layer up (``_upsert_skip_reembed`` raises
    ``ChunkLandingUnverifiedError`` BEFORE ``hooks.fire_batch`` runs);
    this test proves the consequence at THIS file's call site by driving
    the real production entry point (``nexus.doc_indexer._index_document``)
    with a fake T3 that reproduces the "stale-positive probe, cannot-tell
    update response" fault, and asserting `_manifest_write_loop` sees
    zero calls.
    """

    def test_unconfirmed_batch_never_reaches_manifest_write_loop(
        self, tmp_path, monkeypatch,
    ) -> None:
        from nexus.doc_indexer import _index_document
        from nexus.errors import ChunkLandingUnverifiedError

        calls: list = []
        monkeypatch.setattr(
            "nexus.mcp_infra._manifest_write_loop",
            lambda *a, **kw: calls.append((a, kw)),
        )

        class _FakeCol:
            def get(self, where=None, include=None, limit=None, offset=None, **kw):
                return {"ids": [], "metadatas": []}

        class _FakeT3:
            def get_or_create_collection(self, name):
                return _FakeCol()

            def existing_ids(self, collection, ids):
                # Stale-positive probe: reports EVERY id present.
                return list(ids)

            def update_chunks(self, collection, ids, metadatas):
                # "Cannot tell" — the exact D1 trigger.
                return None

            def upsert_chunks_with_embeddings(self, *a, **k):
                raise AssertionError(
                    "must not be called — every id took the stale-positive "
                    "(existing_ids) branch, never the fresh-upsert branch"
                )

        def _chunks(file_path, content_hash, target_model, now_iso, corpus):
            return [(
                "chunk-0", "chunk text",
                {"content_hash": content_hash, "embedding_model": target_model,
                 "chunk_text_hash": "a" * 64, "content_type": "prose"},
            )]

        def _embed(texts, model):
            return [[0.1, 0.2]] * len(texts), model

        f = tmp_path / "doc.md"
        f.write_text("hello tp8yk")

        with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="1.9.99"), \
                patch("nexus.doc_indexer._fence_begin"), \
                patch("nexus.doc_indexer._fence_fail"):
            with pytest.raises(ChunkLandingUnverifiedError):
                _index_document(
                    f, "testowner", _chunks, _FakeT3(),
                    collection_name="docs__testowner__bge-base-en-v15-768__v1",
                    embed_fn=_embed,
                )

        assert calls == [], (
            "_manifest_write_loop must be UNREACHABLE for a batch "
            f"_upsert_skip_reembed could not confirm landed — got "
            f"{len(calls)} call(s)"
        )


# ── nexus-tgrgs / nexus-jk88j (2026-08-08): the fast branch's sweep ────────
#
# jk88j's own tripwire proposal: "assert that IF write_manifest_many is
# reachable through the writer THEN the fast branch performs the sweep."
# TestFastBranchSweep.test_jk88j_tripwire below is that test, written as a
# falsify-by-construction trap — reverting the sweep call in
# _manifest_write_loop's write_many branch (the exact landmine jk88j warns
# about: whitelisting write_manifest_many alone, without folding the sweep
# in, silently re-disables nexus-39upx for the batch path) turns it red.
#
# The remaining tests in this class prove the batch-safe guards the design
# memo (T2 nexus/design-eslkl-hook-lock-narrowing §2b/§7 T1-T2) requires:
# the shared-chash union across the WHOLE batch (a chash any doc in this
# same write_many POST still references — successfully or via an unchanged
# prior manifest on write failure — must never be swept), on top of the
# existing per-doc union guard (docs_for_chashes) and note guard.


class _FakeManyReaderWriter:
    """Stands in for the real writer AFTER nexus-67qsd's whitelist: exposes
    ``write_manifest_many`` (so the fast branch is taken), plus the batched
    reader surface (``get_manifests``) and reverse-lookup (``docs_for_chashes``)
    the fast branch's sweep needs. Same reader/writer double shape as
    ``_FakeCatalogHTTP`` in test_superseded_vector_sweep.py's kgos1 section,
    extended for the batch write path.
    """

    def __init__(
        self,
        *,
        before: dict[str, list[str]] | None = None,
        refs: dict[str, list[str]] | None = None,
        notes: list | None = None,
        failed_doc_ids: list[str] | None = None,
    ) -> None:
        from nexus.catalog.types import ManifestRow

        self._ManifestRow = ManifestRow
        self._before = before or {}
        self._refs = refs or {}
        self._notes = notes or []
        self._failed_doc_ids = failed_doc_ids or []
        self.replaced_many: list[tuple[str, list[dict]]] = []
        self.docs_for_chashes_calls: list[list[str]] = []
        self.get_manifests_calls: list[list[str]] = []

    # -- batched "before" read (nexus-tgrgs) --
    def get_manifests(self, doc_ids):
        self.get_manifests_calls.append(list(doc_ids))
        return {
            d: [self._ManifestRow(position=i, chash=h) for i, h in enumerate(self._before.get(d, []))]
            for d in doc_ids
        }

    # -- reverse lookup (union guard) --
    def docs_for_chashes(self, chashes):
        self.docs_for_chashes_calls.append(list(chashes))
        return {h: self._refs.get(h, []) for h in chashes}

    # -- note guard --
    def list_by_collection(self, collection: str) -> list:
        return list(self._notes)

    # -- the batch write itself --
    def write_manifest_many(self, docs, complete=None, *, collection):
        assert collection, "write_manifest_many called with a blank collection"
        for doc_id, chunks in docs:
            if doc_id not in self._failed_doc_ids:
                self.replaced_many.append((doc_id, chunks))
        return {"failed_doc_ids": list(self._failed_doc_ids)}

    def resync_chunk_count_cache(self, doc_id):
        pass


class TestFastBranchSweep:
    def test_jk88j_tripwire_fast_branch_sweeps_when_write_many_is_reachable(self) -> None:
        """THE tripwire. A doc whose batch drops a chash referenced by
        nothing else must have that chash deleted from T3 via the FAST
        (write_many) branch — not just the per-doc fallback. Reverting the
        sweep-fold-in fix (calling write_manifest_many without the sweep
        that follows it) makes this go red."""
        fake = _FakeManyReaderWriter(before={"doc-A": ["old1", "keep"]}, refs={})
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(fake, _metas_by_doc({"doc-A": ["keep"]}), "coll", reader=fake)

        assert fake.replaced_many, "the batch write must still happen"
        col.delete.assert_called_once()
        assert col.delete.call_args.kwargs["ids"] == ["old1"]

    def test_batch_union_guard_protects_a_chash_another_doc_in_the_SAME_batch_still_needs(self) -> None:
        """The design memo's concrete race (§2b): doc A drops chash X in
        the same write_many POST where doc B's NEW manifest still
        references X. Both writes commit in the SAME call — the fast
        branch must never sweep X, even though nothing OUTSIDE this batch
        references it either (docs_for_chashes returns {})."""
        fake = _FakeManyReaderWriter(
            before={"doc-A": ["X", "keep-a"], "doc-B": []},
            refs={},  # no external reference at all — only the batch-union guard can save X
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(
                fake,
                _metas_by_doc({"doc-A": ["keep-a"], "doc-B": ["X", "keep-b"]}),
                "coll", reader=fake,
            )

        assert len(fake.replaced_many) == 2
        col.delete.assert_not_called(), "X is still live via doc-B's new manifest in the same batch"

    def test_batch_union_guard_still_deletes_a_genuine_cross_doc_orphan(self) -> None:
        """Non-vacuity for the guard above: it must not become a blanket
        refusal that hides a real orphan sharing the sweep call."""
        fake = _FakeManyReaderWriter(
            before={"doc-A": ["X", "keep-a"], "doc-B": ["Y", "keep-b"]},
            refs={},
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(
                fake,
                _metas_by_doc({"doc-A": ["keep-a"], "doc-B": ["keep-b"]}),
                "coll", reader=fake,
            )

        col.delete.assert_called_once()
        assert sorted(col.delete.call_args.kwargs["ids"]) == ["X", "Y"]

    def test_failed_doc_write_protects_its_unchanged_prior_chash(self) -> None:
        """A doc in `failed_doc_ids` never actually replaced its manifest —
        its CURRENT live truth is still its `before` set, not the attempted
        `new` one. If doc B (failed) still has chash Y in its (unchanged)
        manifest, doc A dropping Y in the same batch must not sweep it."""
        fake = _FakeManyReaderWriter(
            before={"doc-A": ["Y", "keep-a"], "doc-B": ["Y", "keep-b"]},
            refs={},
            failed_doc_ids=["doc-B"],
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(
                fake,
                _metas_by_doc({"doc-A": ["keep-a"], "doc-B": ["keep-b"]}),
                "coll", reader=fake,
            )

        # doc-B's write failed: only doc-A actually replaced.
        assert [d for d, _ in fake.replaced_many] == ["doc-A"]
        col.delete.assert_not_called(), "doc-B's unchanged manifest still references Y"

    def test_note_guard_still_applies_on_the_fast_branch(self) -> None:
        """The manifest-less-note guard (RDR-145) must protect a note chash
        on the batch path too, not just the per-doc fallback."""
        from types import SimpleNamespace

        note = SimpleNamespace(file_path="", meta={"doc_id": "note-chash"})
        fake = _FakeManyReaderWriter(
            before={"doc-A": ["note-chash", "keep"]}, refs={}, notes=[note],
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(fake, _metas_by_doc({"doc-A": ["keep"]}), "coll", reader=fake)

        col.delete.assert_not_called()

    def test_before_read_is_batched_into_one_get_manifests_call(self) -> None:
        """nexus-tgrgs: the before-read must be ONE batched round trip
        (get_manifests), not one get_chunk_chashes call per document."""
        fake = _FakeManyReaderWriter(
            before={"doc-A": [], "doc-B": [], "doc-C": []}, refs={},
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(
                fake,
                _metas_by_doc({"doc-A": ["a"], "doc-B": ["b"], "doc-C": ["c"]}),
                "coll", reader=fake,
            )

        assert len(fake.get_manifests_calls) == 1
        assert sorted(fake.get_manifests_calls[0]) == ["doc-A", "doc-B", "doc-C"]

    def test_reverse_lookup_is_one_call_across_the_whole_batch(self) -> None:
        """Round-trip discipline: the union-guard reverse lookup must be
        ONE docs_for_chashes call across every dropped candidate in the
        batch, not one per document."""
        fake = _FakeManyReaderWriter(
            before={"doc-A": ["old-a", "keep-a"], "doc-B": ["old-b", "keep-b"]},
            refs={},
        )
        col = MagicMock()
        with patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
            _manifest_write_loop(
                fake,
                _metas_by_doc({"doc-A": ["keep-a"], "doc-B": ["keep-b"]}),
                "coll", reader=fake,
            )

        assert len(fake.docs_for_chashes_calls) == 1
        assert sorted(fake.docs_for_chashes_calls[0]) == ["old-a", "old-b"]
        col.delete.assert_called_once()
        assert sorted(col.delete.call_args.kwargs["ids"]) == ["old-a", "old-b"]


def _metas_by_doc(new_chashes_by_doc: dict[str, list[str]]) -> dict:
    """Build a ``by_doc`` batch (position-0-bearing, single-chunk-per-chash
    for simplicity) for the fast-branch sweep tests above."""
    return {
        doc_id: [
            (i, {"chunk_text_hash": h, "chunk_index": i})
            for i, h in enumerate(chashes)
        ]
        for doc_id, chashes in new_chashes_by_doc.items()
    }
