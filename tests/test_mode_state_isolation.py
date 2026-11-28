# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 3 (nexus-2jomg.2): cloud-mode state does not carry from one test to the next.

The mode-declarations census (``test_mode_declarations_are_explicit``) guards one thing: a test that names a cloud
embedder and declares no mode must not pass only because an earlier test left cloud mode behind. The cause is the
carry, and ``tests/_env_restore.py``'s ``_restore_mode_state`` ends it. These tests plant the leak: test A makes
the mode cloud by a raw write, test B declares nothing and reads the mode, both in one process in order. Each plant
is run twice, WITHOUT the fixture (B must fail, which proves the leak is real and that this test can fail) and WITH
it (B must pass); one plant also runs under xdist, whose worker holds both tests in order.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._env_restore import MODE_ENV_KEYS

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: One raw write that makes the mode cloud, by test A. ``nexus.config.is_local_mode`` reads each of the first
#: three; the last is the binding itself rebound rather than patched.
_LEAKS = {
    "nx_local_zero": 'os.environ["NX_LOCAL"] = "0"',
    "service_url": 'os.environ["NX_SERVICE_URL"] = "https://managed.invalid"',
    "config_dir_record": (
        'd = pathlib.Path(tempfile.mkdtemp()); (d / "config.yml").write_text("install:\\n  mode: managed\\n");'
        ' os.environ["NEXUS_CONFIG_DIR"] = str(d)'
    ),
    "binding_rebound": "import nexus.config; nexus.config.is_local_mode = lambda: False",
}

_PAIR = '''
import os, pathlib, tempfile

def test_a_leaker():
    {leak}

def test_b_undeclared_reader():
    from nexus.config import is_local_mode
    assert is_local_mode() is True, "test B declares no mode and saw cloud"
'''


@pytest.fixture
def clean_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the planted session from the unset state, with an undo recorded for every key and for the binding, so
    the leak a run WITHOUT the fixture plants cannot outlive this test."""
    import nexus.config  # noqa: PLC0415 - the binding the plant rebinds

    for key in MODE_ENV_KEYS:
        monkeypatch.setenv(key, "placeholder")  # records the undo even when the key starts absent
        monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHONPATH", str(_REPO_ROOT))  # a subprocess session imports ``tests._env_restore``
    monkeypatch.setattr(nexus.config, "is_local_mode", nexus.config.is_local_mode)


def _nested(pytester: pytest.Pytester, leak: str, *, isolated: bool) -> pytest.RunResult:
    pytester.makeconftest('pytest_plugins = ["tests._env_restore"]\n' if isolated else "")
    pytester.makepyfile(_PAIR.format(leak=leak))
    return pytester.runpytest_inprocess("-p", "no:randomly", "-p", "no:cacheprovider", "-q")


@pytest.mark.parametrize("leak", _LEAKS.values(), ids=_LEAKS.keys())
def test_without_the_isolation_test_b_sees_the_leak(pytester, clean_ambient, leak) -> None:
    _nested(pytester, leak, isolated=False).assert_outcomes(passed=1, failed=1)


@pytest.mark.parametrize("leak", _LEAKS.values(), ids=_LEAKS.keys())
def test_with_the_isolation_test_b_does_not(pytester, clean_ambient, leak) -> None:
    _nested(pytester, leak, isolated=True).assert_outcomes(passed=2)


def test_the_isolation_holds_under_xdist_and_the_plant_fails_without_it(pytester, clean_ambient) -> None:
    """Both tests land on one worker (``loadfile``), in file order: the same carry, in another process."""
    pytest.importorskip("xdist")
    pytester.makepyfile(_PAIR.format(leak=_LEAKS["nx_local_zero"]))
    args = ("-p", "no:randomly", "-p", "no:cacheprovider", "-q", "-n", "2", "--dist", "loadfile")
    pytester.makeconftest("")
    pytester.runpytest_subprocess(*args).assert_outcomes(passed=1, failed=1)
    pytester.makeconftest('pytest_plugins = ["tests._env_restore"]\n')
    pytester.runpytest_subprocess(*args).assert_outcomes(passed=2)


def test_a_declared_cloud_mode_test_is_not_undone(pytester, clean_ambient) -> None:
    """The restore must not break the declared path: ``cloud_mode``-style setenv inside a test still reads cloud
    in that test, and the next test reads local."""
    pytester.makeconftest('pytest_plugins = ["tests._env_restore"]\n')
    pytester.makepyfile('''
def test_declares_cloud(monkeypatch):
    from nexus.config import is_local_mode
    monkeypatch.setenv("NX_LOCAL", "0")
    assert is_local_mode() is False

def test_next_reads_local():
    from nexus.config import is_local_mode
    assert is_local_mode() is True
''')
    pytester.runpytest_inprocess("-p", "no:randomly", "-p", "no:cacheprovider", "-q").assert_outcomes(passed=2)

