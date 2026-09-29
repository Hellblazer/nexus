# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4h20a: the restore that keeps a raw ``os.environ`` write from outliving
its test. The leak it closes reproduced only in a serial ``pytest tests/daemon``,
which CI's ``-n auto`` never runs, so the mechanism and its autouse wiring are
pinned here directly."""
from __future__ import annotations

import os
import re
from pathlib import Path

from tests._env_restore import restore_env_after

_KEY = "NX_TEST_ENV_RESTORE_PROBE"


def test_a_raw_write_to_an_absent_key_is_removed(monkeypatch) -> None:
    monkeypatch.delenv(_KEY, raising=False)
    try:
        with restore_env_after(_KEY):
            os.environ[_KEY] = "/leaked/fake/bundle/bin"
        assert _KEY not in os.environ
    finally:
        os.environ.pop(_KEY, None)


def test_a_raw_overwrite_is_put_back(monkeypatch) -> None:
    monkeypatch.setenv(_KEY, "/operator/pg/bin")
    with restore_env_after(_KEY):
        os.environ[_KEY] = "/leaked/fake/bundle/bin"
    assert os.environ[_KEY] == "/operator/pg/bin"


def test_a_raw_delete_is_put_back(monkeypatch) -> None:
    monkeypatch.setenv(_KEY, "/operator/pg/bin")
    with restore_env_after(_KEY):
        del os.environ[_KEY]
    assert os.environ[_KEY] == "/operator/pg/bin"


def test_conftest_wires_the_restore_autouse_for_nexus_pg_bin() -> None:
    src = (Path(__file__).parent / "conftest.py").read_text()
    m = re.search(
        r"@pytest\.fixture\(autouse=True\)\s*\ndef _restore_pg_bin_env\(\):(?P<body>.*?)\n\n\n",
        src, re.S,
    )
    assert m, "tests/conftest.py must keep an autouse _restore_pg_bin_env fixture"
    assert 'restore_env_after("NEXUS_PG_BIN")' in m.group("body")
