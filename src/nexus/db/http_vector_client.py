# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 bead nexus-gmiaf.20 — Seam B HTTP vector client.

Thin Python bridge that routes T3 vector operations (search, query,
upsert-chunks, store_put, store_get, store_list, store_delete) through
the Java nexus-service HTTP endpoints rather than hitting a vector
store / Voyage AI directly from Python.

Since the RDR-155 P4a.2 serving cutover (bead nexus-1k8s1) this is THE
production T3 handle: ``nexus.db.make_t3()`` returns the
:class:`HttpVectorClient` singleton whenever no test ``_client`` is
injected, in both local and cloud mode — the service stores vectors in
pgvector and embeds server-side. ``NX_STORAGE_BACKEND_VECTORS=service``
survives only as the indexer-side opt-in that skips Python-side
embedding (see :func:`is_vector_service_mode`).

Endpoint discovery (nexus-pebfx.1): ``{url, token}`` resolve from the
supervisor's ServiceRegistry lease (``storage_service_addr.<uid>``) by
default, with ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env as per-half
overrides and a single re-resolve retry on 401/connection-refused so
clients ride through supervisor auto-restarts (the port churns on every
restart). No hardcoded fallback URL — unresolvable fails loud.

Chunking stays in Python; embed+write live in the JVM (Seam B contract —
CHUNKING STAYS PYTHON per the bead relay).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, NoReturn

import structlog

_log = structlog.get_logger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

#: Env var for the vector backend flag.
_VECTORS_BACKEND_ENV = "NX_STORAGE_BACKEND_VECTORS"

#: RDR-181 bead nexus-f0r8p.5: log the ``skip_existing`` deprecation notice
#: once per process rather than once per call. Tests reset this directly
#: (module attribute, not a public API).
_skip_existing_deprecation_logged: bool = False


def _warn_skip_existing_deprecated() -> None:
    """Log-once notice: ``skip_existing`` no longer filters the batch.

    RDR-181 made the server's existence-partition
    (``PgVectorRepository.upsertChunksInternal``) authoritative, so the
    client-side probe this flag used to drive is redundant — and worse,
    it silently skipped the ON CONFLICT metadata refresh that the
    server-side check performs. The kwarg / env var are kept readable
    for one deprecation cycle (bead nexus-f0r8p.5) but have no effect
    on what is sent; this is the only remaining observable effect.
    """
    global _skip_existing_deprecation_logged
    if _skip_existing_deprecation_logged:
        return
    _skip_existing_deprecation_logged = True
    _log.warning(
        "http_vector_skip_existing_deprecated",
        message=(
            "skip_existing / NX_UPSERT_SKIP_EXISTING=1 is deprecated "
            "(RDR-181): the client-side existence probe it drove is "
            "redundant now that server-side embed-skip is authoritative. "
            "This flag no longer filters the outgoing batch; use "
            "force_re_embed / NX_UPSERT_SKIP_EXISTING=0 to change what "
            "the server does with existing chashes."
        ),
    )


# ── Endpoint resolution (nexus-pebfx.1) ──────────────────────────────────────
#
# The supervisor (``nx daemon service start``) publishes ``{host, port,
# token}`` to the ServiceRegistry lease (``storage_service_addr.<uid>``)
# after a healthy ``/health`` — and allocates a NEW free port on every
# (re)start. Resolution order:
#
#   1. ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env — each half overrides
#      independently (operator/test override; read fresh on every call).
#   2. ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` env halves (nexus-edwlp:
#      T2 parity via service_endpoint.env_host_port_url — always http).
#   3. The ServiceRegistry lease (cached; invalidated on 401 / connection
#      refused so clients ride through supervisor auto-restarts).
#   4. FAIL LOUD. The legacy hardcoded localhost default is retired — a
#      silent wrong-port fallback is a correctness hazard.

_endpoint_lock = threading.Lock()
#: Cached (base_url, token) from the LEASE only — env halves are read fresh.
#: Module-global: shared by every HttpVectorClient instance and thread in the
#: process (the client itself is a process-wide singleton). Populated only on
#: a SUCCESSFUL discovery — a missing lease is never cached, so a client
#: started before the supervisor picks the lease up as soon as it appears.
_lease_cache: tuple[str, str | None] | None = None


def _discover_lease() -> tuple[str | None, str | None]:
    """(url, token) from the supervisor's lease, or (None, None).

    RDR-152 nexus-fjwxh: delegates to the centralized
    :func:`nexus.db.service_endpoint.discover_lease` so every storage client
    (T2 stores, catalog, T3) shares ONE discovery implementation. Kept as a
    module-local name because the catalog client and the discovery tests
    import ``_discover_lease`` from here.
    """
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 — deferred to avoid circular import

    return discover_lease()


def _resolve_endpoint() -> tuple[str, str]:
    """Return ``(base_url, token)`` per the resolution order above."""
    global _lease_cache
    # env FIRST, then the persisted config.yml credential (RDR-166 nexus-v3p0x:
    # a greenfield managed user who ran `nx config set service_url/service_token`
    # must reach a resolvable endpoint with no env exported). get_credential
    # encodes env>config.yml precedence, so an exported env var still wins.
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    env_url = (get_credential("service_url") or "").strip().rstrip("/") or None
    env_token = (get_credential("service_token") or "").strip() or None
    url, token = env_url, env_token
    if url is None:
        # nexus-edwlp: honor the NX_SERVICE_HOST/PORT env halves (T2 parity —
        # resolve_service_config has always read them; the vector path
        # previously skipped straight to the lease and failed loud on a box
        # with only host/port/token exported). Env wins over the lease, the
        # documented T2 trade-off; the 401/refused single retry corrects a
        # stale env against a restarted supervisor.
        from nexus.db.service_endpoint import env_host_port_url  # noqa: PLC0415 — deferred to avoid circular import

        url = env_host_port_url()
    if url is None or token is None:
        with _endpoint_lock:
            if _lease_cache is None:
                discovered = _discover_lease()
                if discovered[0] is not None:
                    # Cache ONLY on success: a (None, None) miss must not
                    # stick, or a client started before the supervisor would
                    # never discover it (dual-review S1).
                    _lease_cache = discovered  # type: ignore[assignment]
            lease_url, lease_token = _lease_cache or (None, None)
        url = url or lease_url
        token = token or lease_token
        # "credential" = env-or-config.yml (get_credential precedence); the
        # source here is "configured" vs "lease", not specifically env.
        if env_url is not None and token is lease_token and token is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="credential", token_source="lease"
            )
        elif env_token is not None and url is lease_url and url is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="lease", token_source="credential"
            )
    if url is None or token is None:
        # nexus-0rwwv: this is the EXACT wall an un-migrated 5.x→6.x user
        # hits on their first `nx search` — their vector data sits in the
        # retired local Chroma store and the stock remedy ("start the
        # supervisor") is wrong for them. Append the migration pointer.
        from nexus.migration.guided_upgrade import (  # noqa: PLC0415 — deferred import — the bridge dies with the migration module at RDR-155 P4b
            endpoint_failure_migration_hint,
        )

        raise RuntimeError(
            "nexus-service endpoint is not resolvable: T3 vector serving "
            "routes through the nexus-service HTTP API (RDR-155 Phase 4a — "
            "the direct Chroma serving paths are retired). Either start the "
            "supervisor with 'nx daemon service start' (publishes the "
            "endpoint lease this client auto-discovers), set the managed "
            "endpoint with 'nx config set service_url/service_token', or export "
            "NX_SERVICE_URL / NX_SERVICE_TOKEN explicitly."
            + endpoint_failure_migration_hint()
        )
    return url, token


def _invalidate_endpoint() -> None:
    """Drop the cached lease so the next call re-discovers (port churn)."""
    global _lease_cache
    with _endpoint_lock:
        _lease_cache = None


def _is_retryable_endpoint_error(exc: Exception) -> bool:
    """The three auto-restart signatures (dual-review S2 added RST):

    - 401: token rotated + republished with the lease.
    - connection refused: supervisor restarted; old port is dead.
    - connection reset (incl. ``http.client.RemoteDisconnected``): the
      supervisor SIGTERMs the JVM process group on restart, so a request
      IN FLIGHT at restart time gets a TCP RST, not a refusal. Every
      operation this client issues is idempotent (upsert on
      (tenant, collection, chash) ON CONFLICT; deletes; reads), so a
      single retry after a mid-flight reset is safe.
    """
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 401
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        return isinstance(reason, (ConnectionRefusedError, ConnectionResetError))
    return isinstance(exc, (ConnectionRefusedError, ConnectionResetError))


# ── HTTP transport ────────────────────────────────────────────────────────────


def _request_once(
    method: str, path: str, *, tenant: str, timeout: int, body: dict | None
) -> Any:
    """One HTTP round-trip against the currently-resolved endpoint.

    Raises the raw ``urllib.error`` exceptions — the retry wrapper below
    classifies them; the public ``_post``/``_get`` wrap HTTP errors into
    :class:`VectorServiceError`.
    """
    import urllib.request  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    base_url, token = _resolve_endpoint()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Nexus-Tenant": tenant,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


#: Backoff schedule for gateway-transient HTTP codes (502/503/504). Found by
#: the nexus-duoak.4 scaling sweep: concurrent CCE upsert batches slow
#: server-side embedding past the gateway timeout, and a single unretried 504
#: killed an entire ``nx index repo`` run. Upserts are idempotent
#: (content-addressed), so bounded retry is safe for every /v1 call family.
_GATEWAY_RETRY_SLEEPS: tuple[float, ...] = (2.0, 5.0, 10.0)
_GATEWAY_RETRY_CODES = frozenset({502, 503, 504})

