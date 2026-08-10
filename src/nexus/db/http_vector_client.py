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
from collections.abc import Callable, Iterator
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

#: nexus-5xn3k.5 (substantive-critic follow-up): log ``update_chunks``'s
#: "engine omitted the missing field" notice once per process rather than
#: once per call. Without this, a full-repo reindex against a pre-fence
#: engine (a rolling deploy window, or an un-upgraded install) would emit
#: this WARNING once per file — drowning the rarer, more actionable
#: ``update_chunks_missing_reported`` / ``update_chunks_missing_rerouted``
#: signals in the same log stream. Tests reset this directly (module
#: attribute, not a public API) — same shape as
#: ``_skip_existing_deprecation_logged`` above.
_update_chunks_missing_unreported_logged: bool = False


def _warn_update_chunks_missing_unreported(collection: str, count: int) -> None:
    """Log-once notice: the engine's update-metadata response omitted
    "missing" — cannot tell whether a stale ``existing_ids`` probe hit was
    silently left un-repaired. See ``_skip_existing_deprecation_logged``
    above for the identical once-per-process shape.
    """
    global _update_chunks_missing_unreported_logged
    if _update_chunks_missing_unreported_logged:
        _log.debug(
            "update_chunks_missing_unreported",
            collection=collection,
            count=count,
        )
        return
    _update_chunks_missing_unreported_logged = True
    _log.warning(
        "update_chunks_missing_unreported",
        collection=collection,
        count=count,
        # NOTE: kwarg is "detail", not "message" — stdlib logging's
        # LogRecord reserves the "message" attribute name (set internally
        # by getMessage()); passing message= as an extra kwarg raises
        # KeyError("Attempt to overwrite 'message' in LogRecord") the
        # moment structlog is configured to render through stdlib logging
        # (structlog.stdlib.render_to_log_kwargs), which production
        # configs and this bead's own tests both do.
        detail=(
            "the engine's /v1/vectors/update-metadata response omitted the "
            '"missing" field — a pre-nexus-5xn3k.2 engine, or a rolling '
            "deploy window straddling the version boundary. Cannot tell "
            "whether a stale existing_ids probe hit was silently left "
            "un-repaired; today's (pre-fence) behaviour is preserved rather "
            "than assuming zero misses. Logged once per process — further "
            "occurrences in this run are DEBUG."
        ),
    )


#: nexus-hdx2u: log ``_ServiceCollectionStub.get``'s self-truncation
#: tripwire once per process, not once per call — a full-repo aspect
#: extraction or taxonomy rebuild against an engine that caps
#: ``store-get`` independently of the client's own paging would
#: otherwise emit this WARNING once per oversized batch.
_store_get_truncated_logged: bool = False

#: Historical default for the ``where``-filter branch of
#: :meth:`_ServiceCollectionStub.get` (the incremental-sync staleness
#: check). Every in-repo caller of that branch already passes an
#: explicit ``limit``, so this is a fallback of last resort, not a load-
#: bearing default. The ``ids`` branch has NO analogous default — see
#: ``_ServiceCollectionStub.get`` docstring (nexus-hdx2u).
_WHERE_GET_DEFAULT_LIMIT = 10


#: nexus-hdx2u E4: log ``_ServiceCollectionStub.get``'s count-unreported
#: notice once per process, not once per call — same log-once shape as
#: ``_store_get_truncated_logged`` above (a full-repo run against a
#: pre-E3 engine would otherwise emit this WARNING once per batch).
_store_get_count_unreported_logged: bool = False


def _warn_store_get_count_unreported(collection: str) -> None:
    """Log-once notice: a ``store-get`` response omitted the engine-side
    ``count`` field (nexus-hdx2u E3/E4).

    ``count`` — matched-LIVE rows before LIMIT — is an ADDITIVE field a
    pre-E3 engine, or a rolling-deploy window straddling the version
    boundary, simply does not send. This is NOT gated behind a
    ``REQUIRED_ENGINE_VERSION`` floor bump (that conversion — treating an
    absent ``count`` as fail-loud rather than degrade — is a deliberate
    future floor decision, not part of this fix): the client degrades to
    the pre-existing ``_warn_store_get_truncated_at_limit`` heuristic,
    which stays the only truncation signal on such an engine.
    """
    global _store_get_count_unreported_logged
    if _store_get_count_unreported_logged:
        _log.debug("store_get_count_unreported", collection=collection)
        return
    _store_get_count_unreported_logged = True
    _log.warning(
        "store_get_count_unreported",
        collection=collection,
        detail=(
            "the engine's /v1/vectors/store-get response omitted the "
            '"count" field (matched-LIVE rows before LIMIT) — a pre-'
            "nexus-hdx2u-E3 engine, or a rolling deploy window straddling "
            "the version boundary. Degrading to the pre-count "
            "store_get_truncated_at_limit heuristic; no REQUIRED_ENGINE_"
            "VERSION floor is enforced for this field yet. Logged once "
            "per process — further occurrences in this run are DEBUG."
        ),
    )


def _warn_store_get_truncated_at_limit(collection: str, limit_sent: int, requested: int) -> None:
    """Log-once notice: a ``store-get`` page came back with EXACTLY
    ``limit_sent`` rows for a batch that asked for more ids than that —
    the response was silently truncated.

    nexus-hdx2u: this is the only truncation signal available on an
    engine that reports no total-match count. ``len(returned) ==
    len(requested)`` is NOT the right invariant to check here — absent
    ids are legitimate (a store-get for ids that were never written, or
    were since deleted, is expected to come back short). Truncation is
    detectable exactly when the page came back AT the limit that was
    sent while FEWER ids than that were requested in the batch.
    """
    global _store_get_truncated_logged
    if _store_get_truncated_logged:
        _log.debug(
            "store_get_truncated_at_limit",
            collection=collection,
            limit_sent=limit_sent,
            requested=requested,
        )
        return
    _store_get_truncated_logged = True
    _log.warning(
        "store_get_truncated_at_limit",
        collection=collection,
        limit_sent=limit_sent,
        requested=requested,
        detail=(
            "a store-get batch returned exactly limit_sent rows while "
            "requesting more ids than that — the response was silently "
            "truncated. This client pages ids-branch requests at "
            "QUOTAS.MAX_RECORDS_PER_WRITE internally, so a truncation at "
            "a smaller limit than that page size means an intermediate "
            "proxy or an engine that caps store-get independently of the "
            "requested limit. Logged once per process — further "
            "occurrences in this run are DEBUG."
        ),
    )


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
        # RDR-155 P4b: the nexus-0rwwv migration-hint bridge died with the
        # migration module; stranded pre-PG installs are redirected by the
        # stranded-install detector at CLI/MCP startup.
        raise RuntimeError(
            "nexus-service endpoint is not resolvable: T3 vector serving "
            "routes through the nexus-service HTTP API (RDR-155 Phase 4a — "
            "the direct Chroma serving paths are retired). Either start the "
            "supervisor with 'nx daemon service start' (publishes the "
            "endpoint lease this client auto-discovers), set the managed "
            "endpoint with 'nx config set service_url/service_token', or export "
            "NX_SERVICE_URL / NX_SERVICE_TOKEN explicitly."
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
#
# nexus-gbt5u (GH #1419 Issue 3a): every request below is already bounded by
# an explicit ``timeout``, and that was NOT enough — Steve Harris's indexing
# run hung silently for 2+ hours without one firing.
#
# Python derives socket-timeout deadlines from the MONOTONIC clock, and on
# Darwin that is ``mach_absolute_time()``, which does not advance while the
# system is asleep. So a socket timeout budgets AWAKE time, not wall-clock
# time: a laptop that sleeps mid-request burns almost none of a 600s budget.
# On wake the peer is long gone, but the TCP connection is a zombie with no
# RST coming, so the read simply never completes and the timeout — measured
# in a clock that stood still — never fires. Bounded timeout, unbounded hang.
#
# TCP keepalive is what closes it: the OS probes an idle connection and tears
# it down when the peer does not answer, surfacing as an ordinary connection
# error that ``_request``'s existing retry path already handles. The idle
# knob is the load-bearing part — bare SO_KEEPALIVE inherits a 2-HOUR OS
# default on both Darwin and Linux, which would not have helped Steve at all.

#: Seconds of idle before the first keepalive probe. Must beat the 2h OS
#: default by a wide margin (that default is longer than the hang it is
#: supposed to catch). 60s is well above any legitimate inter-packet gap on
#: a live request — the engine streams responses continuously — while still
#: detecting a dead peer within ~2 minutes of wake.
_KEEPALIVE_IDLE_S = 60
#: Seconds between probes once the peer stops answering.
_KEEPALIVE_INTERVAL_S = 15
#: Unanswered probes before the connection is declared dead (~2 min total).
_KEEPALIVE_PROBES = 4


def _enable_tcp_keepalive(sock: Any) -> None:
    """Turn on TCP keepalive with a SHORT idle time. Never raises.

    Best-effort by construction: a platform or transport that lacks any of
    these options degrades to an unkeepalived-but-working connection. This is
    a resilience improvement to the sleep/wake path, not a correctness gate —
    failing a request because a socket option did not apply would trade a rare
    hang for a common outage.
    """
    import socket as _socket  # noqa: PLC0415 — deferred import — branch-local

    try:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
    except Exception:  # noqa: BLE001 — see docstring: never gate a request on this
        return
    # Darwin spells the idle knob TCP_KEEPALIVE; Linux spells it TCP_KEEPIDLE.
    idle_opt = getattr(_socket, "TCP_KEEPALIVE", None) or getattr(
        _socket, "TCP_KEEPIDLE", None
    )
    for opt, value in (
        (idle_opt, _KEEPALIVE_IDLE_S),
        (getattr(_socket, "TCP_KEEPINTVL", None), _KEEPALIVE_INTERVAL_S),
        (getattr(_socket, "TCP_KEEPCNT", None), _KEEPALIVE_PROBES),
    ):
        if opt is None:
            continue
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, opt, value)
        except Exception:  # noqa: BLE001 — partial application is still an improvement
            continue


