# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ao29z: the unstamped-service-jar guard.

Four integration gates errored at SETUP on every developer machine from
2026-07-09 (the fail-loud cloud version probe, 3cb14f96) to 2026-07-31, and the
error blamed the hosted service:

    ManagedServiceIncompatible: The managed nexus service is running an engine
    older than this client requires ... reported no usable release_version

Nothing about the message pointed at the reader's own ``mvn package``. Neither
side was wrong: ``release.properties`` ships ``release_version`` BLANK and is
stamped only at native-release time from an ``engine-service-vX.Y.Z`` tag, so a
locally built jar reports null, and the probe correctly fail-closes on it.

CI's seam job hit this the same day and fixed it by STAMPING the gate jar
(ci.yml, "Stamp release_version into the gate JAR"), deliberately not by
bypassing the probe. ``scripts/build-gate-jar.sh`` is that step for a
developer's machine, and ``unstamped_jar_skip_reason`` turns the confusing probe
error into a pointer at it.

These are hermetic — synthetic zips, no service, no PG — so they run on every
push. The guard they cover exists precisely because the suites that need it do
not.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.db._service_fixture import unstamped_jar_skip_reason

_PROPS = "META-INF/nexus/release.properties"


def _jar(tmp_path: Path, body: str | None, *, name: str = "svc.jar") -> Path:
    """Build a synthetic jar. ``body=None`` omits release.properties entirely."""
    jar = tmp_path / name
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        if body is not None:
            zf.writestr(_PROPS, body)
    return jar


def test_stamped_release_version_is_accepted(tmp_path: Path) -> None:
    assert unstamped_jar_skip_reason(_jar(tmp_path, "release_version=0.1.59\n")) is None


def test_leading_comments_and_other_keys_do_not_confuse_it(tmp_path: Path) -> None:
    """The real file is mostly comment lines explaining the contract, with the
    key last — parsing must not stop at the first non-matching line."""
    body = (
        "# RDR-002 release_version contract.\n"
        "#\n"
        "# release_version is stamped at native-build time from the tag.\n"
        "some_other_key=x\n"
        "release_version=0.1.59\n"
    )
    assert unstamped_jar_skip_reason(_jar(tmp_path, body)) is None


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("release_version=\n", "blank — what `mvn package` actually produces"),
        ("release_version=   \n", "whitespace-only"),
        ("# no key at all\n", "key absent"),
        ("release_version=1.0-SNAPSHOT\n", "SNAPSHOT is a dev coordinate"),
        ("release_version=0.1.59-dev\n", "dev qualifier"),
    ],
)
def test_non_release_values_are_rejected(tmp_path: Path, body: str, why: str) -> None:
    """Mirrors VersionHandler.normalizeReleaseVersion, which maps blank,
    SNAPSHOT and dev all to null. A guard that only checked for empty would pass
    a SNAPSHOT jar straight into the probe's fail-closed path — the exact
    confusing error it exists to prevent."""
    reason = unstamped_jar_skip_reason(_jar(tmp_path, body))
    assert reason is not None, f"must reject: {why}"
    # The remedy is the whole point of the guard; an accurate refusal that does
    # not say what to run leaves the reader where they started.
    assert "scripts/build-gate-jar.sh" in reason
    assert "release_version" in reason


def test_missing_jar_defers_to_the_freshness_gate(tmp_path: Path) -> None:
    """A missing jar is already reported by jar_freshness_skip_reason with its
    own remedy. Reporting it twice, differently, would send the reader to the
    stamping script when they have not built anything at all."""
    assert unstamped_jar_skip_reason(tmp_path / "nope.jar") is None


def test_unreadable_jar_reports_rather_than_raising(tmp_path: Path) -> None:
    """A truncated or corrupt jar must not blow up inside a fixture — the guard
    runs during setup, where a traceback reads as a suite failure."""
    corrupt = tmp_path / "corrupt.jar"
    corrupt.write_bytes(b"not a zip")
    reason = unstamped_jar_skip_reason(corrupt)
    assert reason is not None
    assert "scripts/build-gate-jar.sh" in reason


def test_jar_without_the_properties_entry_is_rejected(tmp_path: Path) -> None:
    reason = unstamped_jar_skip_reason(_jar(tmp_path, None))
    assert reason is not None
    assert "scripts/build-gate-jar.sh" in reason
