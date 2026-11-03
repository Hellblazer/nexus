# SPDX-License-Identifier: AGPL-3.0-or-later
"""The laptop heavy-run guard (nexus-tvt6u): an xdist run whose engine substrate can
boot (``NX_TEST_T2_SUBSTRATE`` is not ``none``) on a Mac laptop, with no lab-run grant, goes to ``lab-remote`` instead of starving the other sessions.

The platform and the environment are injected, so none of this depends on the host being a
Mac or on a grant being present. The one run-through that reads the real environment
(``NX_TEST_T2_SUBSTRATE``) sets it explicitly.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from types import SimpleNamespace

import pytest

from tests import _suite_lease, _xdist_cap
from tests._module_seam import setattr_in
from tests.test_suite_lease import _virtual_time

_SUBSTRATE_ENV = "NX_TEST_T2_SUBSTRATE"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """The real conftest module, with the engine-substrate predicate on and no nesting."""
    module = sys.modules["tests.conftest"]
    monkeypatch.delenv(_SUBSTRATE_ENV, raising=False)  # unset means "optin": the engine can boot
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
    # The predicate is "substrate is not none" (the engine CAN boot), and the message must not claim more.
    assert "NX_TEST_T2_SUBSTRATE is not none" in err.value.msg and "boots the engine" not in err.value.msg


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


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_lab_run_grant_off_macos_skips_the_suite_lease_entirely(gate, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    taken: list[str] = []
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: taken.append("acquire"))
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "0")  # would be an immediate refusal against that holder
    monkeypatch.setenv("LAB_CPUS", "8")
    _run_expecting_to_go_ahead(gate._take_suite_lease, platform=platform)  # does not raise, does not acquire
    assert taken == [] and gate._suite_lease_release is None


@pytest.mark.parametrize("grant", ["LAB_CPUS", "LAB_MEM_MB"])
def test_a_lab_run_grant_on_macos_still_takes_the_suite_lease(gate, monkeypatch: pytest.MonkeyPatch, grant: str) -> None:
    """hellmini's lab-mac grant is admission and accounting only (no cap), and
    tests/e2e/macos-guest/host.sh guard and hold read the ``suite`` lease to keep a VM run off a
    running suite: a grant must not hide the suite from them."""
    taken: list[str] = []
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: None)
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: taken.append("acquire") or (lambda: None))
    monkeypatch.setenv(grant, "8")
    _run_expecting_to_go_ahead(gate._take_suite_lease, platform="darwin")
    assert taken == ["acquire"]


def test_a_lab_run_grant_on_macos_queues_behind_a_hold(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (mac-guest vm)")
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "0")
    monkeypatch.setenv("LAB_MEM_MB", "10240")
    with pytest.raises(pytest.exit.Exception) as err:
        gate._take_suite_lease(platform="darwin")
    assert err.value.returncode == 75 and "pid 4242 (mac-guest vm)" in err.value.msg


def test_without_a_grant_the_lease_is_still_taken(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[str] = []
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: None)
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: taken.append("acquire") or (lambda: None))
    _run_expecting_to_go_ahead(gate._take_suite_lease)
    assert taken == ["acquire"]


def test_session_start_runs_the_guard_before_the_build_lease_and_the_suite_lease() -> None:
    conftest = sys.modules["tests.conftest"]
    assert "_run_session_start_gates(session.config)" in inspect.getsource(conftest.pytest_sessionstart)
    source = inspect.getsource(conftest._run_session_start_gates)
    guard, build, lease, reclamp, preflight = (source.index(name) for name in (
        "_refuse_heavy_laptop_run(", "_gate_on_build_lease()", "_take_suite_lease()",
        "_reclamp_xdist_workers_after_lease_wait(", "_preflight_engine_substrate()"))
    assert guard < build < lease < reclamp < preflight


def test_a_refusal_after_the_lease_is_taken_gives_the_lease_back(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest runs no sessionfinish for an Exit raised in sessionstart, so the gates release it themselves."""
    released: list[str] = []
    monkeypatch.setattr(gate, "_refuse_heavy_laptop_run", lambda config: None)
    monkeypatch.setattr(gate, "_gate_on_build_lease", lambda: None)
    monkeypatch.setattr(gate, "_take_suite_lease", lambda: None)
    monkeypatch.setattr(gate, "_reclamp_xdist_workers_after_lease_wait", lambda config: None)
    monkeypatch.setattr(gate, "_release_suite_lease", lambda: released.append("released"))

    def refuse() -> None:
        pytest.exit("engine substrate: refusing to start", returncode=75)

    monkeypatch.setattr(gate, "_preflight_engine_substrate", refuse)
    with pytest.raises(pytest.exit.Exception):
        gate._run_session_start_gates(SimpleNamespace())
    assert released == ["released"]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("a later gate broke"), SystemExit(3)],
                         ids=["KeyboardInterrupt", "RuntimeError", "SystemExit"])
