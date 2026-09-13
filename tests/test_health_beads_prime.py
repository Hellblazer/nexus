# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s "Beads PRIME.md (user-level)" row (nexus-cnzei.8).

Informational: returns ``[]`` when beads is not detected on this machine at
all, otherwise reports one of the four
:class:`~nexus.beads_prime.PrimeStatus` states. All of beads detection,
path resolution, and status classification are monkeypatched here — this
file never touches a real filesystem path outside ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus.beads_prime as bp
import nexus.health as h


def _patch(monkeypatch: pytest.MonkeyPatch, *, detected: bool, status: bp.PrimeStatus | None, path: Path) -> None:
    monkeypatch.setattr(bp, "beads_detected", lambda **_kw: (detected, "test"))
    monkeypatch.setattr(bp, "user_prime_path", lambda: path)
    if status is not None:
        monkeypatch.setattr(bp, "status", lambda _p: status)


class TestCheckBeadsPrime:
    def test_not_detected_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, detected=False, status=None, path=tmp_path / "PRIME.md")
        assert h._check_beads_prime() == []

    def test_managed_current_is_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, detected=True, status=bp.PrimeStatus.MANAGED_CURRENT, path=tmp_path / "PRIME.md")
        results = h._check_beads_prime()
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert "up to date" in r.detail

    def test_user_authored_is_ok_and_left_alone(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, detected=True, status=bp.PrimeStatus.USER_AUTHORED, path=tmp_path / "PRIME.md")
        results = h._check_beads_prime()
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert "user-authored" in r.detail
        assert "left alone" in r.detail

    def test_stale_is_soft_warn_with_upgrade_fix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, detected=True, status=bp.PrimeStatus.MANAGED_STALE, path=tmp_path / "PRIME.md")
        results = h._check_beads_prime()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert any("nx upgrade" in s for s in r.fix_suggestions)
        assert any("machine-wide" in s and "--no-beads-prime" in s for s in r.fix_suggestions)

    def test_absent_is_soft_warn_with_init_or_upgrade_fix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, detected=True, status=bp.PrimeStatus.ABSENT, path=tmp_path / "PRIME.md")
        results = h._check_beads_prime()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert any("nx init" in s and "nx upgrade" in s for s in r.fix_suggestions)
        assert any("machine-wide" in s and "beads_prime.manage" in s for s in r.fix_suggestions)

    def test_check_failure_degrades_to_soft_warn_never_crashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(bp, "beads_detected", _boom)
        results = h._check_beads_prime()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
