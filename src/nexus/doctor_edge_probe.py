# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --probe-edge``: does ``Idempotency-Key`` reach the engine through whatever sits in front of it?

nexus-t0ms7, from the RDR-222 close critique. The client resends a keyed sweep after an unknown outcome,
and that is safe only if the engine saw the key. A hop that drops the header (RDR-007 A5: a CloudFront
origin request policy did, conexus-c4i2) turns the resend into a second UNKEYED execution, and nothing in
a normal run shows it. nexus-yye8l.10 verified the edge once (2026-10-21); this re-probes it on demand.

The probe sends ONE keyed bounded expire-quarantine page that cannot do anything: the quarantine
sibling is a dedicated name (:data:`PROBE_QUARANTINE_COLLECTION`) that no reaper or client ever fills, so
it holds no row, no real drain contends for its sweep gate, and no cutoff comparison can reach a real
chunk. The origin is a registered collection of the tenant (the engine reads the embedding dimension from
it). The probe then reads ``GET /v1/requests/<key>`` whatever the response header said, and judges:

* a 200 with ``expired == 0``;
* the ledger answering ``state == committed`` under the key (the REQUEST carried the key to the engine);
* the response header ``Idempotency-Key`` echoing the key (the RESPONSE came back with it).

The two key checks fail differently. Committed with no echo means the key reached the engine and the
response header was stripped on the way back; no ledger row means the engine never saw the key.

It writes one request-ledger row per run (kept 7 days, ``NX_REQUEST_LEDGER_RETENTION_DAYS``) and is never
part of the default ``nx doctor`` sweep: a user's ``nx doctor`` must not send a mutating request. It is
opt-in, one mode per invocation. Exit codes: 0 passed, 1 failed (or could not probe: not a managed edge,
nothing to name, wrong ``--expect-host``), 2 inconclusive (credentials cannot send the write, or the sweep
gate was busy: retry or fix the credential, it says nothing about the edge).

It reuses the client's own transport (``_post``/``_get``), so it sends exactly the headers, bearer and
tenant a real sweep sends, and the dev-checkout production-write guard (``guard_production_write``)
still applies: a checkout process refuses to send it at a real store without the reasoned opt-in.
"""
from __future__ import annotations

import http.client
import ipaddress
import urllib.error
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: A date far in the past: no quarantined_at is at or before it.
PROBE_CUTOFF = "2000-01-01T00:00:00Z"
#: One row at most, so even a surprise could delete nothing in bulk.
PROBE_ROW_LIMIT = 1
#: The sibling the page names. Nothing ever quarantines into it, so it holds no row; the engine answers
#: an absent sibling ``expired 0`` with no error and no audit row.
PROBE_QUARANTINE_COLLECTION = "quarantine-nx-doctor-edge-probe"
_FLOOR_FRACTION = 1.0
_FLOOR_MIN_CHUNKS = 100
_POST_TIMEOUT_S = 60.0
_GET_TIMEOUT_S = 30.0

#: Hop names. They say where the key most likely stopped, not where it provably did.
HOP_SETUP = "setup (client side: nothing to probe or no endpoint)"
HOP_NETWORK = "network (client to edge): no HTTP answer"
HOP_EDGE = "edge (the gateway answered, the engine never saw the request)"
HOP_EDGE_OR_ENGINE = "edge or engine (an HTTP error with no key refusal)"
HOP_ENGINE = "engine (it predates the request ledger or this route's keyed form)"
HOP_KEY_DROPPED = (
    "request path: a hop between client and engine (edge, CDN or proxy) dropped or rewrote Idempotency-Key"
)
HOP_RESPONSE_HEADER = (
    "response path: the key reached the engine; the response header was stripped on the way back"
)
HOP_STATUS_ROUTE = "edge or engine (GET /v1/requests/<key> is not served through this path)"
HOP_LEDGER = "engine ledger (it recorded no committed row under the key)"
#: Not hops: the probe could not reach a verdict.
HOP_CREDENTIALS = "credentials (the token cannot send this write; says nothing about the edge)"
HOP_GATE_BUSY = "engine sweep gate (busy; says nothing about the edge)"

#: Hops whose cause is a header lost in transit: only these point at the RDR-007 A5 origin policy.
_KEY_LOSS_HOPS = (HOP_KEY_DROPPED, HOP_RESPONSE_HEADER)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2


@dataclass(frozen=True)
class EdgeProbeResult:
    """The verdict. ``ok`` is true only when every check passed. ``inconclusive`` marks a probe that
    could not reach a verdict (exit 2); ``hop`` is None on a pass."""

    ok: bool
    detail: str
    hop: str | None = None
    key: str | None = None
    origin_collection: str | None = None
    quarantine_collection: str | None = None
    host: str | None = None
    inconclusive: bool = False

    @property
    def exit_code(self) -> int:
        if self.ok:
            return EXIT_PASS
        return EXIT_INCONCLUSIVE if self.inconclusive else EXIT_FAIL


def pick_probe_origin(rows: list[dict[str, Any]]) -> str | None:
    """A registered, live, non-quarantine collection to name as the page's origin, or None.

    The engine resolves the page's embedding dimension from the origin, so it must be registered. The
    choice is deterministic (first by name) so repeated runs probe the same origin.
    """
    from nexus.catalog.chunk_quarantine import QUARANTINE_PREFIX  # noqa: PLC0415 — deferred: keeps CLI startup light

    names = sorted(
        str(r["name"]) for r in rows
        if isinstance(r, dict) and r.get("name") and r.get("lifecycle_state") in (None, "live")
    )
    origins = [n for n in names if not n.startswith(QUARANTINE_PREFIX)]
    return origins[0] if origins else None


def _list_collections() -> list[dict[str, Any]]:
    """The tenant's live collections, names only (the routing listing)."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred: keeps CLI startup light

    return list(make_t3().list_collections(routing=True))


