# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): the local poison check reads the engine's chash constraint state before it scans.

Validated width CHECKs on both poison tables answer the scan's question for every tenant, so the scan is skipped. The
debt tables carry no CHECK, so their probe still runs and its row still shows. Any answer other than True scans as
before and says nothing about the skipped read.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nexus.health as h
from nexus.db import chash_tables
from tests.test_health_service_checks import _make_creds_file, _psql_runner_ok

_DEBT_LABEL = "Chash legacy debt"


def _recording_runner(*, debt: int | None, poison: int = 0) -> tuple[list[str], object]:
    """A diag psql seam that records each statement and answers poison and debt statements separately."""
    poison_sql = set(chash_tables.chash_conformance_statements())
    debt_sql = set(chash_tables.debt_chash_conformance_statements())
    seen: list[str] = []

    def runner(argv: list[str], env: dict) -> subprocess.CompletedProcess:
        joined = " ".join(argv)
        seen.append(joined)
        if any(s in joined for s in debt_sql):
            if debt is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="debt probe failed")
            return subprocess.CompletedProcess(argv, 0, stdout=f"{debt}\n", stderr="")
        val = poison if any(s in joined for s in poison_sql) else 0
        return subprocess.CompletedProcess(argv, 0, stdout=f"{val}\n", stderr="")

    return seen, runner


def _run(tmp_path: Path, validated: bool | None, *, debt: int | None = 0, poison: int = 0) -> tuple[list[str], list[h.HealthResult]]:
    seen, runner = _recording_runner(debt=debt, poison=poison)
    results = h._check_migration_state(
        creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=_psql_runner_ok(7),
        diag_runner=runner, constraints_validated=lambda: validated,
    )
    return seen, results


def _ran_any(seen: list[str], stmts: tuple[str, ...]) -> bool:
    return any(s in call for call in seen for s in stmts)


def test_validated_skips_the_poison_scan_and_keeps_the_debt_probe(tmp_path: Path) -> None:
    seen, results = _run(tmp_path, True, debt=0)
    assert not _ran_any(seen, chash_tables.chash_conformance_statements()), "the poison scan ran despite validated CHECKs"
    assert _ran_any(seen, chash_tables.debt_chash_conformance_statements()), "the debt probe was dropped"
    assert [r.label for r in results] == ["Schema migrations"] and results[0].ok is True


def test_validated_still_reports_legacy_debt(tmp_path: Path) -> None:
    _seen, results = _run(tmp_path, True, debt=2)
    debt = [r for r in results if r.label == _DEBT_LABEL]
    assert len(debt) == 1 and debt[0].warn is True
    assert debt[0].detail.startswith(f"{2 * len(chash_tables.DEBT_CHASH_TABLES)} hex-shaped"), debt[0].detail


def test_validated_with_a_failed_debt_probe_says_unknown(tmp_path: Path) -> None:
    _seen, results = _run(tmp_path, True, debt=None)
    debt = [r for r in results if r.label == _DEBT_LABEL]
    assert len(debt) == 1 and "UNKNOWN" in debt[0].detail


@pytest.mark.parametrize("validated", [False, None])
def test_anything_but_validated_scans_as_before(tmp_path: Path, validated: bool | None) -> None:
    seen, results = _run(tmp_path, validated, poison=1)
    assert _ran_any(seen, chash_tables.chash_conformance_statements())
    poison = [r for r in results if r.label == chash_tables.CHASH_CONFORMANCE_LABEL]
    assert len(poison) == 1 and poison[0].detail.startswith(f"{len(chash_tables.POISON_CHASH_TABLES)} chunk row(s)")
    assert not any("constraint state" in r.detail or "validated" in r.detail for r in results), "the fallback names the skipped read"


@pytest.mark.parametrize("validated", [False, None])
def test_a_clean_scan_on_the_fallback_adds_no_row(tmp_path: Path, validated: bool | None) -> None:
    _seen, results = _run(tmp_path, validated)
    assert [r.label for r in results] == ["Schema migrations"] and results[0].ok is True


def test_the_engine_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> object:
        raise RuntimeError("engine gone")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", broken)
    assert h._chash_constraints_validated_by_engine() is None
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: object())
    assert h._chash_constraints_validated_by_engine() is None
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader",
                        lambda: type("C", (), {"chash_constraints_validated": lambda self: True})())
    assert h._chash_constraints_validated_by_engine() is True


def _default_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, local: bool) -> list[str]:
    """Point the default credentials at tmp_path and record whether the engine is asked."""
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, matches the check under test

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _make_creds_file(cfg).rename(cfg / CREDENTIALS_FILENAME)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: cfg)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: local)
    asked: list[str] = []

    def engine() -> bool:
        asked.append("engine")
        return True

    monkeypatch.setattr(h, "_chash_constraints_validated_by_engine", engine)
    return asked


def _run_default(**kw: object) -> list[str]:
    seen, runner = _recording_runner(debt=0)
    h._check_migration_state(psql_bin=Path("/fake/psql"), psql_runner=_psql_runner_ok(7), diag_runner=runner, **kw)
    return seen


def test_a_local_install_with_default_credentials_asks_the_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _default_creds(tmp_path, monkeypatch, local=True)
    seen = _run_default()
    assert asked == ["engine"] and not _ran_any(seen, chash_tables.chash_conformance_statements())


def test_a_managed_config_beside_local_credentials_scans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _default_creds(tmp_path, monkeypatch, local=False)
    seen = _run_default()
    assert asked == [] and _ran_any(seen, chash_tables.chash_conformance_statements())


def test_explicit_credentials_scan_without_asking_the_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """upgrade_finish's gate passes its own creds_path: the engine the ambient config names need not be that store."""
    asked = _default_creds(tmp_path, monkeypatch, local=True)
    seen = _run_default(creds_path=_make_creds_file(tmp_path))
    assert asked == [] and _ran_any(seen, chash_tables.chash_conformance_statements())
