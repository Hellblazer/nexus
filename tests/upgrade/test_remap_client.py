# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpRemapStore (RDR-186 nexus-146xx.6, client half).

httpx.MockTransport idiom (mirrors tests/db/test_http_telemetry_store_probe_ids.py):
this file pins the CLIENT's wire contract only — request shapes, paging,
fail-loud propagation, the torn-read reconcile, and the composite read.
Server-side semantics live in service/src/test/java RemapHandlerTest /
RemapMembershipFunctionTest.

Pinned here (the .6 critic design inputs):
  - record_batch groups per source_collection and pages at 300/POST (the
    chroma_quotas MAX_RECORDS_PER_WRITE heritage the endpoint enforces)
  - all_pairs is COUNT-RECONCILED: total before, paged fetch to exhaustion,
    total after; any mismatch raises RemapReadTornError — could-not-tell is
    LOUD, never a silently short list (no-silent-fallback directive)
  - all_pairs zero-count short-circuit: no /pairs call at all
  - FAIL-LOUD: transport errors and non-2xx propagate as exceptions
  - CompositeReadMapStore unions engine + read-only local facts so a
    pre-seed install (facts only in chash_remap.db) never probes falsely
    clean
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nexus.migration.remap_client import (
    SEEDED_SUFFIX,
    CompositeReadMapStore,
    HttpRemapStore,
    RemapReadTornError,
    seed_and_quarantine,
    seed_local_map,
)
from nexus.migration.wire_reid import ChashRemapStore, RemapEntry

TOKEN = "fake-remap-token"


def _store_with_handler(handler) -> HttpRemapStore:
    store = HttpRemapStore(base_url="http://svc", _token=TOKEN)
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    return store


def _entry(old: str, source: str = "src__a", new: str | None = None,
           target: str = "tgt__a") -> RemapEntry:
    return RemapEntry(
        tenant_id="",
        source_collection=source,
        old_id=old,
        new_chash=(new or "0" * 32),
        target_collection=target,
        provenance="test",
    )


# ── record_batch ───────────────────────────────────────────────────────────────


def test_record_batch_posts_grouped_by_source_collection():
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/remap/record_batch"
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"recorded": 1})

    store = _store_with_handler(handler)
    store.record_batch([
        _entry("a-1", source="src__a"),
        _entry("b-1", source="src__b"),
        _entry("a-2", source="src__a"),
    ])

    assert len(posts) == 2, "one POST per source_collection group"
    by_source = {p["source_collection"]: p for p in posts}
    assert set(by_source) == {"src__a", "src__b"}
    assert [e["old_id"] for e in by_source["src__a"]["entries"]] == ["a-1", "a-2"]
    assert [e["old_id"] for e in by_source["src__b"]["entries"]] == ["b-1"]
    entry = by_source["src__b"]["entries"][0]
    assert set(entry) == {"old_id", "new_chash", "target_collection", "provenance"}


def test_record_batch_pages_at_300_per_post():
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sizes.append(len(body["entries"]))
        return httpx.Response(200, json={"recorded": len(body["entries"])})

    store = _store_with_handler(handler)
    store.record_batch([_entry(f"id-{i}") for i in range(650)])

    assert sizes == [300, 300, 50], "pages at the 300-entry endpoint cap"


def test_record_batch_empty_is_noop_no_http():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP call expected for an empty batch")

    _store_with_handler(handler).record_batch([])


def test_record_batch_http_error_propagates_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    store = _store_with_handler(handler)
    with pytest.raises(Exception, match="record_batch|500"):
        store.record_batch([_entry("a-1")])


# ── clear_leg / membership / total_count ───────────────────────────────────────


def test_clear_leg_posts_required_pair_and_returns_deleted():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"deleted": 3})

    store = _store_with_handler(handler)
    assert store.clear_leg("src__a", "tgt__a") == 3
    assert captured["path"] == "/v1/remap/clear_leg"
    assert captured["json"] == {"source_collection": "src__a", "target_collection": "tgt__a"}


def test_membership_returns_count_pair():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/remap/membership"
        assert request.url.params["source_collection"] == "src__a"
        assert request.url.params["target_collection"] == "tgt__a"
        return httpx.Response(200, json={"mapped_total": 5, "present_count": 4})

    store = _store_with_handler(handler)
    assert store.membership("src__a", "tgt__a") == (5, 4)


def test_total_count_optional_source_filter():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"total": 7})

    store = _store_with_handler(handler)
    assert store.total_count() == 7
    assert store.total_count("src__a") == 7
    assert seen == [{}, {"source_collection": "src__a"}]


# ── all_pairs: paged, count-reconciled, zero short-circuit ─────────────────────


def _paged_handler(pairs: list[list[str]], totals: list[int]):
    """A handler serving /count from *totals* (popped per call) and /pairs
    pages from *pairs*."""
    calls = {"count": 0, "pairs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/remap/count":
            total = totals[min(calls["count"], len(totals) - 1)]
            calls["count"] += 1
            return httpx.Response(200, json={"total": total})
        assert request.url.path == "/v1/remap/pairs"
        calls["pairs"] += 1
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"pairs": pairs[offset:offset + limit]})

    return handler, calls