def test_any_failure_after_the_lease_is_taken_gives_the_lease_back_and_propagates(
        gate, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    """Not only a pytest.exit: a Ctrl-C during the preflight, or a bug in a gate, also runs no sessionfinish."""
    released: list[str] = []
    monkeypatch.setattr(gate, "_refuse_heavy_laptop_run", lambda config: None)
    monkeypatch.setattr(gate, "_gate_on_build_lease", lambda: None)
    monkeypatch.setattr(gate, "_take_suite_lease", lambda: None)
    monkeypatch.setattr(gate, "_reclamp_xdist_workers_after_lease_wait", lambda config: None)
    monkeypatch.setattr(gate, "_release_suite_lease", lambda: released.append("released"))

    def fail() -> None:
        raise failure

    monkeypatch.setattr(gate, "_preflight_engine_substrate", fail)
    with pytest.raises(type(failure)) as err:
        gate._run_session_start_gates(SimpleNamespace())
    assert err.value is failure
    assert released == ["released"]


@pytest.fixture
def real_lease(gate, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """``_take_suite_lease`` against a real lease in a tmp root, with atexit.register captured."""
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(tmp_path / "leases"))
    monkeypatch.delenv(_suite_lease.HELD_BY_ENV, raising=False)
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(gate, "_suite_lease_backstop_registered", False)
    registered: list[object] = []
    setattr_in(monkeypatch, gate, "atexit.register", lambda fn, *a, **k: registered.append(fn) or fn)
    yield SimpleNamespace(root=tmp_path / "leases", registered=registered)
    gate._release_suite_lease()  # a failing test must not leave a lease behind


def test_the_lease_has_an_exit_backstop_for_the_code_after_the_gates(gate, real_lease) -> None:
    """xdist's own sessionstart, or a Ctrl-C after the gates, ends the process with no sessionfinish."""
    _run_expecting_to_go_ahead(gate._take_suite_lease)
    assert real_lease.registered == [gate._release_suite_lease]
    assert (real_lease.root / _suite_lease.RESOURCE / "pid").exists()
    real_lease.registered[0]()  # what atexit would run
    assert not (real_lease.root / _suite_lease.RESOURCE).exists(), "the backstop left the lease behind"
    assert gate._suite_lease_release is None
    real_lease.registered[0]()  # and it is idempotent: the normal sessionfinish may have run first


def test_the_exit_backstop_is_registered_once_however_many_sessions_take_a_lease(gate, real_lease) -> None:
    for _ in range(2):
        _run_expecting_to_go_ahead(gate._take_suite_lease)
        gate._release_suite_lease()
    assert real_lease.registered == [gate._release_suite_lease]


def test_the_give_up_message_names_the_ticket_ahead_when_the_lease_itself_is_free(
        gate, real_lease, monkeypatch: pytest.MonkeyPatch) -> None:
    queue_dir = real_lease.root / _suite_lease.QUEUE_DIR
    queue_dir.mkdir(parents=True)
    (queue_dir / f"{time.time_ns() - 5_000_000_000:020d}-{os.getpid()}").write_text("sibling worktree: pytest -n 8\n")
    monkeypatch.setattr(_suite_lease, "DEFAULT_WAIT_SECONDS", 3)
    _virtual_time(monkeypatch)
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "1")
    with pytest.raises(pytest.exit.Exception) as err:
        gate._take_suite_lease()
    msg = err.value.msg
    assert err.value.returncode == 75
    assert f"pid {os.getpid()} (sibling worktree: pytest -n 8)" in msg and "ahead in the line" in msg
    assert "has just taken it" not in msg and "rm -rf" not in msg, msg


