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
- ``_post`` / ``_get`` both route through the same ``_send`` retry
  wrapper: on a retryable error (401, or a connection-refused/reset
  signature — see :func:`_is_retryable_endpoint_error`), invalidate and
  re-resolve the endpoint, then retry EXACTLY ONCE. A second failure
  propagates normally — no retry loops.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from nexus.db.service_endpoint import resolve_service_endpoint

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Shared client timeout — matches the ~9 in-scope stores' hardcoded 30.0s
#: (only ``http_aspect_queue`` exposes a public ``timeout`` kwarg today; that
#: is NOT part of this mixin's pinned constructor contract, per the locked
#: plan — a subclass may still override ``self._client`` after ``super().__init__``
#: if it needs a different timeout).
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
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    return isinstance(exc, (ConnectionRefusedError, ConnectionResetError))


class RefreshableHttpStoreMixin:
    """Shared self-healing HTTP transport for T2 ``Http*Store`` classes.

    Subclasses call ``self._post(path, payload)`` / ``self._get(path,
    params)`` instead of touching ``self._client`` directly — every inline
    ``self._client.get/post/...`` call site is exactly the read-path 401 gap
    this mixin exists to close (see the locked plan's AUDIT REVISION #1).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
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

        # Pinned contract (tests/db/test_refreshable_client.py): when either
        # half is omitted, resolve BOTH via resolve_service_endpoint() —
        # confirmed stateless (reads NX_SERVICE_HOST/PORT/TOKEN or
        # NX_SERVICE_URL/TOKEN fresh per call; no caching layer of its own).
        # An explicitly-supplied half is never overwritten by the resolved
        # pair.
        if base_url is None or _token is None:
            resolved_url, resolved_token = resolve_service_endpoint()
            base_url = base_url or resolved_url
            _token = _token or resolved_token

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
        self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT_S)

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
        """Re-resolve ``(base_url, token)`` and update the NON-PINNED
        cached field(s) only.

        ``resolve_service_endpoint()`` has no caching of its own, so
        "invalidate" here just means "discard our stale instance field(s)
        and re-call it" — updating ``self._base_url`` is what actually
        fixes the f2qvx connection-refused case (a header-only refresh
        would still be pointed at a dead port after a supervisor restart).

        Honors the constructor's own pin contract: a half that was
        EXPLICITLY supplied at ``__init__`` (``self._base_url_pinned`` /
        ``self._token_pinned``) is never silently overwritten here. If
        BOTH halves are pinned, there is nothing this retry could actually
        change — re-issuing the identical request would just fail
        identically, so this raises a clear error instead of a pointless
        (and potentially misleading, "it retried and still failed") retry.
        """
        if self._base_url_pinned and self._token_pinned:
            raise RuntimeError(
                f"{type(self).__name__}: cannot self-heal — both base_url "
                "and token were explicitly pinned at construction (not "
                "resolved via resolve_service_endpoint()); a retryable "
                "failure against a fully pinned endpoint is not "
                "recoverable by re-resolving"
            )
        base_url, token = resolve_service_endpoint()
        if not self._base_url_pinned:
            self._base_url = base_url.rstrip("/")
        if not self._token_pinned:
            self._token = token
        _log.info(
            "refreshable_http_store.reresolved",
            store=type(self).__name__,
            base_url=self._base_url,
        )

    # ── Public transport (subclasses call these, never self._client directly) ──

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON *payload* to *path*; self-heals once on a retryable error."""
        return self._send("POST", path, json=payload)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET *path*; self-heals once on a retryable error."""
        return self._send("GET", path, params=params)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        """One round-trip, with ONE re-resolve-and-retry on a retryable error.

        Mirrors ``http_vector_client._request``'s shape: a second failure
        (of ANY kind, including a repeat 401/connection error) propagates
        untouched — no retry loops.
        """
        try:
            return self._request_once(method, path, **kwargs)
        except (
            httpx.HTTPStatusError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
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
            return self._request_once(method, path, **kwargs)

    def _request_once(self, method: str, path: str, **kwargs: Any) -> Any:
        """One HTTP round-trip against the CURRENTLY resolved base_url."""
        url = self._base_url + path
        resp = self._client.request(method, url, headers=self._auth_headers(), **kwargs)
        self._raise_for_status(resp, path)
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