def test_all_pairs_pages_to_exhaustion_and_reconciles():
    pairs = [[f"old-{i}", f"{i:032d}"] for i in range(5)]
    handler, calls = _paged_handler(pairs, totals=[5])
    store = _store_with_handler(handler)
    store._page = 2  # exercise multi-page

    assert store.all_pairs() == [(f"old-{i}", f"{i:032d}") for i in range(5)]
    assert calls["count"] == 2, "count before AND after the paged read"
    assert calls["pairs"] == 3, "ceil(5/2) pages"


def test_all_pairs_zero_count_short_circuits_without_pairs_call():
    handler, calls = _paged_handler([], totals=[0])
    store = _store_with_handler(handler)

    assert store.all_pairs() == []
    assert calls["pairs"] == 0, "zero facts => no /pairs round trips at all"


def test_all_pairs_torn_read_raises_loud():
    # Count moves between the before/after reads: a concurrent write shifted
    # rows across OFFSET boundaries — the result cannot be trusted.
    pairs = [[f"old-{i}", f"{i:032d}"] for i in range(4)]
    handler, _calls = _paged_handler(pairs, totals=[4, 6])
    store = _store_with_handler(handler)

    with pytest.raises(RemapReadTornError):
        store.all_pairs()


def test_all_pairs_row_count_mismatch_raises_loud():
    # Stable totals but the pages returned fewer rows than the total claims
    # (a row silently vanished past an offset boundary).
    pairs = [[f"old-{i}", f"{i:032d}"] for i in range(3)]
    handler, _calls = _paged_handler(pairs, totals=[4])
    store = _store_with_handler(handler)

    with pytest.raises(RemapReadTornError):
        store.all_pairs()


# ── entries / source_collections wire-shape adaptation ────────────────────────


def test_entries_with_targets_rebuilds_dict_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/remap/entries"
        assert request.url.params["source_collection"] == "src__a"
        return httpx.Response(200, json={"entries": [
            {"old_id": "a-1", "new_chash": "1" * 32, "target_collection": "tgt__a"},
            {"old_id": "a-2", "new_chash": "2" * 32, "target_collection": "tgt__b"},
        ]})

    store = _store_with_handler(handler)
    assert store.entries_with_targets("src__a") == {
        "a-1": ("1" * 32, "tgt__a"),
        "a-2": ("2" * 32, "tgt__b"),
    }
    assert store.entries_for_collection("src__a") == {
        "a-1": "1" * 32,
        "a-2": "2" * 32,
    }


def test_source_collections_returns_frozenset():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/remap/source_collections"
        return httpx.Response(200, json={"source_collections": ["src__a", "src__b"]})

    store = _store_with_handler(handler)
    result = store.source_collections()
    assert result == frozenset({"src__a", "src__b"})
    assert isinstance(result, frozenset)


# ── seed_local_map ─────────────────────────────────────────────────────────────


def test_seed_local_map_uploads_every_local_fact(tmp_path: Path):
    local = ChashRemapStore(tmp_path / "chash_remap.db")
    local.record_batch([
        _entry("a-1", source="src__a", new="a" * 32),
        _entry("a-2", source="src__a", new="b" * 32),
        _entry("b-1", source="src__b", new="c" * 32),
    ])

    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/remap/record_batch"
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"recorded": 1})

    http = _store_with_handler(handler)
    seeded = seed_local_map(local, http)
    local.close()

    assert seeded == 3
    uploaded = {
        (p["source_collection"], e["old_id"], e["new_chash"], e["target_collection"])
        for p in posts for e in p["entries"]
    }
    assert uploaded == {
        ("src__a", "a-1", "a" * 32, "tgt__a"),
        ("src__a", "a-2", "b" * 32, "tgt__a"),
        ("src__b", "b-1", "c" * 32, "tgt__a"),
    }


def test_seed_local_map_empty_local_is_noop(tmp_path: Path):
    local = ChashRemapStore(tmp_path / "chash_remap.db")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP call expected for an empty local map")

    assert seed_local_map(local, _store_with_handler(handler)) == 0
    local.close()


# ── seed_and_quarantine: ONE-TIME by construction ─────────────────────────────
#
# The reseed-resurrection hazard (Hal directive 2026-07-18): a rollback's
# clear_leg removes engine rows (D2 absence-encoding, rolled-back = nothing
# owed); a converge that re-seeded from the still-present local file would
# RESURRECT the cleared claims. Quarantine (rename to *.seeded) after a
# verified seed makes the seed one-time by construction — the file survives
# intact as the read-only migration/rollback source, but no code path opens
# it as a fact source again.


def test_seed_and_quarantine_renames_after_verified_seed(tmp_path: Path):
    map_path = tmp_path / "chash_remap.db"
    with ChashRemapStore(map_path) as local:
        local.record_batch([_entry("a-1", new="a" * 32)])

    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"recorded": 1})

    seeded = seed_and_quarantine(map_path, _store_with_handler(handler))

    assert seeded == 1
    assert len(posts) == 1
    assert not map_path.exists(), "the live-named file is gone after quarantine"
    quarantined = map_path.with_name(map_path.name + SEEDED_SUFFIX)
    assert quarantined.exists(), "the file survives intact under the .seeded name"