def test_the_give_up_message_names_the_lease_holder_when_there_is_one(
        gate, real_lease, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = real_lease.root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    (lease / "label").write_text("the long run\n")
    monkeypatch.setattr(_suite_lease, "DEFAULT_WAIT_SECONDS", 3)
    _virtual_time(monkeypatch)
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "1")
    with pytest.raises(pytest.exit.Exception) as err:
        gate._take_suite_lease()
    assert f"pid {os.getpid()} (the long run)" in err.value.msg and "ahead in the line" not in err.value.msg


def test_gates_that_pass_leave_the_lease_held(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[str] = []
    for name in ("_refuse_heavy_laptop_run", "_reclamp_xdist_workers_after_lease_wait"):
        monkeypatch.setattr(gate, name, lambda config: None)
    for name in ("_gate_on_build_lease", "_take_suite_lease", "_preflight_engine_substrate"):
        monkeypatch.setattr(gate, name, lambda: None)
    monkeypatch.setattr(gate, "_release_suite_lease", lambda: released.append("released"))
    gate._run_session_start_gates(SimpleNamespace())
    assert released == []


# -- the follow-ups of nexus-74bc6 ---------------------------------------------------------


@pytest.fixture
def events_log(tmp_path, monkeypatch: pytest.MonkeyPatch):
    log = tmp_path / "events.log"
    monkeypatch.setenv(_suite_lease.EVENTS_LOG_ENV, str(log))
    return log


def test_the_opt_out_leaves_a_local_heavy_line_in_the_events_log(gate, events_log) -> None:
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin",
                               env={"NX_LOCAL_HEAVY": "1"})
    (line,) = events_log.read_text().splitlines()
    assert " lease=suite name=suite " in line and " outcome=local_heavy" in line and " workers=4" in line
    assert "reason=" not in line


def test_the_opt_out_logs_the_reason_it_was_given(gate, events_log) -> None:
    _run_expecting_to_go_ahead(
        gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="darwin",
        env={"NX_LOCAL_HEAVY": "1", "NX_LOCAL_HEAVY_REASON": "needs bd and claude on this laptop"})
    (line,) = events_log.read_text().splitlines()
    assert 'reason="needs bd and claude on this laptop"' in line


def test_a_second_pass_of_the_guard_on_one_run_writes_only_one_local_heavy_line(gate, events_log) -> None:
    """The re-check after a queued run's worker count rose is the same run, not a second bypass."""
    config = _config(4, "-n", "4")
    for _ in range(2):
        _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, config, platform="darwin",
                                   env={"NX_LOCAL_HEAVY": "1"})
    assert len(events_log.read_text().splitlines()) == 1
    other = _config(4, "-n", "4")  # another run still logs its own
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, other, platform="darwin", env={"NX_LOCAL_HEAVY": "1"})
    assert len(events_log.read_text().splitlines()) == 2


def test_a_refusal_and_a_run_that_is_not_guarded_log_nothing(gate, events_log) -> None:
    with pytest.raises(pytest.exit.Exception):
        gate._refuse_heavy_laptop_run(_config(4, "-n", "4"), platform="darwin", env={})
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, _config(4, "-n", "4"), platform="linux",
                               env={"NX_LOCAL_HEAVY": "1"})
    assert not events_log.exists()


def test_collect_only_is_never_refused(gate) -> None:
    config = _config(4, "--collect-only", "-n", "4")
    config.option.collectonly = True
    _run_expecting_to_go_ahead(gate._refuse_heavy_laptop_run, config, platform="darwin", env={})


def test_an_n_from_pytest_addopts_is_in_the_lab_remote_line(gate) -> None:
    with pytest.raises(pytest.exit.Exception) as err:
        gate._refuse_heavy_laptop_run(_config(4, "tests/db"), platform="darwin",
                                      env={"PYTEST_ADDOPTS": "-n 4 -k 'a b'"})
    assert "lab-remote -c lab-l -- uv run pytest -n 4 -k 'a b' tests/db" in err.value.msg


def test_a_pytest_addopts_that_does_not_parse_still_refuses(gate) -> None:
    with pytest.raises(pytest.exit.Exception) as err:
        gate._refuse_heavy_laptop_run(_config(4, "tests/db"), platform="darwin",
                                      env={"PYTEST_ADDOPTS": "-n 4 -k 'unterminated"})
    assert "-n 4" in err.value.msg and "tests/db" in err.value.msg


