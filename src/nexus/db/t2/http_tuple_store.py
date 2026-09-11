# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpTupleStore — thin HTTP client over the RDR-205 Java tuple-space service.

RDR-205 Phase 2 Step 1 (bead nexus-em75s.9): the Linda tuple space's client
surface, over ``/v1/tuples`` as ``service/src/main/java/dev/nexus/service/
http/TupleHandler.java`` serves it (Phase 1, bead nexus-em75s.4, later
revised by nexus-em75s.35 — the ``claim_id``-on-a-read leak fix this module
is written against).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Ten operations (RDR-205 §Technical Design "Operations", verbatim
signatures — ``in`` is a Python keyword, spelled ``in_`` here; every
other name matches):

    out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> tuple_id
    rd (subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0) -> [TupleRow]
    rdp(subspace, keys_pattern=None, *, n=1, since=None) -> [TupleRow]
    in_(subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (TupleRow, claim_id) | None
    inp(subspace, keys_pattern, *, claimant, lease_s) -> (TupleRow, claim_id) | None
    ack(claim_id, claimant) ; nack(claim_id, claimant)
    registry() -> {digest, sources, templates: [...]}
    subspace_list(prefix=None) -> [SubspaceCensus]
    subspace_stats(subspace) -> SubspaceCensus

Two things no other T2 domain store needs, both new code (RDR-205
§Technical Design "Operations" and §Existing Infrastructure Audit):

- **8 KB pre-send guard** (:func:`_check_request_size`): the edge WAF
  rejects request bodies over 8 KB. Nothing pre-checks a request size
  today (``edge_refusal.py`` is POST-rejection — it renders a refusal
  the edge already sent back, it does not stop one from being sent).
  This measures the SERIALISED request — byte-identical to what httpx
  will actually put on the wire, see the module-level note on
  :func:`_check_request_size` — before any network call, and refuses
  before sending.
- **Typed-error mapping** (:func:`_raise_typed`): the engine renders
  each of its nine RDR-205 typed errors (``TupleException`` and its
  subtypes) as ``{"error": "<code>", "detail": "<message>"}`` at the
  error's own HTTP status. Some codes SHARE a status (``UnknownSubspace``
  and ``ClaimNotFound`` are both 404), so classification reads the
  ``error`` field, never the bare status code alone. This override maps
  that body to the matching :class:`TupleError` subclass; anything the
  engine did not name this way comes through unchanged as the mixin's
  ordinary ``httpx.HTTPStatusError``.

