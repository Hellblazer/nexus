# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-157 P4.1 (bead nexus-vwvv5.17): native-image binary launch.

The storage-service supervisor must exec the RDR-157 native binary when one is
present, falling back to ``java -jar`` only when it is not. Config reaches the
service entirely via the environment, so the two launch modes differ only in
argv. These unit tests pin: binary discovery (env override + well-known +
absent), the argv branch in ``_spawn_service``, the JAR-only schema-skew gate,
and ``start_storage_service`` preferring the binary.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.daemon.jar_lifecycle import well_known_binary_path
from nexus.daemon.storage_service_daemon import (
    StorageServiceStartError,
    StorageServiceSupervisor,
    _find_service_binary,
)

_CREDS = {
    "NX_DB_URL": "jdbc:postgresql://127.0.0.1:15432/nexus",
    "NX_DB_USER": "svc",
    "NX_DB_PASS": "pass",
    "NX_DB_ADMIN_URL": "jdbc:postgresql://127.0.0.1:15432/nexus",
    "NX_DB_ADMIN_USER": "admin",
    "NX_DB_ADMIN_PASS": "adminpass",
    "PG_PORT": "15432",
    "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
}


def _make_native_binary(d: Path) -> Path:
    p = d / "nexus-service"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return p


# ── _find_service_binary ───────────────────────────────────────────────────


def test_find_binary_env_override_present(tmp_path, monkeypatch):
    binary = _make_native_binary(tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(binary))
    assert _find_service_binary(tmp_path / "cfg") == binary


def test_find_binary_env_override_missing_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(tmp_path / "nope"))
    with pytest.raises(StorageServiceStartError, match="NEXUS_SERVICE_BIN"):
        _find_service_binary(tmp_path / "cfg")


def test_find_binary_well_known(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    config_dir = tmp_path / "cfg"
    well_known = well_known_binary_path(config_dir)
    well_known.parent.mkdir(parents=True)
    _make_native_binary(well_known.parent).rename(well_known)
    assert _find_service_binary(config_dir) == well_known


def test_find_binary_absent_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    assert _find_service_binary(tmp_path / "cfg") is None


# ── supervisor construction guard ───────────────────────────────────────────


def test_supervisor_requires_an_artifact(tmp_path):
    with pytest.raises(StorageServiceStartError, match="native binary or a JAR"):
        StorageServiceSupervisor(
            config_dir=tmp_path,
            pg_port=15432,
            service_port=0,
            creds=_CREDS,
        )


# ── _spawn_service argv branch ───────────────────────────────────────────────


def _spawn_and_capture_argv(sup) -> list[str]:
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    with patch(
        "nexus.daemon.storage_service_daemon._allocate_free_port", return_value=18091
    ), patch(
        "nexus.logging_setup.open_child_log_or_devnull", return_value=MagicMock()
    ), patch(
        "nexus.daemon.storage_service_daemon.subprocess.Popen", return_value=fake_proc
    ) as popen:
        proc, port = sup._spawn_service()
    assert proc is fake_proc
    assert port == 18091
    return popen.call_args.args[0]


def test_spawn_native_uses_binary_argv(tmp_path):
    binary = _make_native_binary(tmp_path)
    sup = StorageServiceSupervisor(
        config_dir=tmp_path,
        binary_path=binary,
        pg_port=15432,
        service_port=0,
        creds=_CREDS,
    )
    argv = _spawn_and_capture_argv(sup)
    assert argv == [str(binary)], "native launch must exec the binary directly"


def test_spawn_jar_uses_java_argv(tmp_path):
    jar = tmp_path / "nexus-service.jar"
    jar.write_text("fake jar")
    sup = StorageServiceSupervisor(
        config_dir=tmp_path,
        jar_path=jar,
        pg_port=15432,
        service_port=0,
        creds=_CREDS,
    )
    with patch.object(sup, "_find_java", return_value="/usr/bin/java"):
        argv = _spawn_and_capture_argv(sup)
    assert argv == ["/usr/bin/java", "-jar", str(jar)]


# ── schema-skew gate is JAR-only ─────────────────────────────────────────────


def _drive_start_locked(sup):
    """Run _start_locked with PG/spawn/health/publish stubbed so only the
    schema-skew decision is exercised."""
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    with patch(
        "nexus.daemon.storage_service_daemon.ServiceRegistry"
    ) as Reg, patch.object(
        sup, "_ensure_pg_running"
    ), patch.object(
        sup, "_spawn_service", return_value=(fake_proc, 18092)
    ), patch.object(
        sup, "_wait_for_service_ready"
    ), patch.object(
        sup, "_publish"
    ), patch(
        "nexus.daemon.jar_lifecycle.check_schema_skew"
    ) as skew:
        Reg.return_value.discover.return_value = None
        # _publish sets up the supervisor record the payload reads; stubbing it
        # leaves _supervisor None, so short-circuit the payload assembly by
        # making _publish populate a minimal record.
        sup._supervisor = MagicMock()
        sup._supervisor.record.generation = 1
        sup._start_locked()
    return skew


def test_schema_skew_checked_for_jar(tmp_path):
    jar = tmp_path / "nexus-service.jar"
    jar.write_text("fake jar")
    sup = StorageServiceSupervisor(
        config_dir=tmp_path, jar_path=jar, pg_port=15432, service_port=0, creds=_CREDS
    )
    skew = _drive_start_locked(sup)
    skew.assert_called_once()


def test_schema_skew_skipped_for_native(tmp_path):
    binary = _make_native_binary(tmp_path)
    sup = StorageServiceSupervisor(
        config_dir=tmp_path,
        binary_path=binary,
        pg_port=15432,
        service_port=0,
        creds=_CREDS,
    )
    skew = _drive_start_locked(sup)
    skew.assert_not_called()


# ── start_storage_service prefers the binary ─────────────────────────────────


def test_start_storage_service_prefers_binary(tmp_path, monkeypatch):
    from nexus.daemon import storage_service_daemon as mod

    binary = _make_native_binary(tmp_path)
    monkeypatch.setattr(mod, "_load_credentials", lambda cfg: _CREDS)
    monkeypatch.setattr(mod, "_find_service_binary", lambda cfg: binary)

    def _no_jar():
        raise AssertionError("_find_service_jar must not be called when a binary exists")

    monkeypatch.setattr(mod, "_find_service_jar", _no_jar)
    captured = {}

    class _SpySup:
        def __init__(self, **kw):
            captured.update(kw)

        def start(self):
            return {"host": "127.0.0.1", "port": 18093, "pid": 1, "generation": 1}

    monkeypatch.setattr(mod, "StorageServiceSupervisor", _SpySup)
    mod.start_storage_service(config_dir=tmp_path)
    assert captured["binary_path"] == binary
    assert captured["jar_path"] is None
