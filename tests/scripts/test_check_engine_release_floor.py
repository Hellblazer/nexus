# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``scripts/check_engine_release_floor.py`` (nexus-i5c2u, Phase 4).

Root cause this closes: AGENTS.md's release-checklist "Engine-freshness gate"
step was pure prose -- a human had to manually run
``git log <pinned-engine-tag>..HEAD -- service/`` and eyeball whether the drift
was "non-trivial AND cloud-relevant". That eyeball check was skipped in
practice: the cloud engine sat at v0.1.17 for 9+ days across multiple client
releases while develop's ``REQUIRED_ENGINE_VERSION`` floor moved to v0.1.34.
This script makes the check mechanical and blocking: probe the live managed
service, compare against the floor, exit non-zero (with a remedy) if stale.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml``, so ``check_engine_release_floor`` imports directly with no
``sys.path`` hack.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import check_engine_release_floor as gate
from nexus.db.managed_endpoint import ManagedCapabilities, ManagedServiceUnreachable
from nexus.engine_version import REQUIRED_ENGINE_VERSION

_TEST_URL = "https://example.test"


def _caps(release_version: str) -> ManagedCapabilities:
    return ManagedCapabilities(
        base_url=_TEST_URL,
        app_version="1.0-SNAPSHOT",
        release_version=release_version,
        embedding_mode="voyage",
        embedding_models=[],
        schema_latest_id=None,
        schema_changeset_count=None,
    )


def _floor_str() -> str:
    return ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


def test_engine_at_or_above_floor_passes(capsys: pytest.CaptureFixture[str]) -> None:
    above = (REQUIRED_ENGINE_VERSION[0], REQUIRED_ENGINE_VERSION[1], REQUIRED_ENGINE_VERSION[2] + 1)
    with patch.object(gate, "probe_managed_service", return_value=_caps(".".join(str(p) for p in above))):
        rc = gate.check_floor(url=_TEST_URL)
    assert rc == 0
    out = capsys.readouterr().out
    assert "current" in out.lower()


def test_engine_exactly_at_floor_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())):
        rc = gate.check_floor(url=_TEST_URL)
    assert rc == 0


def test_stale_engine_fails_and_names_both_versions(capsys: pytest.CaptureFixture[str]) -> None:
    stale = "0.1.1"
    assert (0, 1, 1) < REQUIRED_ENGINE_VERSION
    with patch.object(gate, "probe_managed_service", return_value=_caps(stale)):
        rc = gate.check_floor(url=_TEST_URL)
    # Exact code, not just non-zero: a regression that swapped the
    # documented stale(1)/unreachable(2) exit codes must be caught here.
    assert rc == 1
    err = capsys.readouterr().err
    assert stale in err
    assert _floor_str() in err
    assert "engine-release" in err  # points at the remedy skill


def test_unreachable_service_fails_loud_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(
        gate,
        "probe_managed_service",
        side_effect=ManagedServiceUnreachable("connect timed out"),
    ):
        rc = gate.check_floor(url=_TEST_URL)
    # Exact code: unreachable must be distinguishable from stale/incompatible.
    assert rc == 2
    err = capsys.readouterr().err
    assert "unreachable" in err.lower()
    assert "connect timed out" in err


def test_main_returns_nonzero_on_stale_engine(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")):
        rc = gate.main(["--url", _TEST_URL])
    assert rc == 1


def test_help_exits_cleanly_without_network_call() -> None:
    with patch.object(gate, "probe_managed_service") as mock_probe:
        with pytest.raises(SystemExit) as exc_info:
            gate.main(["--help"])
    assert exc_info.value.code == 0
    mock_probe.assert_not_called()
