# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4h20a: the fixtures that keep a raw ``os.environ`` write from
outliving its test (``tests/_env_restore.py``).

The leaks they close are order-dependent: a writer and a later reader in the
same process. The ``pytester`` tests here run the real plugin in a nested
session with two tests in a fixed order, so they prove the fixtures' behaviour
regardless of how the outer run is split or ordered.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests._env_restore import (
    ENGINE_DB_ENV_KEYS,
    PRODUCT_WRITTEN_ENV_KEYS,
    restore_changed_keys,
    restore_env_after,
)

_KEY = "NX_TEST_ENV_RESTORE_PROBE"


def test_restore_env_after_undoes_write_overwrite_and_delete(monkeypatch) -> None:
    a, b, c = (_KEY + s for s in ("_A", "_B", "_C"))
    monkeypatch.delenv(a, raising=False)
    monkeypatch.setenv(b, "orig-b")
    monkeypatch.setenv(c, "orig-c")
    with restore_env_after(a, b, c):
        os.environ[a] = "/leaked/fake/bundle/bin"
        os.environ[b] = "overwritten"
        del os.environ[c]
    assert a not in os.environ
    assert os.environ[b] == "orig-b"
    assert os.environ[c] == "orig-c"


def test_restore_env_after_restores_when_the_body_raises(monkeypatch) -> None:
    monkeypatch.delenv(_KEY, raising=False)
    with pytest.raises(RuntimeError), restore_env_after(_KEY):
        os.environ[_KEY] = "leaked"
        raise RuntimeError("test body failed")
    assert _KEY not in os.environ


def test_restore_changed_keys_reports_and_reverts_each_kind_of_change(monkeypatch) -> None:
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
        assert restore_changed_keys(before) == {a: "raw-write", b: "overwritten", c: None}
        assert a not in os.environ
        assert os.environ[b] == "orig-b" and os.environ[c] == "orig-c" and os.environ[d] == "same"
        assert restore_changed_keys(before) == {}, "nothing left to restore"
    finally:
        os.environ.pop(a, None)


def _nested(pytester: pytest.Pytester, body: str) -> pytest.RunResult:
    """Run ``body`` in a nested session that loads only the env plugin."""
    pytester.makeconftest('pytest_plugins = ["tests._env_restore"]\n')
    pytester.makepyfile(body)
    return pytester.runpytest_inprocess("-p", "no:randomly", "-p", "no:cacheprovider", "-q")


def test_a_product_written_key_does_not_reach_the_next_test(pytester, monkeypatch) -> None:
    for k in PRODUCT_WRITTEN_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    result = _nested(pytester, f'''
import os
KEYS = {PRODUCT_WRITTEN_ENV_KEYS!r}

def test_1_writer_leaks_the_way_delenv_raising_false_lets_it(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)   # records no undo: the key is absent
        os.environ[k] = "leaked-" + k          # the product's raw write

def test_2_reader_sees_the_original_state():
    for k in KEYS:
        assert k not in os.environ, k
''')
    result.assert_outcomes(passed=2)


def test_the_engine_db_guard_fails_the_leaking_test_and_restores(pytester, monkeypatch) -> None:
    for k in ENGINE_DB_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    result = _nested(pytester, '''
import os

def test_1_leaks_an_engine_db_key():
    os.environ["NX_DB_ADMIN_URL"] = "jdbc:postgresql://127.0.0.1:15999/dead"

def test_2_next_test_is_clean():
    assert "NX_DB_ADMIN_URL" not in os.environ
''')
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*test left engine DB env changed*NX_DB_ADMIN_URL*"])


def test_conftest_registers_the_env_plugin() -> None:
    src = (Path(__file__).parent / "conftest.py").read_text()
    m = re.search(r"^pytest_plugins = \[(?P<plugins>[^\]]*)\]", src, re.M)
    assert m and '"tests._env_restore"' in m.group("plugins")
