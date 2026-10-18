# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smart App Control detection (nexus-f9bgu.57, RDR-224).

With Smart App Control on, CodeIntegrity blocks the unsigned PostgreSQL bundle
and the unsigned Python that uv installs, and ``nx init`` died with a raw
``[WinError 4551]`` after the bundle download (T2
``nexus_rdr/224-smart-app-control-on-2026-10-14``). These tests pin the state
reader, the up-front refusal in provisioning and in ``nx init``, and the
``nx doctor`` row. The registry reader and the platform are seams, so every
Windows branch runs on any host.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import _smart_app_control as sac
from nexus import health
from nexus.db import pg_provision
from tests._module_seam import setattr_in
from nexus.db.pg_provision import (
    PgSmartAppControlError,
    provision,
    refuse_smart_app_control,
)


class _Tripwire(Exception):
    """Raised by anything the guard is supposed to run BEFORE."""


# ── The state reader ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, sac.OFF), (1, sac.ON), (2, sac.EVALUATION), (7, sac.UNKNOWN), (None, sac.ABSENT)],
)
def test_state_maps_the_policy_value(value: int | None, expected: str) -> None:
    assert sac.state(platform="win32", read_value=lambda: value) == expected


def test_state_is_not_windows_off_windows_and_never_reads() -> None:
    def _must_not_read() -> int:
        raise _Tripwire

    assert sac.state(platform="darwin", read_value=_must_not_read) == sac.NOT_WINDOWS


def test_state_is_unknown_when_the_read_raises() -> None:
    def _raises() -> int:
        raise OSError("access denied")

    assert sac.state(platform="win32", read_value=_raises) == sac.UNKNOWN


def test_the_remedy_names_the_install_guide_section() -> None:
    remedy = pg_provision.smart_app_control_remedy()
    assert "Smart App Control" in remedy
    assert "windows-install.md#smart-app-control" in remedy


# ── The provisioning guard ────────────────────────────────────────────────────


def test_refuse_raises_when_smart_app_control_is_on_and_blocks() -> None:
    with pytest.raises(PgSmartAppControlError):
        refuse_smart_app_control(read_state=lambda: sac.ON, probe=lambda: True)


def test_refuse_raises_when_on_and_the_probe_is_inconclusive() -> None:
    # The registry says on; a probe that could not run proves nothing against it.
    with pytest.raises(PgSmartAppControlError):
        refuse_smart_app_control(read_state=lambda: sac.ON, probe=lambda: None)


def test_refuse_is_silent_when_on_but_an_unsigned_binary_runs() -> None:
    # nexus-vv068: the registry said on while Windows blocked nothing (restored
    # guests, T2 nexus_rdr/224-sac-on-baseline-control-2026-10-17).
    refuse_smart_app_control(read_state=lambda: sac.ON, probe=lambda: False)


def test_probe_is_not_run_for_any_other_state() -> None:
    def _must_not_probe() -> bool:
        raise _Tripwire

    for state in (sac.OFF, sac.EVALUATION, sac.UNKNOWN, sac.ABSENT, sac.NOT_WINDOWS):
        refuse_smart_app_control(read_state=lambda s=state: s, probe=_must_not_probe)


# ── The enforcement probe ─────────────────────────────────────────────────────


def test_probe_is_not_windows_off_windows_and_never_launches() -> None:
    def _must_not_launch() -> None:
        raise _Tripwire

    assert sac.execution_blocked(platform="darwin", launch=_must_not_launch) is None


def _os_error_with_winerror(code: int) -> OSError:
    # OSError ignores its winerror argument off Windows; set the attribute so
    # the Windows branch runs on any host.
    exc = OSError(0, "blocked")
    exc.winerror = code  # type: ignore[attr-defined]
    return exc


def test_probe_reports_blocked_on_winerror_4551() -> None:
    def _launch() -> None:
        raise _os_error_with_winerror(4551)

    assert sac.execution_blocked(platform="win32", launch=_launch) is True


def test_probe_reports_not_blocked_when_the_binary_runs() -> None:
    assert sac.execution_blocked(platform="win32", launch=lambda: None) is False


def test_probe_is_inconclusive_on_any_other_failure() -> None:
    def _launch() -> None:
        raise _os_error_with_winerror(5)

    assert sac.execution_blocked(platform="win32", launch=_launch) is None


@pytest.mark.parametrize(
    "state", [sac.OFF, sac.EVALUATION, sac.UNKNOWN, sac.ABSENT, sac.NOT_WINDOWS]
)
def test_refuse_is_silent_for_every_other_state(state: str) -> None:
    # Non-vacuity for the test above: the guard keys on the state VALUE.
    refuse_smart_app_control(read_state=lambda: state)


