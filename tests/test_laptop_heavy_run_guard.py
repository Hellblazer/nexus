# SPDX-License-Identifier: AGPL-3.0-or-later
"""The laptop heavy-run guard (nexus-tvt6u): an xdist run that boots the engine on a Mac
laptop, with no lab-run grant, goes to ``lab-remote`` instead of starving the other sessions.

The platform and the environment are injected, so none of this depends on the host being a
Mac or on a grant being present. The one run-through that reads the real environment
(``NX_TEST_T2_SUBSTRATE``) sets it explicitly.
"""
from __future__ import annotations

import inspect
import sys
from types import SimpleNamespace

import pytest

from tests import _suite_lease

_SUBSTRATE_ENV = "NX_TEST_T2_SUBSTRATE"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """The real conftest module, with the engine-substrate predicate on and no nesting."""
    module = sys.modules["tests.conftest"]
    monkeypatch.delenv(_SUBSTRATE_ENV, raising=False)  # unset means "optin": boots the engine
    monkeypatch.setattr(_suite_lease, "inside_a_holder", lambda: False)
    for name in ("LAB_CPUS", "LAB_MEM_MB", "LAB_REMOTE", "NX_LOCAL_HEAVY", "NX_SUITE_LEASE_WAIT",
                 module._SUITE_LEASE_UNGUARDED_ENV):
        monkeypatch.delenv(name, raising=False)
    return module


def _run_expecting_to_go_ahead(call, *args, **kwargs) -> None:
    """A refusal here is a test failure, not a session exit (``pytest.exit`` would end the whole outer run)."""
    try:
        call(*args, **kwargs)
    except pytest.exit.Exception as refused:
        pytest.fail(f"refused a run that should have gone ahead: {refused.msg[:300]}")


def _config(workers: object, *args: str) -> SimpleNamespace:
    return SimpleNamespace(option=SimpleNamespace(numprocesses=workers),
                           invocation_params=SimpleNamespace(args=args))


def test_a_mac_xdist_substrate_run_with_no_grant_is_refused_with_the_lab_remote_line(gate) -> None:
    with pytest.raises(pytest.exit.Exception) as err:
        gate._refuse_heavy_laptop_run(
            _config(4, "tests/db", "-n", "4", "-k", "a b"), platform="darwin", env={})
    assert err.value.returncode == 75
    assert "laptop heavy run: refusing to start" in err.value.msg
    assert "lab-remote -c lab-l -- uv run pytest tests/db -n 4 -k 'a b'" in err.value.msg


def test_an_unresolved_n_auto_counts_as_more_than_one(gate) -> None:
    with pytest.raises(pytest.exit.Exception):
        gate._refuse_heavy_laptop_run(_config("auto", "-n", "auto"), platform="darwin", env={})


def test_a_clamp_that_brought_the_count_to_one_is_not_refused(gate) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(1, "-n", "auto"), platform="darwin", env={})


@pytest.mark.parametrize("workers", [None, 0, 1])
def test_a_serial_or_single_worker_run_is_allowed(gate, workers: object) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(workers), platform="darwin", env={})


def test_a_run_that_boots_no_engine_is_allowed(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SUBSTRATE_ENV, "none")
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin", env={})


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_only_macos_is_guarded(gate, platform: str) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform=platform, env={})


@pytest.mark.parametrize("env", [{"LAB_CPUS": "8"}, {"LAB_MEM_MB": "16384"}, {"LAB_REMOTE": "1"}])
def test_a_lab_run_grant_skips_the_guard(gate, env: dict[str, str]) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin", env=env)


def test_a_blank_grant_variable_is_not_a_grant(gate) -> None:
    with pytest.raises(pytest.exit.Exception):
        gate._refuse_heavy_laptop_run(_config(4, "-n", "4"), platform="darwin", env={"LAB_CPUS": "  "})


def test_the_opt_out_proceeds_and_says_so(gate, capsys: pytest.CaptureFixture[str]) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin", env={"NX_LOCAL_HEAVY": "1"})
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "NX_LOCAL_HEAVY=1" in err and "4 xdist workers" in err


def test_the_opt_out_means_exactly_one(gate) -> None:
    with pytest.raises(pytest.exit.Exception):
        gate._refuse_heavy_laptop_run(_config(4, "-n", "4"), platform="darwin", env={"NX_LOCAL_HEAVY": "true"})


def test_a_nested_pytest_inside_a_holder_is_not_refused(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_suite_lease, "inside_a_holder", lambda: True)
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin", env={})


# -- inside a grant the lease steps aside; with no grant it is the fallback ----------------


def test_a_lab_run_grant_skips_the_suite_lease_entirely(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[str] = []
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: taken.append("acquire"))
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "0")  # would be an immediate refusal against that holder
    monkeypatch.setenv("LAB_CPUS", "8")
    _run_expecting_to_go_ahead(gate._take_suite_lease)  # does not raise, does not acquire
    assert taken == [] and gate._suite_lease_release is None


def test_without_a_grant_the_lease_is_still_taken(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[str] = []
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: None)
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: taken.append("acquire") or (lambda: None))
    _run_expecting_to_go_ahead(gate._take_suite_lease)
    assert taken == ["acquire"]


def test_session_start_runs_the_guard_before_the_build_lease_and_the_suite_lease() -> None:
    source = inspect.getsource(sys.modules["tests.conftest"].pytest_sessionstart)
    guard, build, lease = (source.index(name) for name in
                           ("_refuse_heavy_laptop_run(", "_gate_on_build_lease()", "_take_suite_lease()"))
    assert guard < build < lease
