# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for CLI command modules.

nexus-8g79.10: ``default_db_path`` was promoted to ``nexus.config``
so non-CLI modules (mcp_infra, health, collection_health,
collection_audit, context, operators/aspect_sql, merge_candidates,
console/routes/health) can resolve the canonical T2 path without
importing up from this CLI helpers module. Re-exported here for
back-compat with CLI command modules that import from
``commands._helpers`` directly.

The re-export is a thin wrapper (not ``from … import``) so test
monkeypatches on ``nexus.config.default_db_path`` reach the live
binding via attribute access at call time. The ``from x import y``
form captures ``y`` at import time and silently bypasses the patch.
"""
from pathlib import Path  # noqa: F401  -- preserved for back-compat callers

from nexus import config as _config

__all__ = ["default_db_path"]


def default_db_path() -> Path:
    """Delegate to :func:`nexus.config.default_db_path` at call time."""
    return _config.default_db_path()
