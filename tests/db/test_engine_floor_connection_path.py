# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Connection-path regression net for the engine-version floor (nexus-4m6i0.3).

nexus-b6qlf closed a gap CLASS, not just an instance: the probe FUNCTION
(:func:`nexus.db.managed_endpoint.probe_managed_service`) was always correct,
but nothing asserted it sat on the call GRAPH a real cloud-mode client
actually takes. ``tests/db/test_http_vector_client_version_gate.py`` (the
regression suite for the point-fix, nexus-jn0nm) monkeypatches
``probe_managed_service`` wholesale and calls
:func:`nexus.db.http_vector_client.get_http_vector_client` directly — it can
never detect the factory being un-wired from a CLI command, or the probe
being un-wired from the factory. Same anti-pattern in
``tests/commands/test_migrate_cost_guardrail.py`` /
``tests/test_storage_migrate_vectors_cmd.py`` / ``tests/test_health.py`` /
``tests/test_pipeline_version.py`` — all mock at a level that bypasses the
real graph.

This suite drives the REAL graph instead, via :class:`click.testing.CliRunner`
against the actually-registered ``nx`` commands:

    CLI command -> command body -> get_http_vector_client()
      -> probe_managed_service() [REAL] -> httpx.get [the ONLY patched seam]
      -> parse + compare [REAL] -> ManagedServiceIncompatible
      -> _cloud_probe_failure_message() [REAL] -> click.ClickException

One call site is exercised end-to-end: a plain ``make_t3()`` consumer
(``nx search``, via ``nexus.commands.store._t3``). RDR-155 P4b: the two
migration call sites (``nx migrate-to-service``, ``nx storage migrate
vectors``) died with the migration machinery. A future refactor that
silently un-wires the search path from the gate fails a test here instead
of shipping the original nexus-b6qlf bug again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands.search_cmd import search_cmd
from nexus.db.http_vector_client import reset_http_vector_client_for_tests
from nexus.engine_version import REQUIRED_ENGINE_VERSION

# ── floor-relative version strings (nexus-9qq85 single source of truth) ────
# Derived from REQUIRED_ENGINE_VERSION so a future floor bump does not
# silently invalidate this test (the bead's explicit instruction).


def _floor_str() -> str:
    return ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


def _below_floor_str() -> str:
    major, minor, patch = REQUIRED_ENGINE_VERSION
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.999"
    return f"{max(major - 1, 0)}.999.999"


def _version_response(release_version: str) -> httpx.Response:
    """A REAL ``httpx.Response`` shaped like the managed ``GET /version`` reply."""
    return httpx.Response(
        status_code=200,
        json={"app_version": "1.0-SNAPSHOT", "release_version": release_version},
        request=httpx.Request("GET", "https://cloud.test.invalid/version"),
    )


@pytest.fixture(autouse=True)
def _reset_probe_and_singleton_cache():
    """The gate caches per-process (pass forever / fail-and-reraise) — reset
    around every test in this file or a leaked cached-fail poisons subsequent
    tests, and a leaked cached-pass makes them vacuous."""
    reset_http_vector_client_for_tests()
    yield
    reset_http_vector_client_for_tests()


@pytest.fixture()
def cloud_below_floor(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Pin cloud mode + patch ONLY the httpx boundary to a below-floor reply."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    calls: list[tuple[str, Any]] = []

    def fake_get(url: str, timeout: Any = None, **_: Any) -> httpx.Response:
        calls.append((url, timeout))
        return _version_response(_below_floor_str())

    monkeypatch.setattr("nexus.db.managed_endpoint.httpx.get", fake_get)
    return calls


@pytest.fixture()
def cloud_at_floor(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Pin cloud mode + patch the httpx boundary to an AT-floor reply (accept case)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    calls: list[tuple[str, Any]] = []

    def fake_get(url: str, timeout: Any = None, **_: Any) -> httpx.Response:
        calls.append((url, timeout))
        return _version_response(_floor_str())

    monkeypatch.setattr("nexus.db.managed_endpoint.httpx.get", fake_get)
    return calls


def _assert_loud_cloud_failure(result, http_calls: list) -> None:
    """Shared assertions: exit!=0, cloud-framed message, no raw traceback,
    the probe's httpx boundary actually fired.

    The self-contradiction check (nexus-b6qlf Fix 2) applies to the ENTIRE
    captured stream, not just the click-rendered ``Error:`` line
    (nexus-dizod): ``get_http_vector_client`` also structlog-ERRORs the
    probe failure, and at the CLI's default WARNING level that line prints
    to the user's real stderr directly above the click error — a
    contradictory remedy phrase escaping via EITHER channel recreates the
    original incident's confusion for a live cloud user. Pre-fix, that log
    carried the raw (pre-rewrite) exception text ending "...upgrade/
    downgrade the nx client to match"; it now carries the same
    cloud-correct rewritten message the raised exception does.
    """
    assert result.exit_code != 0
    lines = result.output.splitlines()
    error_lines = [ln for ln in lines if ln.strip().lower().startswith("error:")]
    assert error_lines, f"no click 'Error:' line found in output: {result.output!r}"
    click_message = error_lines[-1]
    click_message_lower = click_message.lower()
    assert "cannot be fixed locally" in click_message_lower
    assert _below_floor_str() in click_message
    assert _floor_str() in click_message
    # The underlying ManagedServiceIncompatible's own remedy clause
    # ("Upgrade the managed service, or upgrade/downgrade the nx client to
    # match.") must not reach the user through ANY channel -- neither the
    # click Error: line nor the structlog diagnostic line above it
    # (nexus-b6qlf Fix 2 + nexus-dizod).
    output_lower = result.output.lower()
    assert "upgrade/downgrade" not in output_lower, (
        "contradictory local-remedy phrasing leaked into user-visible "
        f"output (structlog or click channel): {result.output!r}"
    )
    assert "traceback (most recent call last)" not in output_lower
    assert len(http_calls) >= 1
    assert all(url.endswith("/version") for url, _ in http_calls)


class TestPlainMakeT3ConsumerConnectionPath:
    """``nx search`` -> ``nexus.commands.store._t3()`` -> ``make_t3()`` ->
    ``get_http_vector_client()`` — a plain T3 consumer with no migration
    machinery in front of the gate at all."""

    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_TOKEN", "cli-test-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://cloud.test.invalid")
        # _t3() pre-gates on these three cloud credentials before ever
        # reaching make_t3() — set dummy values so we exercise the
        # engine-floor gate itself, not that unrelated earlier check.
        monkeypatch.setenv("CHROMA_DATABASE", "test-database")
        monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
        monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")

    def test_below_floor_fails_loud(self, monkeypatch, cloud_below_floor) -> None:
        self._env(monkeypatch)

        result = CliRunner().invoke(search_cmd, ["some query"])

        _assert_loud_cloud_failure(result, cloud_below_floor)

    def test_at_floor_gate_passes_version_check(
        self, monkeypatch, cloud_at_floor
    ) -> None:
        self._env(monkeypatch)

        result = CliRunner().invoke(search_cmd, ["some query"])

        # Not asserting full search success (no real collections exist in
        # this isolated test env) — only that the version-gate error
        # specifically is absent, proving the probe let a floor-compatible
        # engine through and the command proceeded past the gate.
        assert "cannot be fixed locally" not in result.output.lower()
        assert len(cloud_at_floor) >= 1
