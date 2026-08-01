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
import pathlib
import subprocess
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


_UNPINNED_REMEDY = (
    "Remedy: bump REQUIRED_ENGINE_VERSION (src/nexus/engine_version.py) to that "
    "tag. That single edit also moves PINNED_SERVICE_TAG, which is DERIVED from "
    "it. If the tag is not deployed to the managed service yet, get conexus to "
    "deploy it FIRST -- bumping ahead of the deploy makes cloud clients refuse "
    "the managed service as below-identity (GH #1402 inverted)."
)

#: Sentinel for "the tag list could not be read". Distinct from "no tags", which
#: is itself a failure -- a repo with zero engine tags cannot be release-gated.
_TAGS_UNAVAILABLE = object()


def newest_published_engine(repo_root: pathlib.Path | None = None) -> object:
    """Highest published ``engine-service-v*`` tag, as a version tuple.

    Returns :data:`_TAGS_UNAVAILABLE` when git cannot be consulted at all. An
    EMPTY tag list is returned as ``None`` and treated as a gate FAILURE by the
    caller, not a pass: in CI ``actions/checkout`` fetches no tags by default,
    and a check that silently passes because it saw nothing is the exact
    vacuous-green failure mode this gate exists to prevent.
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "tag", "-l", "engine-service-v*"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _TAGS_UNAVAILABLE
    if out.returncode != 0:
        return _TAGS_UNAVAILABLE
    # parse_engine_version takes a VERSION string ("v0.1.56" / "0.1.56"), not the
    # tag form -- strip the namespace prefix first. Getting this wrong makes every
    # tag unparseable, which the empty-list branch below catches as a FAILURE
    # rather than a vacuous pass (it did, on the first run of this code).
    prefix = "engine-service-"
    versions = [
        v for v in (
            parse_engine_version(line.strip()[len(prefix):])
            for line in out.stdout.splitlines()
            if line.strip().startswith(prefix)
        )
        if v is not None
    ]
    return max(versions) if versions else None


def check_pin_currency(newest: object) -> int:
    """Fail when a gated engine tag exists that this release does not pin.

    The OTHER direction of the freshness gate, and the one that had no check at
    all until 2026-07-25. Cloud users get whatever conexus deployed regardless
    of this constant; LOCAL-mode installs get ONLY what REQUIRED_ENGINE_VERSION
    names. So an engine tag that is cut, validated, published -- and never
    pinned -- reaches nobody, while the pre-existing cloud-vs-pin check reports
    "current" and exits 0. That is precisely how the pin sat at v0.1.52 through
    engine tags .53 .54 .55 .56 (found 2026-07-25).

    Hal directive 2026-07-15: ONE engine identity per release, on EVERY install
    path. Not a compatibility minimum, no "only if the release needs the
    features" carve-out -- that carve-out IS the 2026-07-14 v0.1.42 incident.
    """
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    if newest is _TAGS_UNAVAILABLE:
        print(
            "ENGINE PIN CHECK FAILED: could not read engine-service tags from git. "
            "Cannot verify that every gated engine tag is pinned -- treat as a "
            "failed gate, not a pass. In CI, actions/checkout needs `fetch-tags: true`.",
            file=sys.stderr,
        )
        return 2
    if newest is None:
        print(
            "ENGINE PIN CHECK FAILED: zero engine-service-v* tags visible. Either "
            "the checkout has no tags (CI: set `fetch-tags: true`) or the tag "
            "namespace changed. A gate that sees nothing must not report success.",
            file=sys.stderr,
        )
        return 2
    if newest > REQUIRED_ENGINE_VERSION:
        newest_s = ".".join(str(p) for p in newest)
        print(
            f"ENGINE PIN CHECK FAILED: engine-service-v{newest_s} is published but this "
            f"release pins v{floor}. Local-mode installs receive ONLY the pinned "
            f"identity, so every engine fix between v{floor} and v{newest_s} reaches "
            f"nobody.\n{_UNPINNED_REMEDY}",
            file=sys.stderr,
        )
        return 1
    print(f"engine pin is current: REQUIRED_ENGINE_VERSION v{floor} == newest published tag")
    return 0


def check_floor(url: str | None = None, newest: object | None = None) -> int:
    """Probe the live managed service and compare against the version floor.

    Returns an exit code (0 = current, non-zero = stale or unverifiable).
    Never raises: every failure mode of the probe (unreachable, incompatible,
    or any other :class:`~nexus.db.managed_endpoint.ManagedServiceError`) is
    caught here and turned into a clear stderr message plus non-zero exit --
    an unrelated network blip must fail the gate loudly, not crash with an
    unhandled traceback and definitely not report success.
    """
    # Pin-currency FIRST: local, no network, and a failure here is actionable
    # without contacting anything. The cloud probe follows.
    pin_rc = check_pin_currency(
        newest_published_engine() if newest is None else newest
    )
    if pin_rc != 0:
        return pin_rc

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
