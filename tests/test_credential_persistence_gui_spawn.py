# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m7evs: surface env-only credentials as a doctor warning.

GUI-spawned ``nx-mcp`` (Claude Desktop, Cowork SDK bridge) inherits
launchd's environment, not the user's shell. When credentials live only
as ``export CHROMA_API_KEY=...`` lines in ``.zshrc`` and never get
persisted via ``nx config set``, the spawned process sees empty env,
``is_local_mode()`` returns True, and ``make_t3()`` routes to the
daemon path that fails with ``T3DaemonError``.

The health check below catches this on the CLI side (where shell env IS
visible) and tells the user to persist the credentials. The improved
``make_t3()`` error message catches it on the GUI side, where the
spawned process has no way to inspect the parent's env.
"""
from __future__ import annotations

import pytest


# ── _check_credential_persistence (health.py) ────────────────────────────────


def test_check_credential_persistence_env_only_warns(monkeypatch, tmp_path):
    """When both cloud credentials are in env but NOT in the config
    file, the doctor emits a non-fatal warning naming the four
    ``nx config set`` commands needed for GUI-spawned consumers."""
    monkeypatch.setenv("CHROMA_API_KEY", "ck-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant-uuid")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    from nexus.health import _check_credential_persistence

    results = _check_credential_persistence()
    warning = next((r for r in results if "credential persistence" in r.label.lower()), None)
    assert warning is not None, "expected a credential-persistence warning"
    assert warning.ok is False
    assert warning.fatal is False, "env-only is a warning, not a fatal error"
    msg = " ".join([warning.detail] + warning.fix_suggestions)
    assert "nx config set chroma_api_key" in msg
    assert "nx config set voyage_api_key" in msg
    assert "Claude Desktop" in msg or "GUI" in msg


def test_check_credential_persistence_persisted_silent(monkeypatch, tmp_path):
    """When all four cloud credentials are persisted in config.yml,
    no warning appears (regardless of env state)."""
    monkeypatch.setenv("CHROMA_API_KEY", "ck-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant-uuid")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    config_yml = tmp_path / "config.yml"
    config_yml.write_text(
        "credentials:\n"
        "  chroma_api_key: persisted-chroma\n"
        "  voyage_api_key: persisted-voyage\n"
        "  chroma_tenant: persisted-tenant\n"
        "  chroma_database: persisted-db\n"
    )

    from nexus.health import _check_credential_persistence

    results = _check_credential_persistence()
    warning = next((r for r in results if "credential persistence" in r.label.lower()), None)
    assert warning is None or warning.ok is True, (
        "persisted credentials should NOT trigger a warning"
    )


def test_check_credential_persistence_no_credentials_anywhere_silent(monkeypatch, tmp_path):
    """When neither env nor file has credentials, no warning fires —
    that is genuine local mode (or first-run before any setup), not
    the GUI-spawn trap."""
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.delenv("CHROMA_DATABASE", raising=False)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    from nexus.health import _check_credential_persistence

    results = _check_credential_persistence()
    warning = next((r for r in results if "credential persistence" in r.label.lower()), None)
    assert warning is None or warning.ok is True


def test_check_credential_persistence_partial_warns(monkeypatch, tmp_path):
    """When ONE credential is in env but missing from file (mixed
    state), warn — partial persistence still breaks GUI spawn for the
    missing key."""
    monkeypatch.setenv("CHROMA_API_KEY", "ck-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    config_yml = tmp_path / "config.yml"
    config_yml.write_text(
        "credentials:\n"
        "  chroma_api_key: persisted-chroma\n"
        # voyage_api_key missing from file but present in env
    )

    from nexus.health import _check_credential_persistence

    results = _check_credential_persistence()
    warning = next((r for r in results if "credential persistence" in r.label.lower()), None)
    assert warning is not None
    assert warning.ok is False
    assert "voyage_api_key" in " ".join([warning.detail] + warning.fix_suggestions)


# ── make_t3 error message improvement ────────────────────────────────────────


def test_make_t3_error_mentions_credential_persistence(monkeypatch, tmp_path):
    """When ``make_t3`` falls into the daemon-not-running error path,
    the resulting exception message should ALSO mention the cloud-
    credential persistence option, because the spawned-from-GUI
    process has no way to know which path the user intended.

    Reproduces the actual Cowork failure mode: no env credentials
    (launchd-spawned), no daemon, no config-file credentials. The
    daemon path is hit and the error must self-explain the cloud
    alternative.
    """
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    from nexus.daemon.t3_client import T3DaemonError

    def fake_make_t3_client():
        raise T3DaemonError(
            "T3 daemon not reachable. Start with: nx daemon t3 start"
        )

    monkeypatch.setattr(
        "nexus.daemon.t3_client.make_t3_client", fake_make_t3_client
    )

    from nexus.db import make_t3

    with pytest.raises(T3DaemonError) as exc_info:
        make_t3()

    msg = str(exc_info.value)
    assert "nx daemon t3 start" in msg, "daemon-start path must remain"
    assert "nx config set" in msg, (
        "must point at credential persistence as the alternative; "
        "this is the GUI-spawn hint that the spawned process cannot "
        "self-diagnose otherwise"
    )
    assert "Claude Desktop" in msg or "GUI" in msg or "shell env" in msg
