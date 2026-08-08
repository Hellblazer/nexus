# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7ydks: `nx taxonomy discover`/`rebuild` route through the
HttpTaxonomyStore drop-in when the taxonomy store is service-backed.

These are unit tests over the CLI dispatch seam (`discover_for_collection`)
using fakes — no live service required. The end-to-end exercise against the
real Java service lives in tests/db/test_http_taxonomy_store_integration.py.
"""
from __future__ import annotations

import numpy as np
import pytest

from nexus.commands.taxonomy_cmd import (
    _enumerate_discoverable_collections,
    _fetch_service_vectors,
    discover_for_collection,
)


class _RawColl:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self._count = count

    def count(self) -> int:
        return self._count


class _FakeRawClient:
    def __init__(self, colls) -> None:  # noqa: ANN001
        self._colls = colls

    def list_collections(self):
        return self._colls


class _FakeRawT3:
    """T3Database-shaped: exposes ``_client.list_collections()``."""

    def __init__(self, colls) -> None:  # noqa: ANN001
        self._client = _FakeRawClient(colls)


def test_enumerate_discoverable_collections_filters_and_does_not_crash():
    # nexus-7ydks HIGH-1 regression: the helper referenced module-scope
    # `fnmatch` that was only imported inside discover_cmd → NameError when
    # called from the index.py post-processing path. Exercise the filter.
    t3 = _FakeRawT3([
        _RawColl("docs__demo", 12),       # kept
        _RawColl("code__demo", 9),        # excluded by pattern
        _RawColl("docs__small", 3),       # too few
        _RawColl("taxonomy__centroids", 50),  # internal, skipped
    ])
    got = _enumerate_discoverable_collections(t3, exclude=["code__*"])
    assert got == ["docs__demo"]


class _FakeStub:
    """Service collection stub: paginated get(documents) over a fixed corpus."""

    def __init__(self, ids: list[str], docs: list[str]) -> None:
        self._ids = ids
        self._docs = docs

    def get(self, *, include=None, limit=10, offset=0):  # noqa: ANN001
        sl = slice(offset, offset + limit)
        return {"ids": self._ids[sl], "documents": self._docs[sl]}


class _FakeServiceT3:
    """Minimal HttpVectorClient-shaped handle for the discovery fetch path."""

    def __init__(self, ids, docs, embs) -> None:  # noqa: ANN001
        self._ids = ids
        self._docs = docs
        self._embs = np.asarray(embs, dtype=np.float32)

    def count(self, collection: str) -> int:
        return len(self._ids)

    def get_or_create_collection(self, name: str) -> _FakeStub:
        return _FakeStub(self._ids, self._docs)

    def get_embeddings(self, collection: str, ids: list[str], on_progress=None):  # noqa: ANN001
        # Return rows in request order (mirrors the real client contract).
        index = {i: r for i, r in zip(self._ids, self._embs)}
        return np.asarray([index[i] for i in ids], dtype=np.float32)


class _FakeServiceTaxonomy:
    """HttpTaxonomyStore-shaped store: no `_lock`/`conn` → not raw-access.

    Records the args it was dispatched so the test can assert the CLI handed
    over the fetched vectors verbatim.
    """

    def __init__(self) -> None:
        self.discover_calls: list[tuple] = []
        self.rebuild_calls: list[tuple] = []

    def discover_topics(self, collection_name, doc_ids, embeddings, texts, chroma_client=None):  # noqa: ANN001
        self.discover_calls.append((collection_name, list(doc_ids), np.asarray(embeddings), list(texts)))
        return len(set(doc_ids)) and 3  # pretend 3 topics

    def rebuild_taxonomy(self, collection_name, doc_ids, embeddings, texts, chroma_client=None):  # noqa: ANN001
        self.rebuild_calls.append((collection_name, list(doc_ids), np.asarray(embeddings), list(texts)))
        return 2


def _corpus(n: int):
    ids = [f"c{i}" for i in range(n)]
    docs = [f"text {i}" for i in range(n)]
    embs = [[float(i), float(i) + 1.0, 2.0] for i in range(n)]
    return ids, docs, embs


def test_fetch_service_vectors_returns_aligned_arrays():
    ids, docs, embs = _corpus(6)
    t3 = _FakeServiceT3(ids, docs, embs)
    got = _fetch_service_vectors("docs__demo", t3)
    assert got is not None
    g_ids, g_texts, g_embs = got
    assert g_ids == ids
    assert g_texts == docs
    assert g_embs.shape == (6, 3)


def test_fetch_service_vectors_bails_on_embedding_misalignment():
    ids, docs, embs = _corpus(6)

    class _Drops(_FakeServiceT3):
        def get_embeddings(self, collection, ids, on_progress=None):  # noqa: ANN001
            return np.asarray(self._embs[:-1], dtype=np.float32)  # one short

    assert _fetch_service_vectors("docs__demo", _Drops(ids, docs, embs)) is None


def test_discover_for_collection_service_routes_to_discover_topics():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 3
    assert len(tax.discover_calls) == 1
    assert not tax.rebuild_calls
    col, got_ids, got_embs, got_texts = tax.discover_calls[0]
    assert col == "docs__demo"
    assert got_ids == ids
    assert got_texts == docs
    assert got_embs.shape == (8, 3)


def test_discover_for_collection_service_force_routes_to_rebuild():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    n = discover_for_collection("docs__demo", tax, t3, force=True)

    assert n == 2
    assert len(tax.rebuild_calls) == 1
    assert not tax.discover_calls


def test_discover_for_collection_service_too_few_docs_returns_zero():
    ids, docs, embs = _corpus(4)  # < 5
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    assert discover_for_collection("docs__demo", tax, t3) == 0
    assert not tax.discover_calls


# ── nexus-vgtff: existing-topics guard checked BEFORE fetch+cluster ──────────
#
# Non-force discover on a topic-bearing collection is a designed no-op (both
# backends guard at persist), but the guard fired AFTER the full chunk-text +
# embedding fetch and clustering — 300s of wasted work on every index run
# (2026-07-15 evidence: 0 topics created across three collections). The probe
# moves the same decision before the fetch; the persist guard stays as the
# atomic race backstop.


class _NoFetchServiceT3(_FakeServiceT3):
    """Fails the test if the discovery fetch path is entered at all."""

    def get_or_create_collection(self, name: str):
        raise AssertionError("fetch path entered — guard-first probe did not skip")


class _TaxWithTopics(_FakeServiceTaxonomy):
    def __init__(self, topics: list) -> None:
        super().__init__()
        self._topics = topics

    def get_topics_for_collection(self, col: str):
        return self._topics


class _TaxProbeBoom(_FakeServiceTaxonomy):
    def get_topics_for_collection(self, col: str):
        raise RuntimeError("probe boom")


def test_discover_skips_before_fetch_when_topics_exist():
    ids, docs, embs = _corpus(8)
    t3 = _NoFetchServiceT3(ids, docs, embs)
    tax = _TaxWithTopics([{"id": 1, "label": "existing"}])

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 0
    assert not tax.discover_calls  # never dispatched — and never fetched


def test_discover_force_bypasses_topics_probe():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _TaxWithTopics([{"id": 1, "label": "existing"}])

    n = discover_for_collection("docs__demo", tax, t3, force=True)

    assert n == 2  # rebuild ran
    assert len(tax.rebuild_calls) == 1


def test_discover_probe_error_falls_through_to_normal_path():
    # Probe failure must NOT skip — the persist guard remains the authority.
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _TaxProbeBoom()

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 3
    assert len(tax.discover_calls) == 1


def test_discover_runs_when_no_topics_exist():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _TaxWithTopics([])

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 3
    assert len(tax.discover_calls) == 1


def test_discover_probe_skip_is_silent_in_quiet_mode(capsys):
    # review Medium-1: run_collection_postprocessing(quiet=True) callers must
    # not leak the probe's operator message through raw click.echo.
    ids, docs, embs = _corpus(8)
    t3 = _NoFetchServiceT3(ids, docs, embs)
    tax = _TaxWithTopics([{"id": 1, "label": "existing"}])

    n = discover_for_collection("docs__demo", tax, t3, force=False, quiet=True)

    assert n == 0
    assert capsys.readouterr().out == ""


def test_discover_probe_skip_echoes_when_not_quiet(capsys):
    ids, docs, embs = _corpus(8)
    t3 = _NoFetchServiceT3(ids, docs, embs)
    tax = _TaxWithTopics([{"id": 1, "label": "existing"}])

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 0
    assert "topics already exist" in capsys.readouterr().out


# ── Incremental-assignment hook (nexus-7ydks C1) ────────────────────────────


def test_assign_batch_hook_routes_through_assign_from_chashes(monkeypatch):
    """nexus-yu9w5 (lns3o client half): the per-store_put assignment hook
    must call the engine route directly with the batch's chashes — no
    embedding fetch, no client-side compute_assignments/persist_assignments."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = [f"c{i}" for i in range(4)]

    # A real-typed (isinstance) HttpVectorClient so is_service_backed() is
    # True. get_embeddings raises if ever called — the route replaces the
    # embedding-fetch dance entirely; the hook must never touch T3 for it.
    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

        def get_embeddings(self, collection, doc_ids):  # noqa: ANN001
            raise AssertionError("must not fetch embeddings — the route computes server-side")

    calls: list[tuple] = []

    class _SvcTax:
        def assign_from_chashes(self, collection, chashes, *, cross_collection=True):  # noqa: ANN001
            calls.append((collection, list(chashes), cross_collection))
            return {"assigned": len(chashes), "cross_assigned": 0, "unmatched_chashes": []}

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    # is_local_mode is a local import from nexus.config inside the hook.
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    # t2_index_write just runs the fn with our fake db (no daemon).
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    # embeddings=None (the MCP store_put shape) — must reach the route
    # unchanged; embeddings/contents/metadatas are accepted but not read.
    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 4, None, None)

    assert calls == [("docs__demo", ids, True)], (
        "hook did not call assign_from_chashes with the batch's chashes verbatim"
    )


