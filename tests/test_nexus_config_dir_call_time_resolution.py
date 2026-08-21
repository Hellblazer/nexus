# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Regression pins for nexus-grg79: the 4 MODULE-LEVEL by-value imports of
``nexus_config_dir`` found by nexus-78blw's AST census
(``src/nexus/aspect_worker.py:72``, ``src/nexus/commands/daemon.py:35``,
``src/nexus/migration/state.py:41``, ``src/nexus/console/routes/health.py:17``)
-- the same defect class as the ``gc_purge_marker.py`` leak (PR #1467): a
module-level ``from nexus.config import nexus_config_dir`` binds the
FUNCTION OBJECT once, at first import.

WHY A BARE ``monkeypatch.setenv`` TEST CANNOT DISTINGUISH THIS DEFECT: a
module-level by-value import binds a reference to the SAME function object
as ``nexus.config.nexus_config_dir``. Calling it through either name still
executes the one function body, which reads ``NEXUS_CONFIG_DIR`` from the
environment fresh on every call (``config.py``). So `import the module, set
NEXUS_CONFIG_DIR, call it` passes both BEFORE and AFTER the fix for all four
modules here (verified empirically while authoring this file, and
independently for ``migration.state`` via a throwaway probe script) --
setenv alone never exercises the actual bug.

The real defect only manifests when something else REPLACES the
``nexus.config.nexus_config_dir`` attribute (``monkeypatch.setattr``) and a
consumer module is first-imported (or reimported) while that replacement is
live: the consumer's module-level name then stays bound to the replacement
FOREVER, even after the patch is torn down, because teardown restores
``nexus.config``'s own attribute but has no way to know a second module
copied it into its own namespace. This is exactly the mechanism the
already-fixed ``gc_purge_marker.py`` regression pin proves at
``tests/test_health_service_checks.py::test_marker_path_resolves_config_dir_at_call_time``.

``_poisoned_then_reloaded`` below is the ONE physical ``monkeypatch.setattr``
call site all four tests share (deliberately factored to a single call site
rather than one per test -- ``tests/test_nexus_config_dir_setattr_lint.py``'s
Sweep 1 ratchet counts call sites, and every genuine reproduction of this
defect class necessarily patches ``nexus.config.nexus_config_dir`` at least
once; sharing the helper keeps that ratchet's exemption growth to +1 instead
of +4). Each test is genuinely RED before the corresponding src/ fix (the
consumer stays pinned to the poisoned path, ignoring ``NEXUS_CONFIG_DIR``)
and GREEN after (module-attribute access via ``_config.nexus_config_dir()``
re-resolves against the CURRENT ``nexus.config`` attribute on every call, so
unpatching always restores correct call-time resolution regardless of
import order).
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator
from unittest.mock import patch

import pytest


@contextmanager
def _poisoned_then_reloaded(module: ModuleType, poisoned: Path) -> Iterator[None]:
    """Replace ``nexus.config.nexus_config_dir`` with a lambda returning
    *poisoned*, reload *module* while that replacement is live (simulating
    a first-import inside another test's patched window), then restore the
    original attribute. If *module* captured the replacement BY VALUE at
    module level, its own namespace stays bound to the lambda after this
    context manager exits -- exactly the ``gc_purge_marker.py`` bug class.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("nexus.config.nexus_config_dir", lambda: poisoned)
        importlib.reload(module)
    yield
    # nexus.config.nexus_config_dir is restored here; module's own binding
    # (if it captured one by value) is NOT -- that is exactly the defect.


def test_aspect_worker_lock_path_resolves_at_call_time_not_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """src/nexus/aspect_worker.py:72 -- ``_worker_lock_path()`` must resolve
    ``nexus_config_dir()`` through the ``nexus.config`` module at call time,
    not a frozen by-value import captured at ``aspect_worker``'s own first
    import."""
    import nexus.aspect_worker as aw

    poisoned = tmp_path / "poisoned"
    try:
        with _poisoned_then_reloaded(aw, poisoned):
            pass
        live = tmp_path / "live"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(live))
        result = aw._worker_lock_path()
        assert str(result).startswith(str(live)), (
            f"expected lock path under {live}, got {result} "
            f"(still pinned to poisoned dir {poisoned}?)"
        )
    finally:
        importlib.reload(aw)


def test_daemon_restart_stale_config_dir_resolves_at_call_time_not_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """src/nexus/commands/daemon.py:35 -- the ``restart-stale`` CLI command's
    ``config_dir = nexus_config_dir()`` (line ~1496) must resolve through the
    ``nexus.config`` module at call time, not a frozen by-value import
    captured at ``commands.daemon``'s own first import."""
    from click.testing import CliRunner

    import nexus.commands.daemon as daemon_mod
    from nexus.upgrade_finish import SkewReport

    poisoned = tmp_path / "poisoned"
    try:
        with _poisoned_then_reloaded(daemon_mod, poisoned):
            pass
        live = tmp_path / "live"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(live))
        with patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=SkewReport(installed_version="9.9.9"),
        ), patch(
            "nexus.upgrade_finish.install_source", return_value="PyPI, unpinned",
        ), patch(
            "nexus.upgrade_finish.converge_engine", return_value=[],
        ) as converge, patch(
            "nexus.upgrade_finish.heal_diag_view", return_value=[],
        ), patch(
            "nexus.upgrade_finish.unload_stale_t2_launchagent", return_value=[],
        ):
            runner = CliRunner()
            result = runner.invoke(daemon_mod.daemon_group, ["restart-stale"])
        assert result.exit_code == 0, result.output
        converge.assert_called_once_with(live, dry_run=False)
    finally:
        importlib.reload(daemon_mod)


