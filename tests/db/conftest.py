# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pytest hooks for tests/db integration suites.

nexus-todyv: the ``-m integration`` fixtures launch the prebuilt shaded service
jar (``service/target/nexus-service-1.0-SNAPSHOT.jar``) but do NOT rebuild it.
A jar built before a handler/route change yields false 404s (or false passes if
a route was removed). This autouse fixture gates every integration-marked test
on jar freshness in ONE place.

Disposition (critic nexus-todyv review): a missing/stale jar is a SKIP for local
developers (who may simply not have built it) but a FAILURE in CI — skipping in
CI would let the whole integration suite vanish from the run and report green,
which is the exact silent-false-pass the guard exists to prevent.
"""
from __future__ import annotations

import os

import pytest

from tests.db._service_fixture import jar_freshness_skip_reason

_IN_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


@pytest.fixture(scope="session")
def _jar_freshness_reason() -> str | None:
    """Compute the jar-freshness verdict ONCE per session (the jar and sources
    do not change mid-run); avoids an rglob+stat sweep on every test."""
    return jar_freshness_skip_reason()


@pytest.fixture(autouse=True)
def _service_jar_freshness(
    request: pytest.FixtureRequest, _jar_freshness_reason: str | None
) -> None:
    """Gate integration-marked tests on jar freshness.

    Only acts on tests carrying the ``integration`` marker — unit tests in
    tests/db neither build nor launch the jar and must not be gated on it.
    Tests that opt out with ``@pytest.mark.no_service_jar`` are also exempt:
    some integration suites (e.g. the RDR-157 CA-3 pgvector gate) drive
    PostgreSQL directly via psql and never touch the JVM service.
    SKIP locally, FAIL in CI (a missing/stale jar in CI is a build defect).
    """
    if request.node.get_closest_marker("integration") is None:
        return
    if request.node.get_closest_marker("no_service_jar") is not None:
        return
    if _jar_freshness_reason is not None:
        if _IN_CI:
            pytest.fail(_jar_freshness_reason)
        pytest.skip(_jar_freshness_reason)
