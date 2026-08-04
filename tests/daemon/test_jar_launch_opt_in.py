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
    _raise_or_warn_on_artifact_mismatch,
    _resolve_launch_artifact,
    requested_launch_artifact_if_explicit,
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


# ── nx init --service honours the JAR opt-in (no native binary required) ─────


def test_init_service_skips_binary_acquire_with_jar(monkeypatch, tmp_path):
    """_ensure_service_binary_step returns True (and acquires nothing) when
    NEXUS_SERVICE_JAR is set — so `nx init --service` works on a host with no
    native binary."""
    from nexus.commands.init import _ensure_service_binary_step

    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    # If it tried to acquire, _find_service_binary would be consulted; make it
    # explode so a regression that ignores the opt-in fails loudly.
    monkeypatch.setattr(ssd, "_find_service_binary", lambda cd: (_ for _ in ()).throw(AssertionError("should not check native binary")))
    assert _ensure_service_binary_step(tmp_path) is True


# ── requested_launch_artifact_if_explicit (nexus-4e96a) ──────────────────────


def test_requested_none_when_neither_env_set(monkeypatch, tmp_path):
    """Ambient well-known-path flows set neither var — no opinion, so the
    mismatch check downstream never fires on ordinary drift."""
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    assert requested_launch_artifact_if_explicit(tmp_path) is None


def test_requested_resolves_explicit_jar(monkeypatch, tmp_path):
    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    # Fix round (nexus-4e96a round-1 critique): the returned path is
    # CANONICAL-ABSOLUTE, not necessarily identical to the input spelling.
    assert requested_launch_artifact_if_explicit(tmp_path) == (jar.resolve(strict=False), "jar")


def test_requested_resolves_explicit_bin(monkeypatch, tmp_path):
    binary = tmp_path / "nexus-service"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(binary))
    assert requested_launch_artifact_if_explicit(tmp_path) == (
        binary.resolve(strict=False), "native",
    )


def test_requested_bin_set_but_missing_fails_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(tmp_path / "nope"))
    with pytest.raises(StorageServiceStartError, match="NEXUS_SERVICE_BIN"):
        requested_launch_artifact_if_explicit(tmp_path)


# ── _raise_or_warn_on_artifact_mismatch (nexus-4e96a) ─────────────────────────


def test_mismatch_noop_when_no_explicit_request(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    # Any endpoint shape, even a wildly different artifact: never inspected.
    _raise_or_warn_on_artifact_mismatch(tmp_path, {"artifact": "/some/other/thing"})


def test_mismatch_raises_naming_both_artifacts(monkeypatch, tmp_path):
    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    with pytest.raises(StorageServiceStartError) as exc_info:
        _raise_or_warn_on_artifact_mismatch(
            tmp_path, {"artifact": "/opt/nexus/nexus-service", "launch_kind": "native"},
        )
    msg = str(exc_info.value)
    # Requested side is compared/reported in its CANONICAL-ABSOLUTE form.
    assert str(jar.resolve(strict=False)) in msg
    assert "/opt/nexus/nexus-service" in msg
    assert "nx daemon service stop" in msg


def test_mismatch_same_artifact_is_silent(monkeypatch, tmp_path):
    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    # Same artifact the lease already carries (canonical spelling) -> no raise.
    _raise_or_warn_on_artifact_mismatch(
        tmp_path, {"artifact": str(jar.resolve(strict=False)), "launch_kind": "jar"},
    )


def test_mismatch_unknown_lease_artifact_allows_with_warning(monkeypatch, tmp_path, caplog):
    """A pre-fix lease predates artifact-identity tracking: allow + warn,
    never raise (one-release degrade window)."""
    jar = tmp_path / "svc.jar"
    jar.write_text("x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)

    warnings: list[tuple[str, dict]] = []
    orig_warning = ssd._log.warning

    def _capture_warning(event, **kw):
        warnings.append((event, kw))
        return orig_warning(event, **kw)

    monkeypatch.setattr(ssd._log, "warning", _capture_warning)
    # No "artifact" key at all — the pre-fix lease shape.
    _raise_or_warn_on_artifact_mismatch(tmp_path, {"host": "127.0.0.1", "port": 1234})
    assert any(e == "storage_service_artifact_unverifiable" for e, _ in warnings)


# ── canonical-path normalization (nexus-4e96a round-1 critique fix round) ────
#
# Round-1 dual review converged: the mismatch check compared unnormalized
# path STRINGS cross-process. A relative NEXUS_SERVICE_JAR/BIN resolved
# against different CWDs could (a) false-positive-block the identical file,
# spelled differently, or (b) silently MATCH two genuinely different files
# whose relative spelling happened to coincide — the exact silent-
# misattachment class nexus-4e96a exists to kill. These tests exercise real
# files on disk (not mocks) — the literal bug scenario, at the unit layer
# the round-1 critic identified as the only place it is actually covered
# (the e2e gate never live-exercises the raise arm: the stop-insertion at
# tests/e2e/local-service-gate.sh keeps step 3 lease-free, so the mismatch
# branch is defense-in-depth there, never actually triggered by the gate).


def test_mismatch_relative_vs_absolute_same_file_no_raise(monkeypatch, tmp_path):
    """Same file, spelled two different ways (CWD-relative here, absolute
    at publish time from some other process's CWD) — canonicalization must
    treat these as the SAME artifact. Pre-fix (raw string compare) this
    would have false-positive-raised."""
    binary = tmp_path / "nexus-service"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", "nexus-service")  # relative spelling

    # The "lease", published elsewhere, carries the fully-resolved spelling.
    _raise_or_warn_on_artifact_mismatch(
        tmp_path, {"artifact": str(binary.resolve(strict=False)), "launch_kind": "native"},
    )  # must not raise


def test_mismatch_symlink_spelling_same_file_no_raise(monkeypatch, tmp_path):
    """A symlinked path to the same underlying file must not be treated as a
    different artifact than the real path the lease recorded."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_binary = real_dir / "nexus-service"
    real_binary.write_text("#!/bin/sh\nexit 0\n")
    real_binary.chmod(0o755)

    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    linked_binary = link_dir / "nexus-service"
    assert linked_binary.is_file()  # sanity: the symlink resolves

    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(linked_binary))

    # The lease recorded the REAL (non-symlinked) canonical path.
    _raise_or_warn_on_artifact_mismatch(
        tmp_path, {"artifact": str(real_binary.resolve(strict=False)), "launch_kind": "native"},
    )  # must not raise


def test_mismatch_genuinely_different_files_still_raises(monkeypatch, tmp_path):
    """Sanity check the other half of the fix: normalization must not
    over-collapse — two genuinely distinct files still raise, even when one
    is named relative to a shared parent directory."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    binary_a = dir_a / "nexus-service"
    binary_b = dir_b / "nexus-service"
    for b in (binary_a, binary_b):
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)

    monkeypatch.chdir(dir_a)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_BIN", "nexus-service")  # resolves to binary_a

    with pytest.raises(StorageServiceStartError, match="DIFFERENT artifact"):
        _raise_or_warn_on_artifact_mismatch(
            tmp_path, {"artifact": str(binary_b.resolve(strict=False)), "launch_kind": "native"},
        )
