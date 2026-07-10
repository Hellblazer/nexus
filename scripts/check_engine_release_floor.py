#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Blocking release-gate: is the LIVE deployed cloud engine current? (nexus-i5c2u)

Phase 4 of the engine-version-floor unification (parent bug nexus-b6qlf).
Root cause this closes: the release checklist's "Engine-freshness gate" step
was pure prose -- a human had to manually run
``git log <pinned-engine-tag>..HEAD -- service/`` and judge whether the drift
was "non-trivial AND cloud-relevant". That judgment call was skipped in
practice: the cloud engine sat at ``engine-service-v0.1.17`` for 9+ days
across multiple client releases while develop's
:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` floor moved to
``(0, 1, 34)``. This script replaces the eyeball check with a mechanical one:
probe the live managed service's ``/version`` handshake and compare its
``release_version`` against the floor. Exit non-zero (with the deployed
version, the required floor, and the remedy) when it is stale -- a release
runbook can then treat this as a hard prerequisite instead of an optional
step to skim past.

Reuses :func:`nexus.db.managed_endpoint.resolve_managed_endpoint` and
:func:`nexus.db.managed_endpoint.probe_managed_service` for all HTTP /
endpoint-resolution logic, and :func:`nexus.engine_version.parse_engine_version`
for all version-string parsing -- this module owns none of that, only the
floor comparison and CLI/exit-code wiring. Note that ``probe_managed_service``
itself already fails closed (raises :class:`ManagedServiceIncompatible`) on a
below-floor ``release_version``; the explicit comparison here is a second,
independently-testable layer so this gate does not silently pass if that
internal behavior ever changes, and so a caller sees the SAME "named versions"
message regardless of which layer caught the drift.

Usage::

    uv run python scripts/check_engine_release_floor.py
    uv run python scripts/check_engine_release_floor.py --url https://staging.example.com

Exit codes: ``0`` current, ``1`` stale / incompatible, ``2`` unreachable
(network/DNS/TLS/timeout -- "could not verify" is never treated as "must be
fine").
"""
from __future__ import annotations

import argparse
import sys

from nexus.db.managed_endpoint import (
    ManagedServiceError,
    ManagedServiceUnreachable,
    probe_managed_service,
    resolve_managed_endpoint,
)
from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

_REMEDY = (
    "Remedy: cut + deploy + cloud-gate a fresh engine-service via the "
    "`engine-release` skill (AGENTS.md § Engine-service release), then "
    "re-run this check before cutting the PyPI release."
)


def check_floor(url: str | None = None) -> int:
    """Probe the live managed service and compare against the version floor.

    Returns an exit code (0 = current, non-zero = stale or unverifiable).
    Never raises: every failure mode of the probe (unreachable, incompatible,
    or any other :class:`~nexus.db.managed_endpoint.ManagedServiceError`) is
    caught here and turned into a clear stderr message plus non-zero exit --
    an unrelated network blip must fail the gate loudly, not crash with an
    unhandled traceback and definitely not report success.
    """
    base = url or resolve_managed_endpoint(require_token=False)[0]
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)

    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceUnreachable as exc:
        print(
            f"ENGINE FLOOR CHECK FAILED: managed service at {base} is unreachable "
            f"({exc}). Cannot verify the cloud engine version -- treat this as a "
            "failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    except ManagedServiceError as exc:
        # probe_managed_service already fails closed on a below-floor / missing
        # / unparseable release_version -- its message names the deployed
        # version and the floor already, so surface it verbatim plus the
        # remedy pointer.
        print(
            f"ENGINE FLOOR CHECK FAILED (required v{floor}): {exc}\n{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    parsed = parse_engine_version(caps.release_version)
    if parsed is None or parsed < REQUIRED_ENGINE_VERSION:
        print(
            f"ENGINE FLOOR CHECK FAILED: deployed engine at {caps.base_url} reports "
            f"release_version {caps.release_version!r}, required floor is v{floor}.\n"
            f"{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    print(
        f"cloud engine is current: {caps.base_url} release_version="
        f"{caps.release_version} (floor v{floor})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Managed service base URL override. Defaults to the resolved "
        "managed endpoint (NX_SERVICE_URL / config.yml / "
        "https://api.conexus-nexus.com).",
    )
    args = parser.parse_args(argv)
    return check_floor(url=args.url)


if __name__ == "__main__":
    raise SystemExit(main())