#: Per-collection chunk cap for a SINGLE /v1/vectors/upsert-chunks POST
#: (nexus-nf3n7). CCE collections (docs/knowledge/rdr — voyage-context-3) embed
#: far slower server-side, so a large batch can exceed the control-plane
#: requestTimeout (30s time-to-response-start) and 504; code (voyage-code-3)
#: sustains the full service write cap. :meth:`HttpVectorClient.upsert_chunks`
#: pages any oversize id set into <=cap sub-POSTs, so this is the ONE choke point
#: every caller inherits — the ChunkBatcher's flush AND the oversize per-file
#: fallbacks in prose_indexer / code_indexer / doc_indexer, plus exporter,
#: pipeline, reindex and consolidation. Values match the ChunkBatcher's own
#: per-collection flush cap (live 504 at 172 CCE chunks, 2026-07-04).
_CCE_UPSERT_CHUNK_CAP = 64
_CODE_UPSERT_CHUNK_CAP = 300
_CCE_COLLECTION_PREFIXES = frozenset({"docs", "knowledge", "rdr"})


def per_collection_chunk_cap(collection: str) -> int:
    """Max chunks per single ``/v1/vectors/upsert-chunks`` POST for *collection*.

    This is ONE constraint — the largest batch whose server-side embed + write +
    HNSW completes within the control-plane 30s requestTimeout (nexus-nf3n7). It
    is DELIBERATELY shared (not two independent knobs) by:
      * the ChunkBatcher flush cap (``indexer._cap_for`` delegates here): the
        batcher accumulates then flushes <=cap chunks, i.e. each flush is one POST
        of <=cap; and
      * this client's oversize paging: :meth:`HttpVectorClient.upsert_chunks`
        pages a too-large id set into <=cap sub-POSTs.
    Both emit POSTs bounded by the SAME timeout, so tuning this value changes both
    BY DESIGN — and any change must be validated against the CP timeout, never
    raised for cross-file batching throughput alone.

    Value: 64 for CCE (docs/knowledge/rdr — voyage-context-3, slow server-side
    embed; live 504 at 172 CCE chunks 2026-07-04) is the CONSERVATIVE proven-safe
    cap. conexus's root-cause relay suggested ~128 for the direct path pending a
    re-gate p99 — a throughput optimization tracked in nexus-o1mbu / nexus-9mzkd,
    not taken here without that measurement. Code (voyage-code-3) sustains 300.
    """
    prefix = collection.split("__", 1)[0]
    return _CCE_UPSERT_CHUNK_CAP if prefix in _CCE_COLLECTION_PREFIXES else _CODE_UPSERT_CHUNK_CAP


