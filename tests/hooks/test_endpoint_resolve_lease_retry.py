# SPDX-License-Identifier: AGPL-3.0-or-later
"""``conexus/hooks/scripts/_endpoint_resolve.py`` reads the supervisor lease with
the same bounded Windows sharing-violation retry ``ServiceRegistry`` uses
(RDR-224, nexus-f9bgu.44).

On Windows a lease read can miss while the supervisor replaces the file
(Python opens without ``FILE_SHARE_DELETE``, so a reader's open raises
``PermissionError`` for the instant of the replace). The plugin script is
stdlib-only and cannot import ``ServiceRegistry``, so the retry is restated
there, and these tests pin the restatement to the real one: same constants,
same pause sequence under the same injected clock, same give-up point.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from nexus.daemon import service_registry as sr

PLUGIN_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "_endpoint_resolve.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_lease_retry", PLUGIN_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


class _Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class _AlwaysBusy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        raise PermissionError(13, "sharing violation")


def test_the_retry_constants_are_the_registrys_own() -> None:
    mirror = _load()
    assert mirror._WINDOWS_SHARING_RETRY_BUDGET_S == sr._WINDOWS_SHARING_RETRY_BUDGET_S
    assert mirror._WINDOWS_SHARING_RETRY_FIRST_S == sr._WINDOWS_SHARING_RETRY_FIRST_S
    assert mirror._WINDOWS_SHARING_RETRY_MAX_S == sr._WINDOWS_SHARING_RETRY_MAX_S


def test_the_pause_sequence_and_give_up_point_match_the_registry(tmp_path: Path) -> None:
    """Both retry loops run against a read that never succeeds, on a clock that
    only advances when they sleep: the sequence of pauses, and so the moment
    each gives up, must be identical."""
    mirror = _load()
    lease = tmp_path / "lease"
    lease.write_text("{}", encoding="utf-8")

    real_clock = _Clock()
    registry = sr.ServiceRegistry(
        dir=tmp_path, tier="storage_service", platform="win32",
        monotonic=real_clock.monotonic, sleep=real_clock.sleep,
    )
    busy = _AlwaysBusy()
    with pytest.raises(PermissionError):
        registry._retry_sharing_violation(busy)

    mirror_clock = _Clock()
    mirror_busy = _AlwaysBusy()
    lease_path = tmp_path / "lease"

    def read_once(p: Path) -> str:  # stands in for Path.read_text
        return mirror_busy()

    with pytest.raises(PermissionError):
        mirror.read_lease_text(
            lease_path, platform="win32", read=read_once,
            sleep=mirror_clock.sleep, monotonic=mirror_clock.monotonic,
        )

    assert mirror_clock.sleeps == real_clock.sleeps
    assert mirror_busy.calls == busy.calls
    # Non-vacuity: it retried, it did not give up on the first miss, and it
    # stopped at the budget rather than looping forever.
    assert busy.calls > 10
    assert sum(real_clock.sleeps) == pytest.approx(sr._WINDOWS_SHARING_RETRY_BUDGET_S, abs=0.011)


def test_a_read_that_succeeds_after_a_few_sharing_violations_returns_the_text(tmp_path: Path) -> None:
    mirror = _load()
    clock = _Clock()
    attempts = {"n": 0}

    def read_once(p: Path) -> str:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise PermissionError(13, "sharing violation")
        return '{"ok": true}'

    text = mirror.read_lease_text(
        tmp_path / "lease", platform="win32", read=read_once,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert text == '{"ok": true}'
    assert attempts["n"] == 4 and len(clock.sleeps) == 3


def test_off_windows_a_permission_error_is_not_retried(tmp_path: Path) -> None:
    """A PermissionError on POSIX is a real permission problem and fails at
    once, exactly as before the retry existed."""
    mirror = _load()
    clock = _Clock()
    busy = _AlwaysBusy()
    with pytest.raises(PermissionError):
        mirror.read_lease_text(
            tmp_path / "lease", platform="linux", read=lambda p: busy(),
            sleep=clock.sleep, monotonic=clock.monotonic,
        )
    assert busy.calls == 1 and clock.sleeps == []


def _live_lease(path: Path) -> None:
    path.write_text(json.dumps({
        "status": "live", "heartbeat_epoch": time.time(), "ttl": 60.0,
        "endpoint": {"host": "127.0.0.1", "port": 4242, "token": "t0k3n"},
    }), encoding="utf-8")
    path.chmod(0o600)


class _FlakyRead:
    """``Path.read_text`` for the lease file that raises a sharing violation
    the first *fail* times, as a reader racing a replace does on Windows.
    Installed with :meth:`install`, because a callable instance is not bound
    as a method the way a function is."""

    def __init__(self, real, lease: Path, fail: int) -> None:
        self.real, self.lease, self.fail, self.calls = real, lease, fail, 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_FlakyRead":
        me = self

        def read_text(path: Path, *a, **k) -> str:
            if path == me.lease:
                me.calls += 1
                if me.calls <= me.fail:
                    raise PermissionError(13, "sharing violation")
            return me.real(path, *a, **k)

        monkeypatch.setattr(Path, "read_text", read_text)
        return self


@pytest.fixture
def windows_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mirror = _load()
    monkeypatch.setattr(mirror, "service_identity", lambda **_k: "501")
    monkeypatch.setattr(mirror, "_is_windows", lambda platform=None: True)
    lease = tmp_path / "storage_service_addr.501"
    _live_lease(lease)
    return mirror, lease


def test_the_endpoint_read_survives_a_sharing_violation_on_windows(
    windows_mirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, lease = windows_mirror
    flaky = _FlakyRead(Path.read_text, lease, fail=3).install(monkeypatch)
    got = mirror.read_storage_service_lease(tmp_path)
    assert got == {"host": "127.0.0.1", "port": 4242, "token": "t0k3n"}
    assert flaky.calls == 4


def test_the_token_read_survives_a_sharing_violation_on_windows(
    windows_mirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, lease = windows_mirror
    monkeypatch.setattr(mirror, "owner_only_problem", lambda path, mode: None)
    flaky = _FlakyRead(Path.read_text, lease, fail=3).install(monkeypatch)
    assert mirror.read_local_supervisor_token(tmp_path) == "t0k3n"
    assert flaky.calls == 4


def test_off_windows_the_endpoint_read_still_fails_open_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = _load()
    monkeypatch.setattr(mirror, "service_identity", lambda **_k: "501")
    monkeypatch.setattr(mirror, "_is_windows", lambda platform=None: False)
    lease = tmp_path / "storage_service_addr.501"
    _live_lease(lease)
    flaky = _FlakyRead(Path.read_text, lease, fail=1).install(monkeypatch)
    assert mirror.read_storage_service_lease(tmp_path) is None
    assert flaky.calls == 1


# -- the owner-liveness probe the usable-service check uses for a lease past its TTL ------------------
#
# ``_endpoint_resolve.pid_alive`` restates ``service_registry.pid_alive`` (this file cannot import it),
# and the two are held together the way the retry above is: same constants, and the same answers over
# the same inputs, the Windows arm through one fake kernel32.


def test_the_grace_constants_are_the_registrys() -> None:
    mod = _load()
    assert mod.STALE_LEASE_GRACE_MAX_TTL_MULTIPLE == sr.STALE_LEASE_GRACE_MAX_TTL_MULTIPLE
    assert mod._GRACE_LOOPBACK_HOSTS == sr._GRACE_PROBE_LOOPBACK_HOSTS
    assert mod._PROCESS_QUERY_LIMITED_INFORMATION == sr.PROCESS_QUERY_LIMITED_INFORMATION
    assert mod._SYNCHRONIZE == sr.SYNCHRONIZE
    assert mod._ERROR_INVALID_PARAMETER == sr.ERROR_INVALID_PARAMETER
    assert mod._WAIT_OBJECT_0 == sr.WAIT_OBJECT_0
    assert mod._MAX_WINDOWS_PID == sr._MAX_WINDOWS_PID


def test_pid_alive_answers_as_the_registry_does_on_this_host() -> None:
    import subprocess

    mod = _load()
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 -- our own interpreter
    child.wait()
    import os

    for pid in (os.getpid(), child.pid, 0, -5):
        assert mod.pid_alive(pid) == sr.pid_alive(pid), pid
    assert mod.pid_alive(os.getpid()) is True and mod.pid_alive(child.pid) is False
    # The one difference: a number no pid can be (a corrupt lease) is not alive here, where the registry's os.kill raises.
    assert mod.pid_alive(2**40) is False


class _FakeKernel:
    def __init__(self, handle: int | None, error: int, wait: int) -> None:
        self.handle, self.error, self.wait = handle, error, wait
        self.closed: list[int] = []

    def open_process(self, pid: int) -> tuple[int | None, int]:
        return (self.handle, self.error)

    def wait_zero(self, handle: int) -> int:
        return self.wait

    def close(self, handle: int) -> None:
        self.closed.append(handle)


@pytest.mark.parametrize(
    ("handle", "error", "wait"),
    [
        (None, 5, 0), (None, 87, 0), (None, 6, 0), (None, 0, 0),
        (7, 0, 0x0), (7, 0, 0x102), (7, 0, 0xFFFFFFFF),
    ],
    ids=["denied", "invalid-parameter", "other-error", "no-error", "exited", "running", "wait-failed"],
)
@pytest.mark.parametrize("pid", [1234, 0xFFFFFFFF, 0x100000000], ids=["pid", "max", "over-max"])
def test_the_windows_arm_answers_as_the_registry_does(handle: int | None, error: int, wait: int, pid: int) -> None:
    mod = _load()
    ours, theirs = _FakeKernel(handle, error, wait), _FakeKernel(handle, error, wait)
    assert mod.pid_alive(pid, platform="win32", win_api=ours) == sr.pid_alive(pid, platform="win32", win_api=theirs)
    assert ours.closed == theirs.closed, "a handle that was opened is closed, and only that"


@pytest.mark.parametrize(
    ("raised", "alive"),
    [
        (ProcessLookupError(3, "no such process"), False),
        (PermissionError(1, "not permitted"), True),
        (OSError(3, "ESRCH as a bare OSError"), False),
        (OSError(22, "an errno that proves nothing"), True),
        (None, True),
    ],
    ids=["gone", "other-user", "esrch", "ambiguous", "signalled"],
)
def test_the_posix_arm_reads_ambiguity_as_alive_as_the_registry_does(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException | None, alive: bool,
) -> None:
    """The branches a real pid cannot reach without another user's process."""
    from tests._module_seam import setattr_in

    def kill(pid: int, sig: int) -> None:
        assert sig == 0, "a probe, never a signal"
        if raised is not None:
            raise raised

    mod = _load()
    setattr_in(monkeypatch, mod, "os.kill", kill)
    setattr_in(monkeypatch, sr, "os.kill", kill)
    assert mod.pid_alive(4242, platform="linux") is alive
    assert sr.pid_alive(4242, platform="linux") is alive
