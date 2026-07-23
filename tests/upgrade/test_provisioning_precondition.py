# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P4.0b (nexus-6nmrc): provisioning as a precondition leg.

The guided-upgrade stage-2 job — stand up (or gate) the service stack and
confirm it is genuinely usable — is ACQUISITION, the same class as
package/engine acquisition, not a data rung. P3's process axis only
CYCLES an existing supervisor; without this leg ``nx upgrade`` on a
Chroma-only install has no target to migrate into, and the RDR's
"fresh-or-ancient converges via nx upgrade alone" criterion fails.

Statelessness holds: the verdict is re-derived from ON-DISK evidence
(pg_credentials presence + the legacy-footprint gate), never recorded.
The converge reuses ``establish_verified_service`` verbatim (provision →
health-gate → version-pin → discoverability-gate).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pytest

from nexus.upgrade_ladder.preconditions import (
    check_preconditions,
    converge_preconditions,
)


@dataclass
class _EngineStatus:
    applicable: bool = True
    converged: bool = True
    reason: str | None = None


@dataclass
class _Readiness:
    ready: bool = True
    service_url: str = "http://127.0.0.1:8080"
    reason: str = ""


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


# ── the axis exists and is ordered before the data rungs ────────────────────


def test_provisioning_is_a_reported_axis(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: True,
        _footprint_fn=lambda: False,
        **_kwargs(),
    )
    assert [r.name for r in reports] == [
        "package", "engine", "provisioning", "process", "plugin-lockstep",
    ]


def test_provisioned_install_is_current(tmp_path: pathlib.Path) -> None:
    reports = check_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: True,
        _footprint_fn=lambda: True,  # footprint present but service exists already
        **_kwargs(),
    )
    assert _by(reports)["provisioning"].current is True


def test_chroma_only_install_needs_provisioning(tmp_path: pathlib.Path) -> None:
    """THE gap this leg closes: a legacy Chroma footprint with no service —
    nx upgrade has nothing to migrate INTO until this converges."""
    report = _by(
        check_preconditions(
            config_dir=tmp_path,
            _provisioned_fn=lambda: False,
            _footprint_fn=lambda: True,
            **_kwargs(),
        )
    )["provisioning"]
    assert report.applicable is True
    assert report.current is False
    assert "footprint" in report.detail.lower() or "migrate" in report.detail.lower()


def test_fresh_install_with_no_footprint_is_not_applicable(
    tmp_path: pathlib.Path,
) -> None:
    """nexus-ltix8: a fresh user with an empty footprint must NOT be
    provisioned by the upgrade path — nothing to migrate, no service to
    stand up. N/A, not pending."""
    report = _by(
        check_preconditions(
            config_dir=tmp_path,
            _provisioned_fn=lambda: False,
            _footprint_fn=lambda: False,
            **_kwargs(),
        )
    )["provisioning"]
    assert report.applicable is False
    assert report.current is True  # nothing to do


def test_verdict_is_derived_from_on_disk_evidence_only(
    tmp_path: pathlib.Path,
) -> None:
    """Stateless: both inputs are file-level reads (pg_credentials presence,
    the census's footprint gate) — re-derived per call, never recorded."""
    calls = {"prov": 0, "foot": 0}

    def _prov() -> bool:
        calls["prov"] += 1
        return False

    def _foot() -> bool:
        calls["foot"] += 1
        return True

    for _ in range(2):
        check_preconditions(
            config_dir=tmp_path, _provisioned_fn=_prov, _footprint_fn=_foot, **_kwargs()
        )
    assert calls == {"prov": 2, "foot": 2}  # fresh derivation every call


# ── converge ─────────────────────────────────────────────────────────────────


def test_converge_provisions_when_a_footprint_needs_a_target(
    tmp_path: pathlib.Path,
) -> None:
    established = {"n": 0}

    def _establish() -> _Readiness:
        established["n"] += 1
        return _Readiness(ready=True)

    reports = converge_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: False,
        _footprint_fn=lambda: True,
        _establish_fn=_establish,
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    assert established["n"] == 1
    report = _by(reports)["provisioning"]
    assert report.current is True
    assert report.actions and "127.0.0.1:8080" in report.actions[0]


def test_converge_reports_not_ready_honestly(tmp_path: pathlib.Path) -> None:
    """No silent fallback: a service that came up but failed its gates is
    reported not-current with the reason — the ladder walk then has no
    verified target and the substrate rung stays pending."""
    reports = converge_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: False,
        _footprint_fn=lambda: True,
        _establish_fn=lambda: _Readiness(ready=False, reason="health gate timed out"),
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    report = _by(reports)["provisioning"]
    assert report.current is False
    assert "health gate timed out" in report.detail


def test_converge_never_provisions_an_empty_footprint(
    tmp_path: pathlib.Path,
) -> None:
    """nexus-ltix8 pin: the fresh-user no-op must survive — an empty
    footprint never stands up a service."""
    established = {"n": 0}
    converge_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: False,
        _footprint_fn=lambda: False,
        _establish_fn=lambda: established.__setitem__("n", established["n"] + 1),
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    assert established["n"] == 0


def test_converge_skips_an_already_provisioned_install(
    tmp_path: pathlib.Path,
) -> None:
    established = {"n": 0}
    converge_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: True,
        _footprint_fn=lambda: True,
        _establish_fn=lambda: established.__setitem__("n", established["n"] + 1),
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    assert established["n"] == 0  # detect-and-skip: nothing to acquire


def test_skip_t3_suppresses_provisioning(tmp_path: pathlib.Path) -> None:
    """--skip-t3 is "fast T2-only": standing up the whole service stack is
    exactly what it opts out of (verdict still reported)."""
    established = {"n": 0}
    reports = converge_preconditions(
        config_dir=tmp_path,
        allow_process_cycle=False,  # what --skip-t3 passes
        _provisioned_fn=lambda: False,
        _footprint_fn=lambda: True,
        _establish_fn=lambda: established.__setitem__("n", established["n"] + 1),
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    assert established["n"] == 0
    assert _by(reports)["provisioning"].current is False  # still reported


def test_provisioning_failure_is_reported_not_raised(
    tmp_path: pathlib.Path,
) -> None:
    def _boom() -> _Readiness:
        raise RuntimeError("could not start postgres")

    reports = converge_preconditions(
        config_dir=tmp_path,
        _provisioned_fn=lambda: False,
        _footprint_fn=lambda: True,
        _establish_fn=_boom,
        _engine_converge_fn=lambda: [],
        _cycle_fn=lambda: None,
        **_kwargs(),
    )
    report = _by(reports)["provisioning"]
    assert report.current is False
    assert "could not start postgres" in report.detail
