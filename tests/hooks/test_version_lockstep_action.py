# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 P1.4: ``version_lockstep_action.py`` detached-action tests.

The action is the fire-and-forget, stdlib-only worker dispatched by the
hook. It owns the editable gate and the marker write. Contract:

- EDITABLE GATE FIRST (CA-3): no uv-tool receipt -> dev/editable tree ->
  SKIP (never clobber). Inline re-implementation of init.py:52-68.
- NO-OP FAST PATH: if installed ``nx --version`` already == target ->
  write marker, do not upgrade.
- TWO-COMMAND SAFE ACTION (CA-2), in strict order:
    1. ``uv tool upgrade conexus``  (binary, extras-preserving)
    2. ``nx upgrade``               (migrations only)
  NEVER raw ``uv tool install`` / ``--force`` / ``--reinstall`` (the
  dominant hazard that strips the ``[local]`` extra).
- MARKER ON CONFIRMED SUCCESS ONLY: after both commands succeed, re-read
  ``nx --version``; write the marker only if it now equals the target.
  Any failure leaves the marker stale so the next session retries.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "version_lockstep_action.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("version_lockstep_action", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


@pytest.fixture()
def marker(tmp_path: Path, monkeypatch) -> Path:
    m = tmp_path / "nexus" / "cli_lockstep_marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(m))
    return m


def _wire(mod, monkeypatch, *, receipt: bool, installed_versions, run_results):
    """Install the standard set of seam patches.

    installed_versions: list popped left-to-right on each installed_nx_version()
    run_results: dict mapping the first two argv tokens (e.g. "uv tool") -> bool
                 and records the call order in the returned list.
    """
    monkeypatch.setattr(mod, "uv_receipt_present", lambda: receipt)

    versions = list(installed_versions)

    def fake_installed() -> str | None:
        return versions.pop(0) if versions else None

    monkeypatch.setattr(mod, "installed_nx_version", fake_installed)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 0) -> bool:
        calls.append(list(cmd))
        key = " ".join(cmd[:2])
        return run_results.get(key, True)

    monkeypatch.setattr(mod, "run_cmd", fake_run)
    return calls


class TestScriptPresence:
    def test_script_exists(self) -> None:
        assert SCRIPT.exists()


class TestEditableGate:
    def test_skip_when_no_receipt(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=False,
            installed_versions=["1.0.0"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert calls == [], "editable/dev tree must not shell any upgrade"
        assert not marker.exists(), "must not clobber/write marker in a dev tree"

    def test_uv_receipt_present_inline_logic(self, mod, monkeypatch, tmp_path) -> None:
        """uv_receipt_present mirrors init.py:_uv_receipt_path: receipt file
        present under `uv tool dir`/conexus -> True."""
        tool_dir = tmp_path / "tools"
        (tool_dir / "conexus").mkdir(parents=True)
        (tool_dir / "conexus" / "uv-receipt.toml").write_text("")
        monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/uv")

        def fake_run(args, **k):
            class R:
                stdout = str(tool_dir) + "\n"
                returncode = 0
            return R()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.uv_receipt_present() is True

    def test_uv_missing_means_no_receipt(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda c: None)
        assert mod.uv_receipt_present() is False


class TestNoOpFastPath:
    def test_already_matched_writes_marker_no_upgrade(
        self, mod, marker, monkeypatch
    ) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert calls == [], "no upgrade when already at target"
        assert marker.read_text().strip() == "9.9.9"


class TestTwoCommandOrdering:
    def test_upgrade_then_nx_upgrade_in_order(self, mod, marker, monkeypatch) -> None:
        # stale before, target after
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert len(calls) == 2
        assert calls[0][:4] == ["uv", "tool", "upgrade", "conexus"]
        assert calls[1][:2] == ["nx", "upgrade"]

    def test_never_raw_uv_tool_install(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        for cmd in calls:
            joined = " ".join(cmd)
            assert "uv tool install" not in joined
            assert "--force" not in cmd
            assert "--reinstall" not in cmd


class TestMarkerOnConfirmedSuccess:
    def test_marker_written_when_version_confirmed(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert marker.read_text().strip() == "9.9.9"

    def test_marker_creates_parent_dir(self, mod, marker, monkeypatch) -> None:
        assert not marker.parent.exists()
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert marker.parent.is_dir()


class TestFailureLeavesMarkerStale:
    def test_uv_upgrade_failure_no_marker(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"uv tool": False},
        )
        mod.main(["action", "9.9.9"])
        assert calls[0][:4] == ["uv", "tool", "upgrade", "conexus"]
        # nx upgrade must NOT run after uv failure
        assert len(calls) == 1
        assert not marker.exists()

    def test_nx_upgrade_failure_no_marker(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"nx upgrade": False},
        )
        mod.main(["action", "9.9.9"])
        assert len(calls) == 2
        assert not marker.exists()

    def test_version_still_mismatched_after_upgrade_no_marker(
        self, mod, marker, monkeypatch
    ) -> None:
        # both commands "succeed" but the installed version never reaches target
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "1.0.1"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert not marker.exists(), "marker only on confirmed match"

    def test_missing_target_arg_is_noop(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"], run_results={},
        )
        mod.main(["action"])  # no target version
        assert calls == []
        assert not marker.exists()

    def test_main_swallows_exceptions(self, mod, marker, monkeypatch) -> None:
        def boom() -> bool:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mod, "uv_receipt_present", boom)
        mod.main(["action", "9.9.9"])  # must not raise
        assert not marker.exists()


class TestVersionParsing:
    def test_installed_nx_version_parses_cli_output(self, mod, monkeypatch) -> None:
        def fake_run(args, **k):
            class R:
                stdout = "nx, version 5.7.0\n"
                returncode = 0
            return R()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/nx")
        assert mod.installed_nx_version() == "5.7.0"

    def test_installed_nx_version_none_when_absent(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda c: None)
        assert mod.installed_nx_version() is None
