# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 3 (nexus-2jomg.2): a raw write of the mode state does not reach the next test.

The mode-declarations census (``test_mode_declarations_are_explicit``) guards one thing: a test that names a cloud
embedder and declares no mode must not pass only because an earlier test left cloud mode behind. The cause is the
carry, and ``tests/_env_restore.py``'s ``_restore_mode_state`` ends it for the keys in ``MODE_ENV_KEYS`` and the
``nexus.config.is_local_mode`` binding. These tests plant the leak: test A leaks by a raw write, test B declares
nothing and reads what that write changed, both in one process in order. Each plant is run twice, WITHOUT the fixture
(B must fail, which proves the leak is real and that this test can fail) and WITH it (B must pass); one plant also
runs under xdist, whose worker holds both tests in order. Every key in ``MODE_ENV_KEYS`` has its own plant and a B that
reads it, so dropping a key from the list turns a test red.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._env_restore import MODE_ENV_KEYS

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Bounds a hang of the nested xdist session, never performance.
_NESTED_HANG_TIMEOUT_S = 180

_MODE = "from nexus.config import is_local_mode\n    assert is_local_mode() is True, 'B declares no mode and saw cloud'"
_RECORD = (
    'd = pathlib.Path(tempfile.mkdtemp()); (d / "{file}").write_text("install:\\n  mode: managed\\n");'
    ' os.environ["{key}"] = str(d)'
)

#: plant id -> (the raw write test A makes, what test B reads). ``nexus.config.is_local_mode`` reads the first five;
#: the credential pair is read through ``get_credential``; the last is the binding rebound rather than patched.
_PLANTS: dict[str, tuple[str, str]] = {
    "nx_local_zero": ('os.environ["NX_LOCAL"] = "0"', _MODE),
    "service_url": ('os.environ["NX_SERVICE_URL"] = "https://managed.invalid"', _MODE),
    "config_dir_record": (_RECORD.format(file="config.yml", key="NEXUS_CONFIG_DIR"), _MODE),
    "repo_root_record": (_RECORD.format(file=".nexus.yml", key="NEXUS_REPO_ROOT"), _MODE),
    "service_token": (
        'os.environ["NX_SERVICE_TOKEN"] = "leaked-bearer"',
        "from nexus.config import get_credential\n    assert get_credential('service_token') == ''",
    ),
    "voyage_api_key": (
        'os.environ["VOYAGE_API_KEY"] = "vk_leaked"',
        "from nexus.config import get_credential\n    assert get_credential('voyage_api_key') == ''",
    ),
    "binding_rebound": ("import nexus.config; nexus.config.is_local_mode = lambda: False", _MODE),
}

_PAIR = '''
import os, pathlib, tempfile

def test_a_leaker():
    {leak}

def test_b_undeclared_reader():
    {read}
'''

_ARGS = ("-p", "no:randomly", "-p", "no:cacheprovider", "-q")
_ISOLATED_CONFTEST = 'pytest_plugins = ["tests._env_restore"]\n'