def _is_local_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def run_edge_probe(*, expect_host: str | None = None) -> EdgeProbeResult:
    """Send the keyed no-op page and check the ledger and the echo. Never raises: every failure is a
    result naming the hop.

    The endpoint host is resolved first and carried on every result. A loopback host (a local engine or
    lease: no edge in the path) is NOT PROBED unless ``expect_host`` names exactly that host; any other
    ``expect_host`` that differs from the resolved host is NOT PROBED too, so a scheduled step can pin
    the edge it means to test.
    """
    from nexus.db import http_vector_client as hvc  # noqa: PLC0415 — deferred: keeps CLI startup light
    from nexus.db.service_endpoint import ProductionWriteGuardError  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        base_url, _ = hvc._resolve_endpoint()
        host = (urlsplit(base_url).hostname or "").lower()
    except Exception as exc:  # noqa: BLE001 — boundary: an unresolvable endpoint is a not-probed verdict, never a traceback
        return EdgeProbeResult(False, f"NOT PROBED: no service endpoint resolves ({_redact(exc)})", HOP_SETUP)
    if not host:
        return EdgeProbeResult(False, f"NOT PROBED: the endpoint {base_url!r} has no host", HOP_SETUP)
    wanted = (expect_host or "").strip().lower() or None
    if wanted is not None and wanted != host:
        return EdgeProbeResult(
            False, f"NOT PROBED: the endpoint resolves to {host}, not the expected {wanted}", HOP_SETUP, host=host
        )
    if wanted is None and _is_local_host(host):
        return EdgeProbeResult(
            False,
            f"NOT PROBED: {host} is not a managed edge (a loopback or local-lease engine has no hop to "
            "lose the key; pass --expect-host to probe it anyway)",
            HOP_SETUP, host=host,
        )

    try:
        origin = pick_probe_origin(_list_collections())
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable tenant is a not-probed verdict, never a traceback
        return EdgeProbeResult(False, f"NOT PROBED: cannot list collections ({_redact(exc)})", HOP_SETUP, host=host)
    if origin is None:
        return EdgeProbeResult(
            False,
            "NOT PROBED: the tenant has no registered collection to name as the page's origin "
            "(a pass over nothing is not a pass)",
            HOP_SETUP, host=host,
        )
    quarantine = PROBE_QUARANTINE_COLLECTION
    key = hvc.new_request_key()

    def fail(detail: str, hop: str) -> EdgeProbeResult:
        return EdgeProbeResult(False, detail, hop, key, origin, quarantine, host)

    def inconclusive(detail: str, hop: str) -> EdgeProbeResult:
        return EdgeProbeResult(False, "NOT PROBED: " + detail, hop, key, origin, quarantine, host, inconclusive=True)

    body = {
        "quarantine_collection": quarantine,
        "origin_collection": origin,
        "cutoff": PROBE_CUTOFF,
        "floor_fraction": _FLOOR_FRACTION,
        "floor_min_chunks": _FLOOR_MIN_CHUNKS,
        "force": False,
        "row_limit": PROBE_ROW_LIMIT,
        "count_refused": False,
    }
    hvc._pop_idempotency_response_headers()  # a stale capture from this thread must not read as an echo
    try:
        with hvc._request_key(key):
            answer = hvc._post("/v1/vectors/gc/expire-quarantine", body, timeout=_POST_TIMEOUT_S)
    except ProductionWriteGuardError as exc:
        return fail(f"REFUSED by the dev-checkout production-write guard: {_redact(exc)}", HOP_SETUP)
    except hvc.VectorServiceError as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403) and not exc.edge_refusal:
            return inconclusive(
                f"the credentials cannot send this write (HTTP {code}): {_redact(exc)}", HOP_CREDENTIALS
            )
        if code == 503 and _engine_error(exc) == "sweep_gate_busy":
            return inconclusive("the engine's sweep gate was busy (503 sweep_gate_busy): retry", HOP_GATE_BUSY)
        return fail(_post_failure_detail(exc), _post_failure_hop(exc))
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as exc:
        return fail(f"POST /v1/vectors/gc/expire-quarantine got no HTTP answer: {_redact(exc)}", HOP_NETWORK)
    except Exception as exc:  # noqa: BLE001 — boundary: an unresolvable endpoint or a body that is not JSON
        return fail(f"POST /v1/vectors/gc/expire-quarantine failed: {type(exc).__name__}: {_redact(exc)}", HOP_SETUP)
    echoed = hvc._pop_idempotency_response_headers().get("Idempotency-Key")
    echo_ok = echoed == key

    if not isinstance(answer, dict) or answer.get("expired") != 0:
        return fail(
            f"the no-op page (cutoff {PROBE_CUTOFF}) did not answer expired=0: {_short(answer)}. "
            "Nothing may qualify at that cutoff; check what answered before trusting any other result",
            HOP_EDGE_OR_ENGINE,
        )

    # The ledger is read whatever the header said: it separates "the engine never saw the key" from
    # "the engine saw it and the response header was lost".
    echo_note = "" if echo_ok else f" (the 200 also did not echo Idempotency-Key: got {echoed!r})"
    try:
        status = hvc._get(f"/v1/requests/{key}", timeout=_GET_TIMEOUT_S)
    except hvc.VectorServiceError as exc:
        if exc.code in (401, 403) and not exc.edge_refusal:
            return inconclusive(
                f"the credentials cannot read the request ledger (HTTP {exc.code}): {_redact(exc)}", HOP_CREDENTIALS
            )
        hop = HOP_STATUS_ROUTE if exc.code in (404, 405) else (HOP_EDGE if exc.edge_refusal else HOP_EDGE_OR_ENGINE)
        return fail(f"GET /v1/requests/{key} failed: {_redact(exc)}{echo_note}", hop)
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as exc:
        return fail(f"GET /v1/requests/{key} got no HTTP answer: {_redact(exc)}{echo_note}", HOP_NETWORK)
    except Exception as exc:  # noqa: BLE001 — boundary: a status body that is not JSON is a failed read
        return fail(f"GET /v1/requests/{key} failed: {type(exc).__name__}: {_redact(exc)}{echo_note}", HOP_STATUS_ROUTE)
    state = status.get("state") if isinstance(status, dict) else None
    committed = state == "committed"

    if committed and echo_ok:
        return EdgeProbeResult(
            True,
            f"key {key} echoed and committed (no-op expire-quarantine page on origin {origin}, nothing expired)",
            None, key, origin, quarantine, host,
        )
    if committed:
        return fail(
            f"the ledger committed the request under key {key}, so the key reached the engine, but the 200 "
            f"did not echo Idempotency-Key (got {echoed!r}): a hop on the response path strips the header",
            HOP_RESPONSE_HEADER,
        )
    if not echo_ok:
        return fail(
            f"the 200 did not echo Idempotency-Key (sent {key}, got {echoed!r}) and the ledger has no "
            f"committed row (state={state!r}): the engine ran this page without the key, so a resend after "
            "an unknown outcome would run a second unkeyed execution",
            HOP_KEY_DROPPED,
        )
    return fail(f"GET /v1/requests/{key} answered state={state!r}, not 'committed': {_short(status)}", HOP_LEDGER)


