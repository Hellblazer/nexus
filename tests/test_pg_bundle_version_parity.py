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
