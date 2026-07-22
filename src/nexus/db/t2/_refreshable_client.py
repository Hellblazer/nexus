# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RefreshableHttpStoreMixin — shared credential/connection-refresh shape for
T2 ``Http*Store`` classes (nexus-bikit).

LOCKED design: T2 ``nexus/design-bikit-refreshable-http-store-mixin.md``.
Full plan (audited via ``nx_plan_audit``): T2
``nexus/plan-bikit-refreshable-http-store-mixin.md``.
TDD harness this satisfies: ``tests/db/test_refreshable_client.py``
(nexus-bikit.2).

Problem this closes (substantive-critic, 2026-07-10): ~9 of the 10 T2
``Http*Store`` classes bake their bearer token into a per-instance header
dict / ``httpx.Client(headers=...)`` at ``__init__`` and never rebuild on a
401 or a supervisor-restart port change. Only :class:`~nexus.db.http_vector_client.HttpVectorClient`
(T3) is immune — it resolves the token FRESH per request off a shared,
auto-invalidating lease cache. This mixin ports that SHAPE (not the
``urllib`` error taxonomy — T2 stores use ``httpx``) to a shared T2 base so
each store class stops re-inventing (or omitting) the self-heal logic.

Design shape:

- Per-instance state only (``self._base_url``, ``self._tenant``,
  ``self._token``) — no module-level cache. Unlike
  :mod:`nexus.db.http_vector_client`'s process-wide singleton lease cache,
  each ``Http*Store`` instance owns its own credential/endpoint pair.
- ``self._client`` is a single ``httpx.Client`` kept alive for the mixin's
  entire lifetime — refreshing the endpoint never tears down or rebuilds it
  (avoids connection-pool churn). Critically, the client is constructed
  WITHOUT ``base_url=`` (see :meth:`RefreshableHttpStoreMixin.__init__` for
  why) so ``self._base_url`` stays a plain, freely reassignable string field
  and every request builds its own absolute URL.
- ``_auth_headers()`` builds the ``Authorization`` header fresh on every
  call from the CURRENT value of ``self._token`` — never baked once.
- ``_post`` / ``_get`` / ``_delete`` all route through the same ``_send``
  retry wrapper: on a retryable error (401, or a connection-refused/reset
  signature — see :func:`_is_retryable_endpoint_error`), invalidate and
  re-resolve the endpoint, then retry EXACTLY ONCE. A second failure
  propagates normally — no retry loops.
- When ``base_url`` is supplied explicitly but ``_token`` is not, only the
  token half is resolved (see :func:`_resolve_token_only`) — resolving the
  full endpoint in that case would wrongly require host/port to ALSO be
  independently resolvable.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

# nexus-1ytp6: the gateway-transient retry axis is IMPORTED from the T3
# reference implementation, not redefined -- one source of truth for the
# schedule its production incident (live 504, 2026-07-04) calibrated.
# tests/db/test_refreshable_client.py::test_gateway_constants_match_reference
# additionally pins the two modules' values equal against a future
# local-redefinition drift.
from nexus.db.http_vector_client import _GATEWAY_RETRY_CODES, _GATEWAY_RETRY_SLEEPS
from nexus.db.service_endpoint import (
    DEFAULT_LEASE_WAIT_BUDGET_S,
    discover_lease_with_wait,
    has_ever_resolved_lease,
    resolve_service_endpoint,
)

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Shared client timeout — matches the ~9 in-scope stores' hardcoded 30.0s.
#: ``http_aspect_queue`` is the one in-scope store with a public ``timeout``
#: kwarg on its own constructor (nexus-f2qvx.2): its ``__init__`` accepts
#: ``timeout`` and threads it through to ``super().__init__(..., timeout=timeout)``
#: below, so this default only applies when a caller (or subclass) does not
#: pass an explicit value.
_DEFAULT_TIMEOUT_S = 30.0