def _post_failure_hop(exc: Any) -> str:
    code = getattr(exc, "code", None)
    reason = _engine_error(exc)
    if code == 400 and reason in ("idempotency_key_unsupported", "idempotency_key_malformed"):
        return HOP_ENGINE
    if getattr(exc, "edge_refusal", False):
        return HOP_EDGE
    if code is None:
        return HOP_NETWORK
    return HOP_EDGE_OR_ENGINE


def _post_failure_detail(exc: Any) -> str:
    reason = _engine_error(exc)
    base = f"POST /v1/vectors/gc/expire-quarantine failed: {_redact(exc)}"
    if getattr(exc, "code", None) == 400 and reason == "idempotency_key_unsupported":
        return base + " (the engine has no keyed form of this route: not a key lost in transit)"
    return base


def _engine_error(exc: Any) -> str:
    body = getattr(exc, "engine_body", None)
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def _redact(exc: BaseException) -> str:
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred: keeps CLI startup light

    return redact_credentials(str(exc))


def _short(value: Any, limit: int = 240) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def render(result: EdgeProbeResult) -> str:
    """The doctor row. Every row names the endpoint host it probed. A failure adds the hop; only a hop
    that lost a header in transit points at the RDR-007 A5 origin policy."""
    where = f"host {result.host}" if result.host else "host unresolved"
    if result.ok:
        return f"[✓] Edge idempotency probe ({where}): {result.detail}"
    if result.inconclusive:
        lines = [f"[?] Edge idempotency probe ({where}): {result.detail}"]
        lines.append("      inconclusive: exit 2; fix the cause and run again")
    else:
        lines = [f"[✗] Edge idempotency probe ({where}): FAIL: {result.detail}"]
    if result.hop:
        lines.append(f"      hop: {result.hop}")
    if result.hop in _KEY_LOSS_HOPS:
        lines.append(
            "      see: RDR-222; a dropped header is the RDR-007 A5 shape (conexus-c4i2, the origin request policy)"
        )
    return "\n".join(lines)
