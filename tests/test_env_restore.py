# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4h20a: the restore that keeps a raw ``os.environ`` write from outliving
its test. The leak it closes reproduced only in a serial ``pytest tests/daemon``,
which CI's ``-n auto`` never runs, so the mechanism and its autouse wiring are
pinned here directly."""
from __future__ import annotations

import os
import re
from pathlib import Path

from tests._env_restore import restore_changed_keys, restore_env_after

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


def test_restore_changed_keys_reports_and_reverts_each_kind_of_change(monkeypatch) -> None:
    # The engine-DB-env guard's compare-and-restore (_no_leaked_engine_db_env).
    a, b, c, d = (_KEY + s for s in ("_A", "_B", "_C", "_D"))
    monkeypatch.delenv(a, raising=False)
    monkeypatch.setenv(b, "orig-b")
    monkeypatch.setenv(c, "orig-c")
    monkeypatch.setenv(d, "same")
    before = {k: os.environ.get(k) for k in (a, b, c, d)}
    try:
        os.environ[a] = "raw-write"
        os.environ[b] = "overwritten"
        del os.environ[c]
        leaked = restore_changed_keys(before)
        assert leaked == {a: "raw-write", b: "overwritten", c: None}
        assert a not in os.environ
        assert os.environ[b] == "orig-b" and os.environ[c] == "orig-c" and os.environ[d] == "same"
        assert restore_changed_keys(before) == {}, "nothing left to restore"
    finally:
        os.environ.pop(a, None)


def test_the_engine_db_env_guard_uses_the_tested_restore() -> None:
    src = (Path(__file__).parent / "conftest.py").read_text()
    m = re.search(r"def _no_leaked_engine_db_env\(\):(?P<body>.*?)\n\n\n", src, re.S)
    assert m, "tests/conftest.py must keep _no_leaked_engine_db_env"
    assert "restore_changed_keys(before)" in m.group("body")
    assert "pytest.fail(" in m.group("body")