@pytest.fixture
def clean_ambient(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start the planted session from the unset state, with an undo recorded for every key and for the binding, so
    the leak a run WITHOUT the fixture plants cannot outlive this test. The config directory is an empty one of
    this test's own, not the unset default (which would read the host's)."""
    import nexus.config  # noqa: PLC0415 - the binding the plant rebinds

    for key in MODE_ENV_KEYS:
        monkeypatch.setenv(key, "placeholder")  # records the undo even when the key starts absent
        monkeypatch.delenv(key)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.setenv("PYTHONPATH", str(_REPO_ROOT))  # a subprocess session imports ``tests._env_restore``
    monkeypatch.setattr(nexus.config, "is_local_mode", nexus.config.is_local_mode)


def _pair(pytester: pytest.Pytester, plant: str) -> None:
    leak, read = _PLANTS[plant]
    pytester.makepyfile(_PAIR.format(leak=leak, read=read))


def test_every_mode_key_has_a_plant() -> None:
    planted = "\n".join(leak for leak, _ in _PLANTS.values())
    assert [k for k in MODE_ENV_KEYS if not re.search(rf"\b{k}\b", planted)] == []


@pytest.mark.parametrize("plant", _PLANTS)
def test_without_the_isolation_test_b_sees_the_leak(pytester, clean_ambient, plant) -> None:
    pytester.makeconftest("")
    _pair(pytester, plant)
    pytester.runpytest_inprocess(*_ARGS).assert_outcomes(passed=1, failed=1)


@pytest.mark.parametrize("plant", _PLANTS)
def test_with_the_isolation_test_b_does_not(pytester, clean_ambient, plant) -> None:
    pytester.makeconftest(_ISOLATED_CONFTEST)
    _pair(pytester, plant)
    pytester.runpytest_inprocess(*_ARGS).assert_outcomes(passed=2)


def test_the_isolation_holds_under_xdist_and_the_plant_fails_without_it(pytester, clean_ambient) -> None:
    """Both tests land on one worker (``loadfile``), in file order: the same carry, in another process."""
    pytest.importorskip("xdist")
    _pair(pytester, "nx_local_zero")
    args = (*_ARGS, "-n", "2", "--dist", "loadfile")
    pytester.makeconftest("")
    pytester.runpytest_subprocess(*args, timeout=_NESTED_HANG_TIMEOUT_S).assert_outcomes(passed=1, failed=1)
    pytester.makeconftest(_ISOLATED_CONFTEST)
    pytester.runpytest_subprocess(*args, timeout=_NESTED_HANG_TIMEOUT_S).assert_outcomes(passed=2)


@pytest.mark.parametrize("scope", ["session", "module", "class"])
def test_a_wider_scope_fixture_that_writes_the_mode_raw_keeps_it_for_its_tests(pytester, clean_ambient, scope) -> None:
    """The integration fixtures (``seeded_client`` and the engine-token fixtures) set NX_LOCAL / NX_SERVICE_* raw at
    module scope and put them back at teardown, and a fixture may rebind ``is_local_mode`` on purpose. Pytest sets a
    wider scope up before any function-scoped fixture, so the snapshot already holds that state: every test in the
    scope reads it, and the restore does not fight the fixture."""
    pytester.makeconftest(_ISOLATED_CONFTEST)
    pytester.makepyfile(f'''
import os
import pytest
import nexus.config

@pytest.fixture(scope="{scope}")
def raw_cloud():
    original = nexus.config.is_local_mode
    os.environ["NX_SERVICE_TOKEN"] = "scoped-token"
    os.environ["NX_LOCAL"] = "0"
    nexus.config.is_local_mode = lambda: False
    yield
    nexus.config.is_local_mode = original
    os.environ.pop("NX_SERVICE_TOKEN", None)
    os.environ.pop("NX_LOCAL", None)

class TestInScope:
    def test_1(self, raw_cloud):
        assert os.environ["NX_SERVICE_TOKEN"] == "scoped-token"
        assert nexus.config.is_local_mode() is False

    def test_2(self, raw_cloud):
        assert os.environ["NX_SERVICE_TOKEN"] == "scoped-token"
        assert os.environ["NX_LOCAL"] == "0"
        assert nexus.config.is_local_mode() is False
''')
    pytester.runpytest_inprocess(*_ARGS).assert_outcomes(passed=2)


def test_a_test_that_declares_cloud_through_monkeypatch_still_reads_cloud(pytester, clean_ambient) -> None:
    """Narrow on purpose: what this shows is that the fixture does not undo a test's own declaration while the test
    runs (``cloud_mode``'s ``setenv`` is visible inside it). It does NOT show the fixture restores anything: the
    next test reading local is monkeypatch's own undo, which passes with the fixture removed."""
    pytester.makeconftest(_ISOLATED_CONFTEST)
    pytester.makepyfile('''
def test_declares_cloud(monkeypatch):
    from nexus.config import is_local_mode
    monkeypatch.setenv("NX_LOCAL", "0")
    assert is_local_mode() is False
''')
    pytester.runpytest_inprocess(*_ARGS).assert_outcomes(passed=1)


def _fixture_order(pytester: pytest.Pytester, conftest: str) -> list[str]:
    """The ``SETUP``/``TEARDOWN`` lines of the two autouse fixtures in one nested run, in the order they ran."""
    pytester.makeconftest(conftest)
    pytester.makepyfile("def test_x():\n    pass\n")
    out = pytester.runpytest_inprocess(*_ARGS, "--setup-show").outlines
    return [
        f"{m.group(1)} {m.group(2)}"
        for line in out
        if (m := re.search(r"(SETUP|TEARDOWN)\s+F (_restore_mode_state|_conftest_pin)\b", line))
    ]


_PIN = (
    "import pytest\n"
    "@pytest.fixture(autouse=True)\n"
    "def _conftest_pin(monkeypatch):\n"
    "    monkeypatch.setenv('NX_LOCAL', '1')\n"
)


def test_the_fixture_is_set_up_first_and_torn_down_last(pytester, clean_ambient) -> None:
    """Pin the ordering the module docstring of ``tests/_env_restore.py`` relies on: the snapshot precedes a conftest
    fixture's ``monkeypatch.setenv`` and the comparison follows its undo. The same pin run with the fixture moved
    into the conftest after the pin gets the opposite order, so a move turns this red (a value probe cannot: for a
    restore-only fixture both orders leave the next test clean)."""
    plugin_first = _fixture_order(pytester, _ISOLATED_CONFTEST + _PIN)
    assert plugin_first == [
        "SETUP _restore_mode_state", "SETUP _conftest_pin", "TEARDOWN _conftest_pin", "TEARDOWN _restore_mode_state",
    ]


def test_the_ordering_pin_can_fail(pytester, clean_ambient) -> None:
    moved = _fixture_order(
        pytester, _PIN + "from tests._env_restore import _restore_mode_state  # noqa: F401  (a conftest fixture)\n",
    )
    assert moved == [
        "SETUP _conftest_pin", "SETUP _restore_mode_state", "TEARDOWN _restore_mode_state", "TEARDOWN _conftest_pin",
    ]
