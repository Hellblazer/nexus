# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P4.0b (nexus-6nmrc) → RDR-155 P4b re-ground.

The provisioning axis survives as a REPORT-ONLY precondition: the
legacy-footprint acquisition leg (census gate + establish_verified_service
converge) died with the migration machinery. Post-P4b contract:

- provisioned (pg_credentials / service_url on disk) → applicable, current;
- not provisioned → N/A (current, applicable=False) — acquisition is
  ``nx init``'s job; the upgrade path NEVER provisions;
- converge_preconditions never stands up a service stack for this axis.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from nexus.upgrade_ladder.preconditions import (
    check_preconditions,
    converge_preconditions,
)


@dataclass
class _EngineStatus:
    applicable: bool = True
    converged: bool = True
    reason: str | None = None


def _by(reports):
    return {r.name: r for r in reports}


def _kwargs(**over):
    base = dict(
        _engine_detect_fn=lambda: _EngineStatus(),
        _lease_fn=lambda: None,
        _installed_version_fn=lambda: "6.12.0",
    )
    base.update(over)
    return base


def test_provisioning_is_a_reported_axis(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: True,
        **_kwargs(),
    )
    assert [r.name for r in reports] == [
        "package", "engine", "provisioning", "process", "plugin-lockstep",
    ]


def test_provisioned_install_is_current(tmp_path: pathlib.Path) -> None:
    report = _by(
        check_preconditions(
            config_dir=tmp_path,
            _provisioned_fn=lambda: True,
            **_kwargs(),
        )
    )["provisioning"]
    assert report.applicable is True
    assert report.current is True


def test_unprovisioned_install_is_not_applicable(tmp_path: pathlib.Path) -> None:
    """Post-P4b: no footprint concept — an unprovisioned box is N/A (nx init
    owns acquisition), never a pending action for the upgrade path."""
    report = _by(
        check_preconditions(
            config_dir=tmp_path,
            _provisioned_fn=lambda: False,
            **_kwargs(),
        )
    )["provisioning"]
    assert report.applicable is False
    assert report.current is True
    assert "nx init" in report.detail


def test_verdict_is_derived_from_on_disk_evidence_only(
    tmp_path: pathlib.Path,
) -> None:
    """Stateless: the provisioned input is a file-level read (pg_credentials
    presence) — re-derived per call, never recorded."""
    calls = {"prov": 0}

    def _prov() -> bool:
        calls["prov"] += 1
        return False

    for _ in range(2):
        check_preconditions(config_dir=tmp_path, _provisioned_fn=_prov, **_kwargs())
    assert calls == {"prov": 2}  # fresh derivation every call


def test_converge_never_provisions(tmp_path: pathlib.Path) -> None:
    """converge_preconditions must not stand anything up for this axis —
    report-only in both provisioned states, no actions ever."""
    for provisioned in (True, False):
        reports = converge_preconditions(
            config_dir=tmp_path,
            _provisioned_fn=lambda p=provisioned: p,
            _engine_converge_fn=lambda: [],
            _cycle_fn=lambda: None,
            **_kwargs(),
        )
        report = _by(reports)["provisioning"]
        assert report.actions == ()
        assert report.current is True
