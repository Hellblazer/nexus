# SPDX-License-Identifier: AGPL-3.0-or-later
"""SC-11: store_get_many 500-ID hydration no-truncation test.

Validates that store_get_many returns exactly N entries for N input IDs
with no silent truncation at the ChromaDB MAX_QUERY_RESULTS=300 boundary.

Fan-out fix (census-found, direct fix at Sam's instruction, no bead):
``store_get_many`` used to call ``t3.get_by_id`` once per id per candidate
collection — an HTTP round trip per id even though
``/v1/vectors/store-get`` accepts batches (``MAX_BATCH_IDS=1000``,
nexus-hdx2u) and ``_ServiceCollectionStub.get(ids=...)`` already exists to
call it in batches. The fix routes through
``t3.get_or_create_collection(name).get(ids=[...])`` instead, grouped by
resolved collection, so this file's mocks now stand up a fake
``get_or_create_collection`` rather than a fake ``get_by_id``. Every test
below still proves the SAME behavioural contract the pre-fix suite pinned
(order, missing-id handling, per-id collection routing, broadcast
fallback) — only the mock shape changed to match the new call path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_stub_t3(store_by_collection: dict, call_log: list | None = None):
    """Build a mock T3 handle whose ``get_or_create_collection(name).get(ids=...)``
    mimics ``_ServiceCollectionStub.get(ids=...)``'s Chroma-shaped response
    ``{ids, documents, metadatas}``, backed by a plain dict, and records
    every batch call made against it.

    ``store_by_collection`` maps a resolved collection name to
    ``{doc_id: {"content": ..., **meta}}``. A ``"*"`` key is used as a
    fallback store for tests that only exercise a single collection and
    don't want to compute/care about the exact resolved name.

    Returns ``(mock_t3, call_log)`` — ``call_log`` accumulates
    ``(collection_name, ids_requested)`` tuples, one per batch call, in
    call order. Asserting on ``len(call_log)`` is the direct proof that
    fetches are batched rather than issued one per id.
    """
    if call_log is None:
        call_log = []

    class _FakeCollectionStub:
        def __init__(self, name: str) -> None:
            self._name = name

        def get(self, ids=None, **kwargs):
            ids = list(ids or [])
            call_log.append((self._name, ids))
            coll_store = store_by_collection.get(
                self._name, store_by_collection.get("*", {})
            )
            out_ids: list[str] = []
            docs: list[str] = []
            metas: list[dict] = []
            for doc_id in ids:
                entry = coll_store.get(doc_id)
                if entry is not None:
                    out_ids.append(doc_id)
                    docs.append(entry.get("content", ""))
                    metas.append({k: v for k, v in entry.items() if k != "content"})
            return {"ids": out_ids, "documents": docs, "metadatas": metas}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection = lambda name: _FakeCollectionStub(name)
    return mock_t3, call_log


class TestStoreGetMany500ID:
    """SC-11: 500-ID hydration produces 500 contents with no truncation."""

    def test_500_id_hydration_no_truncation(self):
        """Pass 500 IDs to store_get_many and verify the returned
        contents list has exactly 500 entries — no silent quota truncation."""
        from nexus.mcp.core import store_get_many

        n = 500
        ids = [f"doc-{i:04d}" for i in range(n)]
        fake_docs = {
            f"doc-{i:04d}": {"content": f"content for document {i}"}
            for i in range(n)
        }

        mock_t3, _ = _make_stub_t3({"*": fake_docs})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
            )

        assert isinstance(result, dict)
        assert "contents" in result
        assert "missing" in result
        assert len(result["contents"]) == n, (
            f"Expected {n} contents, got {len(result['contents'])}. "
            f"Silent truncation at ChromaDB quota boundary?"
        )
        assert len(result["missing"]) == 0
        # Verify no empty entries.
        assert all(c for c in result["contents"]), "Some contents are empty"

    def test_hydration_with_missing_ids(self):
        """IDs not found in T3 land in 'missing', not silently dropped —
        and ORDER is preserved even though the batch response omits the
        absent ids entirely (the engine's store-get response only ever
        contains rows for ids it found)."""
        from nexus.mcp.core import store_get_many

        ids = ["exists-1", "missing-1", "exists-2", "missing-2"]
        found = {"exists-1": {"content": "a"}, "exists-2": {"content": "b"}}

        mock_t3, call_log = _make_stub_t3({"*": found})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == 4  # 1:1 with input ids
        assert result["contents"][0] == "a"
        assert result["contents"][1] == ""  # missing → empty string
        assert result["contents"][2] == "b"
        assert result["contents"][3] == ""
        assert set(result["missing"]) == {"missing-1", "missing-2"}
        # All 4 ids resolved via a single batched call, not 4 separate ones.
        assert len(call_log) == 1
        assert set(call_log[0][1]) == set(ids)


class TestStoreGetManyBatching:
    """Direct proof of the fan-out fix: N ids to one collection must
    resolve via ONE batched call, not N per-id calls.

    Red-first: under the pre-fix per-id loop, ``get_or_create_collection``
    was never called at all (the loop called ``t3.get_by_id`` directly),
    so ``call_log`` would stay empty and every assertion below would fail
    — these tests are inherently coupled to, and only pass under, the
    batched call path.
    """

    def test_single_collection_one_call_for_many_ids(self):
        from nexus.mcp.core import store_get_many

        n = 50
        ids = [f"doc-{i}" for i in range(n)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        mock_t3, call_log = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == n
        assert len(result["missing"]) == 0
        # The whole point of the fix: one HTTP round trip, not fifty.
        assert len(call_log) == 1, (
            f"Expected exactly 1 batched call for {n} ids in one collection, "
            f"got {len(call_log)} — fan-out regression"
        )
        assert set(call_log[0][1]) == set(ids)

    def test_duplicate_ids_still_one_call_no_wasted_refetch(self):
        """A caller-supplied id list with repeats is deduped before the
        batch call (the result is looked up back out of a dict either
        way, so refetching a duplicate id buys nothing)."""
        from nexus.mcp.core import store_get_many

        ids = ["a", "b", "a", "c", "b"]
        store = {x: {"content": f"body-{x}"} for x in ("a", "b", "c")}
        mock_t3, call_log = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert result["contents"] == ["body-a", "body-b", "body-a", "body-c", "body-b"]
        assert len(call_log) == 1
        assert call_log[0][1] == ["a", "b", "c"]  # deduped, first-seen order


class TestStoreGetManyBatchBoundary:
    """Verify store_get_many correctly reassembles a batch call's results
    at and above the historical ChromaDB MAX_QUERY_RESULTS=300 boundary —
    proving the id-list-to-response reassembly in ``store_get_many``
    itself doesn't introduce a fresh cap. (The actual wire-level chunking
    at ``QUOTAS.MAX_RECORDS_PER_WRITE`` lives in, and is covered by tests
    for, ``_ServiceCollectionStub.get`` in ``http_vector_client.py`` —
    out of scope for this fix, which only changes what
    ``store_get_many`` calls.)
    """

    def test_300_id_boundary_no_truncation(self):
        """301 IDs must all be returned — no off-by-one at the quota boundary —
        AND the returned body for each id must be the one actually requested,
        not just a count that happens to line up.

        Code review T2 nexus/store-get-many-code-review-2026-08-21 [23303]
        Item 2: a count-only version of this test (``len(contents) == n``,
        ``len(missing) == 0``) still PASSES against the reverted pre-fix
        per-id loop, because that loop calls ``t3.get_by_id`` — an attribute
        this test's stub (built around ``get_or_create_collection``) never
        configures, so it auto-resolves to an unconfigured Mock call that
        returns a truthy garbage Mock for every id. Every id looks "found",
        satisfying both count assertions with content nobody checked. The
        per-id content-equality assertion below is what actually falls over
        when reverted: garbage-Mock ``str(entry.get("content") or "")``
        never equals ``f"body-{i}"``.
        """
        from nexus.mcp.core import store_get_many

        n = 301  # one above the ChromaDB MAX_QUERY_RESULTS cap
        ids = [f"doc-{i:04d}" for i in range(n)]
        store = {doc_id: {"content": f"body-{i}"} for i, doc_id in enumerate(ids)}
        mock_t3, _ = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == n, (
            f"Expected {n} contents at quota boundary, got {len(result['contents'])}"
        )
        assert len(result["missing"]) == 0
        # Content-value check, not just count — see docstring above.
        assert result["contents"] == [f"body-{i}" for i in range(n)]

    def test_all_missing_above_boundary(self):
        """301 IDs that are all absent land in 'missing', not silently dropped."""
        from nexus.mcp.core import store_get_many

        n = 301
        ids = [f"absent-{i}" for i in range(n)]
        mock_t3, _ = _make_stub_t3({"*": {}})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=True)

        assert len(result["contents"]) == n
        assert all(c == "" for c in result["contents"])
        assert len(result["missing"]) == n


class TestStoreGetManyTruncation:
    """max_chars_per_doc must apply identically regardless of the batched
    fetch path."""

    def test_max_chars_per_doc_truncates_batched_content(self):
        from nexus.mcp.core import store_get_many

        long_body = "x" * 100
        store = {"doc-1": {"content": long_body}}
        mock_t3, _ = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["doc-1"],
                collections="knowledge",
                structured=True,
                max_chars_per_doc=10,
            )

        from nexus.mcp.core import display_truncation_marker

        # nexus-lugwx: the cut announces itself where the model reads it.
        assert result["contents"][0] == (
            ("x" * 10) + "…" + display_truncation_marker(10)
        )
        assert "NOT a defect" in result["contents"][0]

    def test_untruncated_content_carries_no_marker(self):
        from nexus.mcp.core import store_get_many

        store = {"doc-1": {"content": "x" * 10}}
        mock_t3, _ = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["doc-1"], collections="knowledge", structured=True,
                max_chars_per_doc=10,
            )

        assert result["contents"][0] == "x" * 10


class TestStoreGetManyMultiCollectionRouting:
    """Multi-collection candidate routing: broadcast (try each candidate
    collection in order, first match wins) must still resolve an id found
    only in a LATER candidate collection, and must do so via one batched
    call per candidate collection rather than per (id, collection) pair."""

    def test_id_found_only_in_second_candidate_collection(self):
        """3 ids against 2 candidate collections — deliberately NOT equal
        counts, so this unambiguously exercises the broadcast branch
        (``len(coll_list) == len(id_list)`` is the pre-existing, unchanged
        disambiguator between per-id 1:1 routing and broadcast; an equal
        count would coincidentally read as per-id routing instead)."""
        from nexus.mcp.core import store_get_many, t3_collection_name

        alpha = t3_collection_name("knowledge__alpha")
        beta = t3_collection_name("knowledge__beta")
        store_by_collection = {
            alpha: {"only-in-alpha": {"content": "alpha body"}},
            beta: {"only-in-beta": {"content": "beta body"}},
        }
        mock_t3, call_log = _make_stub_t3(store_by_collection)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["only-in-alpha", "only-in-beta", "in-neither"],
                collections="knowledge__alpha,knowledge__beta",
                structured=True,
            )

        assert result["contents"] == ["alpha body", "beta body", ""]
        assert result["missing"] == ["in-neither"]
        # Broadcast semantics preserved: alpha is tried first for every
        # still-unresolved id (one batched call with all 3), then only
        # the ids still missing after alpha ("only-in-beta", "in-neither")
        # are retried against beta — not a second full pass over all 3.
        assert len(call_log) == 2
        assert call_log[0] == (
            alpha,
            ["only-in-alpha", "only-in-beta", "in-neither"],
        )
        assert call_log[1] == (beta, ["only-in-beta", "in-neither"])

    def test_id_missing_from_every_candidate_collection(self):
        from nexus.mcp.core import store_get_many, t3_collection_name

        alpha = t3_collection_name("knowledge__alpha")
        beta = t3_collection_name("knowledge__beta")
        mock_t3, call_log = _make_stub_t3({alpha: {}, beta: {}})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["nowhere"],
                collections="knowledge__alpha,knowledge__beta",
                structured=True,
            )

        assert result["contents"] == [""]
        assert result["missing"] == ["nowhere"]
        assert len(call_log) == 2  # tried both candidates, found in neither

    def test_parallel_ids_with_parallel_collections_aligns(self):
        """Per-id (1:1) explicit routing: ids are grouped by their
        assigned collection and each group is fetched in one batched
        call — not one call per id."""
        from nexus.mcp.core import store_get_many, t3_collection_name

        stream_a = ["a1", "a2"]
        stream_b = ["b1", "b2"]
        alpha = t3_collection_name("knowledge__alpha")
        beta = t3_collection_name("knowledge__beta")
        store_by_collection = {
            alpha: {"a1": {"content": "body-a1"}, "a2": {"content": "body-a2"}},
            beta: {"b1": {"content": "body-b1"}, "b2": {"content": "body-b2"}},
        }
        mock_t3, call_log = _make_stub_t3(store_by_collection)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections=["knowledge__alpha", "knowledge__beta"],
                structured=True,
            )

        assert len(result["contents"]) == 4
        assert result["contents"] == ["body-a1", "body-a2", "body-b1", "body-b2"]
        # One batched call per assigned collection — 2 calls, not 4.
        assert len(call_log) == 2
        assert (alpha, ["a1", "a2"]) in call_log
        assert (beta, ["b1", "b2"]) in call_log

    def test_parallel_ids_with_scalar_collections_broadcasts(self):
        from nexus.mcp.core import store_get_many

        stream_a = ["a1"]
        stream_b = ["b1"]
        store = {"a1": {"content": "body-a1"}, "b1": {"content": "body-b1"}}
        mock_t3, _ = _make_stub_t3({"*": store})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
            )
        assert len(result["contents"]) == 2
        assert result["contents"] == ["body-a1", "body-b1"]


class TestStoreGetManyCollectionBatchFailureDegradesGracefully:
    """A whole-batch failure for one candidate collection (network error,
    unknown collection, ...) must never crash the caller and never poison
    the other candidate collections.

    Blast radius (code review T2 nexus/store-get-many-code-review-
    2026-08-21 [23303] Item 1): a batch failure does NOT simply blank up
    to 300 ids as "missing" the way an earlier version of this fix did —
    ``_batched_get_by_ids`` logs the failure loud and falls back to a
    per-id retry via ``HttpVectorClient.get_by_id`` (the same primitive
    the pre-fix loop used directly), restoring the old one-id blast
    radius for exactly the failure path."""

    def test_one_collection_raising_falls_through_to_next_candidate(self):
        from nexus.mcp.core import store_get_many, t3_collection_name

        alpha = t3_collection_name("knowledge__alpha")
        beta = t3_collection_name("knowledge__beta")

        class _RaisingCollection:
            def get(self, ids=None, **kwargs):
                raise RuntimeError("simulated transient failure")

        class _WorkingCollection:
            def get(self, ids=None, **kwargs):
                ids = list(ids or [])
                return {
                    "ids": ids,
                    "documents": [f"body-{i}" for i in ids],
                    "metadatas": [{} for _ in ids],
                }

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection = lambda name: (
            _RaisingCollection() if name == alpha else _WorkingCollection()
        )
        # The batch call to alpha raises; the per-id fallback then also
        # tries alpha via get_by_id — genuinely not found there either
        # (unconfigured MagicMock.get_by_id would return truthy garbage
        # instead of None, masking the fall-through to beta this test
        # means to prove, so it must be configured explicitly).
        mock_t3.get_by_id = lambda col, doc_id: None

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["x"],
                collections="knowledge__alpha,knowledge__beta",
                structured=True,
            )

        assert result["contents"] == ["body-x"]
        assert result["missing"] == []

    def test_batch_failure_logs_loud_with_collection_and_id_count(self):
        """A batch-level failure must be visible in the log — the
        difference between 'genuinely absent' and 'the batch call to
        fetch it failed' must not be silent (code review Item 1)."""
        from structlog.testing import capture_logs

        from nexus.mcp.core import store_get_many, t3_collection_name

        alpha = t3_collection_name("knowledge__alpha")

        class _RaisingCollection:
            def get(self, ids=None, **kwargs):
                raise RuntimeError("simulated outage")

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection = lambda name: _RaisingCollection()
        mock_t3.get_by_id = lambda col, doc_id: None  # fallback also empty

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            with capture_logs() as cap:
                store_get_many(
                    ids=["a", "b", "c"], collections="knowledge__alpha",
                    structured=True,
                )

        matches = [
            e for e in cap
            if e.get("event") == "store_get_many_batch_fetch_failed"
        ]
        assert len(matches) == 1, f"expected exactly one failure log, got {cap}"
        assert matches[0]["collection"] == alpha
        assert matches[0]["id_count"] == 3

    def test_batch_failure_falls_back_to_per_id_restoring_blast_radius(self):
        """When the batch call fails outright but the underlying per-id
        primitive still works (a plausible real asymmetry — e.g. a
        malformed-batch-payload edge case that a single-id request never
        hits), every id must still resolve via the per-id fallback —
        proving the fix does NOT blank the whole batch on one failed
        request the way a bare 'except Exception: return {}' would."""
        from nexus.mcp.core import store_get_many, t3_collection_name

        alpha = t3_collection_name("knowledge__alpha")
        store = {f"doc-{i}": {"content": f"body-{i}"} for i in range(10)}

        class _RaisingCollection:
            def get(self, ids=None, **kwargs):
                raise RuntimeError("simulated batch-endpoint glitch")

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection = lambda name: _RaisingCollection()
        mock_t3.get_by_id = lambda col, doc_id: store.get(doc_id)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=list(store.keys()),
                collections="knowledge__alpha",
                structured=True,
            )

        assert result["contents"] == [f"body-{i}" for i in range(10)]
        assert result["missing"] == []