def _is_retryable_endpoint_error(exc: Exception) -> bool:
    """httpx-flavored analog of ``http_vector_client._is_retryable_endpoint_error``.

    Mirrors the SHAPE of the T3 vector client's classifier
    (``src/nexus/db/http_vector_client.py:194-213``), not its ``urllib``
    taxonomy — the T2 stores transport over ``httpx``:

    - HTTP 401: token rotated + republished (by this store's own retry, a
      sibling process, or an operator).
    - ``httpx.ConnectError``: the supervisor restarted and the old port is
      dead (connection refused), or the host is otherwise unreachable.
    - ``httpx.ConnectTimeout``: the supervisor restart's brief half-open
      socket window can hang the connect attempt rather than refusing it
      outright — same underlying cause as ``ConnectError``, different httpx
      exception (substantive-critic Critical finding, nexus-bikit.3 review:
      ``ConnectTimeout`` subclasses ``TimeoutException``, NOT ``ConnectError``,
      so it was silently unhandled without this explicit entry — verified via
      the actual httpx exception MRO, not assumed).
    - ``httpx.ReadError``: a TCP RST arriving mid-response-read; httpx
      classifies this as a network error distinct from
      ``RemoteProtocolError`` (which is httpx's own malformed-response-shape
      error). Same restart-window cause as the reset case below, different
      httpx exception (same review finding as ``ConnectTimeout`` above —
      ``ReadError`` subclasses ``NetworkError``, not ``RemoteProtocolError``).
    - ``httpx.ReadTimeout`` / ``httpx.WriteTimeout`` / ``httpx.WriteError``
      (nexus-1ytp6, decision-surface audit 2026-07-12): the read/write-phase
      siblings of the pairs above, enumerated from httpx's actual exception
      taxonomy rather than added reactively. A restart-window failure can
      manifest in the SAME phase as either a reset (``ReadError``/
      ``WriteError``) or a hang (``ReadTimeout``/``WriteTimeout``) — e.g. a
      proxy/LB that does not immediately propagate the backend's RST, or
      the JVM's shutdown drain leaving the socket open without writing.
      ``ReadTimeout`` is NOT a subclass of ``ReadError`` (they live under
      ``TimeoutException`` vs ``NetworkError``) — the identical non-subclass
      relationship that justified adding ``ConnectTimeout`` alongside
      ``ConnectError`` in the nexus-bikit.3 round.
    - ``httpx.CloseError`` (nexus-acp20 — found by the critique of the
      nexus-1ytp6 fix itself, the same sibling-enumeration miss recurring
      one member over): the FOURTH direct ``NetworkError`` sibling
      (Connect/Read/Write/Close). A restart-window failure closing the
      connection gets the same single self-heal retry as every other
      member of its family.
    - ``httpx.PoolTimeout`` is deliberately EXCLUDED: it signals LOCAL
      connection-pool exhaustion (too many concurrent in-flight requests on
      this client), not endpoint/credential staleness — re-resolving cannot
      fix it and an immediate retry would pile onto the exhausted pool.
      Pinned by ``test_pool_timeout_is_not_retryable``.
    - ``httpx.ProxyError`` is deliberately EXCLUDED: no nexus store
      topology (local supervisor lease, or direct managed ``service_url``)
      routes through a proxy, and re-resolving the ENDPOINT cannot fix a
      broken PROXY. Pinned by ``test_proxy_error_is_not_retryable``.
      Remaining taxonomy members are out of scope by kind, not omission:
      ``LocalProtocolError`` / ``UnsupportedProtocol`` (our own bug /
      config error), ``DecodingError`` / ``TooManyRedirects`` (response
      handling, not transport staleness).
    - ``httpx.RemoteProtocolError``: the supervisor SIGTERMs the JVM
      process group on restart, so a request IN FLIGHT at restart time can
      see the connection reset rather than refused. Every ``_post``/``_get``
      caller in this mixin's target classes issues idempotent requests
      (upserts, reads, deletes keyed by natural id), so a single retry
      after a mid-flight reset is safe.
    - Bare ``ConnectionRefusedError`` / ``ConnectionResetError``: defensive
      fallback in case a lower transport layer raises the raw OS error
      instead of httpx's wrapped exception type.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 401
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.CloseError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    return isinstance(exc, (ConnectionRefusedError, ConnectionResetError))


def _resolve_token_only(*, wait_budget_s: float = 0.0) -> str:
    """Resolve just the bearer token, WITHOUT requiring host/port to also
    be independently resolvable (nexus-bikit.4 adoption finding).

    A caller that supplies ``base_url`` explicitly (e.g. pointing a store
    at a fake/test server, or a pre-discovered endpoint) but omits
    ``_token`` should not be forced through the full
    :func:`resolve_service_endpoint` resolution — that function's
    local-supervisor leg (:func:`~nexus.db.service_endpoint.resolve_service_config`)
    unconditionally raises if ``NX_SERVICE_PORT`` is not resolvable via env
    or a supervisor lease, even though the caller already told us where to
    connect and only the token is missing. This broke real callers: three
    integration-test fixtures (``tests/db/test_http_memory_store_integration.py``,
    ``tests/db/test_mvv_memory_service.py``, ``tests/db/test_memory_etl.py``)
    construct ``HttpMemoryStore(base_url=<explicit>, tenant=...)`` relying on
    ``NX_SERVICE_TOKEN`` alone, with no ``NX_SERVICE_PORT`` set in the parent
    process's environment (only in a subprocess env dict for the JVM child).

    Mirrors :func:`resolve_service_endpoint`'s ``service_url``-configured
    branch specifically: ``get_credential("service_token")`` (which itself
    checks ``NX_SERVICE_TOKEN`` env first, then ``config.yml``) then the
    supervisor lease token. This is NOT a general guarantee of returning
    the identical token :func:`resolve_service_endpoint` would have handed
    back in every case (substantive-critic + code-review-expert, both
    nexus-bikit.4 review round 1): when NO ``service_url`` credential is
    configured, :func:`resolve_service_endpoint` instead delegates to
    :func:`~nexus.db.service_endpoint.resolve_service_config`, whose token
    precedence is narrower (``os.environ["NX_SERVICE_TOKEN"]`` only, no
    ``config.yml`` fallback, then lease) — this function does NOT detect or
    branch on which leg would actually apply, so it can return a
    ``config.yml``-sourced token in a scenario where the full-resolution
    path would have ignored ``config.yml`` and fallen through to the
    lease or failed loud instead. Verified narrow in practice: every
    current call site sets ``NX_SERVICE_TOKEN`` via env, which short-
    circuits both orderings identically, so this divergence is not live
    today — but a future caller relying on a persisted ``config.yml``
    token with no ``NX_SERVICE_TOKEN`` env and no ``service_url``
    configured could observe a different resolution than going through
    :func:`resolve_service_endpoint` directly would have produced.

    ``wait_budget_s`` (nexus-7dsgp): threaded to
    :func:`~nexus.db.service_endpoint.discover_lease_with_wait` for the
    same bounded-wait mitigation as :func:`resolve_service_endpoint`'s
    local-supervisor leg — see :meth:`RefreshableHttpStoreMixin._invalidate_and_reresolve`,
    the only caller that passes a nonzero value.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    token = (get_credential("service_token") or "").strip()
    if not token:
        _, lease_token = discover_lease_with_wait(budget_s=wait_budget_s)
        token = lease_token or ""
    if not token:
        raise RuntimeError(
            "no service token is resolvable: base_url was supplied "
            "explicitly but no token was — set NX_SERVICE_TOKEN, run "
            "'nx config set service_token <bearer>', or start the "
            "supervisor with 'nx daemon service start' (publishes a "
            "discoverable lease)."
        )
    return token