def _wait_for_lease_republication() -> None:
    """Bounded poll for the lease to republish, priming ``_lease_cache`` if
    it reappears (nexus-7dsgp, GH #1405 defect 1).

    Called between :func:`_invalidate_endpoint` and the retry attempt in
    :func:`_request`: a retry landing in the 5-10s supervisor-respawn gap
    (old lease TTL expired, new lease not yet published) would otherwise
    race the SAME ``(None, None)`` miss the invalidated attempt just hit.
    Polling here gives the new lease a chance to appear within the budget
    and caches it directly, so the retry's :func:`_resolve_endpoint` picks
    it up on the first read instead of gambling on timing.

    Never for the managed-cloud URL path (bead requirement — mirrors
    :func:`~nexus.db.service_endpoint.recover_endpoint_from_lease`'s
    identical guard): when ``NX_SERVICE_URL``/``service_url`` is configured
    the endpoint is not lease-sourced at all, so this no-ops immediately
    rather than burning the wait budget on a lease that will never appear.

    ALSO never when ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` are pinned
    (code-review round 1, Medium): :func:`_resolve_endpoint`'s own
    precedence (``url = url or lease_url``, line ~153) means an env-pinned
    ``url`` is NEVER overridden by a freshly-discovered lease, however
    fresh — priming ``_lease_cache`` here would be pure wasted latency
    with ZERO possibility of the retry actually picking it up, since the
    retry re-reads the SAME pinned env url first. The env-pinned box's
    connection-class retry still fires (dual-review H1, unchanged); it
    just retries against the identical (still-dead) pinned endpoint, same
    as it always has — the fix is only "don't ALSO wait 12s for nothing."
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    if (get_credential("service_url") or "").strip():
        return
    from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred to avoid circular import
        DEFAULT_LEASE_WAIT_BUDGET_S,
        discover_lease_with_wait,
        env_host_port_url,
    )

    if env_host_port_url() is not None:
        return

    global _lease_cache
    discovered = discover_lease_with_wait(budget_s=DEFAULT_LEASE_WAIT_BUDGET_S)
    if discovered[0] is not None:
        with _endpoint_lock:
            _lease_cache = discovered  # type: ignore[assignment]


def _request(
    method: str, path: str, *, tenant: str, timeout: int, body: dict | None
) -> Any:
    """Round-trip with ONE re-resolve retry on the auto-restart signatures.

    The supervisor allocates a new port (and republishes the lease, token
    included) on every restart; a 401 or connection-refused against the
    cached endpoint therefore means "re-read the lease and try once more"
    (nexus-pebfx.1), not "give up". A second failure surfaces normally —
    no retry loops.

    Gateway-transient HTTP codes (``_GATEWAY_RETRY_CODES``) additionally get
    a bounded backoff retry (``_GATEWAY_RETRY_SLEEPS``); all other HTTP
    errors propagate immediately — 4xx/500 are not transient.

    Budget arithmetic (nexus-7dsgp, GH #1405 defect 1 — "must not stack
    with existing retry wrappers into unbounded totals"): the RETRY branch
    below adds ``_wait_for_lease_republication()``'s bounded 12s poll on
    top of the existing two-attempt shape (each attempt already bounded by
    ``timeout`` plus up to 17s of gateway backoff). Worst case for one
    ``_request`` call: attempt 1 (~timeout, or +17s if gateway-transient)
    + 12s lease wait + attempt 2 (~timeout, or +17s again) — a fixed 12s
    added to the pre-existing two-attempt total, never unbounded.
    """
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    def _once_with_gateway_retry() -> Any:
        for i, delay in enumerate((*_GATEWAY_RETRY_SLEEPS, None)):
            try:
                return _request_once(
                    method, path, tenant=tenant, timeout=timeout, body=body
                )
            except urllib.error.HTTPError as exc:
                if exc.code not in _GATEWAY_RETRY_CODES or delay is None:
                    raise
                _log.warning(
                    "vector_gateway_retry",
                    path=path,
                    code=exc.code,
                    attempt=i + 1,
                    sleep_s=delay,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # loop always returns or raises

    try:
        return _once_with_gateway_retry()
    # Narrow catch (dual-review H1): only the transport/auth error families
    # participate in retry classification. RuntimeError from an unresolvable
    # endpoint propagates untouched — fail-loud must never become a retry.
    #
    # nexus-7dsgp (GH #1405 defect 1) considered ALSO catching the bare
    # RuntimeError here (the "not resolvable" first-attempt case, no
    # connection-refused precursor) and retrying it with the same bounded
    # wait below. That was reverted (test-driven, nexus-1091's aspect-worker
    # drain suite): a RuntimeError has NO evidence a lease was ever
    # resolved — every cold-start caller with no supervisor running AT ALL
    # (every unit test that touches T3 without a fake service, and any
    # genuinely-unconfigured production install) would silently start
    # paying the FULL 12s wait before its immediate fail-loud, a real
    # latency regression with no compensating benefit for a case that will
    # never resolve. The connection-class branch below has the opposite,
    # load-bearing property: it only fires after ``_resolve_endpoint``
    # ALREADY succeeded once (the failing call reached an actual TCP
    # attempt) — i.e. positive evidence of exactly the lease-was-working,
    # now-mid-respawn scenario the bead's trigger names ("connect-refused
    # against a LEASE-RESOLVED LOCAL endpoint"). The wait belongs only
    # where that evidence exists.
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError) as exc:
        # TimeoutError is intentionally NOT in this retry classifier (it is not an
        # auto-restart signature); it propagates straight to the _get/_post handler,
        # which reframes it for managed endpoints (nexus-kf679).
        if not _is_retryable_endpoint_error(exc):
            raise
        _log.info(
            "vector_endpoint_reresolve",
            path=path,
            reason=type(exc).__name__,
        )
        _invalidate_endpoint()
        # nexus-7dsgp: give a not-yet-republished lease a bounded chance to
        # appear before the retry re-reads it — see _wait_for_lease_republication's
        # docstring for the managed-cloud exclusion and budget arithmetic.
        _wait_for_lease_republication()
        return _once_with_gateway_retry()


def _managed_remedy() -> str | None:
    """Remedy text when the client is pointed at an EXPLICIT managed endpoint.

    RDR-001 (nexus-kf679): a misconfigured managed-cloud endpoint otherwise fails
    at the first /v1 call with a bare connection error / HTTP 401 and no guidance.
    When ``NX_SERVICE_URL`` is explicitly set we reframe that failure with an
    actionable remedy. Returns ``None`` for the local/lease topology
    (``NX_SERVICE_URL`` unset) so a local user's transient errors are NEVER
    reframed as a managed-service problem — and their error type/flow is unchanged.

    Note: a managed-cloud user with ``NX_SERVICE_URL`` UNSET is not a silent dead
    zone — there is no local supervisor lease to discover, so
    :func:`_resolve_endpoint` fails loud first ("export NX_SERVICE_URL / TOKEN")
    before any request reaches here. This reframing covers the set-but-wrong case.

    Exception-type note: for an explicit managed endpoint, connection-level errors
    (URLError/ConnectionError/TimeoutError) are surfaced by :func:`_get`/:func:`_post`
    as :class:`VectorServiceError` (``code=None``) rather than the raw urllib/OSError
    — callers that classify transient failures by raw type should catch
    ``VectorServiceError`` for the managed path. Local callers are unaffected.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    # env FIRST, then config.yml — so a config.yml-only greenfield user gets the
    # actionable managed remedy on a 401/connection error, not a bare error
    # (RDR-166 nexus-v3p0x).
    base = (get_credential("service_url") or "").strip()
    if not base:
        return None
    return (
        f"the managed nexus service at {base} could not be reached/authenticated "
        "— check NX_SERVICE_URL is reachable and NX_SERVICE_TOKEN is valid "
        "(verify with `nx service probe` or `nx doctor`)."
    )


def _post(path: str, body: dict, *, tenant: str = "default", timeout: int = 120) -> Any:
    """POST JSON to the service endpoint, return parsed response body.

    ``timeout`` defaults to 120s for read/search/delete paths. The upsert-chunks
    call site passes 600s: a 300-chunk CCE (voyage-context-3) upsert batch
    routinely exceeds 120s server-side (embed is synchronous in the request);
    the RDR-155 production migration false-timed-out on exactly this until
    raised (bead nexus-rvfwj, 2026-06-10 — docs__1-16 + docs__1-1 evidence).
    Per dual-review S2 the raise is deliberately NOT global — a slow search
    should still fail fast.
    """
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    try:
        return _request("POST", path, tenant=tenant, timeout=timeout, body=body)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:  # noqa: BLE001 — error-body decode is best-effort; fall back to raw bytes
            err = {"error": body_bytes.decode(errors="replace")}
        msg = f"POST {path} → HTTP {e.code}: {err.get('error', err)}"
        remedy = _managed_remedy() if e.code in (401, 403) else None
        if remedy:
            msg += f"\n{remedy}"
        raise VectorServiceError(msg, code=e.code) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        # Connection-level failure (bad/unreachable endpoint). Reframe with a
        # remedy ONLY for an explicit managed endpoint; local/lease users keep
        # the original error and flow unchanged.
        remedy = _managed_remedy()
        if remedy is None:
            raise
        raise VectorServiceError(f"POST {path} failed: {e}\n{remedy}") from e


def _get(path: str, *, tenant: str = "default") -> Any:
    """GET from the service endpoint, return parsed response body."""
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    try:
        return _request("GET", path, tenant=tenant, timeout=30, body=None)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:  # noqa: BLE001 — error-body decode is best-effort; fall back to raw bytes
            err = {"error": body_bytes.decode(errors="replace")}
        msg = f"GET {path} → HTTP {e.code}: {err.get('error', err)}"
        remedy = _managed_remedy() if e.code in (401, 403) else None
        if remedy:
            msg += f"\n{remedy}"
        raise VectorServiceError(msg, code=e.code) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        remedy = _managed_remedy()
        if remedy is None:
            raise
        raise VectorServiceError(f"GET {path} failed: {e}\n{remedy}") from e


class VectorServiceError(RuntimeError):
    """Raised when the vector service returns an error.

    ``code`` carries the HTTP status when the failure was an HTTP error
    response (404 from an older service JAR, 422 model-unavailable, ...);
    ``None`` for transport-level failures. Callers use it for
    deployment-skew fallbacks (RDR-156 P3: /stats absent on a pre-catalog-005
    JAR → fall back to /collections + /count).
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# ── Collection-handle stub ────────────────────────────────────────────────────


class _ServiceCollectionStub:
    """Minimal Chroma-collection-like handle for doc_indexer staleness + prune.

    doc_indexer._index_document uses the collection handle for:
      - Incremental staleness check: ``col.get(where=..., include=[...], limit=N)``
      - Stale-chunk prune: ``col.delete(ids=[...])``

    Both are forwarded to the service's HTTP API so the Python indexer
    stays consistent with the service's Chroma view.

    RDR-152 Seam B (nexus-gmiaf.22): this stub is the minimal surface
    required to satisfy doc_indexer's incremental-sync protocol without
    adding a full Chroma collection client to the service mode.
    """

    def __init__(self, name: str, tenant: str = "default") -> None:
        self._name = name
        self._tenant = tenant

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
        *,
        include_source_uri: bool = False,
    ) -> dict:
        """Query chunks from the service. Returns Chroma-style result dict.

        RDR-152 nexus-enehl: added ``ids`` parameter to support the
        frecency manifest-based lookup path (``col.get(ids=natural_ids,
        include=["metadatas"])``). When ``ids`` is provided the request is
        routed to ``/v1/vectors/store-get``; when ``where`` is provided it
        is routed to ``/v1/vectors/get`` (staleness-check path).
        """
        try:
            if ids is not None:
                # Manifest-based lookup: fetch specific chunk IDs
                body: dict[str, Any] = {
                    "collection": self._name,
                    "ids": ids,
                    "limit": limit,
                    "offset": offset,
                }
                if include_source_uri:
                    body["include_source_uri"] = True
                result = _post("/v1/vectors/store-get", body, tenant=self._tenant)
            else:
                # Where-filter lookup (incremental-sync staleness check)
                body = {
                    "collection": self._name,
                    "limit": limit,
                    "offset": offset,
                }
                if where:
                    body["where"] = where
                if include:
                    body["include"] = include
                if include_source_uri:
                    body["include_source_uri"] = True
                result = _post("/v1/vectors/get", body, tenant=self._tenant)
            # Normalise to Chroma shape: {ids, documents, metadatas}.
            # RDR-169 G5 (nexus-jkv85): chashes + spans always present when service is G5+.
            # source_uris present only when include_source_uri=True was forwarded.
            out: dict[str, Any] = {
                "ids":       result.get("ids", []),
                "documents": result.get("documents", []),
                "metadatas": result.get("metadatas", []),
            }
            for key in ("chashes", "source_uris", "spans"):
                if key in result:
                    out[key] = result[key]
            return out
        except VectorServiceError as exc:
            _log.warning(
                "service_collection_get_failed",
                collection=self._name,
                error=str(exc),
            )
            return {"ids": [], "documents": [], "metadatas": []}

    @property
    def name(self) -> str:
        """Chroma ``Collection.name`` parity — indexer logging does
        ``getattr(col, "name", "?")`` and logged '?' for every service-mode
        collection (tail-review suggestion, nexus-c9xr2)."""
        return self._name

    def count(self) -> int:
        """Chunk count for this collection — Chroma ``Collection.count()``
        parity (nexus-c9xr2: the collection re-embed / backfill paths call
        ``col.count()`` on the handle ``get_collection`` returns; without
        this the stub was the only handle shape missing it).

        Unlike ``get``/``delete`` this does NOT catch-and-degrade: a wrong
        count silently reshapes paging loops, so the caller owns the
        boundary (re-embed wraps it in its ClickException convention).
        """
        from urllib.parse import quote  # noqa: PLC0415 — stdlib deferred to call site (urllib.parse)

        result = _get(
            "/v1/vectors/count?collection=" + quote(self._name),
            tenant=self._tenant,
        )
        return int(result.get("count", 0))

    def get_all_metadata(self, where: dict | None = None) -> dict:
        """ids + metadata for EVERY chunk in this collection in ONE round trip
        (nexus-duoak follow-up: collapses the indexer's staleness-cache-build
        paginated ``/get`` loop -- measured ~113s of a ~116s phase on this
        repo's own 24k-chunk ``code__`` collection).

        Unlike :meth:`get`/:meth:`delete`, this does NOT catch-and-degrade to
        an empty result on failure -- a silent empty result here would look
        identical to "collection has 0 chunks" to the caller, which would
        build an empty staleness cache instead of falling back to the
        paginated path (mirrors :meth:`count`'s "caller owns the boundary"
        contract, not :meth:`get`'s silent-degrade one). Raises
        :class:`VectorServiceError` on any failure, including the server's
        422 "too many rows for one call" cap -- callers should catch and
        fall back to the paginated :meth:`get` loop.
        """
        body: dict[str, Any] = {"collection": self._name}
        if where:
            body["where"] = where
        result = _post("/v1/vectors/get-all-metadata", body, tenant=self._tenant)
        return {
            "ids": result.get("ids", []),
            "metadatas": result.get("metadatas", []),
        }

    def delete(self, ids: list[str]) -> None:
        """Delete chunks by ID from the service."""
        if not ids:
            return
        try:
            _post(
                "/v1/vectors/store-delete",
                {"collection": self._name, "ids": ids},
                tenant=self._tenant,
            )
        except VectorServiceError as exc:
            _log.warning(
                "service_collection_delete_failed",
                collection=self._name,
                count=len(ids),
                error=str(exc),
            )


# ── HttpVectorClient ─────────────────────────────────────────────────────────


class HttpVectorClient:
    """Drop-in subset of ``T3Database`` that routes to the Java service.

    Implements only the methods exercised by the MCP tools and the
    doc_indexer upsert path:

    - :meth:`upsert_chunks` / :meth:`upsert_chunks_with_embeddings`
    - :meth:`search`
    - :meth:`put`
    - :meth:`get_by_id`
    - :meth:`delete_by_id`
    - :meth:`list_collections`
    - :meth:`list_store` / :meth:`collection_info`
    - :meth:`find_ids_by_title` / :meth:`batch_delete` (nexus-umvh2)

    Methods NOT implemented here (not needed for Seam B or stubbed
    as no-ops) will raise ``NotImplementedError`` or return safe defaults.
    Taxonomy hooks and the ``_client`` attribute are also excluded — the
    Python code that uses them still routes through T3Database (flag unset).

    Thread-safe: all state is in the HTTP request payload.
    """

    # Exposed so mcp_infra.get_collection_names() and taxonomy hooks can
    # skip the expensive list call. Set to None to force a real fetch.
    # Tests may patch this.
    _tenant: str

    def __init__(self, *, tenant: str = "default") -> None:
        self._tenant = tenant

    # ── Context manager (no-op: stateless HTTP, parity with T3Database) ──────

    def __enter__(self) -> "HttpVectorClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass  # No persistent connection to close.

    # NOTE — no ``_client`` attribute, deliberately (pinned by
    # tests/db/test_http_vector_client.py): chroma-client-coupled features
    # (taxonomy-via-chroma, catalog span/link embedding probes, raw collection
    # surgery) retire with the Chroma serving paths (RDR-155 P4a.2,
    # nexus-1k8s1). Accessing ``._client`` raises AttributeError — callers
    # guard with :func:`is_service_backed`; pg-side equivalents are tracked
    # follow-ons (taxonomy: nexus-gmiaf.21+).

    # ── Seam B write path ────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
        *,
        force_re_embed: bool | None = None,
        embeddings: list[list[float]] | None = None,
        skip_existing: bool | None = None,
    ) -> None:
        """Embed + write via the Java service.

        Dedup + conflict-merge are SERVER-ENFORCED (nexus-57dh4): the service's
        ``PgVectorRepository.upsertChunksInternal`` does first-wins in-batch dedup
        and ``ON CONFLICT (tenant_id, collection, chash) DO UPDATE``. There is no
        client-side quota check or 300-record cap on this path — the whole id set
        is sent in one POST. (The old "quota-check" framing was a ChromaDB-Cloud
        leftover; Postgres has no such limit.)

        CHUNKING STAYS PYTHON — this method is called with pre-chunked text.
        Embeddings are computed server-side by default (Seam B). The ONE
        exception is the same-model migration PASSTHROUGH (nexus-hxry2): when
        ``embeddings`` is supplied, the vectors are sent and stored verbatim and
        the server skips the (billed) re-embed — used only when the source
        collection's model equals the target's wired model, so the vectors are
        already correct. Every non-migration caller leaves ``embeddings`` None.
        (Note: :meth:`upsert_chunks_with_embeddings` deliberately still DISCARDS
        its vectors — indexers re-embed server-side as the single authority.)

        ``skip_existing`` (or env ``NX_UPSERT_SKIP_EXISTING=1``): DEPRECATED
        (RDR-181, bead nexus-f0r8p.5) — kept readable for one deprecation
        cycle only, no longer changes what is sent. It used to pre-filter
        ids through :meth:`existing_ids` (a client-side round-trip) and
        drop chunks the collection already held (nexus-7zuzz orphan
        remediation), but that pre-filter also silently skipped the ON
        CONFLICT DO UPDATE metadata refresh — the "metadata caveat" RDR-181
        closed. Server-side embed-skip
        (``PgVectorRepository.upsertChunksInternal``'s existence-partition,
        beads .1/.2) now does the equivalent filtering losslessly and
        universally, with no extra round-trip and no opt-in required.
        Setting this kwarg (or the env var) is now a no-op on the outgoing
        batch — the whole batch is always sent — and only triggers a
        one-time deprecation log line (see
        :func:`_warn_skip_existing_deprecated`).

        ``force_re_embed`` (RDR-181, bead nexus-f0r8p.3; or the deprecated
        env escape ``NX_UPSERT_SKIP_EXISTING=0``): tells the SERVER to bypass
        its own existence-partition entirely (``PgVectorRepository``'s
        RDR-181 embed-skip optimization) and re-embed every chunk in the
        batch, even ones whose chash already has a stored vector — the rare
        model-drift-within-a-collection recompute, and the escape for the
        (0%-hit) first-index path so it never pays for the server-side
        existence SELECT with no offsetting benefit. This is now the ONLY
        kwarg/env lever on this method that changes what is sent or how the
        server treats existing chashes — ``skip_existing`` above no longer
        does. Sending it is a no-op when ``embeddings`` is supplied (the
        migration passthrough already skips the server's existence check
        unconditionally).
        """
        if not ids:
            return
        if skip_existing is None:
            skip_existing = os.environ.get("NX_UPSERT_SKIP_EXISTING", "") == "1"
        if force_re_embed is None:
            force_re_embed = os.environ.get("NX_UPSERT_SKIP_EXISTING", "") == "0"
        if skip_existing:
            _warn_skip_existing_deprecated()
        # nexus-nf3n7: page an oversize id set into <=cap sub-POSTs so no single
        # request exceeds the control-plane requestTimeout (a large CCE upsert
        # 504s otherwise). This is the ONE choke point — the ChunkBatcher already
        # sends <=cap so its flushes are a single page (unchanged); the oversize
        # per-file fallbacks (prose/code/doc) and every other caller inherit the
        # cap here. NOT atomic across pages: a sub-POST failure raises with earlier
        # pages already committed, but the write is idempotent-retry-safe — ON
        # CONFLICT dedup + full-file staleness retry heal a partial mid-paging
        # failure next run. Same-model vector PASSTHROUGH (nexus-hxry2): supplied
        # vectors are sliced in lockstep with the ids.
        cap = per_collection_chunk_cap(collection)
        metas = metadatas or [{}] * len(ids)
        n = len(ids)
        for start in range(0, n, cap):
            end = min(start + cap, n)
            body: dict[str, Any] = {
                "collection": collection,
                "ids": ids[start:end],
                "documents": documents[start:end],
                "metadatas": metas[start:end],
            }
            if embeddings is not None:
                body["embeddings"] = embeddings[start:end]
            if force_re_embed:
                body["force_re_embed"] = True
            _post("/v1/vectors/upsert-chunks", body, tenant=self._tenant, timeout=600)
        _log.debug(
            "http_vector_upsert_chunks",
            collection=collection,
            count=n,
            pages=(n + cap - 1) // cap if n else 0,
        )

    def upsert_chunks_with_embeddings(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict] | None = None,
        *,
        force_re_embed: bool = False,
    ) -> None:
        """Server-side embed path: forward chunk text, ignore caller's embeddings.

        The Java service embeds server-side; the Python-side embeddings are
        discarded (Seam B: embed moves to JVM). This method signature matches
        ``T3Database.upsert_chunks_with_embeddings`` so it works transparently
        as a drop-in.

        Param name ``collection_name`` (not ``collection``) matches
        ``T3Database.upsert_chunks_with_embeddings`` so callers using the kwarg
        form (code_indexer.py:470, prose_indexer.py:233, exporter.py:431,448)
        don't get a TypeError (nexus-7zuzz).

        ``force_re_embed`` (RDR-181 §Approach step 3): forwarded verbatim to
        :meth:`upsert_chunks` so the indexer's ``--force`` path reaches the
        server's ``forceReEmbed`` escape (bypass the existence-partition,
        re-embed every chunk in the batch) — closes the plumbing gap where
        beads .3/.5 wired the engine and client kwarg but no production
        caller ever set it.
        """
        self.upsert_chunks(
            collection_name, ids, documents, metadatas=metadatas,
            force_re_embed=force_re_embed,
        )

    def put(
        self,
        collection: str,
        content: str,
        title: str = "",
        tags: str = "",
        category: str = "",
        session_id: str = "",
        source_agent: str = "",
        store_type: str = "knowledge",
        ttl_days: int = 0,
        catalog_doc_id: str = "",
    ) -> str:
        """Upsert *content* into *collection*. Returns the document ID.

        Drop-in parity with ``T3Database.put`` (nexus-7zuzz): same parameter
        list, same doc_id derivation (sha256(content)[:32]), and metadata built
        via the SAME :func:`nexus.metadata_schema.make_chunk_metadata` factory
        that T3Database.put uses — parity by construction, not by duplication.

        ``store_type`` is accepted for API symmetry but intentionally not
        forwarded: T3Database also ignores it (RDR-101 Phase 5c dropped
        store_type from ALLOWED_TOP_LEVEL; content_type derives from the
        collection prefix, identical logic is applied here).

        ``catalog_doc_id`` is an HTTP-path superset: T3Database.put() accepts
        the param but normalize() strips it from the Chroma write (not in
        ALLOWED_TOP_LEVEL); on the T3 path catalog association is via the hook
        chain, not chunk metadata. HttpVectorClient stamps it into the service
        request body so the Java layer can persist the tumbler cross-reference
        if the service endpoint accepts it. This is a documented divergence, not
        a parity gap — see EXCLUSIONS comment in the parity test.

        Single-chunk: one HTTP call per put() call. T3Database.put uses
        ``fail_on_oversized=True``; the server is responsible for rejecting
        oversized content on the HTTP path.
        """
        from nexus.corpus import (  # noqa: PLC0415 — circular-dep avoidance (corpus)
            embedding_model_for_collection_name,
            index_model_for_collection,
        )
        from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — circular-dep avoidance (metadata_schema)

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        doc_id = content_hash[:32]
        now_iso = datetime.now(UTC).isoformat()

        # Derive content_type from collection prefix — mirrors T3Database.put
        # at t3.py:860-870 exactly.
        prefix_to_ct = {
            "code__": "code",
            "docs__": "prose",
            "rdr__": "markdown",
            "knowledge__": "prose",
        }
        content_type = "prose"
        for prefix, ct in prefix_to_ct.items():
            if collection.startswith(prefix):
                content_type = ct
                break

        metadata = make_chunk_metadata(
            content_type=content_type,
            chunk_text_hash=content_hash,
            content_hash=content_hash,
            chunk_start_char=0,
            chunk_end_char=len(content),
            indexed_at=now_iso,
            embedding_model=(
                embedding_model_for_collection_name(collection)
                or index_model_for_collection(collection)
            ),
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            source_agent=source_agent,
            session_id=session_id,
        )

        # catalog_doc_id: HTTP-path superset (see docstring). Stamp when present;
        # omit when empty to keep the body clean for the legacy/no-catalog path.
        if catalog_doc_id:
            metadata["catalog_doc_id"] = catalog_doc_id

        body: dict[str, Any] = {
            "collection": collection,
            "doc_id": doc_id,
            "content": content,
            "metadata": metadata,
        }
        result = _post("/v1/vectors/store-put", body, tenant=self._tenant)
        return result.get("id", doc_id)

    # ── Read path ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection_names: list[str],
        n_results: int = 10,
        where: dict | None = None,
        *,
        cluster_by: str = "",
        threshold: float | None = None,
        structured: bool = False,
        include_source_uri: bool = False,
    ) -> list[dict] | dict:
        """Semantic search via the Java service.

        Param name ``collection_names`` (not ``collections``) matches
        ``T3Database.search`` (nexus-7zuzz). The HTTP body key stays
        ``"collections"`` — that is what the Java VectorHandler reads.

        The service embeds the query server-side and returns ranked results.
        Returns the same list-of-dicts shape as ``T3Database.search()``
        when ``structured=False``, or a ``{ids, tumblers, distances, collections}``
        dict when ``structured=True``.

        When ``include_source_uri=True``, gates a catalog JOIN server-side to
        populate ``source_uri`` on each row (RDR-169 G5, bead nexus-jkv85).
        Default False — omits the field so default callers pay zero JOIN cost.
        """
        body: dict[str, Any] = {
            "query": query,
            "collections": collection_names,
            "n_results": n_results,
        }
        if where:
            body["where"] = where
        if include_source_uri:
            body["include_source_uri"] = True

        results = _post("/v1/vectors/search", body, tenant=self._tenant)
        # results is a list of {id, content, distance, collection, ...}

        if structured:
            # Return the plan-runner compatible structured form
            return {
                "ids":         [r.get("id", "")         for r in results],
                "tumblers":    [r.get("tumbler", "")    for r in results],
                "distances":   [r.get("distance", 0.0)  for r in results],
                "collections": [r.get("collection", "") for r in results],
            }
        return results

    def search_metadata_scoped(
        self,
        query: str,
        collection_names: list[str],
        *,
        content_type: str | None = None,
        author: str | None = None,
        year: int | None = None,
        corpus: str | None = None,
        subtree: str | None = None,
        where: dict | None = None,
        n_results: int = 10,
    ) -> list[dict]:
        """Metadata-scoped combined search (RDR-156 P4, Decision 5; catalog-008).

        Routes to ``POST /v1/vectors/search-metadata-scoped`` —
        ``nexus.search_metadata_scoped_<dim>``, which joins the chunk table to
        the catalog manifest + documents and filters by the catalog dimensions
        in ONE statement (the unification of the ``query`` tool's app-side
        catalog-routing dance). A ``None``/empty filter is omitted (no filter on
        that dimension). ``author`` is matched case-insensitively as a SUBSTRING
        (ILIKE), ``subtree`` is a tumbler-prefix scope, ``where`` is a
        chunk-metadata equality map (JSONB containment). Returns the flat
        ``{id, content, distance, collection, chash}`` row list; ``id`` is the
        document tumbler (de-dup per id is the caller's job); ``chash`` is the
        matched chunk's hash (RDR-086 ``chunk_text_hash`` source).
        """
        body: dict[str, Any] = {
            "query": query,
            "collections": collection_names,
            "n_results": n_results,
        }
        if content_type is not None:
            body["content_type"] = content_type
        if author is not None:
            body["author"] = author
        if year is not None:
            body["year"] = year
        if corpus is not None:
            body["corpus"] = corpus
        if subtree is not None:
            body["subtree"] = subtree
        if where:
            body["where"] = where
        return _post("/v1/vectors/search-metadata-scoped", body, tenant=self._tenant)

    def search_topic_scoped(
        self,
        query: str,
        topic: str,
        collection: str,
        *,
        n_results: int = 10,
    ) -> list[dict]:
        """Topic-scoped combined search (RDR-156 P4, Decision 5).

        Routes to ``POST /v1/vectors/search-topic-scoped`` —
        ``nexus.search_topic_scoped_<dim>`` (catalog-006). Topic membership is
        chunk-level (``topic_assignments.doc_id`` is a chunk chash, nexus-sa14p),
        so results are chunk-level (``id`` is the chunk chash). Returns the flat
        ``{id, content, distance, collection}`` row list.
        """
        body: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "collection": collection,
            "n_results": n_results,
        }
        return _post("/v1/vectors/search-topic-scoped", body, tenant=self._tenant)

    def search_graph_hop(
        self,
        query: str,
        seeds: list[str],
        collection_names: list[str],
        *,
        link_type: str | None = None,
        depth: int = 1,
        direction: str = "both",
        where: dict | None = None,
        n_results: int = 10,
    ) -> list[dict]:
        """Graph-hop combined search (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).

        Routes to ``POST /v1/vectors/search-graph-hop`` —
        ``nexus.search_graph_hop_<dim>`` (catalog-007, where-extended by catalog-012): a
        ``WITH RECURSIVE`` BFS over ``catalog_links`` from ``seeds`` to ``depth`` hops
        collects the reachable document set, joins ``chunks_<dim>``, and vector-ranks.
        The single-statement unification of the ``query`` tool's ``follow_links``
        app-side graphBFS dance. ``link_type=None`` follows all edge types;
        ``direction`` is ``"out"``/``"in"``/``"both"`` (default ``"both"``, matching
        ``Catalog.graph``); ``depth`` is clamped to [1,3] service-side. ``where``
        (nexus-7ndh3) is a chunk-metadata equality map applied as JSONB containment in
        the post-BFS rank — the same semantics as ``search_metadata_scoped``'s
        ``where``. Returns the flat ``{id, content, distance, collection, chash}`` row
        list; ``id`` is the document tumbler, ``chash`` the MATCHED chunk's content
        hash (the repoint populates the RDR-086 ``chunk_text_hash`` from it).
        """
        body: dict[str, Any] = {
            "query": query,
            "seeds": seeds,
            "collections": collection_names,
            "depth": depth,
            "direction": direction,
            "n_results": n_results,
        }
        if link_type is not None:
            body["link_type"] = link_type
        if where:
            body["where"] = where
        return _post("/v1/vectors/search-graph-hop", body, tenant=self._tenant)

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        """Fetch a single chunk by ID.

        Returns a FLAT dict of ``id`` + ``content`` + all metadata fields, to
        match ``T3Database.get_by_id`` (the drop-in oracle). nexus-ij9hg: the
        prior shape (``id``/``document``/nested ``metadata``) diverged from the
        SQLite oracle, so MCP ``store_get`` / ``store_get_many`` and
        ``nx store get`` — which read ``entry["content"]`` / ``entry["title"]``
        etc. — silently rendered EMPTY content in service mode (the
        post-P4a default). That is the nexus-7zuzz behavioural-divergence class
        signature parity cannot catch.
        """
        try:
            result = _post(
                "/v1/vectors/store-get",
                {"collection": collection, "ids": [doc_id]},
                tenant=self._tenant,
            )
        except VectorServiceError:
            return None

        ids = result.get("ids") or []
        if not ids:
            return None
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        meta = metas[0] if metas else {}
        return {
            "id": ids[0],
            "content": docs[0] if docs else "",
            **(meta if isinstance(meta, dict) else {}),
        }

    def delete_by_id(self, collection: str, doc_id: str) -> bool:
        """Delete a chunk by ID. Returns True if the chunk existed."""
        try:
            result = _post(
                "/v1/vectors/store-delete",
                {"collection": collection, "ids": [doc_id]},
                tenant=self._tenant,
            )
            return result.get("deleted", 0) > 0
        except VectorServiceError:
            return False

    def collection_stats(self) -> list[dict]:
        """Per-collection live statistics via ``GET /v1/vectors/stats``.

        RDR-156 P3 (nexus-70r3c.12): served from the
        ``nexus.collection_vector_stats`` SECURITY INVOKER view — one
        round-trip for all of the tenant's collections, TOMBSTONE-FILTERED
        (chunks whose only manifest rows point to trashed documents are not
        counted; manifest-less note chunks are).

        Returns ``[{"name": ..., "dim": 384, "count": N,
        "last_write": "2026-..."}, ...]``, name ascending. Collections with
        zero live chunks do not appear. ``last_write`` may be absent.

        Raises :class:`VectorServiceError` on failure — including ``code=404``
        from a pre-catalog-005 service JAR (deployment skew); callers that
        must work across the skew use :meth:`list_collections`, which falls
        back automatically.
        """
        result = _get("/v1/vectors/stats", tenant=self._tenant)
        return result if isinstance(result, list) else []

    def list_collections(self) -> list[dict]:
        """List the tenant's vector collections with live chunk counts.

        T3Database parity: returns ``[{"name": ..., "count": N}, ...]`` —
        ``nx collection list`` and friends index both keys (the missing
        ``count`` was a live KeyError on every service-mode box, RDR-156 P3).

        Primary path is ONE ``/v1/vectors/stats`` round-trip
        (tombstone-filtered live counts, replacing T3Database's N-way
        threadpooled ``col.count()`` fan-out). On a pre-catalog-005 service
        JAR the route 404s; fall back to ``/collections`` + per-collection
        ``/count`` so the surface keeps working across the deployment skew
        (raw counts — tombstones do not exist on a pre-catalog-005 schema).

        Multi-dim collections (same name in two ``chunks_<dim>`` tables —
        cross-dim re-indexing residue) collapse to one entry, counts summed.
        """
        try:
            stats = self.collection_stats()
        except VectorServiceError as e:
            if e.code != 404:
                _log.warning("http_vector_list_collections_failed", error=str(e))
                return []
            _log.info("http_vector_stats_unavailable_fallback", error=str(e))
            return self._list_collections_via_count()
        merged: dict[str, int] = {}
        for row in stats:
            name = row.get("name", "")
            if name:
                # `or 0` guards an explicit null count, not just an absent key
                merged[name] = merged.get(name, 0) + int(row.get("count") or 0)
        return [{"name": n, "count": c} for n, c in sorted(merged.items())]

    def _list_collections_via_count(self) -> list[dict]:
        """Deployment-skew fallback: ``/collections`` names + N ``/count`` calls.

        Pre-catalog-005 JARs have no ``/stats`` route. Counts here are RAW
        (the old endpoint's semantics); a failing per-collection count is
        reported as -1 rather than dropping the collection from the listing.
        """
        try:
            result = _get("/v1/vectors/collections", tenant=self._tenant)
        except VectorServiceError as e:
            _log.warning("http_vector_fallback_collections_failed", error=str(e))
            return []
        names = [c.get("name", "") for c in result] if isinstance(result, list) else []
        out: list[dict] = []
        for name in names:
            if not name:
                continue
            try:
                out.append({"name": name, "count": self.count(name)})
            except VectorServiceError as e:
                _log.warning(
                    "http_vector_collection_count_failed",
                    collection=name,
                    error=str(e),
                )
                out.append({"name": name, "count": -1})
        return out

    def collection_exists(self, name: str) -> bool:
        """True if *name* holds at least one LIVE chunk (no create side-effect).

        T3Database parity (RDR-155 P4a.2): on the pgvector path a collection
        is a column value, so existence == "has rows for this tenant".
        Since RDR-156 P3 this reads the tombstone-filtered stats view via
        :meth:`list_collections`: a collection whose every chunk belongs to
        trashed documents reads as absent — the Decision 6 single-enforcement
        -point semantics (consumers see live state only).
        """
        return any(c.get("name") == name for c in self.list_collections())

    def count(self, collection: str) -> int:
        """Number of chunks in *collection* visible to this tenant."""
        from urllib.parse import quote  # noqa: PLC0415 — stdlib deferred to call site (urllib.parse)

        result = _get(
            "/v1/vectors/count?collection=" + quote(collection),
            tenant=self._tenant,
        )
        return int(result.get("count", 0))

    def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
        """Return the subset of *ids* present in *collection*.

        T3Database parity (``nx catalog verify`` / gc paths). Pages at 300
        ids per request to mirror the historical batch shape; a missing or
        unreachable collection resolves to the empty set, matching
        ``T3Database.existing_ids``.
        """
        if not ids:
            return set()
        found: set[str] = set()
        page = 300
        try:
            for start in range(0, len(ids), page):
                batch = ids[start : start + page]
                result = _post(
                    "/v1/vectors/store-get",
                    {"collection": collection, "ids": batch, "limit": len(batch)},
                    tenant=self._tenant,
                )
                found.update(result.get("ids") or [])
        except VectorServiceError as exc:
            _log.warning(
                "http_vector_existing_ids_failed",
                collection=collection,
                error=str(exc),
            )
            return set()
        return found

    def update_chunks(
        self,
        collection: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """Metadata-only update on existing chunks — no re-embedding.

        RDR-152 bead nexus-enehl: the frecency-only reindex path calls
        ``db.update_chunks(collection=..., ids=..., metadatas=...)`` on the
        db object.  In service mode ``db`` is an :class:`HttpVectorClient`;
        this method routes the update through the service's
        ``/v1/vectors/update-metadata`` endpoint so the frecency_score lands
        in the service's Chroma (the one search reads) — not daemon-Chroma.

        Sends in request-sized batches of 300 ids. NOTE (nexus-57dh4): this is a
        pragmatic HTTP request-size chunk, NOT a backend quota — the pgvector
        service has no 300-record limit (300 was a ChromaDB-Cloud free-tier quota,
        inapplicable to Postgres). Dedup + conflict-merge are server-enforced in
        ``PgVectorRepository.upsertChunksInternal`` (first-wins in-batch dedup +
        ``ON CONFLICT DO UPDATE``); clients need not pre-dedup or quota-check.
        The constant is reused only to keep a sane per-request size.
        """
        if not ids:
            return
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)
        # Request-size chunk only (see docstring) — not a backend quota.
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        for start in range(0, len(ids), size):
            batch_ids  = ids[start : start + size]
            batch_meta = metadatas[start : start + size]
            _post(
                "/v1/vectors/update-metadata",
                {"collection": collection, "ids": batch_ids, "metadatas": batch_meta},
                tenant=self._tenant,
            )
        _log.debug(
            "http_vector_update_chunks",
            collection=collection,
            count=len(ids),
        )

    # ── Collection-handle stub for doc_indexer staleness + prune paths ─────────

    def get_collection(self, name: str) -> "_ServiceCollectionStub":
        """Return a collection stub, raising ChromaNotFoundError if the collection does not exist.

        RDR-152 bead nexus-enehl: mirrors T3Database.get_collection() semantics
        for the frecency-only loop.  The loop catches ChromaNotFoundError and
        skips collections that have not yet been indexed.

        Checks existence via the service's ``/v1/vectors/collections`` list.
        A missing collection raises ``chromadb.errors.NotFoundError`` rather than
        creating a zombie collection (contrast with
        :meth:`get_or_create_collection`).
        """
        from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415 — optional dependency deferred (chromadb.errors)
        try:
            cols = self.list_collections()
            if not any(c.get("name") == name for c in cols):
                raise _ChromaNotFoundError(f"collection {name!r} not found in service")
        except VectorServiceError as exc:
            raise _ChromaNotFoundError(
                f"service unavailable checking collection {name!r}"
            ) from exc
        return _ServiceCollectionStub(name=name, tenant=self._tenant)

    def get_or_create_collection(self, name: str) -> "_ServiceCollectionStub":
        """Return a stub collection handle for staleness checks.

        doc_indexer._index_document / _index_pdf_incremental use the
        returned handle for:
          - ``col.get(where=..., ...)`` incremental staleness check
          - ``col.delete(ids=...)`` stale-chunk pruning

        The stub routes the staleness check through the service's
        ``/v1/vectors/get`` endpoint and routes deletes through
        ``/v1/vectors/store-delete``, making both paths work end-to-end
        against the Java service.
        """
        return _ServiceCollectionStub(name=name, tenant=self._tenant)

    def get_embeddings(self, collection_name: str, ids: list[str]):
        """Fetch stored embeddings for *ids* via the service (nexus-pebfx.7).

        Param name ``collection_name`` matches ``T3Database.get_embeddings``
        (nexus-7zuzz). The HTTP body key stays ``"collection"`` — that is what
        the Java VectorHandler reads.

        Mirrors ``T3Database.get_embeddings``: returns an ``(N, D)`` float32
        ndarray with rows in request order; ids the service does not find
        are DROPPED (``N < len(ids)``), which the search-engine caller
        already treats as a per-collection shape-mismatch failure —
        identical to the Chroma path's semantics.
        """
        import numpy as np  # noqa: PLC0415 — heavy/optional dependency deferred to call time

        result = _post(
            "/v1/vectors/get-embeddings",
            {"collection": collection_name, "ids": ids},
            tenant=self._tenant,
        )
        return np.array(result.get("embeddings", []), dtype=np.float32)

    # ── Stubs for T3Database surface not used by Seam B ─────────────────────

    def delete_collection(self, name: str) -> None:
        raise NotImplementedError("delete_collection not implemented in HttpVectorClient")

    def ids_for_source(self, collection_name: str, source_path: str) -> list[str]:
        """Return all chunk IDs for a given source path. Does not fetch content.

        Mirrors ``T3Database.ids_for_source``: paginates the service's
        ``/v1/vectors/get`` where-filter endpoint at the 300-record quota and
        returns an empty list when the collection does not exist (the service
        returns no ids). Param name ``collection_name`` matches the oracle
        (nexus-7zuzz).
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        ids: list[str] = []
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection_name,
                        "where": {"source_path": source_path},
                        "include": [],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                # Match T3Database, which suppresses ONLY collection-not-found
                # (404) and returns []. A 5xx / 422 / transport failure — or ANY
                # error mid-pagination after ids were already collected — must
                # NOT be masked as "no chunks": delete_by_source would then
                # under-delete and report success, silently orphaning the
                # unread chunks (review: over-broad catch). Re-raise so the
                # prune-stale call site's except-clause reports SKIP loudly.
                if exc.code == 404 and offset == 0:
                    return []
                raise
            page = result.get("ids", []) or []
            ids.extend(page)
            if len(page) < page_limit:
                break
            offset += len(page)  # match T3Database oracle (not += page_limit)
        return ids

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """Delete all chunks for a given source path; return the count deleted.

        nexus-vhyua: previously a NotImplementedError stub, which made
        ``nx t3 prune-stale --no-dry-run`` print 'delete failed' per path and
        silently do nothing in service mode (the post-P4a default). Now built
        from existing primitives — ``ids_for_source`` (``/v1/vectors/get``
        where-filter) + ``/v1/vectors/store-delete`` — so no new Java endpoint
        is required. Param name ``collection_name`` matches
        ``T3Database.delete_by_source`` (nexus-7zuzz).

        Count semantics differ slightly from the oracle by design: T3Database
        returns ``len(ids)`` (ids it asked to delete); this returns the sum of
        the service's CONFIRMED ``deleted`` counts. They match unless a
        concurrent delete already removed some — in which case the prune-stale
        caller's ``deleted != len(ids)`` WARN correctly fires.
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        ids = self.ids_for_source(collection_name, source_path)
        if not ids:
            return 0
        # Batch at the 300-record write quota — a source with many chunks would
        # otherwise exceed MAX_RECORDS_PER_WRITE in a single store-delete.
        batch = QUOTAS.MAX_RECORDS_PER_WRITE
        deleted = 0
        for i in range(0, len(ids), batch):
            result = _post(
                "/v1/vectors/store-delete",
                {"collection": collection_name, "ids": ids[i:i + batch]},
                tenant=self._tenant,
            )
            deleted += int(result.get("deleted", 0))
        return deleted

    def find_ids_by_title(self, collection: str, title: str) -> list[str]:
        """Return all chunk IDs whose title metadata exactly matches *title*.

        nexus-umvh2: was missing entirely, crashing ``nx store delete
        --title`` and the MCP ``store_get`` title-fallback with
        ``AttributeError`` in service mode (the post-P4a2 default). Mirrors
        :meth:`ids_for_source`'s where-filter pagination pattern
        (``/v1/vectors/get``) at the 300-record quota; T3Database parity
        (``collection`` / ``title`` param names match).
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        ids: list[str] = []
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection,
                        "where": {"title": title},
                        "include": [],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                # Match ids_for_source: a 404 on the first page means "no
                # such collection" -> no matches. A failure mid-pagination
                # must NOT be swallowed as "no more matches" -- the caller
                # (nx store delete --title) would under-delete and report
                # success.
                if exc.code == 404 and offset == 0:
                    return []
                raise
            page = result.get("ids", []) or []
            ids.extend(page)
            if len(page) < page_limit:
                break
            offset += len(page)
        return ids

    def batch_delete(self, collection: str, ids: list[str]) -> None:
        """Delete *ids* from *collection* in service-quota-bounded batches.

        nexus-umvh2: was missing entirely -- the second half of the ``nx
        store delete --title`` crash (after :meth:`find_ids_by_title`
        resolves the id list). Batches at ``QUOTAS.MAX_RECORDS_PER_WRITE``
        like :meth:`update_chunks` / :meth:`delete_by_source`.
        """
        if not ids:
            return
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        size = QUOTAS.MAX_RECORDS_PER_WRITE
        for start in range(0, len(ids), size):
            _post(
                "/v1/vectors/store-delete",
                {"collection": collection, "ids": ids[start:start + size]},
                tenant=self._tenant,
            )

    def list_store(
        self, collection: str, limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        """Return metadata for entries in *collection*, paginated.

        nexus-umvh2 sibling audit: was missing entirely, crashing ``nx
        store list``, ``nx store list --docs``, and MCP ``store_list`` in
        service mode -- the same class of bug as :meth:`find_ids_by_title`,
        just unreported because the CLI test fixtures mock the whole T3
        client with a bare ``MagicMock`` (no ``spec=``), which cannot
        surface a missing method.

        Built from the service's plain (no ``where``) ``/v1/vectors/get``
        listing, matching :meth:`ids_for_source`'s call shape. Returns
        ``[]`` on a 404 (T3Database parity: "collection does not exist" ->
        empty list, never an exception).
        """
        try:
            result = _post(
                "/v1/vectors/get",
                {
                    "collection": collection,
                    "include": ["metadatas"],
                    "limit": limit,
                    "offset": offset,
                },
                tenant=self._tenant,
            )
        except VectorServiceError as exc:
            if exc.code == 404:
                return []
            raise
        ids = result.get("ids", []) or []
        metas = result.get("metadatas", []) or []
        return [
            {"id": doc_id, **(meta if isinstance(meta, dict) else {})}
            for doc_id, meta in zip(ids, metas)
        ]

    def update_source_path(
        self, collection_name: str, old_path: str, new_path: str
    ) -> int:
        """Rewrite source_path metadata for all chunks matching *old_path*.

        nexus-h8rf6.6: was missing entirely — ``nx doctor fix-paths``
        (non-dry-run) crashed with ``AttributeError`` on the first row in
        service mode (doctor.py calls it per-row with no guard). Built from
        the where-filter get (:meth:`ids_for_source` shape) +
        :meth:`update_chunks`; T3Database parity (returns count updated,
        missing collection -> 0, idempotent). Matching rows are accumulated
        across all pages BEFORE updating — updating mid-pagination would
        shrink the where-match set and shift offsets.
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        ids: list[str] = []
        metadatas: list[dict] = []
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection_name,
                        "where": {"source_path": old_path},
                        "include": ["metadatas"],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                # 404 on the first page = no such collection (T3 parity: 0).
                # Mid-pagination failures must NOT be swallowed — the caller
                # would under-update and report success.
                if exc.code == 404 and offset == 0:
                    return 0
                raise
            page_ids = result.get("ids", []) or []
            page_metas = result.get("metadatas", []) or []
            for doc_id, meta in zip(page_ids, page_metas):
                ids.append(doc_id)
                updated = dict(meta) if isinstance(meta, dict) else {}
                updated["source_path"] = new_path
                metadatas.append(updated)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break
        if not ids:
            return 0
        self.update_chunks(collection_name, ids, metadatas)
        return len(ids)

    def delete_by_chunk_ids(
        self, collection_name: str, chunk_ids: list[str],
    ) -> int:
        """Delete chunks by explicit id. Returns count deleted.

        nexus-h8rf6.7: was missing — ``nx t3 gc``'s orphan deletion silently
        no-oped in service mode (the call site is try/except-wrapped, so the
        AttributeError degraded instead of crashing). T3Database parity:
        empty ``chunk_ids`` is a no-op (0), missing collection returns 0
        without raising. Delegates to :meth:`batch_delete` for the
        quota-bounded batching.
        """
        if not chunk_ids:
            return 0
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        size = QUOTAS.MAX_RECORDS_PER_WRITE
        deleted = 0
        for start in range(0, len(chunk_ids), size):
            batch = chunk_ids[start:start + size]
            try:
                _post(
                    "/v1/vectors/store-delete",
                    {"collection": collection_name, "ids": batch},
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                # 404 before anything was deleted = missing collection (T3
                # parity: 0). A failure AFTER a successful batch must NOT be
                # reported as 0 — the caller (nx t3 gc) would log "deleted 0"
                # despite partial deletion (wave review, sibling convention:
                # mid-pagination failures are never swallowed).
                if exc.code == 404 and deleted == 0:
                    return 0
                raise
            deleted += len(batch)
        return deleted

    def list_unique_source_paths(self, collection_name: str) -> list[str]:
        """Return every distinct ``source_path`` value in *collection_name*.

        nexus-h8rf6.7: was missing — ``nx t3 prune-stale``'s staleness sweep
        silently skipped every collection in service mode. Pages the plain
        (no ``where``) ``/v1/vectors/get`` listing and dedupes locally, same
        as T3Database. Empty/missing source_path values are skipped (MCP-put
        chunks have no on-disk source by design). Missing collection -> [].
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        seen: set[str] = set()
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection_name,
                        "include": ["metadatas"],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                if exc.code == 404 and offset == 0:
                    return []
                raise
            page_ids = result.get("ids", []) or []
            page_metas = result.get("metadatas", []) or []
            if not page_ids:
                break
            for meta in page_metas:
                if not isinstance(meta, dict):
                    continue
                src = meta.get("source_path") or ""
                if src:
                    seen.add(src)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break
        return sorted(seen)

    def list_chunks_with_metadata(
        self,
        collection_name: str,
        *,
        fields: tuple[str, ...] = ("doc_id", "indexed_at"),
    ) -> Iterator[tuple[str, dict[str, str]]]:
        """Yield ``(chunk_id, metadata_subset)`` for every chunk in a collection.

        nexus-h8rf6.7: was missing — ``nx t3 gc``'s orphan scan silently
        skipped every collection in service mode. T3Database parity:
        ``metadata_subset`` contains only the requested ``fields`` with
        empty strings for missing keys; missing collection yields nothing.
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection_name,
                        "include": ["metadatas"],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                if exc.code == 404 and offset == 0:
                    return
                raise
            page_ids = result.get("ids", []) or []
            page_metas = result.get("metadatas", []) or []
            if not page_ids:
                break
            for cid, meta in zip(page_ids, page_metas):
                if not isinstance(meta, dict):
                    meta = {}
                yield cid, {f: str(meta.get(f, "")) for f in fields}
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break

    def expire(self) -> int:
        """Delete all expired entries from ``knowledge__*`` collections.

        nexus-h8rf6.5: was missing entirely — ``nx store expire`` crashed
        with ``AttributeError`` in service mode. T3Database parity, with one
        translation (historical: range operators landed later, nexus-4l80g, but
        this equivalent rewrite predates them and stays), T3's
        ``{"ttl_days": {"$gt": 0}}`` pre-filter becomes
        ``{"ttl_days": {"$ne": 0}}`` — equivalent for its only purpose,
        excluding the permanent ``ttl_days == 0`` sentinel (TTLs are never
        negative). The server's ``$ne`` is NULL-inclusive, so rows with
        absent ``ttl_days`` come back too; ``is_expired`` (the authoritative
        Python-side check, same as T3) rejects them.

        Expired IDs are accumulated per collection BEFORE deleting —
        deleting mid-pagination would shift offsets and skip rows. Server
        errors propagate (a swallowed failure would report "0 expired"
        while expired rows survive).

        Returns the total number of deleted documents.
        """
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)
        from nexus.metadata_schema import is_expired  # noqa: PLC0415 — circular-dep avoidance (metadata_schema)

        now_iso = datetime.now(UTC).isoformat()
        ttl_where = {"ttl_days": {"$ne": 0}}
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        total = 0
        for entry in self.list_collections():
            name = entry.get("name", "")
            if not name.startswith("knowledge__"):
                continue
            expired_ids: list[str] = []
            offset = 0
            while True:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": name,
                        "where": ttl_where,
                        "include": ["metadatas"],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
                page_ids = result.get("ids", []) or []
                metas = result.get("metadatas", []) or []
                for doc_id, meta in zip(page_ids, metas):
                    if isinstance(meta, dict) and is_expired(meta, now_iso=now_iso):
                        expired_ids.append(doc_id)
                offset += len(page_ids)
                if len(page_ids) < page_limit:
                    break  # last page (short or empty)
            if expired_ids:
                self.batch_delete(name, expired_ids)
            total += len(expired_ids)
        return total

    def collection_info(self, name: str) -> dict:
        """Return ``{"count": N, "metadata": {}}`` for *name*.

        nexus-umvh2 sibling audit: was missing entirely, crashing ``nx
        store list``'s total-count display, ``nx collection info``, and
        ``nx collection reindex`` in service mode.

        Raises ``KeyError`` when *name* has no live chunks -- T3Database
        parity ("not found"). On the pgvector path a collection with zero
        live rows is indistinguishable from an absent one (matches
        :meth:`collection_exists`'s already-established semantics, RDR-156
        Decision 6) -- callers (``nx collection reindex``) rely on the
        ``KeyError`` to detect a genuinely missing collection. No
        ``metadata`` equivalent exists server-side (Chroma-native collection
        metadata is not exposed by the service API), so that key is always
        ``{}``.
        """
        return {"count": self._count_or_key_error(name), "metadata": {}}

    def _count_or_key_error(self, name: str) -> int:
        """Return the live chunk count for *name*, raising ``KeyError`` on absent.

        Shared by :meth:`collection_info` and :meth:`collection_metadata`
        (wave review: the block was duplicated verbatim). On the pgvector
        path a collection with zero live rows is indistinguishable from an
        absent one (RDR-156 Decision 6), so ``count == 0`` also raises.
        NOTE: callers that enumerate via :meth:`list_collections` can never
        hit the zero-count branch — that listing only returns collections
        with live chunks — so the doctor probes iterating it are unaffected.
        """
        try:
            n = self.count(name)
        except VectorServiceError as exc:
            if exc.code == 404:
                raise KeyError(f"Collection not found: {name!r}") from exc
            raise
        if n == 0:
            raise KeyError(f"Collection not found: {name!r}")
        return n

    def collection_metadata(self, collection_name: str) -> dict:
        """Return metadata dict for a collection.

        nexus-h8rf6.8: was missing — doctor's model-drift probe
        (``doctor_search._collection_metadata``) degraded to
        ``ProbeResult(outcome='error')`` for every collection in service
        mode. Full T3Database parity is achievable client-side: T3 derives
        ``embedding_model`` / ``index_model`` from the collection NAME
        (conformant names embed the model; ``index_model_for_collection``
        is an alias of ``embedding_model_for_collection``) — only ``count``
        needs the server.

        Keys returned: ``name``, ``count``, ``embedding_model`` (query-time
        model), ``index_model`` (index-time model, may differ for CCE
        collections). Raises ``KeyError`` if the collection does not exist
        — on pgvector, zero live rows is indistinguishable from absent
        (:meth:`collection_info` semantics, RDR-156 Decision 6).
        """
        from nexus.corpus import (  # noqa: PLC0415 — circular-dep avoidance (corpus imports config)
            embedding_model_for_collection,
            embedding_model_for_collection_name,
            index_model_for_collection,
        )

        n = self._count_or_key_error(collection_name)
        parsed = embedding_model_for_collection_name(collection_name)
        return {
            "name": collection_name,
            "count": n,
            "embedding_model": parsed or embedding_model_for_collection(collection_name),
            "index_model": parsed or index_model_for_collection(collection_name),
        }

# ── Module-level routing helper ───────────────────────────────────────────────

_vector_client_lock = threading.Lock()
_vector_client_instance: HttpVectorClient | None = None

#: Cloud-mode version-compatibility probe cache (nexus-jn0nm). ``None`` means
#: "not yet probed this process". A cached exception means the probe already
#: failed once -- every subsequent call re-raises a FRESH instance built from
#: the same type + message (nexus-b6qlf Fix 3: re-raising the SAME instance
#: across call frames makes CPython prepend a new frame to its
#: ``__traceback__`` every time, growing unboundedly in a long-running
#: process) rather than re-probing (no repeated HTTP round-trips for a state
#: we already know). The fast-path reads of these two globals (the check at
#: the top of :func:`get_http_vector_client`, before the lock) are
#: INTENTIONALLY unguarded -- a standard double-checked-locking pattern,
#: safe under the GIL for a read of a bool/reference. Only the WRITE path
#: (a probe result being cached) holds :data:`_vector_client_lock`. Cleared
#: by :func:`reset_http_vector_client_for_tests`.
_version_probe_done: bool = False
_version_probe_error: Exception | None = None


def _reraise_cached_probe_error(cached: Exception) -> NoReturn:
    """Raise a FRESH instance of *cached*'s type/message (nexus-b6qlf Fix 3).

    Assumes every :class:`~nexus.db.managed_endpoint.ManagedServiceError`
    subclass accepts a single positional string-message constructor arg
    (true today for both :class:`~nexus.db.managed_endpoint.
    ManagedServiceUnreachable` and :class:`~nexus.db.managed_endpoint.
    ManagedServiceIncompatible` -- the latter's ``deployed_version`` /
    ``required_version`` fields are keyword-only with defaults, see
    ``managed_endpoint.py``). A future field addition to either subclass
    that becomes a REQUIRED positional/keyword arg would break this
    reconstruction -- keep it optional if that ever changes.

    Chains ``__cause__`` to *cached* so the original failure remains
    inspectable, but each raised object is a distinct instance: reusing the
    same instance across repeated calls is exactly what accumulates
    traceback frames without bound.
    """
    raise type(cached)(str(cached)) from cached


def _cloud_probe_failure_message(exc: Exception) -> str:
    """Reword a probe failure for a cloud-mode audience (nexus-b6qlf).

    A cloud-mode client cannot fix an incompatible managed engine itself --
    there is no local install to upgrade, only a shared multitenant service
    the maintainer/operator controls. The prior (pre-unification) warning
    told users to "upgrade the engine this install is pointed at", which is
    actively wrong advice in cloud mode. :class:`ManagedServiceUnreachable`
    keeps its own message unchanged -- connectivity (``NX_SERVICE_URL``,
    network) genuinely IS something the caller can act on locally.

    nexus-b6qlf Fix 2: the below-floor :class:`ManagedServiceIncompatible`
    carries structured ``deployed_version`` / ``required_version`` fields
    (see ``managed_endpoint.py``) precisely so this function never has to
    embed the underlying exception's own remedy text verbatim -- that text
    ends "...Upgrade the managed service, or upgrade/downgrade the nx
    client to match.", which directly contradicts the "cannot be fixed
    locally" framing below when a cloud user reads it. When those
    structured fields are present we state just the two version numbers
    (deployed vs. required) for diagnostic value; when absent (every other
    ManagedServiceIncompatible shape: no token, non-200, non-JSON, no
    usable release_version -- none of which carry that contradictory
    remedy clause) we fall back to embedding the message as before.
    """
    from nexus.db.managed_endpoint import ManagedServiceIncompatible  # noqa: PLC0415 -- deferred, see module docstring

    if not isinstance(exc, ManagedServiceIncompatible):
        return str(exc)

    deployed = exc.deployed_version
    required = exc.required_version
    if deployed and required:
        detail = (
            f"The deployed engine reports version {deployed}; this client "
            f"requires at least {required}."
        )
    else:
        detail = f"Underlying probe failure: {exc}"
    return (
        "The managed nexus service is running an engine older than this "
        "client requires. This cannot be fixed locally -- it is a "
        "hosted-service issue that will be resolved when the service "
        "operator deploys a compatible engine, not by any local action "
        f"you can take. {detail}"
    )


def get_http_vector_client() -> HttpVectorClient:
    """Return the process-local HttpVectorClient singleton.

    Cloud mode (``not is_local_mode()``) runs a one-time-per-process
    compatibility probe (:func:`nexus.db.managed_endpoint.probe_managed_service`)
    before the singleton is usable -- nexus-b6qlf: previously
    ``probe_managed_service`` was only ever invoked from ``nx init`` /
    ``nx doctor`` / ``nx service probe``, never from this, the actual
    connection path every cloud-mode T3 operation goes through. A too-old
    managed engine used to degrade silently (a missing endpoint 404s deep
    inside some workflow, with only a buried log warning); this is a
    deliberate HARD FAIL instead.

    * Probe passes: cached forever, never re-probed again this process --
      every later cloud-mode call returns the singleton with zero extra
      HTTP round-trips.
    * Probe fails: the (reworded, cloud-specific) error is cached and
      RAISED immediately, blocking construction. Every subsequent call
      this process re-raises a FRESH instance of the same cached error
      (type + message, chained via ``__cause__``) rather than re-probing a
      state we already know -- re-raising the SAME instance across call
      frames would make CPython prepend a new traceback frame every time,
      growing unboundedly in a long-running process (nexus-b6qlf Fix 3).
    * Local mode: the probe is skipped entirely. Local mode's own floor
      enforcement (the native ``guided_upgrade`` / ``nx upgrade`` flow) is
      untouched by this gate.
    """
    global _vector_client_instance, _version_probe_done, _version_probe_error
    from nexus.config import is_local_mode  # noqa: PLC0415 -- deferred for test patchability

    cloud_mode = not is_local_mode()

    if cloud_mode and _version_probe_error is not None:
        _reraise_cached_probe_error(_version_probe_error)

    if _vector_client_instance is not None and (not cloud_mode or _version_probe_done):
        return _vector_client_instance

    with _vector_client_lock:
        if cloud_mode:
            if _version_probe_error is not None:
                _reraise_cached_probe_error(_version_probe_error)
            if not _version_probe_done:
                from nexus.db.managed_endpoint import (  # noqa: PLC0415 -- deferred, see module docstring
                    ManagedServiceError,
                    probe_managed_service,
                )

                try:
                    probe_managed_service()
                except ManagedServiceError as exc:
                    wrapped = type(exc)(_cloud_probe_failure_message(exc))
                    _version_probe_error = wrapped
                    # nexus-dizod: log the REWRITTEN (cloud-correct) message,
                    # never str(exc) -- the raw ManagedServiceIncompatible
                    # text ends "...upgrade/downgrade the nx client to
                    # match", and at the CLI's default WARNING level this
                    # ERROR line prints to the user's real stderr directly
                    # above the click-rendered "cannot be fixed locally"
                    # error, recreating the exact b6qlf Fix-2
                    # self-contradiction across two adjacent lines.
                    _log.error(
                        "cloud_engine_version_probe_failed",
                        error_type=type(exc).__name__,
                        error=str(wrapped),
                    )
                    raise wrapped from exc
                _version_probe_done = True
                _log.debug("cloud_engine_version_probe_ok")
        if _vector_client_instance is None:
            _vector_client_instance = HttpVectorClient()
    return _vector_client_instance


def reset_http_vector_client_for_tests() -> None:
    """Test helper: reset the singleton and the cloud version-probe cache."""
    global _vector_client_instance, _version_probe_done, _version_probe_error
    with _vector_client_lock:
        _vector_client_instance = None
        _version_probe_done = False
        _version_probe_error = None


def is_vector_service_mode() -> bool:
    """Return True unless NX_STORAGE_BACKEND_VECTORS explicitly opts out.

    nexus-tawx0: since the RDR-155 P4a.2 serving cutover, ``make_t3()``
    returns the service-backed client UNCONDITIONALLY — service mode is
    the default reality, so this defaults True. The opt-in era left the
    no-Python-embed stubs (doc/prose/code indexers) inert in default
    environments: every indexing run client-embedded via Voyage, the
    client discarded the vectors, and the server embedded again — double
    spend per chunk, empirically proven by voyageai tracebacks in
    production hook runs (2026-06-11).

    The env var survives as an explicit OPT-OUT (any value other than
    ``service``/empty, conventionally ``chroma``) for test setups that
    inject a chroma-backed ``T3Database``. For "can this HANDLE do
    chroma-client things?" decisions use :func:`is_service_backed` on the
    handle instead: env state and handle type diverge in those tests.
    """
    value = os.environ.get(_VECTORS_BACKEND_ENV, "").strip().lower()
    return value in ("", "service")


def is_service_backed(db: object) -> bool:
    """True when *db* routes T3 ops through the nexus-service HTTP API.

    The instance-based capability guard (RDR-155 P4a.2, nexus-1k8s1):
    service-backed handles have no raw ``._client`` and no chroma-coupled
    surface. Prefer this over :func:`is_vector_service_mode` wherever the
    handle is in hand — injected chroma-backed ``T3Database`` test fixtures
    must keep taking the legacy branches regardless of env state.
    """
    return isinstance(db, HttpVectorClient)
