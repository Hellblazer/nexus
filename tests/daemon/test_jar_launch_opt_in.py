# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit-opt-in JAR launch for the storage service (amends RDR-161).

The cosign-verified native binary stays the production default; NEXUS_SERVICE_JAR
is an explicit dev/test opt-in launched via the JVM. Never auto-discovered,
never a silent fallback.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.storage_service_daemon import (
    StorageServiceStartError,
    StorageServiceSupervisor,
    _find_service_jar,
    _resolve_launch_artifact,
)


# ── _find_service_jar (explicit only) ───────────────────────────────────────


def test_find_jar_unset_returns_none(monkeypatch):
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    assert _find_service_jar() is None


def test_find_jar_set_and_present(monkeypatch, tmp_path):
    jar = tmp_path / "nexus-service.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    assert _find_service_jar() == jar


def test_find_jar_set_but_missing_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(tmp_path / "nope.jar"))
    with pytest.raises(StorageServiceStartError, match="does not exist"):
        _find_service_jar()


# ── _resolve_launch_artifact (native default, jar opt-in) ───────────────────


def test_resolve_prefers_native_when_no_jar(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    binary = tmp_path / "nexus-service"
    monkeypatch.setattr(ssd, "_find_service_binary", lambda cd: binary)
    path, kind = _resolve_launch_artifact(tmp_path)
    assert (path, kind) == (binary, "native")


def test_resolve_jar_opt_in_wins(monkeypatch, tmp_path):
    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    # Even if a native binary exists, the explicit opt-in is honoured.
    monkeypatch.setattr(ssd, "_find_service_binary", lambda cd: tmp_path / "native")
    path, kind = _resolve_launch_artifact(tmp_path)
    assert (path, kind) == (jar, "jar")


def test_resolve_no_artifact_fails_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setattr(ssd, "_find_service_binary", lambda cd: None)
    with pytest.raises(StorageServiceStartError, match="No nexus-service launch artifact"):
        _resolve_launch_artifact(tmp_path)


# ── supervisor launch_kind validation ───────────────────────────────────────


def test_supervisor_rejects_bad_launch_kind(tmp_path):
    with pytest.raises(StorageServiceStartError, match="launch_kind"):
        StorageServiceSupervisor(
            config_dir=tmp_path, pg_port=5432, service_port=0,
            creds={"NX_SERVICE_TOKEN": "tok"}, binary_path=tmp_path / "x",
            launch_kind="bogus",
        )


# ── argv construction: native vs jvm ────────────────────────────────────────


def _spawn_capture(monkeypatch, *, launch_kind, artifact, max_heap=None):
    """Drive _spawn_service with Popen stubbed; return the captured argv."""
    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(argv, **kw):  # noqa: ANN001
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(ssd.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(ssd, "_allocate_free_port", lambda host="127.0.0.1": 55000)
    monkeypatch.setattr(
        "nexus.logging_setup.open_child_log_or_devnull",
        lambda name, cfg: os.open(os.devnull, os.O_WRONLY),
    )
    if max_heap is not None:
        monkeypatch.setenv("NX_SERVICE_MAX_HEAP", max_heap)
    sup = StorageServiceSupervisor(
        config_dir=Path("/tmp"), pg_port=5432, service_port=0,
        creds={"NX_SERVICE_TOKEN": "tok"}, binary_path=artifact,
        launch_kind=launch_kind,
    )
    sup._spawn_service()
    return captured["argv"]


def test_native_argv_is_the_binary(monkeypatch):
    binary = Path("/opt/nexus/nexus-service")
    argv = _spawn_capture(monkeypatch, launch_kind="native", artifact=binary)
    assert argv == [str(binary)]


def test_jar_argv_is_java_dash_jar(monkeypatch):
    monkeypatch.setattr(ssd, "_resolve_java_executable", lambda: "/usr/bin/java")
    jar = Path("/build/nexus-service.jar")
    argv = _spawn_capture(monkeypatch, launch_kind="jar", artifact=jar)
    assert argv == ["/usr/bin/java", "-jar", str(jar)]


def test_jar_argv_with_heap_orders_xmx_before_jar(monkeypatch):
    monkeypatch.setattr(ssd, "_resolve_java_executable", lambda: "/usr/bin/java")
    jar = Path("/build/nexus-service.jar")
    argv = _spawn_capture(monkeypatch, launch_kind="jar", artifact=jar, max_heap="1g")
    assert argv == ["/usr/bin/java", "-Xmx1g", "-jar", str(jar)]
