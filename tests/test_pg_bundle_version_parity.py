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

import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

PINNED_FILES = [
    "ci.yml",
    "engine-service-release.yml",
    "pg-bundle-cache-seed.yml",
]

#: The job in each workflow that actually builds/restores the PG bundle and
#: therefore owns the PG-bundle-specific MACOSX_DEPLOYMENT_TARGET pin.
#: engine-service-release.yml ALSO carries a second, DIFFERENT
#: MACOSX_DEPLOYMENT_TARGET (14.0, in its native-build job, for the ENGINE
#: BINARY) -- extraction must be scoped to this job id, never to the whole
#: file, or that unrelated value masks a divergence in the pg-bundle pin
#: (nexus-cd8b7 review finding: a whole-file regex + "value in set" check
#: passed a mutation that changed the pg-bundle pin from 13.0 to 12.0,
#: because the file-level set still contained the native-build job's "14.0"
#: and unrelated files' "13.0").
PG_BUNDLE_JOBS: dict[str, str] = {
    "engine-service-release.yml": "build-publish-pg-bundle",
    "pg-bundle-cache-seed.yml": "seed",
    "ci.yml": "ca3-pgvector-bundle-macos",
}

CA3_BUNDLE_TEST = Path(__file__).parent / "db" / "test_pg_provision_ca3_bundle.py"


def _pg_bundle_job_macosx_deployment_target(workflow: str) -> str | None:
    """The MACOSX_DEPLOYMENT_TARGET value from THIS workflow's PG-bundle job
    env block specifically -- not a whole-file regex. Returns None if the
    job or the pin is absent."""
    job_id = PG_BUNDLE_JOBS[workflow]
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text())
    job = doc.get("jobs", {}).get(job_id)
    if job is None:
        return None
    env = job.get("env") or {}
    value = env.get("MACOSX_DEPLOYMENT_TARGET")
    return str(value) if value is not None else None


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


def test_macosx_deployment_target_pinned_and_equal_across_pg_bundle_jobs() -> None:
    """All three PG-bundle build/restore jobs (engine-service-release.yml's
    build-publish-pg-bundle, pg-bundle-cache-seed.yml's seed,
    ci.yml's ca3-pgvector-bundle-macos) must pin MACOSX_DEPLOYMENT_TARGET
    explicitly (never left to scripts/build_pg_bundle.sh's own 13.0
    default) and the three pins must be EQUAL -- same shape as the
    PG_VERSION/PGVECTOR_VERSION tests above, but scoped per-job (see
    PG_BUNDLE_JOBS) rather than whole-file, since engine-service-release.yml
    carries a second, unrelated MACOSX_DEPLOYMENT_TARGET (14.0, for the
    engine binary) that a whole-file scan cannot distinguish from the
    PG-bundle pin.
    """
    values = {
        workflow: _pg_bundle_job_macosx_deployment_target(workflow)
        for workflow in PG_BUNDLE_JOBS
    }
    missing = {k: v for k, v in values.items() if not v}
    assert not missing, (
        f"missing MACOSX_DEPLOYMENT_TARGET pin in a PG-bundle job's env: "
        f"{values} (job ids: {PG_BUNDLE_JOBS})"
    )
    flat = set(values.values())
    assert len(flat) == 1, (
        f"MACOSX_DEPLOYMENT_TARGET pins diverge across PG-bundle jobs: "
        f"{values} — bump ALL of them together (see the DECISION comment "
        f"in service/pom.xml's native-libs-mac profile)"
    )


def test_macosx_deployment_target_matches_ca3_bundle_test_floor() -> None:
    """The PG-bundle MACOSX_DEPLOYMENT_TARGET pin (from any of the three
    jobs -- they're already asserted equal above) must match
    MACOS_MIN_FLOOR in tests/db/test_pg_provision_ca3_bundle.py, the
    fourth restatement of this same value (the assertion the CA-3 test
    itself makes at build time)."""
    workflow_pin = _pg_bundle_job_macosx_deployment_target(
        "engine-service-release.yml"
    )
    assert workflow_pin, "engine-service-release.yml's pg-bundle job pin is missing"

    ca3_source = CA3_BUNDLE_TEST.read_text()
    match = re.search(
        r"MACOS_MIN_FLOOR:\s*tuple\[int,\s*int\]\s*=\s*\((\d+),\s*(\d+)\)",
        ca3_source,
    )
    assert match, (
        f"could not find MACOS_MIN_FLOOR in {CA3_BUNDLE_TEST} — did its "
        f"declaration shape change?"
    )
    ca3_floor = f"{match.group(1)}.{match.group(2)}"
    assert workflow_pin == ca3_floor, (
        f"workflow MACOSX_DEPLOYMENT_TARGET={workflow_pin!r} disagrees with "
        f"MACOS_MIN_FLOOR={ca3_floor!r} in {CA3_BUNDLE_TEST}"
    )