class TestStoreGetManyLimitPerSource:
    """RDR-097 P1.0: ``limit_per_source`` truncation kwarg.

    Three input shapes:
      - ``None`` (default): no truncation; preserves existing behavior.
      - ``int``: truncate single-stream ``ids`` to first N.
      - ``list[int]``: pair with parallel-stream ``ids`` (``list[list[str]]``);
        truncate each stream to its corresponding limit, then flatten.
    """

    def _make_t3(self, store: dict[str, dict]):
        mock_t3, _ = _make_stub_t3({"*": store})
        return mock_t3

    def test_limit_per_source_none_preserves_default(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(10)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=ids, collections="knowledge", structured=True
            )
        assert len(result["contents"]) == 10
        assert len(result["missing"]) == 0

    def test_limit_per_source_int_truncates_single_stream(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(20)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        mock_t3 = self._make_t3(store)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=5,
            )

        assert len(result["contents"]) == 5
        assert result["contents"] == [f"body-doc-{i}" for i in range(5)]
        assert len(result["missing"]) == 0

    def test_limit_per_source_zero_returns_empty(self):
        from nexus.mcp.core import store_get_many

        ids = [f"doc-{i}" for i in range(5)]
        store = {doc_id: {"content": f"body-{doc_id}"} for doc_id in ids}
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=0,
            )
        assert result["contents"] == []
        assert result["missing"] == []

    def test_limit_per_source_negative_raises_valueerror(self):
        from nexus.mcp.core import store_get_many

        ids = ["doc-1", "doc-2", "doc-3"]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=-1,
            )
        assert "error" in result
        assert "limit_per_source" in result["error"]
        assert "negative" in result["error"].lower() or "non-negative" in result["error"].lower()

    def test_limit_per_source_list_truncates_parallel_streams(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(3)]
        store = {
            **{x: {"content": f"body-{x}"} for x in stream_a},
            **{x: {"content": f"body-{x}"} for x in stream_b},
        }
        mock_t3 = self._make_t3(store)

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=[2, 1],
            )

        # Stream-major flatten: [a0, a1] then [b0]
        assert len(result["contents"]) == 3
        assert result["contents"] == ["body-a0", "body-a1", "body-b0"]
        assert result["missing"] == []

    def test_limit_per_source_list_with_single_stream_ids_raises(self):
        from nexus.mcp.core import store_get_many

        ids = ["doc-1", "doc-2", "doc-3"]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=ids,
                collections="knowledge",
                structured=True,
                limit_per_source=[2],
            )
        assert "error" in result
        assert "parallel" in result["error"].lower()

    def test_limit_per_source_list_length_mismatch_raises_valueerror(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(3)]
        with patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=[2],
            )
        assert "error" in result
        msg = result["error"].lower()
        assert "1" in result["error"] and "2" in result["error"]
        assert "length" in msg or "stream" in msg

    def test_limit_per_source_int_with_parallel_ids_broadcasts(self):
        from nexus.mcp.core import store_get_many

        stream_a = [f"a{i}" for i in range(4)]
        stream_b = [f"b{i}" for i in range(4)]
        store = {
            **{x: {"content": f"body-{x}"} for x in stream_a},
            **{x: {"content": f"body-{x}"} for x in stream_b},
        }
        with patch("nexus.mcp.core._get_t3", return_value=self._make_t3(store)):
            result = store_get_many(
                ids=[stream_a, stream_b],
                collections="knowledge",
                structured=True,
                limit_per_source=2,
            )

        assert len(result["contents"]) == 4
        assert result["contents"] == ["body-a0", "body-a1", "body-b0", "body-b1"]


