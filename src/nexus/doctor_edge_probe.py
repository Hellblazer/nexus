# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --probe-edge``: does ``Idempotency-Key`` reach the engine through whatever sits in front of it?

nexus-t0ms7, from the RDR-222 close critique. The client resends a keyed sweep after an unknown outcome,
and that is safe only if the engine saw the key. A hop that drops the header (RDR-007 A5: a CloudFront
origin request policy did, conexus-c4i2) turns the resend into a second UNKEYED execution, and nothing in
a normal run shows it. nexus-yye8l.10 verified the edge once (2026-10-21); this re-probes it on demand.

The probe sends ONE keyed bounded expire-quarantine page with a cutoff of 2000-01-01, so no row
qualifies and nothing is deleted. It then requires, in order:

1. a 200 whose body says ``expired == 0`` (the page was a no-op),
2. the response header ``Idempotency-Key`` echoing the key sent (the engine saw it, and the hops
   between it and us relay it back), and
3. ``GET /v1/requests/<key>`` answering ``state == committed`` (the ledger recorded the request under
   that key, and the status route is reachable through the same hops).

A failure names the hop that most likely lost the key. The probe writes one request-ledger row per run
and is never part of the default ``nx doctor`` sweep: a user's ``nx doctor`` must not send a mutating
request. It is opt-in, one mode per invocation, and its exit code is its verdict, so a scheduler can
run ``nx doctor --probe-edge`` as it would any other smoke step.

It reuses the client's own transport (``_post``/``_get``), so it sends exactly the headers, bearer and
tenant a real sweep sends, and the dev-checkout production-write guard (``guard_production_write``)
still applies: a checkout process refuses to send it at a real store without the reasoned opt-in.
"""
from __future__ import annotations

import http.client
import urllib.error
from dataclasses import dataclass
from typing import Any

#: Nothing qualifies at this cutoff: it predates the quarantine feature by decades.
PROBE_CUTOFF = "2000-01-01T00:00:00Z"
#: The page's row bound. Immaterial (no row qualifies); the engine clamps it.
PROBE_ROW_LIMIT = 300
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
    "a hop between client and engine (edge, CDN or proxy) dropped or rewrote Idempotency-Key"
)
HOP_STATUS_ROUTE = "edge or engine (GET /v1/requests/<key> is not served through this path)"
HOP_LEDGER = "engine ledger (it recorded no committed row under the key)"


@dataclass(frozen=True)
class EdgeProbeResult:
    """The verdict. ``ok`` is true only when all three checks passed. ``hop`` is None on a pass."""

    ok: bool
    detail: str
    hop: str | None = None
    key: str | None = None
    origin_collection: str | None = None
    quarantine_collection: str | None = None


def pick_probe_collections(rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``(origin, quarantine)`` to probe, or None when the tenant has no collection to probe.

    The engine resolves the page's embedding dimension from the ORIGIN, which must therefore be a
    registered collection, so the origin is a live non-quarantine collection. One whose quarantine
    sibling already exists is preferred, so the page reads a real collection; otherwise the first
    origin by name, with the sibling named ``quarantine-<origin>``, the reaper's name for a conformant
    origin (no catalog read: a probe that needs the catalog would fail for the wrong reason). The choice
    is deterministic (sorted by name) so repeated runs probe the same pair.
    """
    from nexus.catalog.chunk_quarantine import QUARANTINE_PREFIX  # noqa: PLC0415 — deferred: keeps CLI startup light

    names = sorted(
        str(r["name"]) for r in rows
        if isinstance(r, dict) and r.get("name") and r.get("lifecycle_state") in (None, "live")
    )
    origins = [n for n in names if not n.startswith(QUARANTINE_PREFIX)]
    if not origins:
        return None
    present = set(names)
    for origin in origins:
        sibling = f"{QUARANTINE_PREFIX}-{origin}"
        if sibling in present:
            return origin, sibling
    return origins[0], f"{QUARANTINE_PREFIX}-{origins[0]}"


