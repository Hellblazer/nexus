# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m8au7 (review finding): PG_VERSION / PGVECTOR_VERSION are pinned in
FOUR places — ci.yml (two jobs), engine-service-release.yml, and
pg-bundle-cache-seed.yml — and the cache handshake between the release
workflow and the seed workflow depends on the pins (and the whole cache KEY
line) being identical. A version bump applied to one file but not the others
would mean permanent cache misses at best (silent compile-per-tag
regression) or a wrong-version bundle at worst. This is the mechanical
parity gate; scripts/build_pg_bundle.sh holds the defaults but env pins
select the version, so the pins are what must agree.
"""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

PINNED_FILES = [
    "ci.yml",
    "engine-service-release.yml",
    "pg-bundle-cache-seed.yml",
]


def _pins(name: str, var: str) -> set[str]:
    text = (WORKFLOWS / name).read_text()
    return set(re.findall(rf'{var}:\s*"([^"]+)"', text))


def test_pg_version_pins_identical_across_workflows() -> None:
    values = {name: _pins(name, "PG_VERSION") for name in PINNED_FILES}
    assert all(v for v in values.values()), f"missing PG_VERSION pin: {values}"
    flat = set().union(*values.values())
    assert len(flat) == 1, (
        f"PG_VERSION pins diverge across workflows: {values} — bump ALL "
        f"files together (see pg-bundle-cache-seed.yml header)"
    )


def test_pgvector_version_pins_identical_across_workflows() -> None:
    values = {name: _pins(name, "PGVECTOR_VERSION") for name in PINNED_FILES}
    assert all(v for v in values.values()), f"missing PGVECTOR_VERSION pin: {values}"
    flat = set().union(*values.values())
    assert len(flat) == 1, (
        f"PGVECTOR_VERSION pins diverge across workflows: {values} — bump "
        f"ALL files together (see pg-bundle-cache-seed.yml header)"
    )


def test_cache_key_lines_byte_identical() -> None:
    """The seed workflow's save key and the release workflow's restore key
    must be the SAME expression, or every tag silently misses the cache."""
    keys: dict[str, set[str]] = {}
    for name in ("engine-service-release.yml", "pg-bundle-cache-seed.yml"):
        text = (WORKFLOWS / name).read_text()
        keys[name] = {
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("key: pg-bundle-")
        }
    assert keys["engine-service-release.yml"] == keys["pg-bundle-cache-seed.yml"] != set(), (
        f"pg-bundle cache key expressions diverge: {keys}"
    )


def test_cache_key_folds_in_macosx_deployment_target() -> None:
    """nexus-cd8b7 (follow-up to nexus-280ei): the mac-arm64 cache key must
    encode MACOSX_DEPLOYMENT_TARGET, not just PG/pgvector versions and the
    build script hash. Without this, the release job's explicit floor pin
    and the seed workflow's floor pin can drift silently: both currently
    resolve to 13.0, but nothing would catch one changing without the
    other -- the key would still restore a bundle compiled to the WRONG
    floor with no error and no signal. This is a narrower assertion than
    test_cache_key_lines_byte_identical: that test alone would pass even if
    macfloor were dropped from both keys identically, so it wouldn't catch
    a same-day regression to the pre-fix "floor came from the script
    default" shape.
    """
    for name in ("engine-service-release.yml", "pg-bundle-cache-seed.yml"):
        text = (WORKFLOWS / name).read_text()
        key_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("key: pg-bundle-")
        ]
        assert key_lines, f"no pg-bundle cache key line found in {name}"
        for line in key_lines:
            assert "macfloor" in line and "MACOSX_DEPLOYMENT_TARGET" in line, (
                f"{name}'s pg-bundle cache key does not encode "
                f"MACOSX_DEPLOYMENT_TARGET: {line!r}"
            )


def test_macosx_deployment_target_pinned_in_both_pg_bundle_jobs() -> None:
    """Both the release job's build-publish-pg-bundle and the seed
    workflow's seed job must pin MACOSX_DEPLOYMENT_TARGET explicitly (never
    left to scripts/build_pg_bundle.sh's own 13.0 default), and the two
    pins must agree -- same shape as the PG_VERSION/PGVECTOR_VERSION tests
    above.
    """
    values = {
        "engine-service-release.yml": _pins(
            "engine-service-release.yml", "MACOSX_DEPLOYMENT_TARGET"
        ),
        "pg-bundle-cache-seed.yml": _pins(
            "pg-bundle-cache-seed.yml", "MACOSX_DEPLOYMENT_TARGET"
        ),
    }
    assert all(v for v in values.values()), (
        f"missing MACOSX_DEPLOYMENT_TARGET pin in a pg-bundle job: {values}"
    )
    flat = set().union(*values.values())
    assert "13.0" in flat, f"expected the PG bundle's 13.0 floor pin: {values}"
