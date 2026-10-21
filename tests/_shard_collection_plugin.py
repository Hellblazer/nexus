# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes a pytest-split shard's ``nx-shard-collection-<worker>.json`` (nexus-bo5lv).

Only when ``--splits``/``--group`` are given and ``NX_DDL_REPORT_DIR`` names the shard's
report directory (CI's test matrix); anything else is untouched. The record and the
pytest-gate check are ``tests/_shard_collection.py``.

pytest-split replaces the item list in its own ``trylast`` ``pytest_collection_modifyitems``
and reports the tests it dropped through ``pytest_deselected``. This wrapper sees the kept
list after it and catches that last deselection, which together are the split's input. It
then reruns the split on that input and records whether the rerun kept the same tests
(``verified``); the check prints an unverified report and does not fail on it, since the
cross-shard partition is what it gates.

The same shard also writes ``nx-shard-durations.json``: each test's setup + call + teardown
seconds as this shard ran them (nexus-zkr7q), summed the way pytest-split's
``--store-durations`` sums them. ``python -m tests._shard_collection <dir> --shards 6
--write-durations .test_durations.json`` merges one run's six into the split's input.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests._shard_collection import DURATIONS_NAME, report

REPORT_DIR_ENV = "NX_DDL_REPORT_DIR"
# pytest-split's STORE_DURATIONS_SETUP_AND_TEARDOWN_THRESHOLD: a longer setup or teardown is not counted.
_SETUP_TEARDOWN_LIMIT = 600.0


def _report_dir(config: pytest.Config) -> str | None:
    """The shard's report directory, or None when nothing should be written."""
    out = os.environ.get(REPORT_DIR_ENV)
    # A pytest started from inside a test (the split-shard probe runs eight with --splits 4) inherits the
    # shard's report dir and worker id; PYTEST_CURRENT_TEST is set only while a test runs, so it marks
    # such a child, which must not write over its parent shard's report.
    if not (getattr(config.option, "splits", None) and getattr(config.option, "group", None) and out):
        return None
    return None if os.environ.get("PYTEST_CURRENT_TEST") else out


class _Durations:
    """Sums each test's report durations. Under xdist only the controller registers it (a worker's file
    would be overwritten by the controller's, which ends last, but need not be written at all); every
    worker's reports reach it there."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.seconds: dict[str, float] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.duration < 0 or (report.when != "call" and report.duration > _SETUP_TEARDOWN_LIMIT):
            return
        self.seconds[report.nodeid] = self.seconds.get(report.nodeid, 0.0) + report.duration

    def pytest_sessionfinish(self) -> None:
        try:
            path = Path(self.out)
            path.mkdir(parents=True, exist_ok=True)
            (path / DURATIONS_NAME).write_text(json.dumps(self.seconds, sort_keys=True) + "\n")
        except OSError:
            pass  # no durations: a refresh names the missing group


def pytest_configure(config: pytest.Config) -> None:
    out = _report_dir(config)
    if out and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_Durations(out), "nx-shard-durations")


def _split_again(config: pytest.Config, full: list[pytest.Item], splits: int, group: int) -> list[str] | None:
    try:
        from pytest_split import algorithms

        plugin = config.pluginmanager.get_plugin("pytestsplitplugin")
        durations = getattr(plugin, "cached_durations", None)
        algo = algorithms.Algorithms[config.option.splitting_algorithm].value
        return [i.nodeid for i in algo(splits, full, durations or {})[group - 1].selected]
    except Exception:  # noqa: BLE001 — no answer means unverified, never a crash in collection
        return None


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):  # noqa: ANN201
    out = _report_dir(config)
    if out is None:
        yield
        return
    splits, group = config.option.splits, config.option.group
    batches: list[list[pytest.Item]] = []

    class _Catch:
        def pytest_deselected(self, items: list[pytest.Item]) -> None:
            batches.append(list(items))

    # Collection order as the hooks first see it; chunked splits depend on it.
    order = {id(item): n for n, item in enumerate(items)}
    catcher = _Catch()
    config.pluginmanager.register(catcher, "nx-shard-collection-catch")
    try:
        yield
    finally:
        config.pluginmanager.unregister(catcher)
    kept = list(items)
    full = sorted(kept + (batches[-1] if batches else []), key=lambda item: order.get(id(item), len(order)))
    rec = report((i.nodeid for i in full), (i.nodeid for i in kept), group, splits)
    rec["verified"] = _split_again(config, full, splits, group) == [i.nodeid for i in kept]
    # The worker id from xdist's own config, not PYTEST_XDIST_WORKER: a serial pytest started from an xdist
    # worker inherits that variable (PR #1615 run 39623937382 named a serial child gw1).
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    try:
        path = Path(out)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"nx-shard-collection-{worker}.json").write_text(json.dumps(rec) + "\n")
    except OSError:
        pass  # no report: pytest-gate names the missing group