def _list_collections() -> list[dict[str, Any]]:
    """The tenant's live collections, names only (the routing listing)."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred: keeps CLI startup light

    return list(make_t3().list_collections(routing=True))


def run_edge_probe() -> EdgeProbeResult:
    """Send the keyed no-op page and check the echo and the ledger. Never raises: every failure is a
    result naming the hop."""
    from nexus.db import http_vector_client as hvc  # noqa: PLC0415 — deferred: keeps CLI startup light
    from nexus.db.service_endpoint import ProductionWriteGuardError  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        pair = pick_probe_collections(_list_collections())
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable tenant is a not-probed verdict, never a traceback
        return EdgeProbeResult(False, f"NOT PROBED: cannot list collections ({_redact(exc)})", HOP_SETUP)
    if pair is None:
        return EdgeProbeResult(
            False,
            "NOT PROBED: the tenant has no live collection to name in the page "
            "(a pass over nothing is not a pass)",
            HOP_SETUP,
        )
    origin, quarantine = pair
    key = hvc.new_request_key()

    def fail(detail: str, hop: str) -> EdgeProbeResult:
        return EdgeProbeResult(False, detail, hop, key, origin, quarantine)

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
        return fail(_post_failure_detail(exc), _post_failure_hop(exc))
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as exc:
        return fail(f"POST /v1/vectors/gc/expire-quarantine got no HTTP answer: {_redact(exc)}", HOP_NETWORK)
    except Exception as exc:  # noqa: BLE001 — boundary: an unresolvable endpoint or a body that is not JSON
        return fail(f"POST /v1/vectors/gc/expire-quarantine failed: {type(exc).__name__}: {_redact(exc)}", HOP_SETUP)
    echoed = hvc._pop_idempotency_response_headers().get("Idempotency-Key")

    if not isinstance(answer, dict) or answer.get("expired") != 0:
        return fail(
            f"the no-op page (cutoff {PROBE_CUTOFF}) did not answer expired=0: {_short(answer)}. "
            "Nothing may qualify at that cutoff; check what answered before trusting any other result",
            HOP_EDGE_OR_ENGINE,
        )
    if echoed != key:
        return fail(
            f"the 200 did not echo Idempotency-Key (sent {key}, got {echoed!r}): the engine ran this page "
            "without the key, so a resend after an unknown outcome would run a second unkeyed execution",
            HOP_KEY_DROPPED,
        )
    try:
        status = hvc._get(f"/v1/requests/{key}", timeout=_GET_TIMEOUT_S)
    except hvc.VectorServiceError as exc:
        hop = HOP_STATUS_ROUTE if exc.code in (404, 405) else (HOP_EDGE if exc.edge_refusal else HOP_EDGE_OR_ENGINE)
        return fail(f"GET /v1/requests/{key} failed: {_redact(exc)}", hop)
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as exc:
        return fail(f"GET /v1/requests/{key} got no HTTP answer: {_redact(exc)}", HOP_NETWORK)
    except Exception as exc:  # noqa: BLE001 — boundary: a status body that is not JSON is a failed read
        return fail(f"GET /v1/requests/{key} failed: {type(exc).__name__}: {_redact(exc)}", HOP_STATUS_ROUTE)
    state = status.get("state") if isinstance(status, dict) else None
    if state != "committed":
        return fail(
            f"GET /v1/requests/{key} answered state={state!r}, not 'committed': {_short(status)}", HOP_LEDGER
        )
    return EdgeProbeResult(
        True,
        f"key {key} echoed and committed (expire-quarantine page on {origin}, nothing expired)",
        None, key, origin, quarantine,
    )


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
    """The doctor row: one line on a pass; a failure adds the hop and the remedy pointer."""
    if result.ok:
        return f"[✓] Edge idempotency probe: {result.detail}"
    lines = [f"[✗] Edge idempotency probe: FAIL: {result.detail}"]
    if result.hop:
        lines.append(f"      hop: {result.hop}")
    lines.append(
        "      see: RDR-222; a dropped header is the RDR-007 A5 shape (conexus-c4i2, the origin request policy)"
    )
    return "\n".join(lines)
