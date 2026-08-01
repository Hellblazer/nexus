# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bawvu: doctor's process-freshness check (health.py `_check_process_skew`)
must never let a probe failure VANISH the row.

Before this fix, `_check_process_skew` caught every exception from
`detect_stale_processes()` / `install_source()` and returned `[]` — a bare
empty list is indistinguishable from "checked, found nothing stale", so a
probe failure and a genuinely healthy machine rendered identically in `nx
doctor` output. The oyo2g stall diagnosis depends on this row existing (even
as a loud could-not-check) precisely so a probe outage is never silently
read as "all clear".
"""
from __future__ import annotations

import pytest

import nexus.upgrade_finish as upgrade_finish_module
from nexus.health import _check_process_skew
from nexus.upgrade_finish import SkewReport, StaleProcess


class TestProcessFreshnessCheck:
    def test_probe_raises_yields_soft_warn_naming_the_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A probe exception must produce a visible ⚠ row that names the
        error, not an empty list."""

        def _boom() -> SkewReport:
            raise RuntimeError("proc enumeration failed: permission denied")

        monkeypatch.setattr(upgrade_finish_module, "detect_stale_processes", _boom)

        results = _check_process_skew()

        assert len(results) == 1, "probe failure must not vanish the row"
        r = results[0]
        assert r.label == "Process freshness"
        assert r.ok is False
        assert r.warn is True, "must be a soft warn (⚠), not a hard fail or silent pass"
        assert not r.fatal
        assert "could not check" in r.detail.lower()
        assert "permission denied" in r.detail

    def test_healthy_machine_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No stale processes: the pre-existing green-path behavior must be
        untouched by the fix."""
        report = SkewReport(installed_version="7.0.0", install_mtime=0.0, stale=[])
        monkeypatch.setattr(
            upgrade_finish_module, "detect_stale_processes", lambda: report
        )
        monkeypatch.setattr(
            upgrade_finish_module, "install_source", lambda: "uv tool — pinned"
        )

        results = _check_process_skew()

        assert len(results) == 1
        r = results[0]
        assert r.label == "Process freshness"
        assert r.ok is True
        assert r.warn is False
        assert "7.0.0" in r.detail

    def test_stale_processes_found_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale processes detected: the pre-existing soft-warn behavior for
        an actual finding must be untouched by the fix."""
        stale = [StaleProcess(pid=1234, kind="aspect-worker", command="nx", age_s=999)]
        report = SkewReport(installed_version="7.0.0", install_mtime=0.0, stale=stale)
        monkeypatch.setattr(
            upgrade_finish_module, "detect_stale_processes", lambda: report
        )
        monkeypatch.setattr(
            upgrade_finish_module, "install_source", lambda: "uv tool — pinned"
        )

        results = _check_process_skew()

        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert "pid 1234" in r.detail
