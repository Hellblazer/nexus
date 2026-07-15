# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Single source of truth for the engine-service version floor (nexus-b6qlf).

Prior to this module the minimum engine-service release a client would accept
was hand-maintained as TWO independent constants that could silently drift
apart: ``guided_upgrade.REQUIRED_RELEASE_VERSION`` (the native/local floor,
actively bumped alongside RDR work) and ``managed_endpoint.MIN_MANAGED_
RELEASE_VERSION`` (the managed-cloud floor, introduced 2026-06-24 at
``(0, 1, 8)`` and never bumped again). Both currently gate the identical
``release_version`` field on the same ``GET /version`` handshake — there was
never a topology reason for two numbers, only an accident of two modules
independently owning "their" constant. This module unifies both into one
pinned floor plus one parser; bumping it once raises the floor everywhere.

**Leaf module contract**: this file MUST NOT import anything from the
``nexus`` package (stdlib only). Both ``nexus.db.managed_endpoint`` and
``nexus.migration.guided_upgrade`` import from here, and those two packages
have no dependency relationship with each other — a leaf module is what lets
both import the shared floor without introducing a ``db`` <-> ``migration``
circular-import risk. Enforced by ``tests/test_engine_version.py::
test_module_is_stdlib_only_leaf`` (AST-walks this file's imports) and by the
release-checklist grep gate over this file for any intra-package import
statement, which must report nothing.
"""
from __future__ import annotations

#: Minimum engine-service release ANY nexus client (native/local or managed
#: cloud) requires, pinned on the dedicated ``release_version`` field of the
#: unauthenticated ``GET /version`` handshake (RDR-002 contract; conexus PR
#: #78). NOT ``app_version`` — that field is the JAR's frozen Maven coordinate
#: ``1.0-SNAPSHOT`` and is a structural no-op to gate on (any build clears it).
#:
#: History: (0,1,5) -> (0,1,8) for nexus-x2g1z (2026-06-24, the managed-cloud
#: probe's introduction). -> (0,1,34) for 6.5.0: the client hard-requires
#: catalog-012 (graph-hop `where` — pre-012 engines silently ignore the key,
#: the H2 version-skew failure class) and catalog-013-1b (pre-1b engines fail
#: boot VALIDATE on tenants with legacy 64-char chash rows — the nexus-1wjmq
#: incident). -> (0,1,39) for nexus-rn3wo.1 (2026-07-12): T1 scratch now
#: defaults to the PG-backed service with no Chroma fallback, and every
#: engine before v0.1.38 has a native-image reflection-registration gap that
#: 500s on every T1 get/search/list (nexus-opr9m) — an engine below this no
#: longer degrades, it silently breaks a hard default. Bump this ONE
#: constant to raise the floor for every client path (native guided-upgrade
#: handoff, the managed-cloud probe, AND — since 2026-07-12 —
#: ``nexus.daemon.binary_install.PINNED_SERVICE_TAG``, the exact tag a fresh
#: local install downloads, which is now DERIVED from this constant rather
#: than independently hand-typed) — there is no second knob to remember.
#:
#: WHEN TO BUMP (refined 2026-07-14, per Hal): not only when the client
#: hard-requires new engine features — ALSO when the engine release carries
#: user-facing FIXES the client release will advertise. For local
#: service-mode installs this floor/pin is the ONLY fix-delivery vehicle:
#: the engine on a local box moves solely via PINNED_SERVICE_TAG (fresh
#: installs) or a floor bump (doctor / upgrade path). A release whose
#: changelog claims an engine-side fix without moving this floor ships a
#: broken promise to every local install and pins fresh installs to the
#: still-broken engine.
#:
#: -> (0,1,41) for the 2026-07-13 release-gate
#: arc: service-mode remediation consent audit hard-requires the consents
#: table (telemetry-002, v0.1.40+); retention markers + range where-operators
#: hard-require v0.1.41; and conexus declared tags <=0.1.40 invalid rollback
#: targets after the A6 view-era grants changeset (engine-rollback-floor-0141).
#: -> (0,1,42) 2026-07-14: catalog-015 FTS filename-token fix (nexus-8gue1,
#: the GH #1397 search blindness) + indexed_at repair provenance
#: (nexus-p5qk8) live in the engine — the fix-delivery rule above, applied.
REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 43)


def parse_engine_version(raw: str | None) -> tuple[int, int, int] | None:
    """Parse ``X.Y.Z`` (optional leading ``v``/``V``) to a tuple, else ``None``.

    Fail-closed by construction: a blank, ``SNAPSHOT``/``dev``-qualified, or
    otherwise unparseable value returns ``None`` so the caller refuses. Trailing
    pre-release/build qualifiers (``-rc1``, ``+meta``) and a non-3-segment
    version (``0.1``, ``1.2.3.4``) are rejected rather than silently accepted
    — a dev/malformed identity is by definition not a comparable release.

    This is the union of the two previously-duplicated parsers
    (``guided_upgrade._parse_semver`` and ``managed_endpoint.
    _parse_release_version``) — read side by side, their bodies were
    byte-for-byte identical, so no behavior merge was needed beyond picking
    one canonical home.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s[:1] in ("v", "V"):
        s = s[1:]
    lower = s.lower()
    if "snapshot" in lower or "dev" in lower:
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return (major, minor, patch)
