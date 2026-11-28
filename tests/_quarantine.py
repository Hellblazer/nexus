# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply the `quarantine` marker from ``tests/quarantine.toml`` at collection (RDR-258 Phase 5, nexus-2jomg.6).

A test is quarantined by an entry in the registry (``tests/_quarantine_registry.py`` holds the format), never by
editing the test file. At collection each ``pytest`` entry's test gets
``@pytest.mark.quarantine(since=, deadline=, bead=)``; ``java`` entries are ignored here (the nightly runs them
through surefire). The marker changes nothing about whether a test runs in the default suite or in CI: quarantined
tests run there exactly as before, and taking them out of the merge gate is Sam's decision, not this module's.

A registry that breaks the rules (a deadline that is not since + 14 days, a missing field, a duplicate) fails
EVERY run at once, before any test runs, whatever the run selects (``pytest.UsageError`` in ``pytest_configure``,
so an xdist controller stops before it starts a worker). A registry entry that no test has does NOT abort an
ordinary run, scoped or not; the nightly passes the registry's nodeids to pytest, which exits 4 on a nodeid it
cannot find, and ``quarantine_report.py classify`` reads that as red.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _quarantine_registry as registry

_REGISTRY_ENV = "NX_QUARANTINE_REGISTRY"


def registry_path() -> Path:
    override = os.environ.get(_REGISTRY_ENV)
    return Path(override) if override else registry.REGISTRY_PATH


def _load() -> list[registry.Entry]:
    path = registry_path()
    return registry.load(path) if path.exists() else []


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return  # the controller already checked it
    try:
        _load()
    except registry.RegistryError as exc:
        raise pytest.UsageError(f"quarantine registry refused: {exc}") from exc


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    entries = [e for e in _load() if e.kind == "pytest"]
    if not entries:
        return
    by_id = {e.nodeid: e for e in entries}
    for item in items:
        entry = by_id.get(item.nodeid)
        if entry is not None:
            item.add_marker(
                pytest.mark.quarantine(since=entry.since.isoformat(), deadline=entry.deadline.isoformat(), bead=entry.bead)
            )
