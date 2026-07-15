# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Single source of truth for the engine-service dependency version (nexus-b6qlf).

ONE-ENGINE MODEL (nexus-cfgo9, 2026-07-15, after the 14h GH #1402 delivery
failure): a conexus release has ONE engine dependency — the exact version it
was built and tested against. This is NOT a "minimum" or "floor" that older
engines might still limp along under; it is a required dependency, installed
like any other, on EVERY box, via EVERY install path (fresh ``nx init`` AND
convergence on an existing box's upgrade — see
:mod:`nexus.upgrade_finish`'s ``converge_engine``). A version mismatch on a
local install is a convergence step (install the dependency, restart the
service), never a user-facing refusal. The ONE place a ``>=`` "floor"
comparison legitimately survives is the cloud/managed handshake
(:mod:`nexus.db.managed_endpoint`): the client cannot install the managed
service's engine, and the managed deployment legitimately runs ahead of any
given client release between PyPI cuts, so "deployed >= tested-with" is the
right check there — nowhere else.

Prior to nexus-b6qlf this module's value was hand-maintained as TWO
independent constants that could silently drift apart:
``guided_upgrade.REQUIRED_RELEASE_VERSION`` (the native/local floor,
actively bumped alongside RDR work) and ``managed_endpoint.MIN_MANAGED_
RELEASE_VERSION`` (the managed-cloud floor, introduced 2026-06-24 at
``(0, 1, 8)`` and never bumped again). Both currently gate the identical
``release_version`` field on the same ``GET /version`` handshake — there was
never a topology reason for two numbers, only an accident of two modules
independently owning "their" constant. This module unifies both into one
pinned dependency version plus one parser; bumping it once moves the
requirement everywhere — and, since nexus-cfgo9, converges every existing
local install to match rather than merely raising a refusal threshold.

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

#: THE engine-service release this conexus build was made and tested
#: against — the one engine dependency, pinned on the dedicated
#: ``release_version`` field of the unauthenticated ``GET /version``
#: handshake (RDR-002 contract; conexus PR #78). NOT ``app_version`` — that
#: field is the JAR's frozen Maven coordinate ``1.0-SNAPSHOT`` and is a
#: structural no-op to gate on (any build clears it).
#:
#: This is a DEPENDENCY VERSION, not a compatibility minimum: every local
#: (native/service-mode) install is expected to converge to exactly this
#: version, via fresh ``nx init`` (which installs it directly) or via
#: convergence on an existing box (:func:`nexus.upgrade_finish.converge_engine`,
#: run automatically post-upgrade and as the ``nx doctor`` backstop). Only
#: the managed-cloud handshake (:mod:`nexus.db.managed_endpoint`) still reads
#: this as a floor (``deployed >= this``), because a client cannot install
#: the cloud's engine and the cloud legitimately runs ahead of any one
#: client release.
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
#: constant to move the dependency for every client path (native
#: guided-upgrade handoff, the managed-cloud probe, the automated
#: convergence pass, AND — since 2026-07-12 —
#: ``nexus.daemon.binary_install.PINNED_SERVICE_TAG``, the exact tag a fresh
#: local install downloads, which is now DERIVED from this constant rather
#: than independently hand-typed) — there is no second knob to remember.
#:
#: WHEN TO BUMP (refined 2026-07-14, per Hal): not only when the client
#: hard-requires new engine features — ALSO when the engine release carries
#: user-facing FIXES the client release will advertise. For local
#: service-mode installs this dependency/pin is the ONLY fix-delivery
#: vehicle: the engine on a local box moves via PINNED_SERVICE_TAG (fresh
#: installs) or convergence (existing installs — see nexus-cfgo9). A release
#: whose changelog claims an engine-side fix without moving this dependency
#: ships a broken promise to every local install and pins fresh installs to
#: the still-broken engine.
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