@pytest.mark.parametrize(
    "embeddings",
    [
        None,
        [[] for _ in range(4)],  # server-side-embed placeholder shape (nexus-reskd)
        [np.array([1.0, 2.0, 3.0], dtype=np.float32) for _ in range(4)],  # bge-768 local tier shape (nexus-h8rf6.11)
        [[0.1, 0.2, 0.3] for _ in range(4)],
    ],
)
def test_assign_batch_hook_ignores_embeddings_entirely(monkeypatch, embeddings):
    """Whatever shape ``embeddings`` arrives in — absent, placeholder-empty,
    a list of numpy arrays, or real vectors — the hook must route to
    assign_from_chashes unchanged. The nexus-reskd empty-placeholder dance
    and the nexus-h8rf6.11 numpy-truthiness crash both required the hook to
    INSPECT embeddings; since the route computes server-side, embeddings are
    now simply never read, so neither failure mode is reachable."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = [f"c{i}" for i in range(4)]

    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

        def get_embeddings(self, collection, doc_ids):  # noqa: ANN001
            raise AssertionError("must not fetch embeddings under any embeddings= shape")

    calls: list[tuple] = []

    class _SvcTax:
        def assign_from_chashes(self, collection, chashes, *, cross_collection=True):  # noqa: ANN001
            calls.append((collection, list(chashes), cross_collection))
            return {"assigned": len(chashes), "cross_assigned": 0, "unmatched_chashes": []}

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    # Must not raise regardless of embeddings= shape (no more ValueError
    # 'truth value of an array ... is ambiguous', no more None-vs-empty
    # placeholder branching).
    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 4, embeddings, None)

    assert calls == [("docs__demo", ids, True)]


def test_assign_batch_hook_no_fallback_when_route_errors(monkeypatch):
    """nexus-yu9w5 NO-FALLBACK contract, falsified by construction: the
    double's compute_assignments/persist_assignments are LIVE traps that
    would happily succeed if called — if a future edit reintroduced a
    client-side recompute after assign_from_chashes fails, this test fails
    (not a vacuous "method absent" double, which would swallow a fallback
    call as an AttributeError indistinguishable from the intended error
    path). assign_from_chashes raising must reach the SAME tripwire every
    other service-path failure uses, and the batch must not otherwise
    resolve via the old two-call dance."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = [f"c{i}" for i in range(3)]

    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

    fallback_calls: list[str] = []

    class _SvcTax:
        def assign_from_chashes(self, collection, chashes, *, cross_collection=True):  # noqa: ANN001
            raise RuntimeError("simulated: engine lacks this route (pre-floor engine)")

        # LIVE traps — if the hook ever falls back to these, they succeed
        # silently and this test's assertions below catch it.
        def compute_assignments(self, collection, doc_ids, embeddings, *, cross_collection=False):  # noqa: ANN001
            fallback_calls.append("compute_assignments")
            return [{"doc_id": d, "topic_id": 1} for d in doc_ids]

        def persist_assignments(self, assignments):  # noqa: ANN001
            fallback_calls.append("persist_assignments")
            return len(assignments)

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    tripwired: list[dict] = []

    def _spy_tripwire(collection, doc_ids, error, *, kind=""):
        tripwired.append({"collection": collection, "doc_ids": list(doc_ids), "error": error, "kind": kind})

    monkeypatch.setattr(mi, "_record_taxonomy_tripwire", _spy_tripwire)

    # Must not raise — the hook is best-effort and swallows its own failure.
    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 3, None, None)

    assert not fallback_calls, (
        f"hook fell back to client-side compute after the route errored: {fallback_calls}"
    )
    assert tripwired, "route error must reach the RDR-172 tripwire, not be silently swallowed"
    assert tripwired[0]["doc_ids"] == ids
    assert "RuntimeError" in tripwired[0]["error"]


