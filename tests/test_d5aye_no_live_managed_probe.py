# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-d5aye: the unit suite cannot reach the live managed service.

The autouse ``_blackhole_default_managed_endpoint`` fixture in
``tests/conftest.py`` points ``DEFAULT_MANAGED_SERVICE_URL`` at a loopback
port nothing listens on, so any code path that falls through to the default
(no ``NX_SERVICE_URL``, no ``config.yml`` credential — the isolated state
every unit test runs in) fails fast and loud instead of dialling
``api.conexus-nexus.com`` from inside the suite.
"""
from __future__ import annotations

import time

import pytest

from nexus.db.managed_endpoint import (
    ManagedServiceUnreachable,
    probe_managed_service,
    resolve_managed_endpoint,
)


@pytest.fixture
def _no_pinned_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state a test is in when the engine-substrate pin is OFF
    (``NX_TEST_T2_SUBSTRATE=none``, the worktree agents' narrow runs) or a
    test clears the env: no ``NX_SERVICE_URL``, so the resolver falls
    through to the DEFAULT. Under the pinned suite the pin supplies the
    local test engine's URL first, which is why the incident probe hit the
    engine that was actually reachable — this fixture removes it."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)


def test_the_default_endpoint_is_a_black_hole_under_pytest(_no_pinned_endpoint: None) -> None:
    base, _token = resolve_managed_endpoint(require_token=False)
    assert base == "http://127.0.0.1:9", base


def test_an_unpatched_probe_fails_fast_and_names_the_black_hole(_no_pinned_endpoint: None) -> None:
    """The exact class nexus-d5aye names: a probe nobody patched, with no
    pinned endpoint to catch it. It must raise the unreachable error within
    seconds, naming the loopback address — never a hang, never a real
    cloud answer."""
    started = time.monotonic()
    with pytest.raises(ManagedServiceUnreachable) as excinfo:
        probe_managed_service()
    assert time.monotonic() - started < 5.0
    assert "127.0.0.1:9" in str(excinfo.value)
    assert "api.conexus-nexus.com" not in str(excinfo.value)


@pytest.mark.real_managed_default
def test_the_opt_out_restores_the_real_default_without_dialling_it(_no_pinned_endpoint: None) -> None:
    from nexus.db import managed_endpoint as me

    assert me.DEFAULT_MANAGED_SERVICE_URL == "https://api.conexus-nexus.com"
    base, _token = resolve_managed_endpoint(require_token=False)
    assert base == "https://api.conexus-nexus.com"