def _resolve_endpoint_with_evidence_gate() -> tuple[str, str]:
    """``resolve_service_endpoint()``, with a bounded-wait retry gated on
    process-level evidence (nexus-7dsgp, critic round 1 CRITICAL).

    ``mcp_infra.t2_ctx()`` builds a FRESH ``RefreshableHttpStoreMixin``-backed
    store per call — there is no long-lived instance whose
    ``_invalidate_and_reresolve`` retry path (the mixin's ONLY other
    wait-aware call site) could ever fire for a resolution failure that
    happens at construction time itself. Every ``memory_delete`` /
    ``memory_put`` / T2Database-constructing MCP tool call in service mode
    goes through exactly this path, so a call landing in the 5-10s
    supervisor-respawn gap used to raise "endpoint is not resolvable"
    immediately with NO chance to retry at all — the T2 analog of the
    vector-client gap nexus-7dsgp's T3 half already closed.

    The fix is NOT "always wait" (that reintroduces the nexus-1091
    regression: a genuinely cold process — no supervisor running, ever —
    would pay the full 12s before its correct, unavoidable fail-loud, on
    EVERY T2Database construction). It is evidence-gated:
    :func:`~nexus.db.service_endpoint.has_ever_resolved_lease` records
    whether THIS process has EVER successfully discovered a live lease
    (via any store, any prior call). Positive evidence of a lease-based
    topology that was recently working makes the bounded wait a good bet;
    its absence means fail fast exactly as before — the identical
    evidence-gate principle the T3 fix already applies via its
    ``_lease_cache``, extended to the T2 family's fresh-per-call shape.

    Catches ONLY :class:`~nexus.db.service_endpoint.ServiceEndpointUnresolvableError`
    (code-review round 2, Medium), not bare ``RuntimeError`` — a plain
    ``except RuntimeError`` here would ALSO catch a malformed
    ``NX_SERVICE_PORT`` (a config-typo bug, not a respawn-gap symptom;
    retrying it burns 12s to reproduce the identical parse failure) and,
    if a future change ever threaded ``wait_budget_s`` through the
    managed-``service_url`` leg, would have silently retried that too —
    reintroducing exactly the "never for the managed-cloud URL path"
    violation this bead's other half was fixed to prevent. The subclass
    is raised at, and only at, the genuine not-resolvable site.
    """
    # nexus-bgh2j: the gate body now lives PUBLICLY in service_endpoint
    # (resolve_service_endpoint_with_evidence_gate) so the standalone
    # non-mixin stores share the identical logic — this wrapper survives
    # for its docstring and existing call sites.
    from nexus.db.service_endpoint import resolve_service_endpoint_with_evidence_gate  # noqa: PLC0415 — deferred to avoid circular import

    return resolve_service_endpoint_with_evidence_gate()