def test_provision_refuses_before_touching_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pg_provision, "smart_app_control_state", lambda: sac.ON)
    monkeypatch.setattr(pg_provision, "_sac_execution_blocked", lambda: True)
    monkeypatch.setattr(
        pg_provision, "bootstrap_superuser", lambda: (_ for _ in ()).throw(_Tripwire())
    )
    monkeypatch.setattr(
        pg_provision, "discover_pg_binaries", lambda: (_ for _ in ()).throw(_Tripwire())
    )
    with pytest.raises(PgSmartAppControlError):
        provision(config_dir=tmp_path)


def test_init_refuses_before_the_bundle_download(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus.commands import init as init_cmd

    monkeypatch.setattr(pg_provision, "smart_app_control_state", lambda: sac.ON)
    monkeypatch.setattr(pg_provision, "_sac_execution_blocked", lambda: True)
    monkeypatch.setattr(
        init_cmd, "_select_bundled_pg", lambda *_a, **_k: (_ for _ in ()).throw(_Tripwire())
    )
    with pytest.raises(SystemExit) as exc:
        init_cmd._provision_postgres_step()
    assert exc.value.code == 1
    assert "Smart App Control" in capsys.readouterr().err


# ── The nx doctor row ─────────────────────────────────────────────────────────


def test_doctor_has_no_row_off_windows() -> None:
    assert health._check_smart_app_control(state=sac.NOT_WINDOWS, local=True) == []


def test_doctor_has_no_row_where_windows_has_no_smart_app_control() -> None:
    # Windows 10 and older Windows 11 builds write no policy value; a permanent
    # warning there would be noise (closure critique round 3).
    assert health._check_smart_app_control(state=sac.ABSENT, local=True) == []


def test_doctor_row_fails_in_local_mode_when_on() -> None:
    (row,) = health._check_smart_app_control(state=sac.ON, local=True)
    assert row.label == "Smart App Control"
    assert (row.ok, row.warn, row.fatal) == (False, False, True)
    assert row.fix_suggestions


def test_doctor_row_warns_in_cloud_mode_when_on() -> None:
    (row,) = health._check_smart_app_control(state=sac.ON, local=False)
    assert (row.ok, row.warn) == (False, True)


@pytest.mark.parametrize("state", [sac.EVALUATION, sac.UNKNOWN])
def test_doctor_row_warns_for_evaluation_and_unknown(state: str) -> None:
    (row,) = health._check_smart_app_control(state=state, local=True)
    assert (row.ok, row.warn) == (False, True)


def test_doctor_row_passes_when_off() -> None:
    (row,) = health._check_smart_app_control(state=sac.OFF, local=True)
    assert row.ok


def test_default_launcher_copies_runs_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The real launcher body, on any host: a stub "interpreter" is copied,
    # appended to, spawned, and its temp directory is gone afterwards.
    import tempfile

    stub = tmp_path / "python-stub"
    stub.write_bytes(b"MZ-stub")
    seen: list[tuple[str, bytes]] = []

    def _fake_run(argv: list[str], **_kw: object) -> None:
        seen.append((argv[0], Path(argv[0]).read_bytes()))

    setattr_in(monkeypatch, sac, "sys.executable", str(stub))
    monkeypatch.setattr(sac, "run_bounded", _fake_run)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert sac.execution_blocked(platform="win32") is False
    ((path, data),) = seen
    assert data.startswith(b"MZ-stub") and len(data) == len(b"MZ-stub") + 8
    assert not Path(path).exists()


@pytest.mark.parametrize("exc", [RuntimeError("boom"), TimeoutError("30s")])
def test_probe_is_inconclusive_on_a_crash_or_timeout(exc: Exception) -> None:
    # The dangerous direction: a crash must never count as "the binary ran".
    def _launch() -> None:
        raise exc

    assert sac.execution_blocked(platform="win32", launch=_launch) is None


# ── The backstop: a blocked spawn carries the remedy (nexus-cntvz) ────────────


def test_a_blocked_pg_spawn_raises_the_remedy_not_a_raw_winerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The probe can say "ran" while Windows blocks the real bundle (signed
    # interpreter, unobserved True branch). The first bundle binary then fails
    # with WinError 4551 after the download; that must name the cause.
    def _blocked(*_a: object, **_k: object) -> None:
        raise _os_error_with_winerror(4551)

    monkeypatch.setattr(pg_provision, "run_bounded", _blocked)
    monkeypatch.setattr(pg_provision, "refuse_root", lambda: None)
    with pytest.raises(PgSmartAppControlError) as exc:
        pg_provision._run(["initdb.exe", "--version"], timeout=5)
    assert "Smart App Control" in str(exc.value)


def test_another_spawn_failure_is_not_taken_for_smart_app_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_a: object, **_k: object) -> None:
        raise _os_error_with_winerror(2)

    monkeypatch.setattr(pg_provision, "run_bounded", _missing)
    monkeypatch.setattr(pg_provision, "refuse_root", lambda: None)
    with pytest.raises(OSError) as exc:
        pg_provision._run(["initdb.exe", "--version"], timeout=5)
    assert not isinstance(exc.value, PgSmartAppControlError)
