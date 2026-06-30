# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 1 (Gap 2) — first-run must not launch the T2 daemon in service mode.

The MCP first-run path (``ensure_installed_and_running``) unconditionally ran
``nx daemon t2 ensure-running`` on every server boot. The daemon opens the local
``.db`` read-write with ``run_migrations=True`` and stamps ``_nexus_version``
forward — the PRIMARY mutation source that broke the downgrade guarantee in the
6.0.0 dogfood (mem: bead nexus-gq5f9). In service mode the SQLite tier is a
migration SOURCE only, so the daemon must not be launched at all.

Companion to ``tests/db/test_rdr176_non_mutation.py`` (the bootstrap_schema guard).
"""
from __future__ import annotations

import pytest

from nexus.mcp import _first_run


@pytest.fixture
def _stub_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``ensure_installed_and_running`` reach the daemon-launch decision
    cheaply: nx binary present, OS unit already installed (skip install), banner
    a no-op. Leaves only the storage-backend branch to drive the outcome.
    """
    monkeypatch.setattr(_first_run, "_find_nx_binary", lambda: "/usr/bin/nx")
    monkeypatch.setattr(_first_run, "_os_unit_exists", lambda: True)
    monkeypatch.setattr(_first_run, "_installed_unit_path", lambda: None)
    monkeypatch.setattr(_first_run, "maybe_banner", lambda *a, **k: None)


def _record_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(cmd, *a, **k):  # noqa: ANN001, ANN002, ANN003
        calls.append(list(cmd))
        raise AssertionError("subprocess.run should not be reached in this test path")

    monkeypatch.setattr(_first_run.subprocess, "run", _fake_run)
    return calls


def test_service_mode_skips_t2_daemon_launch(
    monkeypatch: pytest.MonkeyPatch, _stub_install: None
) -> None:
    """In service mode, no ``nx daemon t2 ensure-running`` subprocess is spawned."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    calls = _record_subprocess(monkeypatch)

    _first_run.ensure_installed_and_running()  # must not raise, must not spawn

    assert calls == []


def test_sqlite_mode_launches_t2_daemon(
    monkeypatch: pytest.MonkeyPatch, _stub_install: None
) -> None:
    """In sqlite mode the daemon IS launched (the early-return must be mode-gated,
    not an unconditional skip)."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    spawned: list[list[str]] = []

    def _fake_run(cmd, *a, **k):  # noqa: ANN001, ANN002, ANN003
        spawned.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(_first_run.subprocess, "run", _fake_run)

    _first_run.ensure_installed_and_running()

    assert spawned == [["/usr/bin/nx", "daemon", "t2", "ensure-running", "--quiet"]]
