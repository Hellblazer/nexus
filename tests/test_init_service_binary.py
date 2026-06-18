# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 P1: `nx init --service` wiring of the native-binary acquisition step.

Tests :func:`nexus.commands.init._ensure_service_binary_step` in isolation — a
throwaway ``config_dir`` (``tmp_path``), ``install_binary`` monkeypatched so no
network call and no live daemon/state is touched (mem:
feedback_dont_break_live_nexus_install). The full ``init_cmd`` is not driven
here: it provisions Postgres and starts the service, which this bead's wiring
sits between.

Contract (RDR-161 §Approach P1, third bullet):
- a present, resolvable binary is a NO-OP (never re-downloaded);
- when absent, an explicit ``engine-service-v*`` tag (env / build pin) drives
  ``install_binary`` — no "latest" resolution (RF-161-2);
- when absent AND no tag is configured, the step instructs the user to install
  one explicitly and does NOT fail init or guess a tag;
- a broken ``NEXUS_SERVICE_BIN`` override is surfaced, never silently
  downloaded over.
"""
from __future__ import annotations

import os

import pytest

from nexus.commands import init as init_mod
from nexus.daemon import binary_install
from nexus.daemon.jar_lifecycle import well_known_binary_path


@pytest.fixture(autouse=True)
def _clean_service_env(monkeypatch):
    """No env-sourced binary/tag leaks in from the host."""
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_TAG", raising=False)
    monkeypatch.setattr(binary_install, "PINNED_SERVICE_TAG", None, raising=False)


def _place_fake_binary(config_dir):
    dest = well_known_binary_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(dest, 0o755)
    return dest


def test_present_binary_is_a_noop(tmp_path, monkeypatch):
    _place_fake_binary(tmp_path)

    def _boom(*a, **k):  # install must NOT be attempted
        raise AssertionError("install_binary called for an already-present binary")

    # The step does a function-local `from binary_install import install_binary`,
    # which resolves the (patched) module attribute at call time.
    monkeypatch.setattr(binary_install, "install_binary", _boom)

    assert init_mod._ensure_service_binary_step(tmp_path) is True  # ready, no download


def test_absent_binary_with_tag_installs(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.3")
    calls = {}

    def _fake_install(tag, config_dir, *, installed_by=""):
        calls["tag"] = tag
        calls["config_dir"] = config_dir
        dest = _place_fake_binary(config_dir)
        return dest, {"asset": "nexus-service-linux-amd64", "version": "0.1.3"}

    monkeypatch.setattr(binary_install, "install_binary", _fake_install)

    assert init_mod._ensure_service_binary_step(tmp_path) is True
    assert calls["tag"] == "engine-service-v0.1.3"
    assert calls["config_dir"] == tmp_path
    assert well_known_binary_path(tmp_path).is_file()


def test_second_init_is_a_noop_after_install(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.3")
    n = {"installs": 0}

    def _fake_install(tag, config_dir, *, installed_by=""):
        n["installs"] += 1
        dest = _place_fake_binary(config_dir)
        return dest, {"asset": "nexus-service-linux-amd64", "version": "0.1.3"}

    monkeypatch.setattr(binary_install, "install_binary", _fake_install)

    init_mod._ensure_service_binary_step(tmp_path)  # installs
    init_mod._ensure_service_binary_step(tmp_path)  # idempotent no-op
    assert n["installs"] == 1


def test_absent_binary_no_tag_instructs_without_failing(tmp_path, monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("install_binary called with no tag configured")

    monkeypatch.setattr(binary_install, "install_binary", _boom)

    # Returns False (not ready) without raising; the caller gates start on this.
    assert init_mod._ensure_service_binary_step(tmp_path) is False
    out = capsys.readouterr().out.lower()
    assert "install-binary" in out
    assert not well_known_binary_path(tmp_path).is_file()


def test_build_pin_used_when_env_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(binary_install, "PINNED_SERVICE_TAG", "engine-service-v0.2.0")
    seen = {}

    def _fake_install(tag, config_dir, *, installed_by=""):
        seen["tag"] = tag
        dest = _place_fake_binary(config_dir)
        return dest, {"asset": "nexus-service-mac-arm64", "version": "0.2.0"}

    monkeypatch.setattr(binary_install, "install_binary", _fake_install)
    assert init_mod._ensure_service_binary_step(tmp_path) is True
    assert seen["tag"] == "engine-service-v0.2.0"


def test_init_cmd_does_not_start_service_when_no_binary(tmp_path, monkeypatch):
    """CRE C1: with no binary and no tag, `nx init --service` must NOT fall
    through to _start_service_step (which would hit the legacy java -jar path).
    It exits non-zero with an actionable message instead."""
    from click.testing import CliRunner

    from nexus import config as _config

    monkeypatch.setattr(init_mod, "PINNED_SERVICE_TAG", None, raising=False)
    monkeypatch.setattr(binary_install, "PINNED_SERVICE_TAG", None)
    # isolate: throwaway config dir, local mode, PG + embedder steps stubbed.
    monkeypatch.setattr(_config, "is_local_mode", lambda: True)
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setattr(init_mod, "_provision_postgres_step", lambda: None)
    monkeypatch.setattr(init_mod, "_provision_service_embedder_step", lambda e: None)

    started = {"called": False}

    def _start_must_not_run():
        started["called"] = True

    monkeypatch.setattr(init_mod, "_start_service_step", _start_must_not_run)

    result = CliRunner().invoke(init_mod.init_cmd, ["--service", "--yes"])
    assert result.exit_code != 0
    assert started["called"] is False
    assert "not started" in result.output.lower()


def test_broken_override_surfaces_not_downloads(tmp_path, monkeypatch):
    # NEXUS_SERVICE_BIN set to a missing file: _find_service_binary raises;
    # the step must surface it (SystemExit), never download over the override.
    monkeypatch.setenv("NEXUS_SERVICE_BIN", str(tmp_path / "does-not-exist"))

    def _boom(*a, **k):
        raise AssertionError("install_binary called over a broken override")

    monkeypatch.setattr(binary_install, "install_binary", _boom)
    with pytest.raises(SystemExit):
        init_mod._ensure_service_binary_step(tmp_path)


def test_resolve_service_tag_precedence(monkeypatch):
    monkeypatch.setattr(binary_install, "PINNED_SERVICE_TAG", "engine-service-v0.2.0")
    monkeypatch.delenv("NEXUS_SERVICE_TAG", raising=False)
    assert binary_install.resolve_service_tag() == "engine-service-v0.2.0"
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v9.9.9")
    assert binary_install.resolve_service_tag() == "engine-service-v9.9.9"  # env wins
