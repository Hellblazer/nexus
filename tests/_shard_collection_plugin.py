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
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests._shard_collection import report

REPORT_DIR_ENV = "NX_DDL_REPORT_DIR"


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
    splits = getattr(config.option, "splits", None)
    group = getattr(config.option, "group", None)
    out = os.environ.get(REPORT_DIR_ENV)
    # A pytest started from inside a test (the split-shard probe runs eight with --splits 4) inherits the
    # shard's report dir and worker id; PYTEST_CURRENT_TEST is set only while a test runs, so it marks
    # such a child, which must not write over its parent shard's report.
    if not (splits and group and out) or os.environ.get("PYTEST_CURRENT_TEST"):
        yield
        return
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