Retry (RDR-205 §Technical Design "Operations": "The client reuses the
retry classification in ``nexus.retry`` rather than a new one"): no
operation here opts out of RefreshableHttpStoreMixin's default
``idempotent=True`` gateway retry (502/503/504). The RDR states
``rd``/``rdp`` are freely retryable, a retried ``out`` lands on the same
tuple by its id formula, and a retried ``in``/``inp`` shares the
identical crash-after-claim ambiguity the lease + sweep already cover —
so, unlike ``HttpAspectQueue``'s queue-claim verbs (nexus-tjvgf), nothing
here needs the non-idempotent opt-out.

HTTP timeout ordering (RDR-205 §Technical Design "Operations": "The
client's own HTTP timeout is set above ``timeout_s`` so the server's cap
fires first"): a blocking ``rd``/``in_`` call (``timeout_s > 0``) passes a
per-call HTTP timeout of ``timeout_s + _PARK_TIMEOUT_MARGIN_S`` — strictly
above the caller's park budget, so the engine's own cap (25 s by default,
CA 3) always returns its probe result before this client's transport
timeout could fire on its own.
"""
from __future__ import annotations

import json
from typing import Any, NoReturn

import httpx
import structlog

from nexus.db.t2.records import SubspaceCensus, TupleRow

# nexus-em75s.9: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get) are inherited wholesale from
# RefreshableHttpStoreMixin — this store needs no __init__ override (it
# carries no store-specific constructor parameter the way HttpAspectQueue's
# rename_lock does).
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

#: RDR-205 §Technical Environment: "The WAF (the web application firewall)
#: rejects request bodies over 8 KB." Measured on the SERIALISED body, not
#: the tuple body field alone — see :func:`_check_request_size`.
_MAX_REQUEST_BODY_BYTES: int = 8 * 1024

#: Per-call HTTP-timeout margin (seconds) added on top of a caller-supplied
#: ``timeout_s`` for a blocking ``rd``/``in_`` call, so the request-level
#: httpx timeout is always strictly ABOVE the engine's own park budget for
#: that call. Not tied to the engine's default 25 s cap (``NX_TUPLE_
#: TIMEOUT_CAP_SECONDS``, engine-side and not known to this client) — this
#: margin must hold for whatever valid ``timeout_s`` the caller passes.
_PARK_TIMEOUT_MARGIN_S: float = 5.0

_ROUTE_PREFIX: str = "/v1/tuples"


# ── Typed errors (mirrors dev.nexus.service.db.TupleException's nine subtypes) ──


class TupleError(RuntimeError):
    """Base of the nine RDR-205 typed tuple-space client errors.

    ``code`` matches the engine's ``TupleException#code()`` verbatim
    (e.g. ``"UnknownSubspace"``); the exception's message is the
    engine's own ``detail`` text, never a client-reconstructed one.
    """

    code: str = ""


class UnknownSubspaceError(TupleError):
    """``subspace`` resolves to no registered template."""

    code = "UnknownSubspace"


class SchemaViolationError(TupleError):
    """A write or claim pattern breaches the template's schema."""

    code = "SchemaViolation"


class TakeDisabledError(TupleError):
    """``in_``/``inp`` against a template whose ``take.enabled`` is false."""

    code = "TakeDisabled"


class TimeoutTooLongError(TupleError):
    """``timeout_s`` above the engine's cap (default 25 s, CA 3)."""

    code = "TimeoutTooLong"


class ClaimNotFoundError(TupleError):
    """``ack``/``nack`` against a ``claim_id`` with no live claim."""

    code = "ClaimNotFound"


class ClaimOwnershipError(TupleError):
    """``ack``/``nack`` against a live claim held by a different claimant."""

    code = "ClaimOwnership"


class ParkCapExceededError(TupleError):
    """A blocking ``rd``/``in_`` could not park — the per-claimant or
    global park cap is already at capacity; back off and retry."""

    code = "ParkCapExceeded"


class TtlTooLongError(TupleError):
    """``out``'s explicit ``ttl_seconds`` exceeds the template's
    ``retention_seconds`` ceiling."""

    code = "TtlTooLong"


class LeaseTooLongError(TupleError):
    """``in_``/``inp``'s explicit ``lease_s`` exceeds the template's
    ``take.max_lease_seconds``."""

    code = "LeaseTooLong"


_ERROR_CLASSES_BY_CODE: dict[str, type[TupleError]] = {
    cls.code: cls
    for cls in (
        UnknownSubspaceError,
        SchemaViolationError,
        TakeDisabledError,
        TimeoutTooLongError,
        ClaimNotFoundError,
        ClaimOwnershipError,
        ParkCapExceededError,
        TtlTooLongError,
        LeaseTooLongError,
    )
}


class RequestTooLargeError(ValueError):
    """The serialised request body exceeds the edge WAF's 8 KB cap
    (RDR-205 §Technical Environment) — refused before sending."""


def _check_request_size(payload: dict[str, Any]) -> None:
    """Refuse *payload* before it is sent when its SERIALISED size is over
    the edge WAF's 8 KB cap.

    Measures with the EXACT same ``json.dumps`` call httpx's own
    ``encode_json`` uses to build the request body it sends
    (``ensure_ascii=False, separators=(",", ":"), allow_nan=False`` —
    confirmed against the pinned httpx version's ``httpx._content.
    encode_json``), so this is byte-identical to what would actually go
    over the wire, not an approximation via ``len(str(payload))`` or a
    default-separator ``json.dumps`` that would overcount whitespace.
    """
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(body) > _MAX_REQUEST_BODY_BYTES:
        raise RequestTooLargeError(
            f"serialised tuple request is {len(body)} bytes, over the "
            f"{_MAX_REQUEST_BODY_BYTES}-byte edge WAF cap (RDR-205 "
            f"§Technical Environment) -- refused before sending"
        )


def _raise_typed(exc: httpx.HTTPStatusError) -> NoReturn:
    """Re-raise *exc* as one of the nine typed :class:`TupleError`
    subclasses when the engine's response body names one; otherwise
    re-raise *exc* unchanged.

    Classification reads the JSON ``error`` field, never the bare HTTP
    status alone — ``UnknownSubspace`` and ``ClaimNotFound`` share 404,
    ``SchemaViolation``/``TimeoutTooLong``/``TtlTooLong``/``LeaseTooLong``
    all share 400, so the status code alone cannot disambiguate.
    """
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 — boundary catch: a non-JSON or already-consumed body falls through to the generic error
        raise exc from None
    code = body.get("error") if isinstance(body, dict) else None
    cls = _ERROR_CLASSES_BY_CODE.get(code)
    if cls is None:
        raise exc from None
    detail = body.get("detail") if isinstance(body, dict) else None
    raise cls(detail or code) from exc


# ── Wire-shape helpers ───────────────────────────────────────────────────────


def _body_to_tuple_row(body: dict[str, Any]) -> TupleRow:
    return TupleRow(
        id=body.get("id", ""),
        subspace=body.get("subspace", ""),
        template=body.get("template", ""),
        keys=body.get("keys") or {},
        dims=body.get("dims") or {},
        body=body.get("body"),
        claim_state=body.get("claim_state"),
        claimant=body.get("claimant"),
        lease_until=body.get("lease_until"),
        attempts=int(body.get("attempts", 0)),
        consumed_at=body.get("consumed_at"),
        consumed_by=body.get("consumed_by"),
        expires_at=body.get("expires_at"),
        created_at=body.get("created_at"),
    )


def _body_to_census(body: dict[str, Any]) -> SubspaceCensus:
    return SubspaceCensus(
        subspace=body.get("subspace", ""),
        total=int(body.get("total", 0)),
        available=int(body.get("available", 0)),
        claimed=int(body.get("claimed", 0)),
        dead=int(body.get("dead", 0)),
        consumed=int(body.get("consumed", 0)),
        expired_unpurged=int(body.get("expired_unpurged", 0)),
        oldest_created_at=body.get("oldest_created_at"),
        newest_created_at=body.get("newest_created_at"),
    )


def _since_payload(since: tuple[str, str] | None) -> dict[str, str] | None:
    """``since`` on the wire is ``{"created_at": ..., "id": ...}``
    (``TupleHandler.readCursor``); the client-facing shape is the plain
    ``(created_at, id)`` cursor pair RDR-205 §Technical Design
    "Operations" describes the caller as keeping."""
    if since is None:
        return None
    created_at, cursor_id = since
    return {"created_at": created_at, "id": cursor_id}


# ── HttpTupleStore ──────────────────────────────────────────────────────────


class HttpTupleStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """RDR-205 Linda tuple-space client over ``/v1/tuples``.

    On the ``HttpAspectQueue`` shape: constructor-injected like every
    other T2 domain store, delegating construction, credential/endpoint
    self-heal, and the HTTP transport itself wholesale to
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`.
    The local ``_post``/``_get`` overrides below stay LOCAL (not a
    straight inherit) for the same reason ``HttpAspectQueue``'s do: every
    method in this class calls ``self._post``/``self._get`` with a SHORT
    path suffix (e.g. ``"/out"``) — the ``/v1/tuples`` prefix is
    store-specific routing, not part of the mixin's shared contract —
    and they additionally apply the 8 KB pre-send guard and the typed-
    error mapping described in the module docstring. Every actual HTTP
    round-trip still goes through the inherited, self-healing
    ``super()._post``/``_get`` (``RefreshableHttpStoreMixin._send``),
    never ``self._client`` directly.
    """

    # ── Internal transport (route prefix + 8 KB guard + typed errors) ───────

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotent: bool = True,
        mutates: bool = True,
        timeout: float | None = None,
    ) -> Any:
        _check_request_size(payload)
        try:
            if timeout is not None:
                return super()._post(
                    f"{_ROUTE_PREFIX}{path}", payload,
                    idempotent=idempotent, mutates=mutates, timeout=timeout,
                )
            return super()._post(
                f"{_ROUTE_PREFIX}{path}", payload, idempotent=idempotent, mutates=mutates,
            )
        except httpx.HTTPStatusError as exc:
            _raise_typed(exc)

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        idempotent: bool = True,
        timeout: float | None = None,
    ) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        try:
            return super()._get(f"{_ROUTE_PREFIX}{path}", q, idempotent=idempotent, timeout=timeout)
        except httpx.HTTPStatusError as exc:
            _raise_typed(exc)

    # ── out ───────────────────────────────────────────────────────────────

    def out(
        self,
        subspace: str,
        keys: dict[str, str],
        dims: dict[str, str] | None = None,
        body: str | None = None,
        *,
        nonce: str | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """Write a tuple. Idempotent by construction (RDR-205 §Technical
        Design "Operations"): the id is derived from the template's
        ``id_from`` fields only, so a retry across the deploy gap lands
        on the SAME tuple. Returns the tuple id, lowercase hex.
        """
        if not subspace:
            raise ValueError("subspace must not be empty")
        payload: dict[str, Any] = {"subspace": subspace, "keys": keys or {}}
        if dims:
            payload["dims"] = dims
        if body is not None:
            payload["body"] = body
        if nonce is not None:
            payload["nonce"] = nonce
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        r = self._post("/out", payload)
        return r["id"]

    # ── rd / rdp ──────────────────────────────────────────────────────────

    def rd(
        self,
        subspace: str,
        keys_pattern: dict[str, str] | None = None,
        *,
        n: int = 1,
        since: tuple[str, str] | None = None,
        timeout_s: int = 0,
    ) -> list[TupleRow]:
        """Non-destructive read. Blocks up to *timeout_s* seconds
        (capped by the engine, CA 3) when nothing matches immediately;
        ``timeout_s=0`` (default) never blocks.
        """
        if not subspace:
            raise ValueError("subspace must not be empty")
        payload: dict[str, Any] = {"subspace": subspace, "n": n}
        if keys_pattern:
            payload["keys_pattern"] = keys_pattern
        since_body = _since_payload(since)
        if since_body is not None:
            payload["since"] = since_body
        if timeout_s:
            payload["timeout_s"] = timeout_s
        req_timeout = timeout_s + _PARK_TIMEOUT_MARGIN_S if timeout_s > 0 else None
        r = self._post("/rd", payload, mutates=False, timeout=req_timeout)
        return [_body_to_tuple_row(t) for t in (r or {}).get("tuples", [])]

    def rdp(
        self,
        subspace: str,
        keys_pattern: dict[str, str] | None = None,
        *,
        n: int = 1,
        since: tuple[str, str] | None = None,
    ) -> list[TupleRow]:
        """Non-destructive, non-blocking probe read."""
        if not subspace:
            raise ValueError("subspace must not be empty")
        payload: dict[str, Any] = {"subspace": subspace, "n": n}
        if keys_pattern:
            payload["keys_pattern"] = keys_pattern
        since_body = _since_payload(since)
        if since_body is not None:
            payload["since"] = since_body
        r = self._post("/rdp", payload, mutates=False)
        return [_body_to_tuple_row(t) for t in (r or {}).get("tuples", [])]

    # ── in_ / inp ─────────────────────────────────────────────────────────

    def in_(
        self,
        subspace: str,
        keys_pattern: dict[str, str],
        *,
        claimant: str,
        lease_s: int,
        timeout_s: int = 0,
    ) -> tuple[TupleRow, str] | None:
        """Destructive (claiming) read. Spelled ``in_`` — ``in`` is a
        Python keyword. Blocks up to *timeout_s* seconds when nothing
        matches immediately; ``timeout_s=0`` (default) never blocks.
        Returns ``(row, claim_id)`` on a claim, ``None`` on a probe miss.
        """
        if not subspace:
            raise ValueError("subspace must not be empty")
        if not claimant:
            raise ValueError("claimant must not be empty")
        payload: dict[str, Any] = {
            "subspace": subspace,
            "keys_pattern": keys_pattern or {},
            "claimant": claimant,
            "lease_s": lease_s,
        }
        if timeout_s:
            payload["timeout_s"] = timeout_s
        req_timeout = timeout_s + _PARK_TIMEOUT_MARGIN_S if timeout_s > 0 else None
        r = self._post("/in", payload, timeout=req_timeout)
        return self._claim_from_response(r)

    def inp(
        self,
        subspace: str,
        keys_pattern: dict[str, str],
        *,
        claimant: str,
        lease_s: int,
    ) -> tuple[TupleRow, str] | None:
        """Destructive (claiming) probe — never blocks."""
        if not subspace:
            raise ValueError("subspace must not be empty")
        if not claimant:
            raise ValueError("claimant must not be empty")
        payload: dict[str, Any] = {
            "subspace": subspace,
            "keys_pattern": keys_pattern or {},
            "claimant": claimant,
            "lease_s": lease_s,
        }
        r = self._post("/inp", payload)
        return self._claim_from_response(r)

    @staticmethod
    def _claim_from_response(r: dict[str, Any] | None) -> tuple[TupleRow, str] | None:
        if not r or r.get("tuple") is None:
            return None
        return (_body_to_tuple_row(r["tuple"]), r.get("claim_id"))

    # ── ack / nack ────────────────────────────────────────────────────────

    def ack(self, claim_id: str, claimant: str) -> None:
        """Consume the claimed row; the row is invisible to ``rd``/``in_``
        after this."""
        if not claim_id:
            raise ValueError("claim_id must not be empty")
        if not claimant:
            raise ValueError("claimant must not be empty")
        self._post("/ack", {"claim_id": claim_id, "claimant": claimant})

    def nack(self, claim_id: str, claimant: str) -> None:
        """Release the claim; counts an attempt toward the template's
        ``max_attempts`` (dead-lettered at the cap)."""
        if not claim_id:
            raise ValueError("claim_id must not be empty")
        if not claimant:
            raise ValueError("claimant must not be empty")
        self._post("/nack", {"claim_id": claim_id, "claimant": claimant})

    # ── registry / census ────────────────────────────────────────────────

    def registry(self) -> dict[str, Any]:
        """The boot-loaded template set: ``{digest, sources, templates: [...]}``."""
        return self._get("/registry")

    def subspace_list(self, prefix: str | None = None) -> list[SubspaceCensus]:
        """Concrete subspaces that exist, optionally filtered by *prefix*."""
        params: dict[str, Any] = {}
        if prefix:
            params["prefix"] = prefix
        r = self._get("/subspace_list", params)
        return [_body_to_census(c) for c in (r or {}).get("subspaces", [])]

    def subspace_stats(self, subspace: str) -> SubspaceCensus:
        """The exact-name form of :meth:`subspace_list` for one subspace."""
        if not subspace:
            raise ValueError("subspace must not be empty")
        r = self._get("/subspace_stats", {"subspace": subspace})
        return _body_to_census(r)
