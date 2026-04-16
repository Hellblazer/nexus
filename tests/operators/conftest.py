# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-test reset for the pool module's cross-test caches.

- `_auth_checked_flag` (idempotent auth guard) is module-level and would
  persist across tests without the autouse reset below. Tests that
  monkeypatch ``subprocess.run`` to exercise different auth outcomes
  need a clean slate each run.
- `_operator_pool` singleton in ``mcp_infra`` is cleared so singleton
  tests don't pollute each other.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_pool_caches():
    """Clear the pool module's process-level caches before each test."""
    from nexus.operators import pool as pool_mod
    from nexus import mcp_infra

    pool_mod._reset_auth_cache()
    mcp_infra.reset_operator_pool()
    yield
    pool_mod._reset_auth_cache()
    mcp_infra.reset_operator_pool()
