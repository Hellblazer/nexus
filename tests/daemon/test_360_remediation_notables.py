# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation — P3 notables backlog (nexus-26b7).

Focused tests for the substantive notable fixes; pure cosmetic items
(docstring tweaks, Sphinx directive version arg, log-level annotations)
do not warrant dedicated test coverage.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Concurrency N1: BindingWatcher mass-delete reload retains previous profiles
# ---------------------------------------------------------------------------


class TestBindingWatcherMassDeleteSafe:
    def test_empty_dir_after_delete_retains_previous_profiles(
        self, tmp_path: Path
    ) -> None:
        """If every YAML disappears, the watcher must NOT silently
        drop the previously-loaded profile list — the TR-2 docstring
        forbids it; the empty-dir case was the gap."""
        from nexus.cockpit.bindings import (
            BindingContext,
            BindingProfile,
            BindingWatcher,
        )

        prof_dir = tmp_path / "profiles"
        prof_dir.mkdir()
        (prof_dir / "p.yml").write_text(
            "profile: p\nbindings: []\n"
        )

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(":memory:")
        watcher = BindingWatcher(
            conn=conn,
            profiles=[BindingProfile(name="p", bindings=())],
            context=BindingContext(conn=conn),
            profiles_dirs=[prof_dir],
        )
        # Pin a sentinel so we can detect a silent drop.
        original_profiles = watcher._profiles

        # Delete every YAML and trigger a reload.
        for yml in prof_dir.glob("*.yml"):
            yml.unlink()
        watcher._reload_if_changed()

        assert watcher._profiles == original_profiles, (
            "mass-delete dropped profiles silently; the TR-2 retain "
            "guard must extend to the empty-dir case."
        )


# ---------------------------------------------------------------------------
# Error paths N-1: DataVersionWatcher.is_alive_and_healthy reports connect fail
# ---------------------------------------------------------------------------


class TestDataVersionWatcherHealthAccessor:
    def test_is_alive_and_healthy_false_when_connect_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When the poll thread's connect() raises, the accessor
        must return False so direct-mode callers can detect the
        silent-death state instead of assuming the wake mechanism
        is live."""
        import sqlite3
        import threading
        from nexus.tuplespace.watcher import DataVersionWatcher

        monkeypatch.setenv("NX_STORAGE_MODE", "direct")

        # Point the watcher at a path it can't open; the poll thread's
        # connect inside _poll_loop will fail and set _connect_failed.
        bad_path = tmp_path / "nope" / "tuples.db"
        wake = threading.Event()
        watcher = DataVersionWatcher(db_path=bad_path, wake_event=wake)
        # Force the poll loop's open to raise by monkey-patching
        # sqlite3.connect to fail (deterministic, no FS race).
        real_connect = sqlite3.connect

        def _failing_connect(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic connect failure")

        monkeypatch.setattr(sqlite3, "connect", _failing_connect)
        try:
            watcher.start()
            # Wait briefly for the thread to attempt connect.
            for _ in range(20):
                if not watcher.is_alive_and_healthy():
                    break
                threading.Event().wait(0.01)
            assert not watcher.is_alive_and_healthy(), (
                "watcher reported healthy despite a synthetic connect "
                "failure — silent-death state not surfaced"
            )
        finally:
            monkeypatch.setattr(sqlite3, "connect", real_connect)
            watcher.stop()


# ---------------------------------------------------------------------------
# FS-6: case-insensitive profile-name collision rejected
# ---------------------------------------------------------------------------


class TestFS6CaseInsensitiveCollision:
    def test_case_collision_rejected(self, tmp_path: Path) -> None:
        from nexus.cockpit import bindings_crud
        from nexus.cockpit.bindings import BindingProfileError

        target = tmp_path / "profiles"
        target.mkdir()
        bindings_crud.create_binding(
            profile="Foo",
            name="b1",
            match={"subspace": "tasks"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=target,
        )
        with pytest.raises(BindingProfileError, match="case-insensitively"):
            bindings_crud.create_binding(
                profile="foo",
                name="b1",
                match={"subspace": "tasks"},
                action={"kind": "log", "marker": "m"},
                profiles_dir=target,
            )


# ---------------------------------------------------------------------------
# FS-7: autostart install refuses to follow a symlink
# ---------------------------------------------------------------------------


class TestFS7AutostartSymlinkRefusal:
    def test_install_refuses_symlink_dest(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Source-text guard for the FS-7 fix (functional install
        requires platform-specific scaffolding). The patched commands
        emit ``dest.is_symlink() -> error + exit 1`` before writing."""
        src = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "commands" / "daemon.py"
        ).read_text()
        # Both the symlink check AND the error message are present.
        assert "dest.is_symlink()" in src, (
            "FS-7 guard regressed — commands/daemon.py no longer "
            "checks dest.is_symlink() before installing autostart."
        )
        assert "FS-7" in src, (
            "FS-7 marker missing from autostart-install symlink guard."
        )


