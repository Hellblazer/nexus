"""The macOS posix_spawn adapter in tests/_posix_spawn.py.

Each case checks two things. The spawn behaves exactly as Popen documents,
including where it runs and what it raises. And the census shows which path
CPython took, which is the property that keeps a fork child out of
Network.framework's atfork handler.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys

import pytest

from tests import _posix_spawn as ps

pytestmark = pytest.mark.skipif(not ps.ACTIVE, reason="posix_spawn adapter is macOS-only")

# The cases that assert the posix_spawn path need the adjustment on; a run with
# NX_POSIX_SPAWN=0 is a census-only comparison run and skips them.
needs_adjust = pytest.mark.skipif(not ps.ADJUST, reason="NX_POSIX_SPAWN=0: census-only run")


@pytest.fixture
def census():
    """A clean census for the test, restored afterwards."""
    counters = (ps.spawn_paths, ps.fork_reasons, ps.inheritable_sites, ps.inheritable_samples)
    saved = [c.copy() for c in counters]
    saved_sites = {k: v.copy() for k, v in ps.fork_sites.items()}
    for c in counters:
        c.clear()
    ps.fork_sites.clear()
    yield ps
    for c, old in zip(counters, saved):
        c.clear()
        c.update(old)
    ps.fork_sites.clear()
    ps.fork_sites.update(saved_sites)


_PRINT_CWD = [sys.executable, "-c", "import os; print(os.getcwd())"]
_PRINT_SESSION = [
    sys.executable, "-c",
    "import os; p = os.getpid(); print(os.getsid(0) == p, os.getpgid(0) == p)",
]


@needs_adjust
def test_cwd_runs_in_the_directory_and_takes_posix_spawn(census, tmp_path) -> None:
    out = subprocess.run(_PRINT_CWD, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert os.path.realpath(out.stdout.strip()) == os.path.realpath(tmp_path)
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)


@needs_adjust
def test_shell_string_with_cwd_runs_in_the_directory(census, tmp_path) -> None:
    (tmp_path / "marker").write_text("x")
    out = subprocess.run("ls marker && echo ok", shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["marker", "ok"]
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)


@needs_adjust
def test_bare_program_name_keeps_its_argv0_under_cwd(census, tmp_path) -> None:
    out = subprocess.run(["sh", "-c", 'echo "$0"'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "sh"
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)


@needs_adjust
def test_bare_program_name_resolves_and_takes_posix_spawn(census) -> None:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.startswith("git version")
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)


@needs_adjust
@pytest.mark.parametrize(
    ("kwargs", "leads_session"),
    [
        ({"start_new_session": True}, True),
        ({"preexec_fn": os.setsid}, True),
        ({"process_group": 0}, False),
        ({"preexec_fn": os.setpgrp}, False),
    ],
    ids=["start_new_session", "preexec_setsid", "process_group", "preexec_setpgrp"],
)
def test_session_and_group_requests_hold_and_take_posix_spawn(census, kwargs, leads_session) -> None:
    out = subprocess.run(_PRINT_SESSION, capture_output=True, text=True, check=True, **kwargs)
    assert out.stdout.split() == [str(leads_session), "True"]
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)


def test_exit_status_passes_through_the_cwd_step(census, tmp_path) -> None:
    out = subprocess.run([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path)
    assert out.returncode == 7


def test_missing_cwd_still_raises(census, tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.run(["git", "--version"], cwd=tmp_path / "absent")


def test_missing_program_still_raises(census) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.run(["nx-no-such-program-for-this-test"])


def test_pass_fds_keeps_close_fds_and_forks(census) -> None:
    r, w = os.pipe()
    try:
        subprocess.run([sys.executable, "-c", "pass"], pass_fds=(w,), check=True)
    finally:
        os.close(r)
        os.close(w)
    assert census.fork_reasons == {"close_fds": 1}


@needs_adjust
def test_a_stray_inheritable_descriptor_does_not_reach_the_child(census) -> None:
    """close_fds=True closes a stray inheritable descriptor in the child; the
    adjusted spawn, which drops close_fds, must give the child the same."""
    r, w = os.pipe()
    os.set_inheritable(w, True)
    probe = f"import os, sys\ntry:\n    os.fstat({w})\nexcept OSError:\n    sys.exit(0)\nsys.exit(3)\n"
    try:
        out = subprocess.run([sys.executable, "-c", probe])
    finally:
        os.close(r)
        os.close(w)
    assert out.returncode == 0, "the child inherited a descriptor close_fds=True would have closed"
    assert census.spawn_paths == {"posix_spawn": 1}, dict(census.fork_reasons)
    assert sum(census.inheritable_sites.values()) == 1


def test_a_spawn_that_still_forks_keeps_every_argument_it_was_given(census) -> None:
    """An arbitrary preexec_fn keeps the fork path, so close_fds must stay True:
    an inheritable descriptor the caller did not pass is still closed in the child."""
    r, w = os.pipe()
    os.set_inheritable(w, True)
    probe = f"import os, sys\ntry:\n    os.fstat({w})\nexcept OSError:\n    sys.exit(0)\nsys.exit(3)\n"
    try:
        out = subprocess.run([sys.executable, "-c", probe], preexec_fn=lambda: None)
    finally:
        os.close(r)
        os.close(w)
    assert out.returncode == 0, "the child inherited a descriptor close_fds=True should have closed"
    assert census.fork_reasons == {"preexec_fn": 1}


def test_census_records_a_fork_when_the_adjustment_is_off(census, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ps, "ADJUST", False)
    subprocess.run(["git", "--version"], cwd=tmp_path, capture_output=True, check=True)
    assert census.spawn_paths == {"fork": 1}
    assert census.fork_reasons == {"bare executable": 1}
    assert any("test_posix_spawn_adapter.py" in site for site in census.fork_sites["bare executable"])


def _send_pid(conn) -> None:
    conn.send(os.getpid())
    conn.close()


@needs_adjust
def test_multiprocessing_spawn_runs_through_posix_spawn(census) -> None:
    """The spawn start method reaches fork_exec directly, never Popen; the
    child must still start, receive its pipe and report back."""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_send_pid, args=(child,))
    proc.start()
    child.close()
    try:
        assert parent.poll(60), "the spawned child never reported"
        assert parent.recv() == proc.pid
    finally:
        proc.join(60)
    assert proc.exitcode == 0
    assert census.spawn_paths.get("fork", 0) == 0
    assert census.spawn_paths.get("posix_spawn", 0) >= 1


def test_unenterable_cwd_still_raises(census, tmp_path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        with pytest.raises(PermissionError):
            subprocess.run([sys.executable, "-c", "pass"], cwd=locked)
    finally:
        locked.chmod(0o700)


def test_explicit_executable_keeps_the_callers_argv0_under_cwd(census, tmp_path) -> None:
    out = subprocess.run(
        ["callers-name", "-c", 'echo "$0"'], executable="/bin/sh", cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "callers-name"
    assert census.fork_reasons == ({"cwd": 1} if ps.ADJUST else {"close_fds": 1})


def test_session_request_forks_when_the_adjustment_is_off(census, monkeypatch) -> None:
    monkeypatch.setattr(ps, "ADJUST", False)
    out = subprocess.run(_PRINT_SESSION, capture_output=True, text=True, check=True, start_new_session=True)
    assert out.stdout.split() == ["True", "True"]
    assert census.fork_reasons == {"close_fds": 1}