def test_seed_and_quarantine_second_call_is_noop(tmp_path: Path):
    map_path = tmp_path / "chash_remap.db"
    with ChashRemapStore(map_path) as local:
        local.record_batch([_entry("a-1", new="a" * 32)])

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"recorded": 1})

    engine = _store_with_handler(handler)
    seed_and_quarantine(map_path, engine)
    first = calls["n"]

    # The resurrection regression: a rollback cleared engine rows between
    # converges — the second converge must NOT re-upload from the local file.
    assert seed_and_quarantine(map_path, engine) == 0
    assert calls["n"] == first, "no re-seed POSTs after quarantine"


def test_seed_and_quarantine_failed_seed_does_not_quarantine(tmp_path: Path):
    map_path = tmp_path / "chash_remap.db"
    with ChashRemapStore(map_path) as local:
        local.record_batch([_entry("a-1", new="a" * 32)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(Exception):
        seed_and_quarantine(map_path, _store_with_handler(handler))

    assert map_path.exists(), "an unverified seed must leave the file live for retry"
    assert not map_path.with_name(map_path.name + SEEDED_SUFFIX).exists()


def test_seed_and_quarantine_missing_file_is_noop(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP call expected when no local map exists")

    assert seed_and_quarantine(tmp_path / "absent.db", _store_with_handler(handler)) == 0


# ── CompositeReadMapStore: engine ∪ read-only local ───────────────────────────


def test_composite_unions_engine_and_local_pairs(tmp_path: Path):
    local = ChashRemapStore(tmp_path / "chash_remap.db")
    local.record_batch([
        _entry("local-1", source="src__old", new="d" * 32),
        _entry("shared-1", source="src__old", new="e" * 32),
    ])

    handler, _calls = _paged_handler(
        [["engine-1", "f" * 32], ["shared-1", "e" * 32]], totals=[2])
    http = _store_with_handler(handler)

    composite = CompositeReadMapStore(http, local)
    pairs = composite.all_pairs()
    local.close()

    # Union, deduped on exact (old_id, new_chash) pairs — the pre-seed
    # install's local-only facts stay visible; identical facts collapse.
    assert sorted(pairs) == sorted([
        ("engine-1", "f" * 32),
        ("local-1", "d" * 32),
        ("shared-1", "e" * 32),
    ])


def test_composite_source_collections_union(tmp_path: Path):
    local = ChashRemapStore(tmp_path / "chash_remap.db")
    local.record_batch([_entry("l-1", source="src__old")])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/remap/source_collections":
            return httpx.Response(200, json={"source_collections": ["src__new"]})
        raise AssertionError(f"unexpected path {request.url.path}")

    composite = CompositeReadMapStore(_store_with_handler(handler), local)
    result = composite.source_collections()
    local.close()

    assert result == frozenset({"src__new", "src__old"})


def test_composite_without_local_is_engine_only():
    handler, _calls = _paged_handler([["e-1", "9" * 32]], totals=[1])
    composite = CompositeReadMapStore(_store_with_handler(handler), None)
    assert composite.all_pairs() == [("e-1", "9" * 32)]


# ── gate r2 ordering, engine-backed (the .6 crash test) ───────────────────────


def test_map_batch_posts_to_engine_strictly_before_target_write():
    """Gate r2 by construction, preserved over the engine write: when the
    target write crashes, the map POST for that very batch has ALREADY
    committed server-side (the POST returns only after the PG transaction) —
    a crash can produce map-without-target (safe: resume re-upserts
    idempotently) but never target-without-map. Falsification per the bead:
    move ``map_store.record_batch`` after the seam's target write in
    ``make_wire_reid_transform``/``run_batched_etl`` and this test fails —
    the crash strikes with zero POSTs recorded."""
    import hashlib

    from nexus.migration.etl_ports import run_batched_etl
    from nexus.migration.wire_reid import make_wire_reid_transform

    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"recorded": 1})

    class CrashingTarget:
        def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
            raise ValueError("crash between map persist and target write")

        def count(self, collection: str) -> int:
            return 0

    class OneBatchSource:
        def iter_batches(self, collection, *, page, include_embeddings=False):
            yield [{"id": "legacy-a", "document": "alpha", "metadata": {}}]

        def count(self, collection: str) -> int:
            return 1

    store = _store_with_handler(handler)
    transform = make_wire_reid_transform(
        store, source_collection="src", target_collection="dst", provenance="p"
    )
    result = run_batched_etl(
        OneBatchSource(), CrashingTarget(),
        source_collection="src", target_collection="dst", page=10,
        transform=transform,
    )

    assert not result.ok, "the target write crashed"
    expected = hashlib.sha256(b"alpha").hexdigest()
    assert len(posts) == 1, "the map batch reached the engine BEFORE the crash"
    assert posts[0]["entries"][0] == {
        "old_id": "legacy-a",
        "new_chash": expected,
        "target_collection": "dst",
        "provenance": "p",
    }