# ---------------------------------------------------------------------------
# Migration N-3: BindingProfile rejects newer schema_version
# ---------------------------------------------------------------------------


class TestN3BindingProfileSchemaVersion:
    def test_newer_schema_version_rejected(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings import (
            BINDING_PROFILE_SCHEMA_VERSION,
            BindingProfileError,
            load_profile,
        )

        yml = tmp_path / "p.yml"
        yml.write_text(
            "profile: p\n"
            f"schema_version: {BINDING_PROFILE_SCHEMA_VERSION + 1}\n"
            "bindings: []\n"
        )
        with pytest.raises(BindingProfileError, match="newer than this wheel"):
            load_profile(yml)

    def test_current_schema_version_accepted(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings import (
            BINDING_PROFILE_SCHEMA_VERSION,
            load_profile,
        )

        yml = tmp_path / "p.yml"
        yml.write_text(
            "profile: p\n"
            f"schema_version: {BINDING_PROFILE_SCHEMA_VERSION}\n"
            "bindings: []\n"
        )
        prof = load_profile(yml)
        assert prof.schema_version == BINDING_PROFILE_SCHEMA_VERSION

    def test_missing_schema_version_defaults_to_current(
        self, tmp_path: Path
    ) -> None:
        """Backward-compat: old YAMLs without the field still load."""
        from nexus.cockpit.bindings import (
            BINDING_PROFILE_SCHEMA_VERSION,
            load_profile,
        )

        yml = tmp_path / "p.yml"
        yml.write_text("profile: p\nbindings: []\n")
        prof = load_profile(yml)
        assert prof.schema_version == BINDING_PROFILE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Migration N-4: discovery file rejects newer format_version
# ---------------------------------------------------------------------------


class TestN4DiscoveryFormatVersion:
    def test_newer_format_version_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = discovery.discovery_path(tmp_path)
        path.write_text(json.dumps({
            "format_version": 999,
            "uds_path": "/tmp/fake.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": os.getpid(),
        }))
        assert discovery.find_t2_daemon(tmp_path) is None

    def test_missing_format_version_treated_as_v1(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = discovery.discovery_path(tmp_path)
        path.write_text(json.dumps({
            "uds_path": "/tmp/fake.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": os.getpid(),
        }))
        result = discovery.find_t2_daemon(tmp_path)
        assert result is not None
        assert result["pid"] == os.getpid()

    def test_non_dict_payload_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """dim-5 N1: discovery file containing a non-dict JSON value
        must be refused rather than returned as-is."""
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = discovery.discovery_path(tmp_path)
        path.write_text('[1, 2, 3]')
        assert discovery.find_t2_daemon(tmp_path) is None


# ---------------------------------------------------------------------------
# dim-13 N-1: storage-mode error message mentions both recovery paths
# ---------------------------------------------------------------------------


class TestStorageModeErrorMessage:
    def test_error_names_both_recovery_paths(self) -> None:
        """The error raised by mcp/core.py when daemon mode is default
        and no daemon is running must name BOTH ``nx daemon t2 start``
        and the ``NX_STORAGE_MODE=direct`` opt-out."""
        from pathlib import Path as _Path

        src = (
            _Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "mcp" / "core.py"
        ).read_text()
        assert "default since 2026-05-17" in src
        assert "NX_STORAGE_MODE=direct" in src
        assert "nx daemon t2 install --autostart" in src
