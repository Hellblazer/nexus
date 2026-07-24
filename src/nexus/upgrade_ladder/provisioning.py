# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Service provisioning + verification family (RDR-155 P4b P0e rehome).

P0e rehome (nexus-g37fr plan v3, partition record T2
``nexus/p4b-sqlite-partition-2026-07-23``): this module is the new
permanent home of the provision → health-gate → version-pin →
discoverability-gate family that used to live in
:mod:`nexus.migration.guided_upgrade`. ``guided_upgrade`` — the
Chroma→PG guided-migration bridge — DELETES WHOLE-FILE at P2 of the
combined 7.0.0 wave; this family is the ladder's standing
service-acquisition machinery (RDR-185 convergence, not migration
plumbing), consumed by the SURVIVING :mod:`nexus.upgrade_ladder.preconditions`
(engine precondition's ``_default_establish``). ``guided_upgrade`` keeps
thin re-export shims delegating here until it dies; dying consumers
(``guided_upgrade_cmd``, ``vector_etl``'s ingest-cloud probe) stay
pointed at ``guided_upgrade`` and die with it.

Moved verbatim (pure move, no behavior change):

* :func:`verify_service_version` / :class:`VersionPinOutcome` — the
  RDR-002 ez5.4 release-version pin (fail-closed).
* :func:`verify_voyage_capability` / :class:`VoyageCapabilityOutcome` —
  the nexus-8o9pm voyage-capability pre-flight.
* :func:`establish_verified_service` / :class:`ServiceReadiness` — the
  ez5.7 Stage 2→3 readiness integrator (+ the nexus-f9y78
  discoverability gate).
* :func:`provision_and_serve` / :class:`ProvisionResult` — the ez5.6
  provision+serve sequence (full ``nx init --service`` reuse).
* :func:`wait_for_service_health` / :class:`HealthGateResult` — the
  ez5.5 bounded health-gate (ez5.1 pinned /health contract).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog

from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class VersionPinOutcome:
    """Result of the engine-service version-pin check (ez5.4 seam).

    ``ok`` is True only when the running service is at or above the required
    release (>= v0.1.5). ``reason`` carries the remedy when not.
    """

    ok: bool
    reason: str | None


def verify_service_version(
    service_url: str,
    *,
    required: tuple[int, int, int] = REQUIRED_ENGINE_VERSION,
    http_get: Callable[[str, float], Any] | None = None,
    timeout_s: float = 5.0,
) -> VersionPinOutcome:
    """RDR-002 ez5.4 version-pin: assert ``release_version >= required``.

    GETs ``{service_url}/version`` and pins on the dedicated ``release_version``
    field (the RDR-002 contract; ``app_version`` is the dev coordinate and is
    NOT used). FAIL-CLOSED on every uncertain outcome: transport error, non-200,
    a missing / null / blank / dev / SNAPSHOT ``release_version`` (an engine
    predating the field is by definition older than the required release), an
    unparseable version, or a version below ``required``. ``http_get`` is an
    injection seam for tests.
    """
    req = ".".join(str(n) for n in required)
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            return httpx.get(url, timeout=timeout)

    url = service_url.rstrip("/") + "/version"
    try:
        resp = _get(url, timeout_s)
    except Exception as exc:  # noqa: BLE001 — any probe failure is fail-closed
        return VersionPinOutcome(
            ok=False, reason=f"could not reach {url} to verify version: {exc}"
        )
    if getattr(resp, "status_code", None) != 200:
        return VersionPinOutcome(
            ok=False,
            reason=f"{url} returned HTTP {getattr(resp, 'status_code', '?')}",
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body cannot confirm a version
        body = {}
    raw = body.get("release_version")
    parsed = parse_engine_version(raw if isinstance(raw, str) else None)
    if parsed is None:
        return VersionPinOutcome(
            ok=False,
            reason=(
                f"engine-service reported no usable release_version "
                f"(got {raw!r}); a dev/unstamped or pre-RDR-002 build is older "
                f"than the required v{req} — refusing to proceed"
            ),
        )
    if parsed < required:
        got = ".".join(str(n) for n in parsed)
        return VersionPinOutcome(
            ok=False,
            # nexus-cfgo9 (ONE-engine model, GH #1402 postmortem): a stale
            # local engine converges automatically — point at that step
            # rather than leaving "upgrade the service" as the only remedy.
            reason=(
                f"engine-service v{got} < required v{req} — run "
                "`nx daemon restart-stale` to converge it automatically "
                "(installs the pinned tag and restarts the service), or "
                "manually: nx daemon service install-binary "
                f"engine-service-v{req} && nx daemon service stop && "
                "nx daemon service start"
            ),
        )
    return VersionPinOutcome(ok=True, reason=None)


# ── nexus-8o9pm: voyage-capability pre-flight ──────────────────────────────


@dataclass(frozen=True)
class VoyageCapabilityOutcome:
    """Whether the target service can serve voyage-model collections."""

    ok: bool
    reason: str | None


def verify_voyage_capability(
    service_url: str,
    *,
    http_get: Callable[[str, float], Any] | None = None,
    timeout_s: float = 5.0,
) -> VoyageCapabilityOutcome:
    """Assert the target service embeds with a voyage model (its actual capability).

    GETs ``{service_url}/version`` and checks ``embedding_models`` for any
    ``voyage-*`` token — the AUTHORITATIVE server-side signal (not the client's
    voyage-key probe, which is wrong in service mode). FAIL-CLOSED on transport
    error, non-200, or a missing/empty/voyage-absent ``embedding_models``: if we
    cannot confirm voyage capability, the voyage collections would block, so the
    caller must surface that early.
    """
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            return httpx.get(url, timeout=timeout)

    url = service_url.rstrip("/") + "/version"
    try:
        resp = _get(url, timeout_s)
    except Exception as exc:  # noqa: BLE001 — any probe failure is fail-closed
        return VoyageCapabilityOutcome(
            ok=False, reason=f"could not reach {url} to check voyage capability: {exc}"
        )
    if getattr(resp, "status_code", None) != 200:
        return VoyageCapabilityOutcome(
            ok=False, reason=f"{url} returned HTTP {getattr(resp, 'status_code', '?')}"
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — fallback path; safe default ({}) returned when response body is not JSON
        body = {}
    models = body.get("embedding_models") or []
    if any(isinstance(m, str) and m.startswith("voyage") for m in models):
        return VoyageCapabilityOutcome(ok=True, reason=None)
    return VoyageCapabilityOutcome(
        ok=False,
        reason=(
            f"target service embeds with {list(models)} and cannot serve voyage "
            "collections (voyage vectors are not re-embeddable into bge without "
            "changing recall)"
        ),
    )


@dataclass(frozen=True)
class ServiceReadiness:
    """Outcome of the Stage 2->3 readiness contract.

    ``service_url`` is the VERIFIED endpoint and is set ONLY when ``ready`` is
    True (health-ready AND version-pinned). On any failure it is ``None`` and
    ``reason`` carries the remedy — the caller (ez5.10) hard-fails and never
    hands a not-ready service to ``migrate-to-service``.
    """

    ready: bool
    service_url: str | None
    reason: str | None
    version_ok: bool
    provision: "ProvisionResult | None"
    health: "HealthGateResult | None"


def _default_discover_gate() -> bool:
    """Confirm a LIVE, discoverable ``storage_service`` lease exists.

    nexus-f9y78: ``/health`` hits the service process directly, so it stays 200
    even when the inline-provisioned supervisor died (e.g. OOM-killed) leaving an
    orphaned-but-serving JVM whose lease aged out (15s TTL). Every env-unpinned
    consumer then resolves the endpoint via lease discovery and races expiry.

    This is a pure DISCOVERABILITY CHECK against the PG-arch canonical resolver
    (``service_endpoint.discover_lease`` — the same path the downstream migration
    legs / T2 / T3 / catalog consumers use): a missing lease means the supervisor
    is gone and downstream discovery would fail, so readiness fails fast. It does
    NOT re-spawn — routing a dead-lease-but-live-JVM case through
    ``ensure_storage_supervisor`` would spawn a SECOND JVM alongside the orphaned
    one (``discover()`` returns ``None`` so the dead-pid guard never fires),
    worsening the OOM that caused the bug.

    On LINUX the orphan scenario is now closed at the source: the JVM is armed
    with PR_SET_PDEATHSIG (storage_service_daemon, nexus-03bcg), so a dead
    supervisor leaves no orphaned-but-serving JVM. This gate remains a correct
    belt-and-suspenders (and covers macOS/non-Linux, where the orphan can still
    linger). Any remaining resolver-layer heal for the macOS-without-autostart
    path is tracked under nexus-03bcg.
    """
    from nexus.db import service_endpoint  # noqa: PLC0415 — deferred import — heavy dep loaded only on this path

    base_url, _token = service_endpoint.discover_lease()
    return base_url is not None


def establish_verified_service(
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    provision: Callable[[], "ProvisionResult"] | None = None,
    health_gate: Callable[..., "HealthGateResult"] | None = None,
    verify_version: Callable[[str], VersionPinOutcome] | None = None,
    discover_gate: Callable[[], bool] | None = None,
) -> ServiceReadiness:
    """Provision -> health-gate -> version-pin -> discoverability-gate; emit a
    verified url iff all pass.

    Order: stand up the service (ez5.6), then BOUNDED health-gate it (ez5.5 —
    a not-ready service short-circuits before the version probe), then version-
    pin it (ez5.4 seam), then confirm a LIVE, DISCOVERABLE lease (nexus-f9y78 —
    ``/health`` alone passes on an orphaned JVM whose supervisor died and whose
    lease aged out). The verified ``service_url`` is emitted ONLY when the
    service is health-ready AND version-pinned AND its lease is discoverable.

    All steps are injection seams for tests; ``verify_version`` defaults to the
    fail-closed placeholder until ez5.4 lands.
    """
    _provision = provision if provision is not None else provision_and_serve
    _health = health_gate if health_gate is not None else wait_for_service_health
    _verify = verify_version if verify_version is not None else verify_service_version
    _discover = discover_gate if discover_gate is not None else _default_discover_gate

    prov = _provision()

    # nexus-bwulw / conexus relay [21082]: a managed (https, non-loopback)
    # target's edge auth-gates /health, so the gate sends the configured
    # bearer there; the loopback provision path stays unauthenticated
    # (ez5.1, the engine contract).
    health_token: str | None = None
    if prov.service_url.startswith("https://"):
        from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid import cycle

        health_token = (get_credential("service_token") or "").strip() or None

    health = _health(
        service_url=prov.service_url, timeout_s=timeout_s, interval_s=interval_s,
        token=health_token,
    )
    if not health.ready:
        reason = (
            f"storage service at {prov.service_url} did not become healthy "
            f"within {timeout_s:.0f}s "
            f"(last status={health.last_status}, error={health.last_error})"
        )
        _log.warning("guided_upgrade_not_ready", stage="health", reason=reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=reason,
            version_ok=False, provision=prov, health=health,
        )

    pin = _verify(prov.service_url)
    if not pin.ok:
        _log.warning("guided_upgrade_not_ready", stage="version", reason=pin.reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=pin.reason,
            version_ok=False, provision=prov, health=health,
        )

    if not _discover():
        # health + version passed, but the lease is not discoverable — the inline
        # supervisor likely died (OOM) leaving an orphaned /health-green service.
        # Emitting the url here would hand downstream legs an endpoint that
        # env-unpinned consumers cannot discover (they race the 15s TTL). Fail
        # closed. (nexus-f9y78)
        reason = (
            f"storage service at {prov.service_url} is health-ready and "
            "version-pinned but its lease is NOT discoverable — the inline "
            "supervisor likely died (e.g. OOM-killed) leaving an orphaned "
            "service; consumers that resolve the endpoint via lease discovery "
            "would race expiry. On a memory-constrained host, set "
            "NX_SERVICE_MAX_HEAP (e.g. 1g) to cap the service heap and reduce "
            "OOM risk, then re-run."
        )
        _log.warning("guided_upgrade_not_ready", stage="discover", reason=reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=reason,
            version_ok=True, provision=prov, health=health,
        )

    _log.info("guided_upgrade_service_verified", service_url=prov.service_url)
    return ServiceReadiness(
        ready=True, service_url=prov.service_url, reason=None,
        version_ok=True, provision=prov, health=health,
    )


# ── ez5.6: provision-and-serve sequence ────────────────────────────────────


@dataclass(frozen=True)
class ProvisionResult:
    """The serving endpoint after the Stage-2 provision+serve sequence.

    ``service_url`` is the UNVERIFIED endpoint (a lease exists, but the service
    has not yet been version-pinned or health-gated). ez5.7 only emits it as a
    VERIFIED url after the pin (ez5.4) and the health-gate (ez5.5) pass.
    """

    service_url: str
    host: str
    port: int
    pid: int | None
    generation: int | None


def _default_serve() -> Any:
    from nexus.commands.init import provision_and_start_service  # noqa: PLC0415 — circular-dep avoidance (nexus.commands.init)

    return provision_and_start_service()


def provision_and_serve(
    *,
    serve: Callable[[], Any] | None = None,
) -> ProvisionResult:
    """Provision + serve the local storage service, returning its endpoint.

    Reuses the FULL ``nx init --service`` sequence (no fork): provision PG, lock
    the embedder + fetch the bge-768 ONNX the service reads, acquire the native
    binary, and start the persistent supervisor — via
    ``init.provision_and_start_service``. (Calling only the PG-provision + start
    steps and skipping the embedder/model fetch is what crashed the service on a
    missing bge ONNX — RDR-002 ez5.13.) The returned ``service_url`` is
    UNVERIFIED — the caller (ez5.7) version-pins + health-gates it first.

    Raises ``RuntimeError`` when the serve step yields no lease — the guided
    upgrade's default provision path is LOCAL-mode only (cloud mode has no local
    service to migrate into; cloud users gate an existing service via
    ``--service-url``). ``serve`` is an injection seam for tests.
    """
    _serve = serve if serve is not None else _default_serve

    lease = _serve()
    if lease is None:
        raise RuntimeError(
            "guided-upgrade provisioning requires a LOCAL service, but the "
            "deployment is in cloud mode (no local service to migrate into) — "
            "point --service-url at the managed service instead"
        )

    endpoint = getattr(lease, "endpoint", None) or {}
    host = endpoint.get("host")
    port = endpoint.get("port")
    # `is None` (not falsiness): port 0 is a valid OS-assigned ephemeral port
    # (code-review M2) — only a genuinely absent host/port is malformed.
    if host is None or port is None or host == "":
        raise RuntimeError(
            "storage service started but its lease endpoint is missing host/port "
            f"(endpoint={endpoint!r}) — cannot derive a service_url"
        )
    result = ProvisionResult(
        service_url=f"http://{host}:{port}",
        host=str(host),
        port=int(port),
        pid=endpoint.get("pid"),
        generation=getattr(lease, "generation", None),
    )
    _log.info(
        "guided_upgrade_provisioned",
        service_url=result.service_url,
        generation=result.generation,
    )
    return result


# ── ez5.5: bounded health-gate ─────────────────────────────────────────────


@dataclass(frozen=True)
class HealthGateResult:
    """Outcome of the bounded wait for engine-service readiness.

    ``ready`` is the gate the handoff (ez5.7) branches on: the command must
    NEVER call ``migrate-to-service`` unless ``ready`` is True. The diagnostic
    fields back the hard-fail remedy message on a not-ready service.
    """

    ready: bool
    attempts: int
    last_status: int | None
    last_error: str | None
    waited_s: float


def _transport_error_types() -> tuple[type[BaseException], ...]:
    """The connection/timeout errors a poll attempt may raise and retry on.

    ``OSError`` (covers ``ConnectionError``) plus httpx's transport errors when
    httpx is importable. Anything outside this set is a real bug and propagates
    loud — the gate never swallows unexpected failures.
    """
    types: list[type[BaseException]] = [OSError]
    try:
        import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

        types.extend([httpx.ConnectError, httpx.TimeoutException])
    except Exception:  # noqa: BLE001 — httpx optional at probe-build time
        pass
    return tuple(types)


def wait_for_service_health(
    *,
    service_url: str,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    token: str | None = None,
    http_get: Callable[[str, float], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> HealthGateResult:
    """Poll ``GET {service_url}/health`` until ready, BOUNDED by ``timeout_s``.

    Ready == HTTP 200 AND ``body["db"] == "up"`` (the ez5.1 pinned /health
    contract). Always makes at least one attempt; never sleeps past the
    deadline; returns ``ready=False`` with the last status/error when the
    service does not come up in time — the caller hard-fails with a remedy
    (ez5.7), it does NOT wait forever.

    ``token`` (nexus-bwulw, conexus relay [21082]): the managed public edge
    auth-gates /health (401 unauthenticated) — ez5.1's UNAUTHENTICATED
    contract is the LOOPBACK/ENGINE contract only. When *token* is set the
    default transport sends ``Authorization: Bearer``; the edge relays the
    engine's ``{status, db}`` body verbatim to authenticated callers
    (IT-pinned conexus-side, conexus-4ap0), so readiness semantics are
    identical on both paths. Loopback callers pass no token and stay
    unauthenticated.

    ``http_get`` / ``sleep`` / ``clock`` are injection seams for deterministic
    tests; production uses ``httpx.get`` / ``time.sleep`` / ``time.monotonic``.
    """
    if timeout_s < 0:
        raise ValueError(f"timeout_s must be non-negative, got {timeout_s}")
    if interval_s <= 0:
        # A non-positive interval never advances an injected clock (and busy-loops
        # a real one), so the bounded-poll guarantee would not hold (code-review M1).
        raise ValueError(f"interval_s must be positive, got {interval_s}")

    import time  # noqa: PLC0415 — stdlib deferred to call site (time)

    _sleep = sleep if sleep is not None else time.sleep
    _clock = clock if clock is not None else time.monotonic
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return httpx.get(url, timeout=timeout, headers=headers)

    url = service_url.rstrip("/") + "/health"
    req_timeout = max(0.1, min(interval_s, 5.0)) if interval_s > 0 else 1.0
    caught = _transport_error_types()

    start = _clock()
    attempts = 0
    last_status: int | None = None
    last_error: str | None = None

    while True:
        try:
            resp = _get(url, req_timeout)
            attempts += 1
            last_status = resp.status_code
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 — a non-JSON body is just "not ready"
                body = {}
            if resp.status_code == 200 and body.get("db") == "up":
                return HealthGateResult(
                    ready=True,
                    attempts=attempts,
                    last_status=last_status,
                    last_error=None,
                    waited_s=_clock() - start,
                )
            detail = body.get("detail")
            last_error = detail or (
                f"status={body.get('status')!r} db={body.get('db')!r}"
            )
        except caught as exc:
            attempts += 1
            last_error = str(exc)

        elapsed = _clock() - start
        if elapsed + interval_s >= timeout_s:
            _log.warning(
                "guided_upgrade_health_gate_timeout",
                url=url,
                attempts=attempts,
                last_status=last_status,
                last_error=last_error,
                waited_s=elapsed,
            )
            return HealthGateResult(
                ready=False,
                attempts=attempts,
                last_status=last_status,
                last_error=last_error,
                waited_s=elapsed,
            )
        _sleep(interval_s)
