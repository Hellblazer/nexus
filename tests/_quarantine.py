# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply the `quarantine` marker from ``tests/quarantine.toml`` at collection (RDR-258 Phase 5, nexus-2jomg.6).

A test is quarantined by an entry in the registry (``tests/_quarantine_registry.py`` holds the format), never by
editing the test file. At collection each listed test gets ``@pytest.mark.quarantine(since=, deadline=, bead=)``.
The marker changes nothing about whether a test runs in the default suite or in CI: quarantined tests run there
exactly as before, and taking them out of the merge gate is Sam's decision, not this module's.

Two loud failures, so a quarantine cannot rot:

- A registry that breaks the rules (a deadline that is not since + 14 days, a missing field, a duplicate) ends the
  run with exit 4 before any test runs.
- An entry whose nodeid no collected test has ends the run the same way. A scoped run (``pytest tests/x.py``) can
  only vouch for entries it could have collected, so it checks those and no others; the check runs before ``-k``
  and ``-m`` deselect, so a listed test that exists is never reported missing because a filter dropped it.

``NX_QUARANTINE_REGISTRY`` points the plugin at another registry file (the plugin's own tests); with
``NX_QUARANTINE_RESULTS`` set to a path, the run writes each listed test's outcome there as
``{"results": {nodeid: passed|failed|error|skipped}}`` for ``scripts/quarantine_report.py --results``. The nightly
job uses it. A test that is listed but did not run (deselected) has no result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import _quarantine_registry as registry

_REGISTRY_ENV = "NX_QUARANTINE_REGISTRY"
_RESULTS_ENV = "NX_QUARANTINE_RESULTS"
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


def registry_path() -> Path:
    override = os.environ.get(_REGISTRY_ENV)
    return Path(override) if override else registry.REGISTRY_PATH


def _load() -> list[registry.Entry]:
    path = registry_path()
    return registry.load(path) if path.exists() else []


def _run_args(config: pytest.Config) -> list[str]:
    """The run's path arguments as rootdir-relative posix paths (``::`` parts kept); an argument outside the rootdir covers nothing."""
    out: list[str] = []
    base = Path(config.invocation_params.dir)
    for arg in config.args:
        path, sep, rest = str(arg).partition("::")
        resolved = (base / path).resolve()
        try:
            rel = resolved.relative_to(config.rootpath.resolve()).as_posix()
        except ValueError:
            continue
        out.append(rel + sep + rest)
    return out


def _stop(config: pytest.Config, message: str) -> None:
    if hasattr(config, "workerinput"):
        # An xdist worker's sys.stderr goes nowhere and the controller only reports "worker crashed", so the
        # reason is written to the inherited fd before the worker exits.
        os.write(2, f"ERROR: {message}\n".encode())
    pytest.exit(message, returncode=4)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = _load()
    except registry.RegistryError as exc:
        _stop(config, f"quarantine registry refused: {exc}")
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
    args = _run_args(config)
    missing = [e.nodeid for e in entries if e.nodeid not in found and registry.run_covers(e.nodeid, args)]
    if missing:
        _stop(
            config,
            "quarantine registry names tests this run did not collect (renamed or deleted? fix or drop the entry): "
            + ", ".join(missing),
        )


def merge_outcome(previous: str | None, when: str, result: str) -> str | None:
    """Fold one phase report into a test's outcome: a setup or teardown failure is an error, a failed call a failure."""
    if when == "call":
        new = {"passed": "passed", "failed": "failed"}.get(result, "skipped")
    elif result == "failed":
        new = "error"
    elif result == "skipped" and when == "setup":
        new = "skipped"
    else:
        return previous
    return new if previous is None or _RANK[new] > _RANK[previous] else previous


class _Recorder:
    """Records the outcome of every registry test that runs. Registered on a serial run or an xdist controller."""

    def __init__(self, path: Path, nodeids: set[str]) -> None:
        self.path = path
        self.nodeids = nodeids
        self.results: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid in self.nodeids:
            merged = merge_outcome(self.results.get(report.nodeid), report.when, report.outcome)
            if merged is not None:
                self.results[report.nodeid] = merged

    def pytest_sessionfinish(self) -> None:
        self.path.write_text(json.dumps({"results": dict(sorted(self.results.items()))}, indent=2) + "\n", encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    target = os.environ.get(_RESULTS_ENV)
    if not target or hasattr(config, "workerinput"):
        return
    try:
        nodeids = {e.nodeid for e in _load()}
    except registry.RegistryError:
        return  # the collection hook reports it
    config.pluginmanager.register(_Recorder(Path(target), nodeids), "nx-quarantine-recorder")