class TestStoreGetManyHumanReadableRendersContent:
    """nexus-z4j8d: the human-readable mode (``structured=False``) of a
    HYDRATION tool must actually render hydrated content, not just a
    ``Hydrated N/N docs`` count. Pre-fix, ``structured=True`` was
    effectively mandatory to see any text."""

    def test_renders_each_documents_content(self):
        from nexus.mcp.core import store_get_many

        ids = ["doc-1", "doc-2"]
        found = {
            "doc-1": {"content": "alpha content body"},
            "doc-2": {"content": "beta content body"},
        }
        mock_t3, _ = _make_stub_t3({"*": found})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=False)

        assert isinstance(result, str)
        assert "Hydrated 2/2 docs" in result
        assert "doc-1" in result
        assert "alpha content body" in result
        assert "doc-2" in result
        assert "beta content body" in result
        # The doc-1 body must appear before the doc-2 body (order preserved).
        assert result.index("alpha content body") < result.index("beta content body")

    def test_renders_missing_ids_without_fabricating_content(self):
        from nexus.mcp.core import store_get_many

        ids = ["exists-1", "missing-1"]
        found = {"exists-1": {"content": "real body"}}
        mock_t3, _ = _make_stub_t3({"*": found})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=False)

        assert "Hydrated 1/2 docs" in result
        assert "real body" in result
        assert "Missing: missing-1" in result

    def test_all_missing_has_no_content_blocks(self):
        from nexus.mcp.core import store_get_many

        ids = ["missing-1", "missing-2"]
        mock_t3, _ = _make_stub_t3({"*": {}})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(ids=ids, collections="knowledge", structured=False)

        assert "Hydrated 0/2 docs" in result
        assert "Missing: missing-1, missing-2" in result

    def test_truncation_marker_visible_in_human_mode(self):
        from nexus.mcp.core import store_get_many

        long_body = "x" * 100
        found = {"doc-1": {"content": long_body}}
        mock_t3, _ = _make_stub_t3({"*": found})

        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=["doc-1"], collections="knowledge",
                structured=False, max_chars_per_doc=10,
            )

        assert "Hydrated 1/1 docs" in result
        assert "x" * 10 in result
        # nexus-lugwx: the cut must be visibly marked, never a bare
        # truncation with no signal.
        assert "…" in result
        assert "x" * 100 not in result
