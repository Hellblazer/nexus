# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply the `quarantine` marker from ``tests/quarantine.toml`` at collection (RDR-258 Phase 5, nexus-2jomg.6).

A test is quarantined by an entry in the registry (``tests/_quarantine_registry.py`` holds the format), never by
editing the test file. At collection each ``pytest`` entry's test gets
``@pytest.mark.quarantine(since=, deadline=, bead=)``; ``java`` entries are ignored here (the nightly runs them
through surefire). The marker changes nothing about whether a test runs in the default suite or in CI: quarantined
tests run there exactly as before, and taking them out of the merge gate is Sam's decision, not this module's.

Two checks, with different reach:

- A registry that breaks the rules (a deadline that is not since + 14 days, a missing field, a duplicate) fails
  EVERY run at once, before any test runs, whatever the run selects (``pytest.UsageError`` in ``pytest_configure``,
  so an xdist controller stops before it starts a worker).
- ``NX_QUARANTINE_STRICT=1`` (the nightly sets it) also requires every ``pytest`` entry to name a collected test,
  so a quarantine of a deleted or renamed test cannot rot: the nightly collects the whole tree. An ordinary run,
  scoped or not, never aborts on that account.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _quarantine_registry as registry

_REGISTRY_ENV = "NX_QUARANTINE_REGISTRY"
_STRICT_ENV = "NX_QUARANTINE_STRICT"


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
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    entries = [e for e in _load() if e.kind == "pytest"]
    if not entries:
        return
    by_id = {e.nodeid: e for e in entries}
    found: set[str] = set()
    for item in items:
        entry = by_id.get(item.nodeid)
        if entry is not None:
            item.add_marker(
                pytest.mark.quarantine(since=entry.since.isoformat(), deadline=entry.deadline.isoformat(), bead=entry.bead)
            )
            found.add(entry.nodeid)
    if os.environ.get(_STRICT_ENV) != "1":
        return
    missing = [e.nodeid for e in entries if e.nodeid not in found]
    if missing:
        message = (
            f"{_STRICT_ENV}=1: the quarantine registry names tests this run did not collect "
            f"(renamed or deleted? fix or drop the entry): {', '.join(missing)}"
        )
        if hasattr(config, "workerinput"):
            # An xdist worker's sys.stderr goes nowhere and the controller only reports "worker crashed", so the
            # reason is written to the inherited fd before the worker exits.
            os.write(2, f"ERROR: {message}\n".encode())
        pytest.exit(message, returncode=4)
