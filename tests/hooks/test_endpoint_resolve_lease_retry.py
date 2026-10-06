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
    lease.write_text("{}")

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
    }))
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