def test_assign_batch_hook_reports_unmatched_chashes_via_tripwire(monkeypatch):
    """The route names chashes it could not find in chunks_<dim> for this
    collection (never upserted, or upserted under a different collection)
    rather than silently dropping them — the hook must surface that via the
    same RDR-172 loudness the tripwire gives every other assignment gap."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = ["c0", "c1", "c2"]

    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

    class _SvcTax:
        def assign_from_chashes(self, collection, chashes, *, cross_collection=True):  # noqa: ANN001
            return {"assigned": 2, "cross_assigned": 0, "unmatched_chashes": ["c1"]}

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    tripwired: list[dict] = []

    def _spy_tripwire(collection, doc_ids, error, *, kind=""):
        tripwired.append({"doc_ids": list(doc_ids), "kind": kind, "error": error})

    monkeypatch.setattr(mi, "_record_taxonomy_tripwire", _spy_tripwire)

    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 3, None, None)

    assert tripwired, "unmatched_chashes must be surfaced, not silently dropped"
    assert tripwired[0]["doc_ids"] == ["c1"]
    assert tripwired[0]["kind"] == "unmatched_chashes"


def test_assign_batch_hook_no_tripwire_when_nothing_unmatched(monkeypatch):
    """The unmatched-chashes tripwire must not fire on the common case (every
    chash matched) — a clean run stays clean."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = ["c0", "c1"]

    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

    class _SvcTax:
        def assign_from_chashes(self, collection, chashes, *, cross_collection=True):  # noqa: ANN001
            return {"assigned": 2, "cross_assigned": 0, "unmatched_chashes": []}

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    tripwired: list[dict] = []
    monkeypatch.setattr(
        mi, "_record_taxonomy_tripwire",
        lambda collection, doc_ids, error, *, kind="": tripwired.append(kind),
    )

    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 2, None, None)

    assert not tripwired