def _keepalive_opener() -> Any:
    """A urllib opener whose connections carry :func:`_enable_tcp_keepalive`.

    urllib offers no socket-options hook, so the connection classes are
    subclassed to set the options immediately after ``connect()``. Built
    fresh per call: openers are cheap, and caching one would pin the
    endpoint/proxy resolution that ``_resolve_endpoint`` may change between
    requests (RDR-149 lease rotation).
    """
    import http.client  # noqa: PLC0415 — deferred import — branch-local
    import urllib.request  # noqa: PLC0415 — deferred import — branch-local

    class _KeepAliveHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            _enable_tcp_keepalive(self.sock)

    class _KeepAliveHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            _enable_tcp_keepalive(self.sock)

    class _KeepAliveHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(_KeepAliveHTTPConnection, req)

    class _KeepAliveHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(_KeepAliveHTTPSConnection, req)

    return urllib.request.build_opener(
        _KeepAliveHTTPHandler, _KeepAliveHTTPSHandler,
    )


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
    # nexus-gbt5u: NOT urlopen — the module-level opener has no socket-options
    # hook, so a sleep-orphaned connection could never be detected. See the
    # transport-section comment above.
    with _keepalive_opener().open(req, timeout=timeout) as resp:
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

#: Hardcoded onnx-local memory-safety default (nexus-33hpq) — the ONE
#: value :func:`_resolve_onnx_local_upsert_chunk_cap` falls back to when
#: ``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP`` is unset, and the floor its
#: validation messages reference.
_ONNX_LOCAL_UPSERT_CHUNK_CAP_DEFAULT = 16


def _resolve_onnx_local_upsert_chunk_cap(raw: str | None) -> int:
    """Validate and resolve ``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP`` (nexus-97dp4).

    *raw* is the literal ``os.environ.get("NX_ONNX_LOCAL_UPSERT_CHUNK_CAP")``
    result, threaded in as a parameter rather than read internally — this
    keeps every validation branch directly unit-testable with no env-var/
    module-reload gymnastics. The module-level assignment below still
    calls ``os.environ.get`` exactly ONCE at import time, preserving the
    existing "every caller reads the SAME constant" contract (ChunkBatcher's
    flush cap and this client's oversize paging must never disagree
    mid-run — see :func:`per_collection_chunk_cap`'s docstring).

    VALIDATION (nexus-97dp4 CRITICAL fix — the original
    ``int(os.environ.get(...) or 16)`` had none): a non-numeric value used
    to raise an UNCAUGHT ``ValueError`` at import time, crashing every
    ``nx`` invocation with a bare traceback. Now it raises a
    ``RuntimeError`` with an actionable message instead — FAIL LOUD, not a
    silent fall-through to the safe default. Per this project's standing
    "no silent fallbacks for data-correctness/safety problems" directive:
    this constant directly defeats the nexus-33hpq 77.4GB-RSS memory-
    safety cap when misconfigured, and this is a low-traffic, test-harness-
    only knob (``tests/e2e/local-index-memory-gate.sh``) — real users
    essentially never set it, so failing loud on a typo costs nothing in
    practice and surfaces a misconfiguration immediately instead of
    silently running with an unintended cap. A non-positive value (<=0)
    also raises: it would make :func:`per_collection_chunk_cap` return a
    cap that blocks every onnx-local upsert outright, a confusing
    downstream failure far removed from its actual cause.

    NO UPPER BOUND (considered and deliberately rejected): this override's
    entire documented purpose is letting the e2e memory gate RAISE the cap
    toward the pre-nexus-33hpq 300 (or beyond) to prove the gate's corpus
    actually binds a higher ceiling too — clamping it to the safe default
    would defeat that harness's whole reason for existing. The risk this
    validation closes is malformed/nonsensical input (a typo, a negative
    number), never "someone might raise it" — raising it IS the intended
    use, and nexus-rn9n7 (a SEPARATE, still-open bug: a probe failure
    silently falls through to the unsafe cap=300 branch of
    :func:`per_collection_chunk_cap`) is the other, independent route to
    an unsafe cap this fix does NOT touch.

    Emits a WARNING-level structured log line whenever the override is
    ACTIVE (the env var is present at all, regardless of value) — a run
    using a non-default cap must never be invisible in the logs.
    """
    if raw is None or raw.strip() == "":
        return _ONNX_LOCAL_UPSERT_CHUNK_CAP_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"NX_ONNX_LOCAL_UPSERT_CHUNK_CAP={raw!r} is not a valid integer. "
            "This overrides the nexus-33hpq onnx-local memory-safety cap "
            f"(default {_ONNX_LOCAL_UPSERT_CHUNK_CAP_DEFAULT} — see "
            "per_collection_chunk_cap's docstring for the 77.4GB-RSS "
            "incident this cap prevents); unset it, or set it to a "
            "positive integer."
        ) from None
    if value <= 0:
        raise RuntimeError(
            f"NX_ONNX_LOCAL_UPSERT_CHUNK_CAP={value} must be a positive "
            "integer — a non-positive cap would block every onnx-local "
            f"upsert outright. Unset it, or set it to a positive integer "
            f"(default {_ONNX_LOCAL_UPSERT_CHUNK_CAP_DEFAULT})."
        )
    _log.warning(
        "onnx_local_upsert_chunk_cap_overridden",
        value=value,
        default=_ONNX_LOCAL_UPSERT_CHUNK_CAP_DEFAULT,
        note=(
            "NX_ONNX_LOCAL_UPSERT_CHUNK_CAP overrides the nexus-33hpq "
            "memory-safety cap — expected only from "
            "tests/e2e/local-index-memory-gate.sh; an unintended override "
            "in a real install can reproduce the 77.4GB-RSS incident this "
            "cap exists to prevent."
        ),
    )
    return value


