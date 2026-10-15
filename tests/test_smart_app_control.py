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
    [(0, sac.OFF), (1, sac.ON), (2, sac.EVALUATION), (7, sac.UNKNOWN), (None, sac.UNKNOWN)],
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


def test_refuse_raises_when_smart_app_control_is_on() -> None:
    with pytest.raises(PgSmartAppControlError):
        refuse_smart_app_control(read_state=lambda: sac.ON)


@pytest.mark.parametrize("state", [sac.OFF, sac.EVALUATION, sac.UNKNOWN, sac.NOT_WINDOWS])
def test_refuse_is_silent_for_every_other_state(state: str) -> None:
    # Non-vacuity for the test above: the guard keys on the state VALUE.
    refuse_smart_app_control(read_state=lambda: state)


def test_provision_refuses_before_touching_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pg_provision, "smart_app_control_state", lambda: sac.ON)
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