# ── split/project service fetch helpers (nexus-9pqoj) ───────────────────────


class _StubWithIds:
    """Service collection stub supporting both ids= (store-get) and paginated get."""

    def __init__(self, ids, docs):  # noqa: ANN001
        self._ids = ids
        self._docs = docs

    def get(self, ids=None, where=None, include=None, limit=None, offset=0):  # noqa: ANN001
        if ids is not None:
            idx = {i: d for i, d in zip(self._ids, self._docs)}
            rids = [i for i in ids if i in idx]
            # nexus-hdx2u: honor an explicit limit on the ids path — the
            # production stub's default is None (unlimited / len(ids)),
            # never a silent hardcoded cap; a caller that DOES pass a
            # small limit must see it actually truncate the response,
            # or this double would mask a real truncation regression.
            if limit is not None:
                rids = rids[:limit]
            return {"ids": rids, "documents": [idx[i] for i in rids]}
        sl = slice(offset, offset + (limit if limit is not None else len(self._ids)))
        return {"ids": self._ids[sl], "documents": self._docs[sl]}


class _SplitT3:
    def __init__(self, ids, docs, embs):  # noqa: ANN001
        self._ids, self._docs = ids, docs
        self._embs = {i: e for i, e in zip(ids, embs)}

    def count(self, collection):  # noqa: ANN001
        return len(self._ids)

    def get_or_create_collection(self, name):  # noqa: ANN001
        return _StubWithIds(self._ids, self._docs)

    def get_embeddings(self, collection, ids, on_progress=None):  # noqa: ANN001
        return np.asarray([self._embs[i] for i in ids], dtype=np.float32)


