# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx guided-upgrade`` Stage-2 logic — provision + version-pin + health-gate
the engine-service, then hand off to the existing ``nx migrate-to-service``.

RDR-002. conexus owns the design; this module is the engine-side host. The
detect / migrate / validate / unlock / rollback machinery already exists
(:mod:`nexus.migration.detection`, :func:`nexus.migration.driver.run_guided_upgrade`,
``nx migrate-to-service``) and is REUSED, never rebuilt. This module adds only
the new pre-flight + provisioning + readiness-contract pieces.

ez5.2 (this commit): :func:`detect_pending_migration` — the pre-flight a
command runs BEFORE provisioning a service, so a fresh user short-circuits to
a no-op instead of standing up a service for an empty footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.migration.detection import (
    DetectionReport,
    classify_collections,
    close_read_client,
    open_read_legs,
    voyage_key_available,
)

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PreflightDetection:
    """The verdict of the pre-provision detection step.

    ``needs_migration`` is the single gate the command branches on: True iff
    at least one data-bearing legacy Chroma collection exists. A fresh user
    (no legs, or only empty collections) yields ``False`` and the command must
    no-op WITHOUT provisioning a service.
    """

    report: DetectionReport
    needs_migration: bool

    @property
    def data_bearing_count(self) -> int:
        """Number of non-empty collections across all detected legs."""
        return sum(1 for c in self.report.classifications if c.has_data)

    @property
    def classified_unsupported_count(self) -> int:
        """Number of collections classified ``unsupported`` by detection.

        This is the RAW classification count — it INCLUDES legacy minilm-384
        collections that RDR-162 auto-remaps (re-embeds into a bge-768 target)
        rather than blocks. It is therefore NOT the count of genuinely-blocked
        collections; a consumer needing the blocked set must filter
        ``report.unsupported`` by :func:`cross_model_remappable`. Kept as a
        coarse informational signal only.
        """
        return len(self.report.unsupported)

    @property
    def total_count(self) -> int:
        """Total classified collections (data-bearing or not)."""
        return len(self.report.classifications)


def detect_pending_migration(
    *,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    open_legs: Callable[[str | Path | None], tuple[Any, Any]] | None = None,
    close_leg: Callable[[Any], None] | None = None,
) -> PreflightDetection:
    """Detect whether a pre-RDR-160 Chroma footprint exists to migrate.

    Opens the local + cloud read legs, classifies the footprint via the
    existing :func:`classify_collections`, then CLOSES the legs before
    returning — the WAL local leg is a single-opener and the downstream ETL
    must be the sole opener (same invariant the driver enforces).

    ``open_legs`` / ``close_leg`` are injection seams for tests; production
    uses :func:`open_read_legs` and :func:`_close_quietly`. ``voyage_key_present``
    defaults to the deployment-mode probe.
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )
    _open = open_legs if open_legs is not None else open_read_legs
    _close = close_leg if close_leg is not None else close_read_client

    local, cloud = _open(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
        )
    finally:
        # Close only the legs that were actually opened — an absent leg is
        # never dispatched to ``_close`` (so injected close hooks need not
        # tolerate ``None``).
        for client in (local, cloud):
            if client is not None:
                _close(client)

    needs = len(report.legs_with_data) > 0
    _log.info(
        "guided_upgrade_preflight",
        needs_migration=needs,
        total=len(report.classifications),
        data_bearing=sum(1 for c in report.classifications if c.has_data),
        unsupported=len(report.unsupported),
    )
    return PreflightDetection(report=report, needs_migration=needs)


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
        import httpx  # noqa: PLC0415

        types.extend([httpx.ConnectError, httpx.TimeoutException])
    except Exception:  # noqa: BLE001 — httpx optional at probe-build time
        pass
    return tuple(types)


def wait_for_service_health(
    *,
    service_url: str,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
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

    ``http_get`` / ``sleep`` / ``clock`` are injection seams for deterministic
    tests; production uses ``httpx.get`` / ``time.sleep`` / ``time.monotonic``.
    """
    if timeout_s < 0:
        raise ValueError(f"timeout_s must be non-negative, got {timeout_s}")

    import time  # noqa: PLC0415

    _sleep = sleep if sleep is not None else time.sleep
    _clock = clock if clock is not None else time.monotonic
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415

            return httpx.get(url, timeout=timeout)

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