def _resolve_token_only_with_evidence_gate() -> str:
    """The ``_resolve_token_only`` analog of
    :func:`_resolve_endpoint_with_evidence_gate` — same evidence-gated
    bounded-wait retry, for the narrower ``base_url``-pinned-but-token-
    omitted construction path (see :func:`_resolve_token_only`'s
    docstring for when that shape occurs)."""
    try:
        return _resolve_token_only()
    except RuntimeError:
        if not has_ever_resolved_lease():
            raise
        return _resolve_token_only(wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S)


class RefreshableHttpStoreMixin:
    """Shared self-healing HTTP transport for T2 ``Http*Store`` classes.

    Subclasses call ``self._post(path, payload)`` / ``self._get(path,
    params)`` / ``self._delete(path, params)`` instead of touching
    ``self._client`` directly — every inline
    ``self._client.get/post/...`` call site is exactly the read-path 401 gap
    this mixin exists to close (see the locked plan's AUDIT REVISION #1).

    TEMPLATE NOTE for the f2qvx adoption sweep (substantive-critic
    observation, nexus-bikit.4 review): ``HttpMemoryStore``'s
    store-SPECIFIC status-code handling (``get()``'s 404-as-``None``,
    ``merge_memories()``'s 409-as-``KeyError``) lives in
    ``http_memory_store.py``, deliberately NOT in this mixin — those are
    THAT store's own API contract, not a general one. Do not copy those
    exact status codes/behaviors onto another store without first checking
    what ITS OWN pre-adoption code actually did for non-2xx responses; a
    different store's Java-side contract may use those same codes for
    something else entirely.
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        # Track which halves were EXPLICITLY pinned by the caller (e.g. a
        # test constructing this store against a fake server) BEFORE the
        # env-resolution fallback below overwrites these locals -- a later
        # retry's re-resolve must never silently overwrite a deliberate pin
        # (substantive-critic Significant finding, nexus-bikit.3 review
        # round 1: the constructor's own "an explicitly-supplied half is
        # never overwritten" contract was not honored by
        # _invalidate_and_reresolve, which unconditionally re-resolved
        # both halves regardless of how the instance was constructed).
        self._base_url_pinned = base_url is not None
        self._token_pinned = _token is not None

        # Pinned contract (tests/db/test_refreshable_client.py): when
        # base_url is omitted, resolve BOTH halves via
        # resolve_service_endpoint() — confirmed stateless (reads
        # NX_SERVICE_HOST/PORT/TOKEN or NX_SERVICE_URL/TOKEN fresh per
        # call; no caching layer of its own). An explicitly-supplied half
        # is never overwritten by the resolved pair.
        #
        # When base_url IS supplied but _token is not, resolve ONLY the
        # token (nexus-bikit.4 adoption finding) — routing this case
        # through resolve_service_endpoint() too would incorrectly demand
        # that host/port ALSO be independently resolvable (env or a
        # supervisor lease), even though the caller already told us where
        # to connect. See _resolve_token_only()'s docstring for the three
        # real call sites this broke.
        if base_url is None:
            resolved_url, resolved_token = _resolve_endpoint_with_evidence_gate()
            base_url = resolved_url
            _token = _token or resolved_token
        elif _token is None:
            _token = _resolve_token_only_with_evidence_gate()

        self._base_url = base_url.rstrip("/")
        self._tenant = tenant
        self._token = _token

        # Deliberately NOT constructed with base_url=... — this is the
        # crux of the bead (design doc's "f2qvx half"). httpx.Client's
        # constructor-time base_url merges the ORIGINAL host:port into
        # every request; a supervisor restart hands back a DIFFERENT port
        # on re-resolve (see _invalidate_and_reresolve below), so
        # self._base_url must stay a plain, freely reassignable string
        # field, and every request must build its own absolute URL
        # (self._base_url + path) rather than lean on httpx to prefix a
        # base_url that was frozen at construction time. The httpx.Client
        # ITSELF still stays alive across a refresh (no pool teardown/
        # rebuild) — only the string field changes, and httpx's connection
        # pool keys per-host internally so a genuine host change simply
        # opens a new pool entry on the next request.
        #
        # timeout defaults to _DEFAULT_TIMEOUT_S (nexus-f2qvx.2 additive
        # change) — every pre-existing caller that does not pass timeout
        # explicitly gets the exact same 30.0s behavior as before this
        # kwarg existed. Only http_aspect_queue passes a non-default value
        # today (its own constructor's public timeout kwarg, threaded
        # through via super().__init__(..., timeout=timeout)).
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()

    # ── Credential / endpoint refresh ───────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        """Build the auth headers FRESH from the CURRENT cached token.

        Never baked once at ``__init__`` — this is what lets a same-instance
        retry (after :meth:`_invalidate_and_reresolve`) actually pick up a
        rotated credential instead of resending the same stale header.
        """
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Nexus-Tenant": self._tenant,
            "Content-Type": "application/json",
        }

    def _invalidate_and_reresolve(self) -> None:
        """Re-resolve credential/endpoint state and update the NON-PINNED
        cached field(s) only.

        Mirrors ``__init__``'s own 3-way resolution branch EXACTLY
        (substantive-critic Critical finding, nexus-bikit.4 review round
        1): the ``base_url``-pinned-but-``token``-not-pinned case must
        re-resolve via :func:`_resolve_token_only`, NOT the full
        :func:`resolve_service_endpoint` — the whole reason that helper
        exists is that full resolution wrongly demands host/port ALSO be
        independently resolvable. The construction-time fix
        (``__init__``'s ``elif _token is None: _token =
        _resolve_token_only()`` branch) was landing in this same review
        round without a matching update HERE, which would have left this
        exact retry path broken for exactly the callers the construction
        fix was meant to unblock: they'd construct successfully, then hit
        ``RuntimeError: nexus-service endpoint is not resolvable`` on the
        very first self-heal attempt.

        ``resolve_service_endpoint()``/``_resolve_token_only()`` have no
        caching of their own, so "invalidate" here just means "discard our
        stale instance field(s) and re-call the right resolver" — updating
        ``self._base_url`` (when not pinned) is what actually fixes the
        f2qvx connection-refused case (a header-only refresh would still
        be pointed at a dead port after a supervisor restart).

        Honors the constructor's own pin contract: a half that was
        EXPLICITLY supplied at ``__init__`` (``self._base_url_pinned`` /
        ``self._token_pinned``) is never silently overwritten here. If
        BOTH halves are pinned, there is nothing this retry could actually
        change — re-issuing the identical request would just fail
        identically, so this raises a clear error instead of a pointless
        (and potentially misleading, "it retried and still failed") retry.

        Budget arithmetic (nexus-7dsgp, GH #1405 defect 1 — "must not stack
        with existing retry wrappers into unbounded totals"): both
        resolution branches below pass
        ``wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S`` (12s), bounding ONLY
        the added lease-republication wait. This method is called from
        ``_send`` AFTER the first ``_once_with_gateway_retry()`` attempt
        has already failed and BEFORE the second (retry) attempt, which
        itself may re-enter the gateway backoff loop
        (``_GATEWAY_RETRY_SLEEPS`` = 2+5+10 = 17s across up to 3 attempts,
        each bounded by the client's ``timeout`` — 30s default). Worst
        case for one ``_send`` call: first attempt (~30s connect timeout,
        no gateway loop for a connection-class error) + this method's 12s
        lease wait + second attempt (up to 30s connect timeout, or up to
        17s extra if it hits gateway-transient codes instead) — bounded at
        roughly the pre-existing 2x-attempt total plus a fixed 12s, never
        unbounded. This is a PER-CALL bound; the outer T1-CLI aggregate
        across multiple sequential calls is nexus-by875's scope, not this
        method's.
        """
        if self._base_url_pinned and self._token_pinned:
            raise RuntimeError(
                f"{type(self).__name__}: cannot self-heal — both base_url "
                "and token were explicitly pinned at construction (not "
                "resolved via resolve_service_endpoint()); a retryable "
                "failure against a fully pinned endpoint is not "
                "recoverable by re-resolving"
            )
        if self._base_url_pinned:
            # base_url pinned, token not -- mirror __init__'s matching
            # branch: resolve ONLY the token, never demand host/port also
            # be independently resolvable.
            self._token = _resolve_token_only(wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S)
        else:
            base_url, token = resolve_service_endpoint(wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S)
            self._base_url = base_url.rstrip("/")
            if not self._token_pinned:
                self._token = token
        _log.info(
            "refreshable_http_store.reresolved",
            store=type(self).__name__,
            base_url=self._base_url,
        )

    # ── Public transport (subclasses call these, never self._client directly) ──

    def _post(self, path: str, payload: dict[str, Any], *, idempotent: bool = True) -> Any:
        """POST JSON *payload* to *path*; self-heals once on a retryable error.

        ``idempotent=False`` (nexus-tjvgf) disables BOTH retry axes for
        operations where a lost-response retry double-applies server-side
        (queue claims, counter increments, content-appending merges) —
        see :meth:`_send`.
        """
        return self._send("POST", path, json=payload, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        """GET *path*; self-heals once on a retryable error."""
        return self._send("GET", path, params=params, idempotent=idempotent)

    def _delete(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        """DELETE *path*; self-heals once on a retryable error.

        Mirrors ``_get``'s shape (no request body, query-string params) —
        added during ``HttpMemoryStore`` adoption (nexus-bikit.4) when its
        ``delete()`` method was found to still be calling
        ``self._client.delete(...)`` inline because the mixin had no
        ``_delete`` convenience method yet.
        """
        return self._send("DELETE", path, params=params, idempotent=idempotent)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _send(self, method: str, path: str, *, idempotent: bool = True, **kwargs: Any) -> Any:
        """One round-trip, with ONE re-resolve-and-retry on a retryable error.

        ``idempotent=False`` (nexus-tjvgf): the request is issued EXACTLY
        ONCE — no gateway 502/503/504 backoff loop, no endpoint
        re-resolve-and-retry. For a non-idempotent server-side operation a
        lost RESPONSE after a successful apply means a retry double-applies
        (a re-claimed queue row orphaning the first claim; a retry-budget
        counter double-incremented toward premature terminal failure; a
        merge appending the same content twice). The caller sees the raise
        and owns recovery — every production call site of the opted-out
        verbs already sits under ``mcp_infra._service_t2_write_locked``,
        which evicts + rebuilds the store singleton on any raise, so no
        supervisor-restart resilience is lost by opting out.

        Mirrors ``http_vector_client._request``'s FULL two-axis shape
        (nexus-1ytp6 — the original port carried only the first axis):

        - Gateway axis (inner): 502/503/504 get a bounded backoff retry
          (``_GATEWAY_RETRY_CODES`` / ``_GATEWAY_RETRY_SLEEPS``, imported
          from the reference) on BOTH the initial attempt and the
          post-re-resolve attempt, exactly as the reference applies
          ``_once_with_gateway_retry`` on both sides of its invalidate.
        - Endpoint axis (outer): a retryable auth/connection error
          invalidates + re-resolves, then retries EXACTLY ONCE. A second
          failure (of ANY kind) propagates untouched — no retry loops.
        """
        if not idempotent:
            return self._request_once(method, path, **kwargs)
        try:
            return self._once_with_gateway_retry(method, path, **kwargs)
        except (
            httpx.HTTPStatusError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.CloseError,
            httpx.RemoteProtocolError,
            ConnectionRefusedError,
            ConnectionResetError,
        ) as exc:
            if not _is_retryable_endpoint_error(exc):
                raise
            _log.info(
                "refreshable_http_store.retry",
                store=type(self).__name__,
                method=method,
                path=path,
                reason=type(exc).__name__,
            )
            self._invalidate_and_reresolve()
            return self._once_with_gateway_retry(method, path, **kwargs)

    def _once_with_gateway_retry(self, method: str, path: str, **kwargs: Any) -> Any:
        """One logical attempt, riding out gateway-transient 502/503/504s.

        Ported from ``http_vector_client._request``'s inner
        ``_once_with_gateway_retry`` (nexus-1ytp6): T2 and T3 resolve the
        SAME managed endpoint, so a T2 write during a redeploy window sees
        the same brief proxy 502/503 the reference's production incident
        (live 504, 2026-07-04) documented. All other HTTP errors propagate
        immediately — 4xx/500 are not transient.

        Idempotency caveat (nexus-tjvgf — the reference's "every caller is
        idempotent" claim does NOT transfer wholesale to this mixin's
        adopter set): MOST adopted-store operations are natural-id
        upserts/reads/deletes where a lost-response retry is safe, but
        ``HttpAspectQueue.claim_next``/``claim_batch`` (SELECT ... FOR
        UPDATE SKIP LOCKED + mark-in-progress), ``mark_retry`` (a
        server-side counter increment), and ``put_or_merge``'s merge
        branch (content append) are not. RESOLVED (nexus-tjvgf): those
        verbs pass ``idempotent=False`` and never reach this loop — this
        method may assume its caller's operation is retry-safe.
        """
        for i, delay in enumerate((*_GATEWAY_RETRY_SLEEPS, None)):
            try:
                return self._request_once(method, path, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _GATEWAY_RETRY_CODES or delay is None:
                    raise
                _log.warning(
                    "refreshable_http_store.gateway_retry",
                    store=type(self).__name__,
                    method=method,
                    path=path,
                    code=exc.response.status_code,
                    attempt=i + 1,
                    sleep_s=delay,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # loop always returns or raises

    def _request_once(self, method: str, path: str, **kwargs: Any) -> Any:
        """One HTTP round-trip against the CURRENTLY resolved base_url.

        A successful response with an empty body (e.g. ``204 No Content`` —
        several T2 endpoints, such as ``HttpMemoryStore.merge_memories``'s
        ``/v1/memory/merge``, respond this way) returns ``None`` rather than
        calling ``resp.json()`` on empty content, which would raise
        ``json.JSONDecodeError`` (found during ``HttpMemoryStore`` adoption,
        nexus-bikit.4, when ``merge_memories`` was routed through ``_post``).
        """
        url = self._base_url + path
        resp = self._client.request(method, url, headers=self._auth_headers(), **kwargs)
        self._raise_for_status(resp, path)
        if not resp.content:
            return None
        return resp.json()

    def _raise_for_status(self, resp: httpx.Response, op: str) -> None:
        """Raise a descriptive exception on non-2xx responses.

        Shape matches the existing per-store pattern (e.g.
        ``http_memory_store.py``'s ``_raise_for_status``) so callers of the
        stores that adopt this mixin see the same clean error they get
        today.
        """
        if resp.is_success:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
            detail = resp.text
        raise httpx.HTTPStatusError(
            f"{type(self).__name__}.{op} failed: HTTP {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )
