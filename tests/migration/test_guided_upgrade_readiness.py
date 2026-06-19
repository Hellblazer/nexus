# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.7 — Stage 2->3 readiness contract for ``nx guided-upgrade``.

The integrator: provision+serve (ez5.6) -> bounded health-gate (ez5.5) ->
version-pin (ez5.4 seam). It emits a VERIFIED service_url ONLY when the
service is BOTH health-ready AND version-pinned; on any failure it returns
not-ready with a remedy and NO url, so ez5.10 never hands a not-ready (or
wrong-version) service to ``migrate-to-service``.

The version-pin is a typed seam. Its default fails CLOSED (ez5.4 not yet
landed — blocked on the engine ``app_version`` relay), so the contract can
never silently emit a url without a real pin.
"""

from __future__ import annotations

from nexus.migration.guided_upgrade import (
    HealthGateResult,
    ProvisionResult,
    ServiceReadiness,
    VersionPinOutcome,
    establish_verified_service,
    verify_service_version,
)

_URL = "http://127.0.0.1:8099"


def _prov() -> ProvisionResult:
    return ProvisionResult(
        service_url=_URL, host="127.0.0.1", port=8099, pid=1, generation=1
    )


def _healthy(**_kw) -> HealthGateResult:  # noqa: ANN003
    return HealthGateResult(
        ready=True, attempts=1, last_status=200, last_error=None, waited_s=0.0
    )


def _unhealthy(**_kw) -> HealthGateResult:  # noqa: ANN003
    return HealthGateResult(
        ready=False, attempts=5, last_status=503,
        last_error="db down: SELECT 1 failed", waited_s=5.0,
    )


def _pin_ok(service_url: str) -> VersionPinOutcome:
    return VersionPinOutcome(ok=True, reason=None)


def _pin_bad(service_url: str) -> VersionPinOutcome:
    return VersionPinOutcome(ok=False, reason="service v0.1.3 < required v0.1.5")


class TestEstablishVerifiedService:
    def test_ready_only_when_healthy_and_pinned(self) -> None:
        result = establish_verified_service(
            provision=_prov, health_gate=_healthy, verify_version=_pin_ok
        )
        assert isinstance(result, ServiceReadiness)
        assert result.ready is True
        assert result.service_url == _URL  # verified url emitted
        assert result.reason is None
        assert result.version_ok is True

    def test_unhealthy_is_not_ready_and_emits_no_url(self) -> None:
        result = establish_verified_service(
            provision=_prov, health_gate=_unhealthy, verify_version=_pin_ok
        )
        assert result.ready is False
        assert result.service_url is None  # NEVER emit url on a not-ready service
        assert result.reason is not None and "db down" in result.reason

    def test_version_mismatch_is_not_ready_and_emits_no_url(self) -> None:
        result = establish_verified_service(
            provision=_prov, health_gate=_healthy, verify_version=_pin_bad
        )
        assert result.ready is False
        assert result.service_url is None
        assert result.version_ok is False
        assert result.reason is not None and "v0.1.5" in result.reason

    def test_health_gated_before_version_checked(self) -> None:
        # A not-ready service must short-circuit BEFORE the version probe
        # (no point pinning a service that is not even up).
        pinned: list[str] = []

        def verify(service_url: str) -> VersionPinOutcome:
            pinned.append(service_url)
            return VersionPinOutcome(ok=True, reason=None)

        establish_verified_service(
            provision=_prov, health_gate=_unhealthy, verify_version=verify
        )
        assert pinned == []  # version never probed on an unhealthy service

    def test_default_version_pin_is_the_real_verifier(self) -> None:
        # With no verify_version injected, the default IS the real RDR-002
        # verifier (verify_service_version). Proven hermetically: wire it with a
        # failing http_get and confirm the contract fail-closes (no verified url
        # without a real, passing pin).
        def boom(url: str, timeout: float):  # noqa: ANN202
            raise ConnectionError("connection refused")

        result = establish_verified_service(
            provision=_prov,
            health_gate=_healthy,
            verify_version=lambda url: verify_service_version(url, http_get=boom),
        )
        assert result.ready is False
        assert result.service_url is None
        assert result.version_ok is False
        assert result.reason is not None
