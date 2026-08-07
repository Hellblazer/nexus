# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpCentroidStore — service-backed taxonomy-centroid port (RDR-156 nexus-t1hnc).

The chroma-free replacement for the ``taxonomy__centroids`` ChromaDB collection
that the SQLite ``CatalogTaxonomy`` reached via a ``chroma_client``. That class
is DELETED (nexus-i711w Stage 2 sub-stage C); references to it here are lineage
for where these shapes came from, not live code. Backs the centroid-ANN reads (``assign_single`` /
``compute_assignments`` / ``compute_cross_links`` / ``project_against``) and the
``discover_topics`` centroid upsert when taxonomy runs on the PG service backend.

Talks to the RDR-156 ``/v1/taxonomy/centroids/*`` endpoints (bead nexus-t1hnc.3).
Endpoint discovery is the SAME centralized resolver
(:func:`nexus.db.service_endpoint.resolve_service_endpoint`) that HttpTaxonomyStore
uses — no per-store env handling.

ERROR-TRANSLATION CONTRACT (Phase-1 gate O2):
    The oracle's ``assign_single`` returns ``None`` (and ``compute_assignments``
    skips) on: collection-absent, count==0, dim-mismatch, empty-filter. In service
    mode the table always exists, so:
    - dim-mismatch / bad request -> HTTP 400 -> :meth:`ann_query` returns ``[]``
      (so :meth:`nearest` returns ``None``), matching the oracle's best-effort
      "don't assign" on a dimension mismatch.
    - empty result -> ``[]`` / ``None`` (no centroids yet).
    - transport / 5xx errors are RAISED, NOT swallowed to ``None``. This is a
      DELIBERATE divergence from the oracle's blanket ``except Exception: return
      None``: silently treating a service outage as "no centroid" during a full
      reindex would produce an untaxonomized corpus — the silent-wrong class the
      project forbids. Callers that want best-effort parity catch explicitly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.db.t2.taxonomy_compute import AssignResult
from nexus.db.t2.http_taxonomy_store import DEFAULT_TENANT

_log = structlog.get_logger(__name__)

#: Matches RefreshableHttpStoreMixin's own default (kept local so the
#: ``_transport`` test-seam rebuild below does not import a private
#: constant across modules).
_DEFAULT_TIMEOUT_S = 30.0


class HttpCentroidStore(RefreshableHttpStoreMixin):
    """Service-backed centroid port mirroring the chroma centroid contract.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    Args:
        base_url: Optional ``http://<host>:<port>`` override. When supplied
            without ``_token``, only the token half is re-resolved
            (host/port need not also be independently resolvable).
        tenant:   Tenant stamped on every request (default: ``DEFAULT_TENANT``).
        _token:   Optional bearer token (test seam / explicit override).
        _transport: Optional ``httpx`` transport (test seam for ``MockTransport``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(base_url, tenant, _token=_token)
        if _transport is not None:
            # Test seam (MockTransport): the mixin's __init__ already built
            # a plain (transport-less) httpx.Client; swap it for one wired
            # to the fake transport, keeping the same timeout.
            self._client.close()
            self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT_S, transport=_transport)
        # nexus-2mb6n: per-store-instance cache for the run-constant centroid
        # envelope reads (get_by_collection / get_foreign). This store lives
        # for the whole indexing run (HttpTaxonomyStore lazily constructs
        # and keeps exactly one instance — see that class's ``_centroid``
        # property), so caching HERE — keyed on (collection, kind) — amortizes
        # across every flush instead of one HTTP round trip per flush for
        # data that never changes mid-run. Cleared by any mutation
        # (upsert/delete_ids/purge) THROUGH THIS SAME INSTANCE — see
        # :meth:`_invalidate_centroid_cache`.
        #
        # PER-KEY locking (review round 2, reviewer Important-1): the first
        # cut held ONE process-wide lock across the full fetch — including
        # ``_send``'s gateway-retry envelope, worst case ~90s — so a slow
        # miss on one key blocked a HIT on an already-warm, unrelated key.
        # ``_centroid_cache_guard`` is held only BRIEFLY (a dict lookup, or
        # get-or-create of a per-key lock) and never across a fetch;
        # ``_centroid_key_locks[key]`` is the one actually held during
        # ``fetch()``, so concurrent requests for DIFFERENT keys never
        # contend, while concurrent requests for the SAME key still
        # single-flight (see :meth:`_cached_envelope`).
        #
        # FETCH-EPOCH guard (review round 2 recurrence, critic-reproduced
        # empirically): per-key locking on its own opened a SAME-PROCESS
        # race the coarse single-lock predecessor didn't have — a reader
        # can be mid-fetch (holding only its own key_lock, not the guard)
        # when a WRITER thread's ``upsert``/``delete_ids``/``purge`` runs
        # and invalidates; the reader's in-flight fetch then still caches
        # its (now pre-mutation, stale) result after the writer's clear
        # has already happened. ``_centroid_cache_epoch`` closes this:
        # bumped by every invalidation, checked by every fetch before it
        # writes the cache (see :meth:`_cached_envelope` /
        # :meth:`_invalidate_centroid_cache`). This guards the SAME-PROCESS
        # race specifically; cross-process staleness (another process
        # mutating via a different store instance, hence a different
        # epoch counter) remains an accepted limitation with its own
        # separate rationale — see :meth:`_invalidate_centroid_cache`.
        self._centroid_cache: dict[tuple[str, str], dict[str, list[Any]]] = {}
        self._centroid_cache_guard = threading.Lock()
        self._centroid_key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._centroid_cache_epoch = 0

    # ── Internal helpers ────────────────────────────────────────────────────────
    #
    # LOCAL overrides (not a straight inherit): every method in this class
    # calls self._post/self._get with a SHORT path suffix (e.g. "/upsert") —
    # the "/v1/taxonomy/centroids" prefix is store-specific routing, not
    # part of the mixin's shared contract. Every actual HTTP round-trip
    # still goes through the inherited, self-healing super()._post/_get
    # (RefreshableHttpStoreMixin._send), never self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/taxonomy/centroids{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/taxonomy/centroids{path}", q, idempotent=idempotent)

    # ── Writes ────────────────────────────────────────────────────────────────────

    def upsert(self, records: list[dict[str, Any]]) -> None:
        """Upsert centroids. Each record: ``{collection, topic_id, embedding,
        label, doc_count}``. Embeddings route to the per-dim table by length
        service-side. No-op on empty input."""
        if not records:
            return
        self._post("/upsert", {"records": records})
        self._invalidate_centroid_cache()

    def delete_ids(self, collection: str, topic_ids: list[int]) -> int:
        """Delete centroids by topic_id within a collection. Returns rows deleted."""
        if not topic_ids:
            return 0
        r = self._post("/delete", {"collection": collection, "topic_ids": topic_ids})
        self._invalidate_centroid_cache()
        return int(r.get("deleted", 0))

    def purge(self, collection: str) -> int:
        """Remove every centroid for a collection. Returns rows deleted."""
        r = self._post("/purge", {"collection": collection})
        self._invalidate_centroid_cache()
        return int(r.get("deleted", 0))

    def _invalidate_centroid_cache(self) -> None:
        """Clear every cached ``get_by_collection``/``get_foreign`` envelope
        and bump the fetch epoch (nexus-2mb6n).

        Blanket clear, not a narrower per-collection evict: a mutation to
        collection A's centroids also changes what ``get_foreign(B)``
        returns for every OTHER collection B (``get_foreign`` is "every
        centroid NOT in this collection", so A's rows are IN that set for
        every B != A). A per-collection evict would leave every other
        collection's cached foreign-set stale. The cache is small (a
        handful of collections, two kinds each) so clearing all of it costs
        nothing and cannot under-invalidate.

        SAME-PROCESS mid-fetch race — GUARDED (review round 2 recurrence,
        critic-reproduced empirically), not merely accepted: because reads
        hold only ``_centroid_key_locks[key]`` (not this coarse guard)
        DURING their fetch, a reader can be mid-flight when a writer
        thread's ``upsert``/``delete_ids``/``purge`` runs concurrently on
        this SAME store instance. Without a guard the reader would then
        cache its already-stale (pre-mutation) result right after this
        method's clear — undoing the invalidation. The fix: bumping
        ``self._centroid_cache_epoch`` here (below) gives every
        invalidation a distinct generation number; :meth:`_cached_envelope`
        records the epoch it saw BEFORE calling ``fetch()`` and refuses to
        write the cache if the epoch moved while the fetch was in flight
        (the fetched data is still returned to that ONE caller — just
        never poisons the cache for subsequent callers). Proven by
        ``test_mid_fetch_invalidation_does_not_poison_cache_epoch_guard``.

        CROSS-PROCESS staleness — remains an ACCEPTED limitation, NOT
        conflated with the above (review round 2, Important-3): a
        DIFFERENT process running ``nx taxonomy rebuild``/``discover``
        mutates through its OWN ``HttpCentroidStore`` instance, with its
        own independent epoch counter — this process's epoch never moves,
        so the guard above cannot and does not detect it. Any assignments
        this run computes for the remainder of its life may be against
        pre-rebuild centroids; self-heals on the NEXT run (a fresh store
        instance, fresh cache, fresh epoch). Accepted because: taxonomy
        rebuilds are rare, operator-initiated, manual operations, not a
        routine concurrent pattern against a live indexing run; and a real
        cross-process fix needs a SERVER-SIDE generation/version counter
        this route does not expose today (the critic explicitly RULED OUT
        ``/meta/last_count`` as a viable proxy — it reflects assignment
        counts, not centroid generation). Candidate follow-up: rides the
        nexus-lns3o server-side compute-and-assign engine route as a
        design rider — moving cosine compute server-side removes the
        client-side cache (and this whole cross-process staleness
        question) entirely, rather than bolting a server-side version
        probe onto a cache that may not survive that migration.
        """
        with self._centroid_cache_guard:
            self._centroid_cache.clear()
            self._centroid_cache_epoch += 1

    # ── ANN reads ─────────────────────────────────────────────────────────────────

    def ann_query(
        self,
        embedding: list[float],
        collection: str,
        *,
        cross_collection: bool = False,
        n_results: int = 1,
    ) -> list[AssignResult]:
        """Nearest centroids for one embedding, distance-ascending.

        Returns a list of :class:`AssignResult` (``topic_id``, ``similarity = 1 -
        distance``). On HTTP 400 (dimension mismatch / bad request) returns ``[]``
        — the oracle's best-effort "don't assign" on a dim mismatch. Transport /
        5xx errors propagate (see the module error-translation contract).
        """
        try:
            rows = self._post("/query", {
                "embedding": list(embedding),
                "collection": collection,
                "cross_collection": cross_collection,
                "n_results": n_results,
            })
        except httpx.HTTPStatusError as e:
            # Swallow to [] ONLY for the dimension-mismatch 400 — the oracle's
            # best-effort "don't assign" when the query vector's space does not
            # match the stored centroids (catalog_taxonomy._check_centroid_dimension).
            # Any OTHER 400 (malformed body, n_results<1, ...) is a CALLER BUG and
            # re-raises — never silently empty (fail-loud, M1/S3).
            detail = e.response.text[:300]
            if e.response.status_code == 400 and "taxonomy_centroids" in detail:
                _log.warning(
                    "centroid_dimension_mismatch",
                    collection=collection,
                    detail=detail,
                )
                return []
            raise
        return [AssignResult(topic_id=int(r["topic_id"]), similarity=float(r["similarity"]))
                for r in rows]

    def nearest(
        self,
        embedding: list[float],
        collection: str,
        *,
        cross_collection: bool = False,
    ) -> AssignResult | None:
        """The ``assign_single`` equivalent: nearest single centroid or ``None``.

        Returns ``None`` when there are no centroids (or a dim mismatch yields an
        empty result), matching :meth:`CatalogTaxonomy.assign_single`'s contract.
        """
        hits = self.ann_query(
            embedding, collection, cross_collection=cross_collection, n_results=1,
        )
        return hits[0] if hits else None

    # ── Bulk / metadata reads ─────────────────────────────────────────────────────

    def count(self, collection: str | None = None) -> int:
        """Count centroids (optionally for one collection) across all per-dim tables."""
        r = self._get("/count", {"collection": collection})
        return int(r.get("count", 0))

    def dimension(self) -> int:
        """The active centroid dimension for this tenant, or ``-1`` when empty.

        Mirrors :func:`catalog_taxonomy._check_centroid_dimension`'s role: resolves
        the deployment's single centroid space for collection-keyed ops.
        """
        r = self._get("/dimension")
        return int(r.get("dimension", -1))

    def get_by_collection(self, collection: str) -> dict[str, list[Any]]:
        """All centroids for ``collection`` in the chroma ``get()`` envelope shape
        the rebuild/project paths index into: ``{ids, embeddings, metadatas}``.

        ``ids`` are ``"{collection}:{topic_id}"`` (the oracle centroid id at
        ``catalog_taxonomy.py:_centroid_records_for``); ``metadatas`` carry
        ``{topic_id, label, collection, doc_count}``.

        CACHED per store instance, keyed on ``(collection, "same")``
        (nexus-2mb6n) — this is the run-constant read
        ``compute_assignments`` makes on every flush (195x/reindex measured,
        T2 nexus/flush-tail-attribution-2026-08-07). Invalidated by any
        ``upsert``/``delete_ids``/``purge`` through this same instance.
        """
        return self._cached_envelope(
            collection, "same",
            lambda: self._envelope(self._get("/by_collection", {"collection": collection})),
        )

    def get_foreign(self, collection: str) -> dict[str, list[Any]]:
        """All centroids in collections OTHER than ``collection`` (cross-collection
        projection source set), in the same envelope shape as
        :meth:`get_by_collection`.

        Serves ``compute_cross_links`` (``$ne`` directly) and ``project_against``
        (``$in`` by filtering this foreign set to the target collections).

        CACHED per store instance, keyed on ``(collection, "foreign")``
        (nexus-2mb6n) — see :meth:`get_by_collection`'s docstring for why.
        """
        return self._cached_envelope(
            collection, "foreign",
            lambda: self._envelope(self._get("/foreign", {"collection": collection})),
        )

    def _cached_envelope(
        self,
        collection: str,
        kind: str,
        fetch: Callable[[], dict[str, list[Any]]],
    ) -> dict[str, list[Any]]:
        """Fetch-once-per-(collection, kind), thread-safe (nexus-2mb6n).

        PER-KEY locking (review round 2 — the original single process-wide
        lock held across the full fetch let one slow miss block every OTHER
        key's cache HIT behind it):

        1. Fast path — check the cache under the short-held GUARD lock
           only (``_centroid_cache_guard``). A hit returns immediately;
           the guard is never held across a network call, so a hit for
           key A can never be blocked by an in-flight fetch for key B.
        2. On a miss, get-or-create THIS key's own lock (also under the
           brief guard) and release the guard.
        3. Acquire the PER-KEY lock for the actual fetch. A second thread
           requesting the SAME key blocks here (single-flight preserved —
           the batched indexer's ``flush_concurrency=3`` flush workers can
           race on the same cold key); a thread requesting a DIFFERENT key
           is unaffected — it has its own lock.
        4. Re-check the cache under the guard once the per-key lock is
           held (a concurrent same-key racer may have already populated
           it while we waited); also record the CURRENT
           ``_centroid_cache_epoch`` here, BEFORE calling ``fetch()``.
        5. FETCH-EPOCH guard (review round 2 recurrence, critic-reproduced
           empirically): after ``fetch()`` returns, re-check the epoch
           under the guard. If it is unchanged, cache the result — normal
           path. If a writer's ``upsert``/``delete_ids``/``purge`` bumped
           the epoch WHILE this fetch was in flight, the fetched data may
           be stale (computed against pre-mutation centroids); it is
           returned to THIS caller (still the freshest data this call
           could get) but deliberately NOT written to the cache, so it
           can never poison a LATER caller. The next call for this key
           re-fetches for real.

        On a fetch failure the exception propagates and NOTHING is cached
        (honest fallback: a cache miss always means a real fetch, a failed
        fetch is never silently remembered as empty). See
        :meth:`_invalidate_centroid_cache`'s docstring for the epoch
        guard's scope (same-process only) versus the still-accepted
        cross-process staleness case.
        """
        key = (collection, kind)
        with self._centroid_cache_guard:
            cached = self._centroid_cache.get(key)
            if cached is not None:
                return cached
            key_lock = self._centroid_key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._centroid_cache_guard:
                cached = self._centroid_cache.get(key)
                if cached is not None:
                    return cached
                epoch_before = self._centroid_cache_epoch
            result = fetch()
            with self._centroid_cache_guard:
                if self._centroid_cache_epoch == epoch_before:
                    self._centroid_cache[key] = result
                # else: an invalidation landed mid-fetch — do NOT cache a
                # result that may be stale relative to it (nexus-2mb6n
                # round 2 recurrence). The caller still gets `result`.
            return result

    @staticmethod
    def _envelope(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
        """Adapt service centroid rows to the chroma ``get()`` envelope."""
        ids: list[str] = []
        embeddings: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        for r in rows:
            collection = r["collection"]
            topic_id = int(r["topic_id"])
            ids.append(f"{collection}:{topic_id}")
            embeddings.append(r["embedding"])
            metadatas.append({
                "topic_id": topic_id,
                "label": r.get("label"),
                "collection": collection,
                "doc_count": r.get("doc_count"),
            })
        return {"ids": ids, "embeddings": embeddings, "metadatas": metadatas}