def test_svc_fetch_by_ids_aligned():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(6)
    t3 = _SplitT3(ids, docs, embs)
    g_ids, g_texts, g_embs = HttpTaxonomyStore._svc_fetch_by_ids(t3, "docs__d", ids[:4])
    assert g_ids == ids[:4]
    assert g_texts == docs[:4]
    assert g_embs.shape == (4, 3)


def test_svc_fetch_by_ids_bails_on_misalign():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(6)

    class _Drop(_SplitT3):
        def get_embeddings(self, collection, ids, on_progress=None):  # noqa: ANN001
            return np.asarray(embs[:2], dtype=np.float32)  # short

    g_ids, g_texts, g_embs = HttpTaxonomyStore._svc_fetch_by_ids(_Drop(ids, docs, embs), "docs__d", ids)
    assert g_embs is None  # refuses misaligned


def test_svc_fetch_by_ids_pages_at_250_without_truncation():
    """nexus-hdx2u: _svc_fetch_by_ids batches ids at _PAGE=250 internally;
    a corpus larger than one page must come back whole — not silently
    truncated the way the stub's old implicit ``limit: int = 10`` default
    used to truncate every >10-id ``stub.get(ids=...)`` call."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(260)
    t3 = _SplitT3(ids, docs, embs)
    g_ids, g_texts, g_embs = HttpTaxonomyStore._svc_fetch_by_ids(t3, "docs__d", ids)
    assert g_ids == ids
    assert g_texts == docs
    assert g_embs.shape == (260, 3)


def test_svc_fetch_by_ids_raises_when_stub_overreturns():
    """The nexus-hdx2u consumer-side safety net: a store-get response can
    never legitimately exceed its request size. A stub that violates that
    (buggy engine, or a masking test double) must trip the local assert
    in HttpTaxonomyStore._svc_fetch_by_ids rather than silently misalign
    ids to texts."""
    import pytest

    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, _docs, _embs = _corpus(4)

    class _OverReturns:
        def get(self, ids=None, where=None, include=None, limit=None, offset=0):  # noqa: ANN001
            # Returns one MORE row than was requested — never legitimate.
            return {
                "ids": [*ids, "phantom"],
                "documents": [*[f"t-{i}" for i in ids], "ghost"],
            }

    class _T3:
        def get_or_create_collection(self, name):  # noqa: ANN001
            return _OverReturns()

    with pytest.raises(AssertionError):
        HttpTaxonomyStore._svc_fetch_by_ids(_T3(), "docs__d", ids)


def test_svc_fetch_all_embeddings_paginates():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(7)
    g_ids, g_embs = HttpTaxonomyStore._svc_fetch_all_embeddings(_SplitT3(ids, docs, embs), "docs__d")
    assert g_ids == ids
    assert g_embs.shape == (7, 3)


def test_svc_fetch_all_embeddings_bails_on_misalign():
    # nexus-9pqoj S1 regression: a count skew between enumerated ids and the
    # returned embeddings must return (ids, None), NOT a silent partial set.
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(7)

    class _Drop(_SplitT3):
        def get_embeddings(self, collection, ids, on_progress=None):  # noqa: ANN001
            return np.asarray(embs[:3], dtype=np.float32)  # short

    g_ids, g_embs = HttpTaxonomyStore._svc_fetch_all_embeddings(_Drop(ids, docs, embs), "docs__d")
    assert g_ids == ids
    assert g_embs is None