def test_migration_state_path_resolves_at_call_time_not_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """src/nexus/migration/state.py:41 -- ``state_path()`` must resolve
    ``nexus_config_dir()`` through the ``nexus.config`` module at call time,
    not a frozen by-value import captured at ``migration.state``'s own
    first import."""
    from nexus.migration import state as m

    poisoned = tmp_path / "poisoned"
    try:
        with _poisoned_then_reloaded(m, poisoned):
            pass
        live = tmp_path / "live"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(live))
        result = m.state_path()
        assert result == live / "migration.state", (
            f"expected {live / 'migration.state'}, got {result} "
            f"(still pinned to poisoned dir {poisoned}?)"
        )
    finally:
        importlib.reload(m)


def test_health_sessions_config_dir_resolves_at_call_time_not_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """src/nexus/console/routes/health.py:17 -- ``_collect_health_data()``'s
    ``scan_sessions_sync(_nexus_config_dir())`` call must resolve through the
    ``nexus.config`` module at call time, not a frozen by-value import
    (aliased ``_nexus_config_dir``) captured at ``health``'s own first
    import.

    Every OTHER field ``_collect_health_data`` populates is best-effort
    (wrapped in its own try/except) except this one -- the aspect-queue
    backend is pointed at the service so that leg does not need a live
    engine either.
    """
    from nexus.console.routes import health as health_mod

    poisoned = tmp_path / "poisoned"
    try:
        with _poisoned_then_reloaded(health_mod, poisoned):
            pass
        live = tmp_path / "live"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(live))
        monkeypatch.setenv("NX_STORAGE_BACKEND_ASPECT_QUEUE", "service")
        with patch(
            "nexus.db.t2.http_aspect_queue.HttpAspectQueue",
        ) as q, patch.object(health_mod, "scan_sessions_sync") as scan:
            q.return_value.pending_count.return_value = 0
            q.return_value.list_failed.return_value = []
            scan.return_value = []
            health_mod._collect_health_data()
        scan.assert_called_once_with(live)
    finally:
        importlib.reload(health_mod)
