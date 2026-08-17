"""nexus-znwc2: response-shape trust hardening regressions.

The 2026-07-23 audit (T2 [21080], nexus-bwulw follow-on) found client
sites that treat a MISSING engine-response field as success or safe-zero.
The conexus edge stubbing /version proved a field-stripping middleman is
a real production topology, not a hypothetical: any hop that strips or
synthesizes a response body must degrade LOUD (or fail-closed), never
read as "everything worked".

One test class per audited site; each pins the stripped-field behavior
AND the intact-field control so the fix can never regress silently in
either direction.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from nexus.db.http_vector_client import HttpVectorClient


def _client() -> HttpVectorClient:
    return HttpVectorClient()


# ── 1. rerank envelope: absence-of-flag is NOT success ───────────────────────


class TestRerankEnvelopePositiveAck:
    def _search(self, monkeypatch: pytest.MonkeyPatch, response: Any) -> dict:
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, *, tenant="default", timeout=120: response,
        )
        meta: dict = {}
        _client().search(
            "q", ["code__x__stub-code-1024__v1"], rerank=True, rerank_meta_out=meta,
        )
        return meta

    def test_envelope_without_degrade_flag_reports_degraded(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An object envelope with results but NO rerank_degraded key cannot
        attest rerank ran — a field-stripping middleman must read as
        degraded+unknown, never as 'server reranked'."""
        meta = self._search(monkeypatch, {"results": [{"id": "a", "distance": 0.1}]})
        assert meta["degraded"] is True
        assert "rerank_degraded" in (meta.get("error") or "")

    def test_intact_envelope_still_reports_reranked(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        meta = self._search(monkeypatch, {
            "results": [{"id": "a", "distance": 0.1}],
            "rerank_degraded": False,
            "rerank_model": "rerank-2.5",
        })
        assert meta["degraded"] is False
        assert meta["model"] == "rerank-2.5"

    def test_intact_degraded_envelope_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        meta = self._search(monkeypatch, {
            "results": [],
            "rerank_degraded": True,
            "rerank_error": "no reranker configured",
        })
        assert meta["degraded"] is True
        assert meta["error"] == "no reranker configured"


# TestManifestOrphansCountRequired DELETED (RDR-191 Phase 6, nexus-o8dil.33):
# manifest_orphans (client method + route + SQL function) is retired
# entirely — the manifest-chunk FK makes the dangling state it detected
# unreachable. The response-shape-trust class this pinned ("a stripped
# `count` field defaulting to 0 would be a vacuous PASS") no longer has a
# subject: there is no method left to strip a field from.


# ── 3. manifest/chashes: count reconciled before orphan classification ───────


class TestManifestChashesCountReconciled:
    """nexus-ir6eh client half: the manifest chashes list is the GC's
    alive-set — chunks absent from it are classified orphan and DELETED.
    A partially-truncated list therefore destroys live data silently.
    The engine emits ``count`` since v0.1.55 (floor (0,1,58) >= that), so
    the client reconciles ``len(chashes) == count`` and a missing count
    field is itself a contract violation (fail loud, never optional)."""

    def _client_with(self, monkeypatch: pytest.MonkeyPatch, response: Any):
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr(
            c, "_get", lambda path, **params: response, raising=False,
        )
        return c

    def test_missing_count_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Floor >= (0,1,55): the engine emits count unconditionally, so a
        response without it means a field-stripping hop — refuse rather
        than hand GC an unverifiable alive-set."""
        c = self._client_with(monkeypatch, {"chashes": ["a" * 64]})
        with pytest.raises(RuntimeError, match="count"):
            c.chashes_for_collection("code__x__stub-code-1024__v1")

    def test_truncated_list_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._client_with(
            monkeypatch, {"chashes": ["a" * 64, "b" * 64], "count": 3},
        )
        with pytest.raises(RuntimeError, match="chashes"):
            c.chashes_for_collection("code__x__stub-code-1024__v1")

    def test_intact_response_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._client_with(
            monkeypatch, {"chashes": ["a" * 64, "b" * 64], "count": 2},
        )
        assert c.chashes_for_collection("code__x__stub-code-1024__v1") == {
            "a" * 64, "b" * 64,
        }

    def test_empty_intact_response_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """count=0 with an empty list is a legitimate empty manifest — the
        indexer's manifest_empty_skipping_gc guard handles it downstream."""
        c = self._client_with(monkeypatch, {"chashes": [], "count": 0})
        assert c.chashes_for_collection("code__x__stub-code-1024__v1") == set()


# ── 3b. manifest/docs_for_chashes: count reconciled + paged (nexus-ocf52) ────


class TestDocsForChashesCountReconciled:
    """nexus-ocf52 client half: ``/manifest/docs_for_chashes`` is the
    superseded-vector sweep's union guard — a chash is hard-deleted from T3
    iff no tumbler here references it, so a partially-delivered ``tumblers``
    list would silently destroy a live shared row. The engine emits
    ``count`` unconditionally (floor >= v0.1.61); the client reconciles
    ``len(tumblers) == count`` PER PAGE before any tumblers are trusted, and
    pages the round-1 POST at 1000 chashes (nexus-uu4b9, mirrors the
    engine's ``MAX_BATCH_DOC_IDS`` cap on ``handleDocsForChashes``)."""

    def _client_with(
        self,
        monkeypatch: pytest.MonkeyPatch,
        post: Any,
        get_manifests: Any = None,
    ):
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        c = object.__new__(HttpCatalogClient)
        if callable(post):
            monkeypatch.setattr(c, "_post", post, raising=False)
        else:
            monkeypatch.setattr(
                c, "_post", lambda path, body=None: post, raising=False,
            )
        if callable(get_manifests):
            monkeypatch.setattr(c, "get_manifests", get_manifests, raising=False)
        else:
            monkeypatch.setattr(
                c, "get_manifests",
                lambda doc_ids: get_manifests if get_manifests is not None else {},
                raising=False,
            )
        return c

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.catalog.types import ManifestRow

        chash = "a" * 64
        c = self._client_with(
            monkeypatch,
            post={"tumblers": ["1.1.1"], "count": 1},
            get_manifests={
                "1.1.1": [ManifestRow(position=0, chash=chash)],
            },
        )
        assert c.docs_for_chashes([chash]) == {chash: ["1.1.1"]}

    def test_missing_count_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._client_with(monkeypatch, post={"tumblers": ["1.1.1"]})
        with pytest.raises(RuntimeError, match="count"):
            c.docs_for_chashes(["a" * 64])

    def test_mismatched_count_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._client_with(
            monkeypatch, post={"tumblers": ["1.1.1"], "count": 2},
        )
        with pytest.raises(RuntimeError, match="tumblers"):
            c.docs_for_chashes(["a" * 64])

    def test_empty_list_with_missing_count_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordering pin: the count guard runs BEFORE the ``not tumblers``
        early-out — a stripped-field EMPTY response must fail loud, never
        slip through indistinguishable from a genuine 'nothing found'."""
        c = self._client_with(monkeypatch, post={"tumblers": []})
        with pytest.raises(RuntimeError, match="count"):
            c.docs_for_chashes(["a" * 64])

    def test_paging_2500_chashes_three_posts_per_batch_reconciled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2500 chashes -> 3 POSTs (1000 + 1000 + 500), each page's count
        reconciled independently, and the resulting tumblers unioned across
        pages before the second round-trip."""
        chashes = [f"{i:064x}" for i in range(2500)]
        posts: list[list[str]] = []
        manifests_calls: list[list[str]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/manifest/docs_for_chashes"
            batch = body["chashes"]
            posts.append(batch)
            base = sum(len(p) for p in posts[:-1])
            tumblers = [f"1.1.{base + j}" for j in range(len(batch))]
            return {"tumblers": tumblers, "count": len(tumblers)}

        def _fake_get_manifests(doc_ids: list[str]) -> dict:
            manifests_calls.append(doc_ids)
            return {}

        c = self._client_with(
            monkeypatch, post=_fake_post, get_manifests=_fake_get_manifests,
        )
        c.docs_for_chashes(chashes)

        assert len(posts) == 3
        assert [len(p) for p in posts] == [1000, 1000, 500]
        assert all(len(p) <= 1000 for p in posts)
        assert len(manifests_calls) == 1
        assert sorted(manifests_calls[0]) == sorted(
            f"1.1.{i}" for i in range(2500)
        )


# ── 4. merge sort: missing distance sorts LAST, never first ──────────────────


class TestDistanceKeySentinel:
    def test_missing_and_none_distance_sort_last(self) -> None:
        from nexus.mcp.core import _distance_key

        rows = [{"id": "no-dist"}, {"id": "none", "distance": None},
                {"id": "near", "distance": 0.1}, {"id": "far", "distance": 0.9}]
        ordered = sorted(rows, key=_distance_key)
        assert [r["id"] for r in ordered][:2] == ["near", "far"]
        assert math.isinf(_distance_key({"id": "x"}))
        assert math.isinf(_distance_key({"id": "x", "distance": None}))

    def test_structured_search_missing_distance_is_none_not_zero(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The plan-runner structured form must not promote a distance-less
        row to best-match (0.0). The emitted value is an honest None —
        NEVER float('inf'), which the MCP text serializer renders as the
        bare `Infinity` token (invalid JSON for strict clients; reviewer
        H1). +inf exists only inside the sort key."""
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, *, tenant="default", timeout=120: [
                {"id": "a", "collection": "c"},
            ],
        )
        out = _client().search("q", ["c"], structured=True)
        assert out["distances"][0] is None

    def test_reported_distances_helper_emits_none_never_zero_or_inf(self) -> None:
        """The shared emitter behind search_metadata_scoped /
        search_topic_scoped / search_graph_hop / query() structured outputs
        (nexus-3809x: these four sites previously fabricated 0.0)."""
        from nexus.mcp.core import _reported_distances

        out = _reported_distances(
            [{"distance": 0.4}, {"id": "stripped"}, {"distance": None}],
        )
        assert out == [0.4, None, None]

    def test_no_fabricated_zero_distance_sites_remain(self) -> None:
        """Census tripwire (nexus-3809x): the defect pattern
        `get("distance", 0.0)` must never reappear in the MCP merge/emit
        layer or the vector client — it fabricates a perfect-match score
        for a stripped field."""
        import pathlib

        import nexus.db.http_vector_client as hvc
        import nexus.mcp.core as core

        for mod in (core, hvc):
            src = pathlib.Path(mod.__file__).read_text()
            assert 'get("distance", 0.0)' not in src, mod.__name__


# ── 5. write-acks: missing ack field is never assumed-durable ────────────────


class TestWriteAckNotAssumed:
    @pytest.mark.parametrize("method", ["log_relevance_batch", "log_search_batch"])
    def test_telemetry_batch_missing_ack_counts_zero(self, method: str) -> None:
        """Telemetry is advisory — missing `inserted` reads as 0 (visible
        undercount), never fabricated len(rows)."""
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

        store = object.__new__(HttpTelemetryStore)
        store._post = lambda path, body: {}
        rows = [("a", "b", "c", 1, 1, 0.1, 0.5)]
        assert getattr(store, method)(rows) == 0

    @pytest.mark.parametrize("method", ["log_relevance_batch", "log_search_batch"])
    def test_telemetry_batch_intact_ack_passes(self, method: str) -> None:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

        store = object.__new__(HttpTelemetryStore)
        store._post = lambda path, body: {"inserted": 1}
        rows = [("a", "b", "c", 1, 1, 0.1, 0.5)]
        assert getattr(store, method)(rows) == 1


# ── 6. upsert-chunks: ack reconciled against ids sent (nexus-ir6eh half) ─────


class TestUpsertChunksAckReconciled:
    def _upsert(self, monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, *, tenant="default", timeout=120: response,
        )
        _client().upsert_chunks(
            "code__x__stub-code-1024__v1",
            ids=["a" * 64, "b" * 64],
            documents=["doc a", "doc b"],
            metadatas=[{}, {}],
        )

    def test_missing_ack_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The engine echoes ids.length as `upserted` unconditionally
        (VectorHandler); a response without it means something interposed
        on the WRITE path — refuse rather than assume the data landed."""
        with pytest.raises(RuntimeError, match="upsert"):
            self._upsert(monkeypatch, {})

    def test_mismatched_ack_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(RuntimeError, match="upsert"):
            self._upsert(monkeypatch, {"upserted": 1})

    def test_intact_ack_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._upsert(monkeypatch, {"upserted": 2})
