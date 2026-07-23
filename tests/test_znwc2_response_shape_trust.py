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


# ── 2. manifest/orphans: count is REQUIRED (feeds a migration gate) ──────────


class TestManifestOrphansCountRequired:
    def _client_with(self, monkeypatch: pytest.MonkeyPatch, response: Any):
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr(
            c, "_get", lambda path, **params: response, raising=False,
        )
        return c

    def test_missing_count_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The P3 validation gate sums count; a stripped field defaulting to 0
        would be a vacuous PASS (sibling relation_counts already fails
        closed — this makes the pair consistent)."""
        c = self._client_with(monkeypatch, {"dim": 768, "orphans": []})
        with pytest.raises(RuntimeError, match="count"):
            c.manifest_orphans(768)

    def test_intact_response_passes_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = self._client_with(
            monkeypatch, {"dim": 768, "count": 3, "orphans": [{"chash": "x"}]},
        )
        result = c.manifest_orphans(768)
        assert result["count"] == 3


# ── 3. ingest-cloud parity: missing copied/dest is NOT 0 == 0 ────────────────


class TestIngestCloudParityRequiresCounts:
    def _run(self, per_collection: dict) -> tuple[list, list[str]]:
        from nexus.migration.vector_etl import _delegate_ingest_cloud

        class _Resp:
            text = ""

            def __init__(self, body: dict, status_code: int) -> None:
                self._body = body
                self.status_code = status_code

            def json(self) -> dict:
                return self._body

        class _Client:
            def post(self, url: str, **kw: Any) -> Any:
                return _Resp({"job_id": "j1"}, 202)

            def get(self, url: str, **kw: Any) -> Any:
                return _Resp(
                    {"state": "done", "per_collection": per_collection}, 200,
                )

            def close(self) -> None:
                pass

        return _delegate_ingest_cloud(
            ["knowledge__k__stub-cce-1024__v1"],
            tenant="t", database="d", api_key="k", base_url="http://x",
            token="tok", nexus_tenant="default", http_client=_Client(),
            sleep=lambda s: None, now=lambda: 0.0,
        )

    def test_stripped_counts_route_to_fallback(self) -> None:
        """An entry with copied/dest stripped must NOT pass parity on the
        0 == 0 defaults and get certified 'migrated' with zero evidence —
        it routes to the client-mediated fallback leg."""
        results, fallback = self._run(
            {"knowledge__k__stub-cce-1024__v1": {"status": "done"}},
        )
        assert results == []
        assert fallback == ["knowledge__k__stub-cce-1024__v1"]

    def test_intact_counts_still_delegate(self) -> None:
        results, fallback = self._run(
            {"knowledge__k__stub-cce-1024__v1": {"copied": 5, "dest": 5}},
        )
        assert fallback == []
        assert len(results) == 1
        assert results[0].collection == "knowledge__k__stub-cce-1024__v1"


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
    def test_remap_record_batch_missing_ack_raises(self) -> None:
        """record_batch's own contract: a map fact that did not durably land
        must abort before the target write. A response without `recorded`
        cannot attest durability — raising beats fabricating len(page)."""
        from nexus.migration.remap_client import HttpRemapStore
        from nexus.migration.wire_reid import RemapEntry

        store = object.__new__(HttpRemapStore)
        store._post = lambda path, body: {}  # stripped ack
        entry = RemapEntry(
            tenant_id="t", source_collection="s", old_id="o",
            new_chash="c" * 64, target_collection="tc", provenance="p",
        )
        with pytest.raises(RuntimeError, match="recorded"):
            store.record_batch([entry])

    def test_remap_record_batch_intact_ack_counts(self) -> None:
        from nexus.migration.remap_client import HttpRemapStore
        from nexus.migration.wire_reid import RemapEntry

        store = object.__new__(HttpRemapStore)
        store._post = lambda path, body: {"recorded": len(body["entries"])}
        entry = RemapEntry(
            tenant_id="t", source_collection="s", old_id="o",
            new_chash="c" * 64, target_collection="tc", provenance="p",
        )
        assert store.record_batch([entry]) == 1

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