#: onnx-local memory-bounded cap (nexus-33hpq), applies to EVERY prefix once
#: the serving engine is onnx-local — see :func:`per_collection_chunk_cap`'s
#: docstring for the memory arithmetic behind the number 16.
#:
#: ``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP`` (nexus-97dp4): read ONCE at import
#: time, deliberately — every caller (ChunkBatcher's flush cap AND this
#: client's oversize paging) reads the SAME module-level constant, so a
#: per-request re-read would let the two choke points disagree mid-run.
#: This exists so tests/e2e/local-index-memory-gate.sh can deliberately
#: raise the cap toward the pre-nexus-33hpq 300 (or beyond) to prove the
#: gate's corpus actually binds a HIGHER ceiling too, without editing this
#: file — a real mechanism, not a runtime sed. Unset (the default) leaves
#: this byte-identical to before the env var existed: 16. Validated (see
#: :func:`_resolve_onnx_local_upsert_chunk_cap`) — a malformed value now
#: fails loud at import time with an actionable message instead of an
#: uncaught ValueError, and an active override always logs a WARNING.
_ONNX_LOCAL_UPSERT_CHUNK_CAP = _resolve_onnx_local_upsert_chunk_cap(
    os.environ.get("NX_ONNX_LOCAL_UPSERT_CHUNK_CAP")
)
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
    Both emit POSTs bounded by the SAME timeout, so tuning the VOYAGE values below
    changes both BY DESIGN — and any change to them must be validated against the
    CP timeout, never raised for cross-file batching throughput alone. The
    onnx-local branch below is a SEPARATE, memory-derived bound (see below) that
    happens to reuse the same choke point rather than a third independent knob —
    that reuse is deliberate: it is still the ONE cap the ChunkBatcher flush and
    the oversize-paging fallback both obey, just keyed on a different binding
    constraint for this mode.

    Voyage/cloud values (unchanged by nexus-33hpq — Voyage embedding is a NETWORK
    call, not local process memory, so the timeout reasoning below still applies
    unmodified): 64 for CCE (docs/knowledge/rdr — voyage-context-3, slow
    server-side embed; live 504 at 172 CCE chunks 2026-07-04) is the CONSERVATIVE
    proven-safe cap. conexus's root-cause relay suggested ~128 for the direct path
    pending a re-gate p99 — a throughput optimization tracked in nexus-o1mbu /
    nexus-9mzkd, not taken here without that measurement. Code (voyage-code-3)
    sustains 300.

    onnx-local value — MEMORY-derived, not timeout-derived (nexus-33hpq,
    2026-08-09): nexus-w3hzw (pre-v7.4.0) widened the CCE prefixes to 300 under
    onnx-local on the reasoning that local bge has no 30s gateway to blow through
    — true, but it silently inherited 300's OTHER justification (the control-plane
    timeout) for a mode where the timeout was never the binding constraint.
    MECHANISM (read from the engine source, not inferred): a local batch is
    embedded by ``Bge768Embedder.embed()`` -> ``embedBatch(texts)`` in ONE ONNX
    forward pass with no sub-batching (``EmbedderRouter`` does not sub-batch
    either), ``.optPadding(false)`` padding EVERY row to a RECTANGULAR
    ``[batchSize, maxLen]`` tensor where ``maxLen = min(longest chunk in the
    batch, MAX_SEQ_LEN=512)``. Attention is therefore O(batch * heads * seq^2).
    This shape is NOT new in v7.5.0 — the pre-combined-write path also embedded
    server-side (``upsert_chunks_with_embeddings`` forwards chunk TEXT and
    ignores the caller's embeddings, as its own docstring says).

    MEASURED — all on the same 36-file shakedown fixture, same box. NOTE the
    ACTUAL batch sizes: that fixture yields only ~92 chunks in total, so a
    cap=300 run never assembles a 300-chunk batch — the observed flushes were
    bounded by the corpus (bisect events at 82 chunks for ``code``, 28 for
    ``docs``), not by the cap. The cap is the CEILING, not the batch size.
        cap=300, this tree -> 77.4 GB peak RSS from a batch of AT MOST ~92,
                              ~15 cores pegged, /livez starved, supervisor
                              stuck-exit. Reproduced 3/3.
        cap=300, v7.4.0    -> ~30 GB peak; survived the step.
        cap=16,  this tree -> 3.03 GB peak; step cleared, 11/11 gate green.
    The cap=16 figure is the real observed peak WITH ``flush_concurrency=3``
    (see ``indexer.py``) actually active — it already covers up to three
    concurrent in-flight flushes and is NOT a single-flush budget.

    OPEN, deliberately NOT asserted: why v7.4.0 survives at the same 300 cap is
    unexplained. That comparison also moves a SECOND variable — v7.4.0 pins
    engine v0.1.68, this tree pins v0.1.69 — so it does not isolate a
    client-side cause, and no causal story should be read into it. Tracked in
    nexus-b32rx; do not treat the delta as understood.

    Choosing 16 — and why NO closed-form model is offered here: the attention
    arithmetic (``batch * 12 * 512^2 * 4 B``) accounts for only ~5% of the
    observed peak, and the two real datapoints do not fit a linear law. Going
    from a ~92-chunk batch to a 16-chunk one (~5.75x) cut peak RSS from 77.4 GB
    to 3.03 GB (~25x) — SUPER-linear, so any through-the-origin extrapolation
    (in either direction) is unsound and none is given. 16 was chosen as a
    deliberately conservative starting point and then VALIDATED end-to-end by
    the measurement above; that measurement, not a model, is the whole basis
    for this value. Raising it is a throughput optimization that REQUIRES
    re-measuring peak RSS at the new value on a corpus large enough to actually
    reach the new ceiling — note the current shakedown fixture is NOT such a
    corpus (its largest flush at cap=16 is 15 chunks, so the cap never binds;
    gate-adequacy tracked in nexus-97dp4).

    Why row-count alone is not a crude proxy, and why no separate token-budget
    cap is added here: one long chunk pads EVERY row in its batch up to that
    chunk's length, so a batch of 16 short chunks is far cheaper than 16 chunks
    each near the 512-token ceiling. The arithmetic above already assumes the
    WORST case (every row at ``maxLen=512``) — a token-budget bound could only
    let batches grow bigger on the (common) case where chunks are shorter than
    512 tokens; it cannot tighten the worst-case guarantee this cap already
    provides. Treated as a throughput optimization for a future pass, not a
    correctness gap: nothing above assumes chunks average anywhere near 512
    tokens, it only bounds what happens if they do.

    Applies to EVERY prefix, not just CCE: nexus-w3hzw's mistake generalizes —
    ``code`` collections return 300 unconditionally (see below) and reach the
    SAME local embedder once onnx-local is serving, so reverting only the CCE
    widening would leave ``code__*`` reproducing the identical 77.4 GB blowup.

    This is the CLIENT-side half of nexus-33hpq's fix (option A/C in the bead).
    It reduces the blast radius but does not by itself make the engine safe
    against a large batch arriving some other way — engine-side sub-batching in
    ``Bge768Embedder`` (nexus-zu4ma, next engine cut) is the defense-in-depth
    half (option B): a client sending 300 must never be able to OOM the engine.
    Trade-off, stated only as far as it was measured: a smaller batch means more
    round trips, and the increase grows with corpus size — it approaches the cap
    ratio (300/16) only for a corpus large enough that flushes actually REACH the
    cap. It is far smaller for anything that flushes on file-grain boundaries
    first. NO throughput regression has been measured: on the shakedown fixture
    the whole indexing step ran 3m20s for 92 chunks / 8 flushes with ~29s of that
    in upload, and the full gate finished inside its documented cold-run band.
    There is no clean before/after to quote, because the cap=300 case WEDGED
    rather than completing. Do not restate a speed cost as fact without measuring
    it (nexus-fdn1c); the correct trade here is against an unusable install, and
    that argument does not need a throughput number to stand up.
    """
    prefix = collection.split("__", 1)[0]
    # nexus-33hpq: onnx-local is a MEMORY-bound mode, not a timeout-bound one —
    # apply the memory-derived cap to every prefix (code included) before the
    # CCE-vs-code split below, which is Voyage-cloud-specific reasoning.
    if _serving_embedding_mode() == "onnx-local":
        return _ONNX_LOCAL_UPSERT_CHUNK_CAP
    if prefix not in _CCE_COLLECTION_PREFIXES:
        return _CODE_UPSERT_CHUNK_CAP
    # Voyage CCE (or unknown mode, which stays on the conservative voyage
    # split — never widen a batch on a guess): slow server-side contextual
    # embedding behind the managed 30s gateway.
    return _CCE_UPSERT_CHUNK_CAP


def _serving_embedding_mode() -> str | None:
    """The serving engine's embedder family via the process-singleton client's
    memoized ``/version`` probe (RDR-188 P3.2). Best-effort: no singleton yet,
    or a probe failure, is ``None`` — this helper never constructs a client
    and never raises (a cap decision must not fail an upsert)."""
    client = _vector_client_instance
    if client is None:
        return None
    try:
        return client.embedding_mode()
    except Exception:  # noqa: BLE001 — cap heuristic is best-effort; unknown falls back conservative
        return None


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
        limit: int | None = None,
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

        nexus-ou4tb: raises :class:`VectorServiceError` rather than degrading
        to an empty result. A silent empty is INDISTINGUISHABLE from
        "collection has 0 chunks", which is the staleness-check answer that
        makes a caller re-embed everything — the degraded service is charged
        to the re-embed budget and nobody is told. This adopts the contract
        :meth:`count` and :meth:`get_all_metadata` already document in this
        same class ("the caller owns the boundary"); those two named ``get``
        and ``delete`` as the inconsistent holdouts, and this closes that.

        nexus-hdx2u: ``limit`` carries NO meaningful default on the ``ids``
        branch. A caller fetching N specific ids means "give me all N I
        have", not an implicit page size — this stub's own former
        ``limit: int = 10`` signature default was silently forwarded to
        ``POST /v1/vectors/store-get`` and truncated every >10-id batch
        (aspect extraction fed 10-of-157 fragments to the summarizer;
        taxonomy centroid rebuilds saw ~4% of a collection) with no
        signal that anything had been dropped. ``limit=None`` on the
        ``ids`` branch now resolves PER PAGE to that page's own batch
        size — the request is paged internally at
        ``QUOTAS.MAX_RECORDS_PER_WRITE`` (mirrors
        :meth:`HttpVectorClient.existing_ids`), so a huge id list still
        goes out in request-sized batches instead of one unbounded POST.
        The ``where`` branch keeps the historical default of
        :data:`_WHERE_GET_DEFAULT_LIMIT` (``10``) — every in-repo caller
        of that branch already passes an explicit ``limit``, so this
        default is a fallback of last resort, not load-bearing. An
        explicit non-``None`` ``limit`` on the ``ids`` branch is honored
        verbatim on every page (unchanged single-request behaviour for
        the common case of a batch that already fits in one page).

        A self-truncation tripwire (:func:`_warn_store_get_truncated_at_limit`)
        fires when a page comes back with EXACTLY as many rows as were
        sent as its limit while FEWER ids were requested in that batch —
        the only signal available on an engine that reports no total
        match count. ``len(returned) == len(requested)`` is NOT the
        right invariant for store-get: absent ids are legitimate (e.g.
        :meth:`HttpVectorClient.existing_ids` depends on partial
        returns), so truncation is only inferable from ``len(returned)
        == limit_sent < len(requested_batch)``.

        nexus-hdx2u E4 — engine-side ``count`` (matched-LIVE rows before
        LIMIT, additive per E3 — a single-statement/single-snapshot
        ``COUNT(*) OVER()`` window function alongside the page SELECT, not
        two sequential reads that could race under READ COMMITTED) is
        consumed as ADVISORY, mirroring the :meth:`update_chunks`
        "missing"-omitted degrade precedent
        (:func:`_warn_update_chunks_missing_unreported`): a per-page
        response that OMITS ``count`` (a pre-E3 engine, or a rolling
        deploy window) logs a once-per-process warning
        (:func:`_warn_store_get_count_unreported`) and falls back to the
        heuristic tripwire above — no ``REQUIRED_ENGINE_VERSION`` floor is
        bumped for this fix; treating an absent ``count`` as fail-loud is
        a deliberate future floor decision, not this one. A response that
        DOES report ``count`` SUPERSEDES the heuristic entirely (the
        heuristic does not also run) — ``count > len(returned)`` is a real
        truncation the engine itself measured (not a client-side guess)
        and raises :class:`VectorServiceError` immediately rather than
        warning; ``count <= len(returned)`` is silent success.

        nexus-hdx2u round-2 (two fail-loud boundaries, both probed
        empirically against the round-1 fix — no caller exercises
        either, and defining working-but-surprising semantics for them
        is scope creep this bead does not need):

        * ``offset`` is ``where``-branch-only. On the ``ids`` branch it
          would be forwarded VERBATIM to every internal page, which has
          no sane meaning for a get-these-specific-rows fetch (skip the
          first N of EACH page? of the whole list, but paging already
          slices the list?). Any non-default ``offset`` on the ``ids``
          branch raises :class:`ValueError` rather than silently doing
          something ill-defined.
        * An explicit ``limit`` combined with an ``ids`` batch spanning
          MORE THAN ONE internal page also raises :class:`ValueError`.
          Applying that limit per-page (the natural reading of "honor it
          verbatim on every page") returns up to ``limit * page_count``
          rows, not a global cap — e.g. ``limit=50`` over 350 ids (2
          pages) would return up to 100. A single-page explicit
          ``limit`` is unaffected and stays honored verbatim. Callers
          that need this: omit ``limit`` (resolves to each page's own
          batch size), or pre-chunk ``ids`` into ``<= page_size``
          batches themselves.
        """
        if ids is not None:
            from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)
            page_size = QUOTAS.MAX_RECORDS_PER_WRITE
            if offset not in (0, None):
                raise ValueError(
                    "_ServiceCollectionStub.get: offset is not supported on "
                    "the ids branch (offset paginates a where-filtered "
                    "scan; ids is a get-these-specific-rows fetch — "
                    "forwarding it verbatim to every internal page has "
                    "undefined semantics). Pass offset=0 (or omit it) and "
                    "slice the ids list yourself if you need to skip some."
                )
            if limit is not None and len(ids) > page_size:
                raise ValueError(
                    f"_ServiceCollectionStub.get: an explicit limit ({limit}) "
                    f"cannot be combined with an ids batch spanning more than "
                    f"one internal page (len(ids)={len(ids)} > "
                    f"QUOTAS.MAX_RECORDS_PER_WRITE={page_size}) — applying it "
                    "per-page would return up to limit * page_count rows, not "
                    "a global cap. Omit limit (resolves to each page's own "
                    "batch size), or pre-chunk ids into <= page_size batches "
                    "yourself."
                )
            out_ids: list[Any] = []
            out_docs: list[Any] = []
            out_metas: list[Any] = []
            extra: dict[str, list[Any]] = {}
            for start in range(0, len(ids), page_size):
                batch = ids[start : start + page_size]
                limit_sent = len(batch) if limit is None else limit
                body: dict[str, Any] = {
                    "collection": self._name,
                    "ids": batch,
                    "limit": limit_sent,
                    "offset": offset,
                }
                if include_source_uri:
                    body["include_source_uri"] = True
                page = _post("/v1/vectors/store-get", body, tenant=self._tenant)
                page_ids = page.get("ids", []) or []
                # nexus-hdx2u E4 (fix-round): engine-side count (E3, now a
                # single-statement/single-snapshot COUNT(*) OVER() — see
                # PgVectorRepository.get) is ADVISORY and, when present,
                # SUPERSEDES the pre-count heuristic below rather than firing
                # alongside it — a present count is authoritative (one
                # snapshot with the rows it describes), so re-running the
                # heuristic on top of it would be redundant at best and, if
                # they ever disagreed, a confusing second signal. Absent
                # (pre-E3 engine, or a rolling deploy window) is the ONLY
                # case that falls back to the heuristic and logs the once-
                # per-process count-unreported notice.
                if "count" in page:
                    reported_count = page["count"]
                    if reported_count > len(page_ids):
                        # A real truncation the engine itself measured (not a
                        # client-side guess) — raises rather than warns,
                        # unlike the update_chunks "missing"-omitted
                        # precedent this mirrors, because an over-limit
                        # count is not an expected condition.
                        raise VectorServiceError(
                            f"store-get for collection '{self._name}': engine "
                            f"reports {reported_count} matched-live rows for "
                            f"this batch but returned only {len(page_ids)} — "
                            f"truncated at limit={limit_sent} (batch size "
                            f"{len(batch)})."
                        )
                else:
                    if len(page_ids) == limit_sent and limit_sent < len(batch):
                        _warn_store_get_truncated_at_limit(self._name, limit_sent, len(batch))
                    _warn_store_get_count_unreported(self._name)
                out_ids.extend(page_ids)
                out_docs.extend(page.get("documents", []) or [])
                out_metas.extend(page.get("metadatas", []) or [])
                for key in ("chashes", "source_uris", "spans"):
                    if key in page:
                        extra.setdefault(key, []).extend(page[key] or [])
            out: dict[str, Any] = {
                "ids": out_ids,
                "documents": out_docs,
                "metadatas": out_metas,
            }
            out.update(extra)
            return out
        else:
            # Where-filter lookup (incremental-sync staleness check)
            resolved_limit = _WHERE_GET_DEFAULT_LIMIT if limit is None else limit
            body = {
                "collection": self._name,
                "limit": resolved_limit,
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
            out = {
                "ids":       result.get("ids", []),
                "documents": result.get("documents", []),
                "metadatas": result.get("metadatas", []),
            }
            for key in ("chashes", "source_uris", "spans"):
                if key in result:
                    out[key] = result[key]
            return out


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
        """Delete chunks by ID from the service.

        nexus-ou4tb: raises :class:`VectorServiceError` rather than logging and
        returning. A silently-failed prune leaves stale chunks that every later
        read treats as live — the caller believes it pruned, search does not
        agree, and the divergence is permanent until something else happens to
        rewrite them. Same "caller owns the boundary" contract as
        :meth:`count` / :meth:`get_all_metadata`.
        """
        if not ids:
            return
        _post(
            "/v1/vectors/store-delete",
            {"collection": self._name, "ids": ids},
            tenant=self._tenant,
        )


# ── HttpVectorClient ─────────────────────────────────────────────────────────



#: nexus-bm8dd: the four methods below address ``source_path`` in CHUNK metadata.
#: RDR-102 D2 HARD-REMOVED that key from the chunk schema — it is not in
#: ``metadata_schema.ALLOWED_TOP_LEVEL`` and ``make_chunk_metadata()`` raises
#: ``TypeError`` if a caller passes it. So no chunk this codebase writes has
#: carried a ``source_path`` since that change, every one of these where-filters
#: matches nothing, and each returned its "no rows" value: ``[]``, ``0``, ``[]``,
#: ``0``. Indistinguishable from "nothing to do".
#:
#: The damage is not the wasted call, it is the FALSE ALL-CLEAR:
#: ``nx t3 prune-stale`` reported "0 stale" on every corpus, and the documented
#: "delete this document's chunks and re-index" recovery silently deleted
#: nothing. They now raise (no-silent-fallbacks directive).
#:
#: The replacement addressing is the catalog: a document's chunks are its
#: manifest rows (``documents.tumbler -> document_chunks.doc_id ->
#: document_chunks.chash``), and its path is ``resolve_path(tumbler)``. Use
#: ``HttpCatalogClient.list_by_collection`` + ``get_manifests``.
_SOURCE_PATH_RETIRED = (
    "chunk metadata has carried no source_path since RDR-102 D2 removed it from "
    "the schema, so this method can only ever match zero chunks (nexus-bm8dd). "
    "Address a document's chunks through the catalog manifest instead: "
    "HttpCatalogClient.list_by_collection() -> get_manifests() -> chashes, with "
    "resolve_path(tumbler) for the on-disk path."
)


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
        pages = (n + cap - 1) // cap if n else 0
        for page_num, start in enumerate(range(0, n, cap), start=1):
            end = min(start + cap, n)
            page_ids = ids[start:end]
            body: dict[str, Any] = {
                "collection": collection,
                "ids": page_ids,
                "documents": documents[start:end],
                "metadatas": metas[start:end],
            }
            if embeddings is not None:
                body["embeddings"] = embeddings[start:end]
            if force_re_embed:
                body["force_re_embed"] = True
            # nexus-gtl01 (upsert-chunks ACK coverage): log the OUTGOING
            # request before the POST, at INFO not DEBUG. This is the only
            # client-side evidence a write was even ATTEMPTED. INFO does NOT
            # survive the CLI's untouched WARNING default — it is visible
            # under NEXUS_LOG_LEVEL=INFO (a realistic troubleshooting
            # setting) and trivially under the scenario journeys' DEBUG env;
            # daemon-family modes (mcp/watchdog/t3_daemon/storage_service)
            # default to INFO, so a background write emits ~2 lines per page
            # into their rotating logs — bounded, and exactly where post-hoc
            # evidence matters most (see the sibling response log below for
            # the full rationale). Cheap: two integer counts + a bool, no
            # chash lists. ``distinct_chash_count`` is separate from ``count``
            # because every real caller sends ``ids`` == the chash list —
            # a page containing a DUPLICATE id collapses server-side
            # (``PgVectorRepository.upsertChunksInternal``'s in-batch dedup,
            # engine event ``upsert_dedup_collapsed``); a divergence here is
            # itself a signal worth having on the wire.
            _log.info(
                "http_vector_upsert_chunks_request",
                collection=collection,
                page=page_num,
                pages=pages,
                count=len(page_ids),
                distinct_chash_count=len(set(page_ids)),
                force_re_embed=force_re_embed,
            )
            result = _post(
                "/v1/vectors/upsert-chunks", body, tenant=self._tenant, timeout=600,
            )
            # nexus-znwc2 / nexus-ir6eh: the engine echoes ids.length as
            # `upserted` unconditionally (VectorHandler), so any deviation —
            # missing field or wrong count — means something interposed on
            # the WRITE path (stub proxy, truncating hop). A write whose ack
            # cannot be reconciled must not read as success.
            ack_present = isinstance(result, dict) and "upserted" in result
            acked = result.get("upserted") if isinstance(result, dict) else None
            # nexus-gtl01: log the verdict unconditionally, at INFO, BEFORE
            # the raise below fires (or doesn't) — the RuntimeError message
            # already carries sent/acked, but that text only surfaces if a
            # caller catches-and-prints it (CliRunner does; a background
            # aspect worker or MCP tool call may not). This line is
            # independent evidence either way.
            #
            # ``ack_present=False`` (the response carried no ``upserted``
            # key at all) is itself the finding, not a degraded case of the
            # mismatch below — the RuntimeError still fires (None != count),
            # but the field's *presence* answers a question the count alone
            # can't: whether the engine gave a verdict at all. Note the
            # documented gap this exposes for the service/ arc: because the
            # engine "echoes ids.length unconditionally" (comment above),
            # ``acked == count`` on a HEALTHY response is not independent
            # confirmation the rows are durably committed — it is a
            # request-shape echo, not a commit receipt. Distinguishing
            # "acked but lost" from "landed" needs an engine-side per-upsert
            # commit log tying (collection, row count) to a transaction —
            # that does not exist today and is out of this path's fence
            # (service/); this log line is the client-side half only.
            _log.info(
                "http_vector_upsert_chunks_response",
                collection=collection,
                page=page_num,
                sent=len(page_ids),
                acked=acked,
                ack_present=ack_present,
                match=(acked == len(page_ids)),
            )
            if acked != end - start:
                raise RuntimeError(
                    f"upsert-chunks ack mismatch for {collection!r}: sent "
                    f"{end - start} ids, service acked {acked!r} — the write "
                    "path may be intercepted or stubbed; refusing to treat "
                    "the write as durable"
                )
        _log.debug(
            "http_vector_upsert_chunks",
            collection=collection,
            count=n,
            pages=pages,
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
        list, same doc_id derivation (the FULL ``sha256(content).hexdigest()``,
        RDR-180 / nexus-jxizy.3), and metadata built
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

        # RDR-180 (nexus-p78a0 rehearsal catch, run 4): the natural id is the
        # FULL digest — this mirror kept the retired [:32] truncation after
        # T3Database.put was converted, so every service-mode store_put wrote
        # a 16-byte key and 409'd on the cohort engine's octet CHECK
        # (chunks_*_chash_octet_check, sqlstate 23514). Parity with
        # T3Database.put is pinned by test_store_put_id_is_the_full_digest.
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        doc_id = content_hash
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

    #: RDR-188: this backend serves the fused rerank stage (P1.2 request
    #: fields on /v1/vectors/search). Capability marker read by
    #: ``search_engine.search_cross_corpus`` — legacy backends without it are
    #: never asked to rerank.
    supports_server_rerank: bool = True

    #: Memoized GET /version ``embedding_mode`` (class-level default so
    #: partially-constructed test instances still resolve; successful probes
    #: shadow it per-instance). RDR-188 P3.2 (nexus-9o6y2.14).
    _embedding_mode_memo: str | None = None
    _embedding_mode_probe_failures: int = 0
    _EMBEDDING_MODE_MAX_PROBES: int = 3

    def embedding_mode(self) -> str | None:
        """The engine's AUTHORITATIVE embedder family from ``GET /version``:
        ``"voyage"`` or ``"onnx-local"`` (``EmbedderRouter.modeName``).

        RDR-188 P3.2 (nexus-9o6y2.14): this replaces client-key inference as
        the search threshold gate's signal — thresholds are calibrated for
        Voyage embeddings, and whether Voyage served the query is a fact
        about the SERVER, not about which keys sit in client config.

        Memoized per instance on success (the engine's mode is fixed for its
        process lifetime). A failed probe returns ``None`` WITHOUT memoizing —
        unknown, not "not voyage" — so a service that was briefly unreachable
        is re-asked on the next call rather than locking thresholds off.
        Bounded (reviewer Medium, T2 [21057]): after
        ``_EMBEDDING_MODE_MAX_PROBES`` consecutive failures the probe settles
        on unknown for the process lifetime — a persistently-failing /version
        beside a working search path must not tax every search with an extra
        round trip forever.
        """
        if self._embedding_mode_memo is None:
            if self._embedding_mode_probe_failures >= self._EMBEDDING_MODE_MAX_PROBES:
                return None
            try:
                info = _get("/version", tenant=self._tenant)
            except Exception as exc:  # noqa: BLE001 — probe is advisory; search itself surfaces a down service
                self._embedding_mode_probe_failures += 1
                if self._embedding_mode_probe_failures >= self._EMBEDDING_MODE_MAX_PROBES:
                    _log.warning(
                        "embedding_mode_probe_settled_unknown",
                        attempts=self._embedding_mode_probe_failures,
                        consequence="voyage-calibrated distance thresholds stay off "
                                    "for this process",
                        error=str(exc),
                    )
                else:
                    _log.debug("embedding_mode_probe_failed", error=str(exc))
                return None
            mode = info.get("embedding_mode") if isinstance(info, dict) else None
            if isinstance(mode, str) and mode:
                self._embedding_mode_probe_failures = 0
                self._embedding_mode_memo = mode
            else:
                # Critic fold (T2 [21058]): a REACHABLE /version without a
                # usable embedding_mode — exactly what every pre-.10 engine
                # returns — is a probe failure too. Counting only exceptions
                # would reset the counter here and re-probe on every search
                # for the process lifetime of the client singleton.
                self._embedding_mode_probe_failures += 1
                if self._embedding_mode_probe_failures >= self._EMBEDDING_MODE_MAX_PROBES:
                    _log.warning(
                        "embedding_mode_probe_settled_unknown",
                        attempts=self._embedding_mode_probe_failures,
                        reason="/version reachable but reports no embedding_mode "
                               "(engine predates the field)",
                        consequence="voyage-calibrated distance thresholds stay off "
                                    "for this process",
                    )
        return self._embedding_mode_memo

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
        rerank: bool = False,
        rerank_top_k: int | None = None,
        rerank_meta_out: dict | None = None,
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

        RDR-188 (bead nexus-9o6y2.8): ``rerank=True`` requests the server's
        fused rerank stage. The response becomes an object envelope
        ``{"results": [...], "rerank_degraded": ..., ...}``; scored rows carry
        ``rerank_score``. The envelope's degrade state is written into
        ``rerank_meta_out`` (``{"degraded", "error", "model"}``) — the caller
        MUST surface a degrade to the user (Gap 2: WARN-only invisibility is
        the retired defect). An engine predating the fused stage ignores the
        unknown field and returns a bare array: reported as
        ``degraded=True, stale_engine=True`` with the convergence remedy —
        one-engine doctrine, never a refusal.
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
        if rerank:
            body["rerank"] = True
            if rerank_top_k is not None:
                body["rerank_top_k"] = rerank_top_k

        results = _post("/v1/vectors/search", body, tenant=self._tenant)
        # results is a list of {id, content, distance, collection, ...} — or,
        # when rerank was requested against a rerank-capable engine, the
        # RerankStage object envelope.

        if rerank:
            if isinstance(results, dict) and "results" in results:
                if "rerank_degraded" in results:
                    meta = {
                        "degraded": bool(results.get("rerank_degraded")),
                        "error": results.get("rerank_error"),
                        "model": results.get("rerank_model"),
                    }
                else:
                    # nexus-znwc2: an object envelope WITHOUT the degrade flag
                    # cannot attest rerank ran. The engine's RerankStage emits
                    # rerank_degraded unconditionally in the rerank envelope,
                    # so absence means a field-stripping middleman (the
                    # /version-stub class) — absence-of-flag is NOT success.
                    meta = {
                        "degraded": True,
                        "error": (
                            "rerank envelope carried no rerank_degraded flag "
                            "(field-stripping middleman?) — cannot attest the "
                            "server reranked; treating results as "
                            "distance-ordered"
                        ),
                    }
                results = results["results"]
            else:
                meta = {
                    "degraded": True,
                    "stale_engine": True,
                    "error": (
                        "engine predates server-side rerank; `nx upgrade` "
                        "converges the local engine (managed cloud: server "
                        "upgrade pending)"
                    ),
                }
            if rerank_meta_out is not None:
                rerank_meta_out.update(meta)

        if structured:
            # Return the plan-runner compatible structured form.
            # nexus-znwc2 (reviewer H1): a missing/None distance is emitted
            # as an honest None, never 0.0 (fabricated best-match on a
            # stripped field) and never float('inf') (the MCP text
            # serializer renders inf as the bare `Infinity` token — invalid
            # JSON for strict clients).
            distances = [r.get("distance") for r in results]
            missing = sum(1 for d in distances if d is None)
            if missing:
                _log.warning(
                    "search_structured_rows_missing_distance",
                    missing=missing, total=len(results),
                    consequence="emitting null distances (never a fabricated 0.0)",
                )
            return {
                "ids":         [r.get("id", "")         for r in results],
                "tumblers":    [r.get("tumbler", "")    for r in results],
                "distances":   distances,
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

        This conflates TOMBSTONED with truly ABSENT under one boolean. Callers
        that must tell the two apart (rename/migration collision guards) use
        :meth:`collection_probe` — or the module-level
        :func:`nexus.db.collection_state.probe_collection_state`, which also
        degrades cleanly for non-HttpVectorClient backends — instead of this
        method alone (nexus-9n485).
        """
        return any(c.get("name") == name for c in self.list_collections())

    def collection_exists_raw(self, name: str) -> bool:
        """True if *name* has ANY physical chunk row for this tenant — LIVE or TOMBSTONED.

        Hits ``GET /v1/vectors/collections`` directly — a bare ``SELECT
        DISTINCT collection`` union over ``chunks_384``/``chunks_768``/
        ``chunks_1024`` (:meth:`PgVectorRepository.listCollections` on the
        Java side), NOT the tombstone-filtered ``collection_vector_stats``
        view :meth:`collection_exists` reads. Trashing a document
        (catalog-003) only sets ``catalog_documents.deleted_at`` — it never
        deletes rows from the physical chunk tables (that is ``purge_trash``'s
        job, a separate later step) — so a collection whose every document is
        trashed still appears in this raw listing even though
        :meth:`collection_exists` reads it as absent.

        Raises rather than degrading to False on a service error: a caller
        using this to distinguish "tombstoned" from "absent" must not read a
        transient failure as a confident ABSENT (nexus-9n485; mirrors the
        no-silent-fallback-for-correctness directive).
        """
        result = _get("/v1/vectors/collections", tenant=self._tenant)
        names = {c.get("name", "") for c in result} if isinstance(result, list) else set()
        return name in names

    def collection_probe(self, name: str) -> "CollectionState":
        """Three-state existence probe: PRESENT / TOMBSTONED / ABSENT.

        Distinguishes "trashed — restore it first" (:data:`CollectionState.TOMBSTONED`)
        from "never existed" (:data:`CollectionState.ABSENT`) — the ambiguity
        :meth:`collection_exists` alone cannot resolve (nexus-9n485, RDR-156 P3
        gate finding nexus-70r3c.13). The raw probe only runs when the live
        check already says no — the common case (a name with live data) never
        pays the extra round trip.
        """
        from nexus.db.collection_state import CollectionState  # noqa: PLC0415 — deferred to avoid an import cycle (collection_state -> http_vector_client)

        if self.collection_exists(name):
            return CollectionState.PRESENT
        if self.collection_exists_raw(name):
            return CollectionState.TOMBSTONED
        return CollectionState.ABSENT

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
        ids per request to mirror the historical batch shape.

        nexus-ou4tb: raises :class:`VectorServiceError` rather than resolving
        an unreachable collection to the empty set. Empty here does not mean
        "nothing present" to any caller — it means "everything you asked about
        is MISSING", and the callers act on that: ``nx catalog verify``
        reports every expected document as a ghost, the migration ETL
        concludes nothing landed and rewrites it, and ``--skip-existing``
        silently skips nothing. A degraded service must not be able to produce
        a clean-looking integrity report.

        ``doc_indexer`` already wraps this call and degrades explicitly to a
        full upsert, which is the shape a caller that genuinely can tolerate
        the failure should use.
        """
        if not ids:
            return set()
        found: set[str] = set()
        page = 300
        for start in range(0, len(ids), page):
            batch = ids[start : start + page]
            result = _post(
                "/v1/vectors/store-get",
                {"collection": collection, "ids": batch, "limit": len(batch)},
                tenant=self._tenant,
            )
            found.update(result.get("ids") or [])
        return found

    def update_chunks(
        self,
        collection: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> list[str] | None:
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

        nexus-5xn3k.5 (memo §3.6, AC6 client half): the engine's response is
        ``{"updated": N, "missing": [ids...]}`` (nexus-5xn3k.2). Returns the
        UNION of ``missing`` across all pages — the subset of *ids* that had
        no matching row, i.e. the caller's ``existing_ids`` probe was a stale
        positive for them. Returns ``None`` — never an assumed-empty list —
        when ANY page's response omits the ``"missing"`` key. This is an
        ALL-PAGES-REPORTED requirement, not "at least one page reported":
        a mixed-version engine fleet (rolling deploy mid-request) can have
        page 1 report ``missing`` and page 2 omit it, and page 2's ids must
        NOT be silently assumed zero-miss just because an earlier page had
        the field — cannot-tell dominates. Logged at WARNING (log-once per
        process — see :func:`_warn_update_chunks_missing_unreported`) so a
        silently-degraded engine (in full, or on just one page) is never
        mistaken for a clean report. Returns ``[]`` when EVERY page reported
        the field and it was genuinely empty (no misses, no warning).

        A genuinely non-empty ``missing`` (every page reported, at least one
        id was actually stale) ALSO logs a WARNING — ``update_chunks_missing_
        reported`` — from THIS method, unconditionally, regardless of what
        the caller does with the return value. Every call site (the
        frecency-only reindex path, ``pipeline_stages.py``, ``indexer.py``,
        and ``doc_indexer.py``'s repair reroute below) gets the anomaly
        signal for free, not just the one caller that happens to act on it.
        ``doc_indexer._upsert_skip_reembed`` additionally logs its own
        ``update_chunks_missing_rerouted`` when it re-routes — that is the
        separate CALLER-SIDE repair log, not a duplicate of this one.

        Division of labor (nexus-5xn3k.5 vs .4): this method — and its one
        reroute caller — repairs a STALE-POSITIVE PROBE miss: the id was
        reported present by ``existing_ids`` but was already gone by the
        time this metadata-only update ran. It does NOT protect against a
        row vanishing AFTER a successful write (a post-repair race) — that
        window is the ``/index-run/complete`` fail-closed verify's job
        (bead nexus-5xn3k.4, RUNFENCE verify-then-stamp). Do not treat this
        path as covering that later window.
        """
        if not ids:
            return []
        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)
        # Request-size chunk only (see docstring) — not a backend quota.
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        missing: list[str] = []
        all_pages_reported = True
        for start in range(0, len(ids), size):
            batch_ids  = ids[start : start + size]
            batch_meta = metadatas[start : start + size]
            result = _post(
                "/v1/vectors/update-metadata",
                {"collection": collection, "ids": batch_ids, "metadatas": batch_meta},
                tenant=self._tenant,
            )
            if isinstance(result, dict) and "missing" in result:
                missing.extend(result.get("missing") or [])
            else:
                all_pages_reported = False
        _log.debug(
            "http_vector_update_chunks",
            collection=collection,
            count=len(ids),
        )
        if not all_pages_reported:
            _warn_update_chunks_missing_unreported(collection, len(ids))
            return None
        if missing:
            # In-method anomaly signal (substantive-critic follow-up): fires
            # for EVERY caller, not just doc_indexer's reroute — a stale-
            # positive probe result is worth surfacing even when the caller
            # ignores the return value.
            _log.warning(
                "update_chunks_missing_reported",
                collection=collection,
                count=len(missing),
                total=len(ids),
            )
        return missing

    # ── Collection-handle stub for doc_indexer staleness + prune paths ─────────

    def get_collection(self, name: str) -> "_ServiceCollectionStub":
        """Return a collection stub, raising ChromaNotFoundError if the collection does not exist.

        RDR-152 bead nexus-enehl: mirrors T3Database.get_collection() semantics
        for the frecency-only loop.  The loop catches the missing-collection
        error and skips collections that have not yet been indexed.

        Checks existence via the service's ``/v1/vectors/collections`` list.
        A missing collection raises the substrate-neutral
        :class:`nexus.errors.CollectionNotFoundError` (RDR-155 P4b P0c — the
        successor to ``chromadb.errors.NotFoundError``; catchers tolerate
        both via :func:`nexus.errors.collection_not_found_errors` during the
        transition window) rather than creating a zombie collection
        (contrast with :meth:`get_or_create_collection`).
        """
        from nexus.errors import CollectionNotFoundError  # noqa: PLC0415 — local import mirrors the deferred style of this method's callers

        try:
            cols = self.list_collections()
            if not any(c.get("name") == name for c in cols):
                raise CollectionNotFoundError(f"collection {name!r} not found in service")
        except VectorServiceError as exc:
            raise CollectionNotFoundError(
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

    def get_embeddings(
        self,
        collection_name: str,
        ids: list[str],
        on_progress: Callable[[int, int], None] | None = None,
    ):
        """Fetch stored embeddings for *ids* via the service (nexus-pebfx.7).

        Param name ``collection_name`` matches ``T3Database.get_embeddings``
        (nexus-7zuzz). The HTTP body key stays ``"collection"`` — that is what
        the Java VectorHandler reads.

        Mirrors ``T3Database.get_embeddings``: returns an ``(N, D)`` float32
        ndarray with rows in request order; ids the service does not find
        are DROPPED (``N < len(ids)``), which the search-engine caller
        already treats as a per-collection shape-mismatch failure —
        identical to the Chroma path's semantics.

        Paged (nexus-g7ubw): a single POST carrying every id deterministically
        504s at the gateway on large collections — 28k ids x 1024-dim vectors
        is a response measured in hundreds of MB. Rows are concatenated
        batch-by-batch, so request order (and the caller's
        ``len(embeddings) == len(ids)`` alignment tripwire) is preserved.

        NOTE: ``MAX_RECORDS_PER_WRITE`` here is a pragmatic per-request
        RESPONSE-SIZE chunk, not a backend write quota (same caveat as
        ``update_metadata_batch``). The constraint being managed is response
        bytes (~3 MB at 300 ids x 1024 dims); if the write quota is ever
        raised or a higher-dimension embedding model becomes the default,
        re-derive this page size from bytes or the 504 returns.

        ``on_progress(done, total)`` (id counts) fires after each batch —
        a 28k-id fetch is ~94 sequential round-trips and looked like a hang
        without it (nexus-g7ubw review follow-up).
        """
        import numpy as np  # noqa: PLC0415 — heavy/optional dependency deferred to call time

        from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        rows: list = []
        for start in range(0, len(ids), page_limit):
            batch = ids[start : start + page_limit]
            result = _post(
                "/v1/vectors/get-embeddings",
                {"collection": collection_name, "ids": batch},
                tenant=self._tenant,
            )
            rows.extend(result.get("embeddings", []))
            if on_progress is not None:
                on_progress(start + len(batch), len(ids))
        return np.array(rows, dtype=np.float32)

    # ── Stubs for T3Database surface not used by Seam B ─────────────────────

    def delete_collection(self, name: str) -> None:
        raise NotImplementedError("delete_collection not implemented in HttpVectorClient")

    def ids_for_source(self, collection_name: str, source_path: str) -> list[str]:
        """UNSUPPORTED — chunk metadata has no ``source_path`` (nexus-bm8dd).

        Raises ``NotImplementedError``. See :data:`_SOURCE_PATH_RETIRED`: this
        used to page ``/v1/vectors/get`` with ``where={"source_path": ...}``,
        which has matched nothing since RDR-102 D2 removed the key from the
        chunk schema — and returned ``[]``, which reads as "this source has no
        chunks".
        """
        raise NotImplementedError(
            f"ids_for_source({collection_name!r}, {source_path!r}): {_SOURCE_PATH_RETIRED}"
        )

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """UNSUPPORTED — chunk metadata has no ``source_path`` (nexus-bm8dd).

        Raises ``NotImplementedError``. It returned ``0``, so the documented
        "delete this document's chunks and re-index" recovery deleted nothing
        and said so in a way indistinguishable from "there was nothing to
        delete". Delete by chash instead: take the document's manifest rows from
        ``HttpCatalogClient.get_manifests`` and pass them to
        :meth:`delete_by_ids`.
        """
        raise NotImplementedError(
            f"delete_by_source({collection_name!r}, {source_path!r}): {_SOURCE_PATH_RETIRED}"
        )

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
        """UNSUPPORTED — chunk metadata has no ``source_path`` (nexus-bm8dd).

        Raises ``NotImplementedError``. There is nothing in a chunk to rewrite:
        a document's path lives on its CATALOG row, which ``nx doctor
        fix-paths`` already updates via ``writer.update(tumbler, file_path=...)``
        on the line after it called this. The ``n`` chunks it reported repaired
        was always 0.
        """
        raise NotImplementedError(
            f"update_source_path({collection_name!r}, {old_path!r} -> {new_path!r}): "
            f"{_SOURCE_PATH_RETIRED}"
        )

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
        """UNSUPPORTED — chunk metadata has no ``source_path`` (nexus-bm8dd).

        Raises ``NotImplementedError``. This one caused the worst of the
        silences: it fed ``nx t3 prune-stale``'s sweep, and an empty list ended
        the loop before it began, so the verb printed a clean "0 stale" on every
        corpus regardless of how many indexed files had been deleted from disk.
        The catalog answers this: ``HttpCatalogClient.list_by_collection`` gives
        the documents, ``resolve_path(tumbler)`` gives each one's path.
        """
        raise NotImplementedError(
            f"list_unique_source_paths({collection_name!r}): {_SOURCE_PATH_RETIRED}"
        )

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
#: (a probe result being cached, or the nexus-5t1jp healing clear of an
#: expired unreachable-class failure) holds :data:`_vector_client_lock`.
#: Because the healing clear made ``_version_probe_error`` production-
#: writable back to None, lock-free readers MUST take a single snapshot via
#: :func:`_authoritative_cached_probe_error` (nexus-aedaw) rather than
#: reading the global twice. Also cleared by
#: :func:`reset_http_vector_client_for_tests`.
_version_probe_done: bool = False
_version_probe_error: Exception | None = None

#: nexus-5t1jp: monotonic timestamp of the cached probe FAILURE, driving the
#: unreachable-class retry window below. None whenever no failure is cached.
_version_probe_failed_at: float | None = None

#: nexus-5t1jp: how long a cached UNREACHABLE-class probe failure stays
#: authoritative before the next call re-probes. Bounded, not per-call: a
#: dead-host probe can burn its full connect timeout, and hammering that on
#: every T3 tool call would make a down service cost seconds per call. Within
#: the window callers get the cheap cached re-raise; after it, one caller
#: pays one probe. The INCOMPATIBLE class never retries (below).
_PROBE_UNREACHABLE_RETRY_S: float = 30.0

#: Indirection for tests: patching the global ``time`` module would leak
#: across the suite; patching this name affects only this cache's clock.
_monotonic = time.monotonic


def _authoritative_cached_probe_error() -> Exception | None:
    """Is the cached probe failure still authoritative? (nexus-5t1jp)

    The cache used to be unconditional for the process lifetime — the same
    disease nexus-brw1s cured for T1, one layer down: a managed service that
    was down at the FIRST T3 call left every T3 tool cached-failed for the
    rest of the session, even after `nx daemon service start` brought it back.

    The split is by FAILURE CLASS, honoring the original nexus-b6qlf
    rationale rather than discarding it:

    * INCOMPATIBLE (below-floor engine, non-nexus endpoint, bad /version
      body): genuinely stable within a process — the deployed engine does not
      change under a running client — so the cache is authoritative forever,
      exactly as b6qlf designed. Never re-probed.
    * UNREACHABLE (connect/TLS/DNS/timeout): transient by nature. The cache
      is authoritative only within :data:`_PROBE_UNREACHABLE_RETRY_S`; after
      that the next call clears it and re-probes, so the session HEALS when
      the service comes back. b6qlf's traceback-accumulation concern applied
      to re-raising the SAME instance, never to re-probing — the fresh-copy
      re-raise mechanism is unchanged within the window.

    Returns the SNAPSHOT of the cached error when it is still authoritative,
    else None. Returning the snapshot (not a bool) is load-bearing
    (nexus-aedaw): the healing clear made ``_version_probe_error`` writable
    in production, so a bool-returning helper plus a re-read in the caller is
    a TOCTOU — a concurrent clear between the two reads would hand
    ``_reraise_cached_probe_error`` a None and surface a raw TypeError
    instead of a ManagedServiceError. Callers must re-raise exactly the
    object returned here.
    """
    # Single snapshot read of _version_probe_error, taken FIRST. A torn read
    # against the (lock-held) writers degrades safely: any inconsistency
    # between the snapshot and _version_probe_failed_at returns None here,
    # and the caller falls through to the lock, which re-validates -- never
    # a wrong fast-path raise.
    cached = _version_probe_error
    if cached is None:
        return None

    from nexus.db.managed_endpoint import ManagedServiceIncompatible  # noqa: PLC0415

    if isinstance(cached, ManagedServiceIncompatible):
        return cached  # incompatible-class: process-lifetime, by design
    # Everything else (unreachable today; any future ManagedServiceError
    # subclass) is treated as transient — the safe default is a retry
    # window, never forever-cached.
    failed_at = _version_probe_failed_at
    if failed_at is None:  # torn/cleared: treat as expired
        return None
    if (_monotonic() - failed_at) < _PROBE_UNREACHABLE_RETRY_S:
        return cached
    return None


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
      RAISED immediately, blocking construction. While the cache is
      authoritative (see :func:`_authoritative_cached_probe_error` -- forever for
      the INCOMPATIBLE class, within :data:`_PROBE_UNREACHABLE_RETRY_S` for
      the UNREACHABLE class, nexus-5t1jp), each call re-raises a FRESH
      instance of the same cached error (type + message, chained via
      ``__cause__``) rather than re-probing a state we already know --
      re-raising the SAME instance across call frames would make CPython
      prepend a new traceback frame every time, growing unboundedly in a
      long-running process (nexus-b6qlf Fix 3). An unreachable-class
      failure whose window elapsed is cleared and re-probed, so the
      session heals when the managed service comes back.
    * Local mode: the probe is skipped entirely. Local mode's own floor
      enforcement (the ``nx upgrade`` / engine-convergence flow) is
      untouched by this gate.
    """
    global _vector_client_instance, _version_probe_done, _version_probe_error
    global _version_probe_failed_at
    from nexus.config import is_local_mode  # noqa: PLC0415 -- deferred for test patchability

    cloud_mode = not is_local_mode()

    if cloud_mode and (cached_failure := _authoritative_cached_probe_error()) is not None:
        _reraise_cached_probe_error(cached_failure)

    if _vector_client_instance is not None and (not cloud_mode or _version_probe_done):
        return _vector_client_instance

    with _vector_client_lock:
        if cloud_mode:
            if (cached_failure := _authoritative_cached_probe_error()) is not None:
                _reraise_cached_probe_error(cached_failure)
            if _version_probe_error is not None:
                # nexus-5t1jp: an unreachable-class failure whose retry window
                # elapsed — clear it and fall through to a fresh probe.
                _log.info("cloud_engine_version_reprobe_after_unreachable")
                _version_probe_error = None
                _version_probe_failed_at = None
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
                    _version_probe_failed_at = _monotonic()
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
    global _version_probe_failed_at
    with _vector_client_lock:
        _vector_client_instance = None
        _version_probe_done = False
        _version_probe_error = None
        _version_probe_failed_at = None

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