# -- the worker clamp is re-read after a queued run gets the lease ---------------------------


@pytest.fixture
def headroom(monkeypatch: pytest.MonkeyPatch):
    """The SysV cap the clamp sees, settable: a peer's suite holds most of it until it leaves."""
    state = {"cap": 2}
    monkeypatch.setattr(_xdist_cap, "effective_cap", lambda: state["cap"])
    monkeypatch.setattr(_xdist_cap, "effective_memory_cap", lambda: None)
    return state


def _xdist_config(workers: int) -> SimpleNamespace:
    config = _config(workers, "-n", str(workers))
    config.option.tx = ["popen"] * workers
    return config


def test_a_run_that_queued_re_reads_the_headroom_the_peer_gave_back(gate, headroom, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    config = _xdist_config(8)
    gate._clamp_xdist_workers(config)  # configure time: the peer's clusters are still up
    assert config.option.numprocesses == 2 and config.option.tx == ["popen"] * 2
    headroom["cap"] = 8  # the peer exited; we got the lease
    monkeypatch.setattr(gate, "_suite_lease_waited", True)
    gate._reclamp_xdist_workers_after_lease_wait(config, platform="linux", env={})  # not a Mac: the laptop guard is not in play
    assert config.option.numprocesses == 8 and config.option.tx == ["popen"] * 8
    assert "xdist workers 2 -> 8" in capsys.readouterr().out


def test_a_run_that_did_not_wait_does_not_re_read(gate, headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _xdist_config(8)
    gate._clamp_xdist_workers(config)
    headroom["cap"] = 8
    monkeypatch.setattr(gate, "_suite_lease_waited", False)
    gate._reclamp_xdist_workers_after_lease_wait(config, platform="linux", env={})
    assert config.option.numprocesses == 2


def test_the_re_read_does_not_raise_a_cap_that_is_still_binding_and_prints_no_note_twice(
        gate, headroom, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = _xdist_config(8)
    gate._clamp_xdist_workers(config)
    capsys.readouterr()
    monkeypatch.setattr(gate, "_suite_lease_waited", True)
    gate._reclamp_xdist_workers_after_lease_wait(config, platform="linux", env={})
    assert config.option.numprocesses == 2
    assert capsys.readouterr().out == ""


def test_a_re_read_that_raises_the_count_puts_the_laptop_guard_to_the_run_again(
        gate, headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard judged the run on the clamped count; a run clamped to one passes it."""
    headroom["cap"] = 1
    config = _xdist_config(4)
    gate._clamp_xdist_workers(config)
    assert config.option.numprocesses == 1
    headroom["cap"] = 4
    monkeypatch.setattr(gate, "_suite_lease_waited", True)
    with pytest.raises(pytest.exit.Exception) as err:
        gate._reclamp_xdist_workers_after_lease_wait(config, platform="darwin", env={})
    assert "laptop heavy run: refusing to start" in err.value.msg


@pytest.mark.parametrize("how", ["none", "raises"])
def test_a_re_read_that_cannot_derive_a_cap_keeps_the_count_the_run_already_had(
        gate, headroom, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Fail closed: a clamped run whose headroom cannot be read must not be handed all its workers back."""
    config = _xdist_config(8)
    gate._clamp_xdist_workers(config)
    assert config.option.numprocesses == 2
    monkeypatch.setattr(gate, "_suite_lease_waited", True)
    if how == "none":
        headroom["cap"] = None
    else:
        def broken() -> int:
            raise OSError("ipcs vanished")

        monkeypatch.setattr(_xdist_cap, "effective_cap", broken)
    gate._reclamp_xdist_workers_after_lease_wait(config, platform="linux", env={})
    assert config.option.numprocesses == 2 and config.option.tx == ["popen"] * 2


def test_waiting_for_the_lease_is_what_marks_a_run_as_having_queued(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_suite_lease_waited", False)
    gate._announce_suite_lease_wait("pid 1 (peer)", 0, 1800)
    assert gate._suite_lease_waited is True


def test_taking_the_lease_clears_the_mark_of_an_earlier_session(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_suite_lease_waited", True)
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: False)
    gate._take_suite_lease()
    assert gate._suite_lease_waited is False
