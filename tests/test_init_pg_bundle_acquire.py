# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1381 / nexus-yv5m4: bare ``nx init`` / ``nx guided-upgrade`` auto-acquire
the signed PG bundle when no usable host PostgreSQL exists.

Tests :func:`nexus.commands.init._acquire_pg_bundle_step` in isolation plus its
wiring inside :func:`nexus.commands.init._provision_postgres_step` — throwaway
``config_dir`` (``tmp_path``), ``install_pg_bundle`` monkeypatched so no network
call and no live daemon/state is touched (mem:
feedback_dont_break_live_nexus_install). The synthetic bundle archive is REAL
(``make_pg_bundle_txz``): extraction + selection run the genuine
``ensure_pg_bundle`` path, only the download is faked.

Contract (nexus-yv5m4):
- no pinned/env tag → no download attempt, step returns None (dev boxes keep
  host-PG discovery unchanged);
- pinned tag + no usable host PG → download via the verified seam, extract,
  select (``NEXUS_PG_BIN`` exported) BEFORE ``provision`` runs;
- a usable host PG (binaries + pgvector) → never downloads;
- an explicit ``NEXUS_PG_BIN`` override → never downloads (a deliberate, even
  broken, override is surfaced, not silently papered over);
- host PG present but pgvector missing (the GH #1381 Homebrew postgresql@17
  case) → downloads the bundle instead of dead-ending at build-from-source;
- download failure → falls back to the original host-PG error path.

``nx guided-upgrade`` coverage is structural, by construction: it routes
through the SAME function object (``guided_upgrade.provision_and_serve`` →
``init.provision_and_start_service`` → ``provision_service_stack`` →
``_provision_postgres_step``) with no divergence, so these tests cover both
entry points. The only bypass is the explicit ``--service-url`` path, which
targets an external service and must never provision local PG.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from nexus.commands import init as init_mod
from nexus.daemon import binary_install
from nexus.db import pg_provision


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No env-sourced PG/bundle/tag state leaks in from the host."""
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.delenv("NEXUS_PG_BUNDLE", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_TAG", raising=False)
    monkeypatch.setattr(binary_install, "PINNED_SERVICE_TAG", None, raising=False)


def _boom_install_pg(*a, **k):
    raise AssertionError("install_pg_bundle must not be called on this path")


def _fake_install_pg_factory(make_pg_bundle_txz, stage_dir: Path, calls: dict):
    """A fake ``install_pg_bundle`` placing a REAL synthetic .txz at the
    canonical destination — extraction downstream is genuine."""

    def _fake(tag, config_dir, *, installed_by=""):
        calls["tag"] = tag
        calls["config_dir"] = config_dir
        dest = binary_install.pg_bundle_dest(config_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        archive = make_pg_bundle_txz(stage_dir, name=dest.name)
        shutil.copy2(archive, dest)
        return dest, {"asset": dest.name, "sha256": "0" * 64}

    return _fake


# ── _acquire_pg_bundle_step in isolation ─────────────────────────────────────


def test_no_pin_no_download(tmp_path, monkeypatch):
    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)
    assert init_mod._acquire_pg_bundle_step(tmp_path) is None


def test_pinned_tag_downloads_extracts_selects(tmp_path, monkeypatch, make_pg_bundle_txz):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")
    calls: dict = {}
    monkeypatch.setattr(
        binary_install,
        "install_pg_bundle",
        _fake_install_pg_factory(make_pg_bundle_txz, tmp_path / "dl", calls),
    )

    bin_dir = init_mod._acquire_pg_bundle_step(tmp_path)

    assert bin_dir is not None
    assert (bin_dir / "initdb").is_file()
    assert calls["tag"] == "engine-service-v0.1.32"
    assert calls["config_dir"] == tmp_path
    # Selected for provisioning: discovery must resolve the bundle first.
    assert os.environ["NEXUS_PG_BIN"] == str(bin_dir)


def test_downloaded_bundle_extraction_failure_returns_none(
    tmp_path, monkeypatch, capsys
):
    """A verified download that fails to EXTRACT (corrupt archive, disk full)
    must return None with the cause on stderr — never a raw traceback."""
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _fake_corrupt(tag, config_dir, *, installed_by=""):
        dest = binary_install.pg_bundle_dest(config_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a tarball")
        return dest, {"asset": dest.name, "sha256": "0" * 64}

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fake_corrupt)

    assert init_mod._acquire_pg_bundle_step(tmp_path) is None
    assert "could not be extracted" in capsys.readouterr().err


def test_download_failure_returns_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _fail(tag, config_dir, *, installed_by=""):
        raise binary_install.BinaryVerificationError("sha256 mismatch")

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fail)

    assert init_mod._acquire_pg_bundle_step(tmp_path) is None
    assert "sha256 mismatch" in capsys.readouterr().err


# ── wiring inside _provision_postgres_step ───────────────────────────────────


def _fake_provision_factory(seen: dict):
    def _fake_provision(config_dir):
        seen["nexus_pg_bin"] = os.environ.get("NEXUS_PG_BIN")
        result = pg_provision.ProvisionResult(
            credentials_path=Path(config_dir) / "pg-credentials.env"
        )
        result.already_provisioned = True
        result.port = 5555
        return result

    return _fake_provision


def test_provision_step_acquires_bundle_when_no_host_pg(
    tmp_path, monkeypatch, make_pg_bundle_txz
):
    """Fresh machine, pinned tag, zero PostgreSQL anywhere → the bundle is
    downloaded + selected BEFORE provision() runs (the GH #1381 fix)."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _no_pg():
        raise pg_provision.PgBinaryNotFoundError("No PostgreSQL binaries found.")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", _no_pg)
    calls: dict = {}
    monkeypatch.setattr(
        binary_install,
        "install_pg_bundle",
        _fake_install_pg_factory(make_pg_bundle_txz, tmp_path / "dl", calls),
    )
    seen: dict = {}
    monkeypatch.setattr(pg_provision, "provision", _fake_provision_factory(seen))

    init_mod._provision_postgres_step()

    assert calls["tag"] == "engine-service-v0.1.32"
    assert seen["nexus_pg_bin"], "bundle must be selected before provision()"
    assert Path(seen["nexus_pg_bin"]).name == "bin"


def test_provision_step_acquires_bundle_when_host_pg_lacks_pgvector(
    tmp_path, monkeypatch, make_pg_bundle_txz
):
    """Steve's exact case: Homebrew postgresql@17 present, pgvector absent →
    acquire the bundle instead of dead-ending at build-from-source."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda: object())

    def _no_vector(bins):
        raise pg_provision.PgVectorNotInstalledError("no vector.control")

    monkeypatch.setattr(pg_provision, "check_pgvector_available", _no_vector)
    calls: dict = {}
    monkeypatch.setattr(
        binary_install,
        "install_pg_bundle",
        _fake_install_pg_factory(make_pg_bundle_txz, tmp_path / "dl", calls),
    )
    seen: dict = {}
    monkeypatch.setattr(pg_provision, "provision", _fake_provision_factory(seen))

    init_mod._provision_postgres_step()

    assert calls["tag"] == "engine-service-v0.1.32"
    assert seen["nexus_pg_bin"], "bundle must be selected before provision()"


def test_provision_step_skips_acquire_with_usable_host_pg(tmp_path, monkeypatch):
    """A pgvector-capable host PG keeps dev boxes download-free."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda: object())
    monkeypatch.setattr(pg_provision, "check_pgvector_available", lambda bins: None)
    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)
    seen: dict = {}
    monkeypatch.setattr(pg_provision, "provision", _fake_provision_factory(seen))

    init_mod._provision_postgres_step()

    assert seen["nexus_pg_bin"] is None  # host PG used, no bundle selected


def test_provision_step_never_downloads_over_explicit_override(
    tmp_path, monkeypatch
):
    """NEXUS_PG_BIN set (even broken) → surfaced, never auto-downloaded over."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")
    monkeypatch.setenv("NEXUS_PG_BIN", str(tmp_path / "custom-pg" / "bin"))

    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)

    def _broken_override(config_dir):
        raise pg_provision.PgBinaryNotFoundError("NEXUS_PG_BIN is set but broken")

    monkeypatch.setattr(pg_provision, "provision", _broken_override)

    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()


def test_provision_step_pgvector_missing_and_download_fails_stays_actionable(
    tmp_path, monkeypatch, capsys
):
    """The original GH #1381 composite: host PG present but pgvector missing,
    AND the bundle download fails → provisioning still exits with the pgvector
    remedy visible (the build-from-source hint is inside the exception text),
    plus the download-failure explanation. Nothing is swallowed silently."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda: object())

    def _no_vector(bins):
        raise pg_provision.PgVectorNotInstalledError(
            "The pgvector extension is not installed (no vector.control)"
        )

    monkeypatch.setattr(pg_provision, "check_pgvector_available", _no_vector)

    def _fail(tag, config_dir, *, installed_by=""):
        raise binary_install.BinaryVerificationError("download failed")

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fail)

    def _provision_rederives(config_dir):
        # provision() re-runs the preflight and re-raises the same error.
        raise pg_provision.PgVectorNotInstalledError(
            "The pgvector extension is not installed (no vector.control)"
        )

    monkeypatch.setattr(pg_provision, "provision", _provision_rederives)

    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()
    err = capsys.readouterr().err
    assert "download failed" in err  # the acquisition attempt is visible
    assert "pgvector extension is not installed" in err  # remedy not swallowed


def test_provision_step_surfaces_original_error_when_acquire_fails(
    tmp_path, monkeypatch, capsys
):
    """No host PG + bundle download fails → the actionable host-PG error path
    still fires (SystemExit with the install hint), no traceback."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _no_pg():
        raise pg_provision.PgBinaryNotFoundError("No PostgreSQL binaries found.")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", _no_pg)

    def _fail(tag, config_dir, *, installed_by=""):
        raise binary_install.BinaryVerificationError("download failed")

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fail)

    def _provision_raises(config_dir):
        raise pg_provision.PgBinaryNotFoundError("No PostgreSQL binaries found.")

    monkeypatch.setattr(pg_provision, "provision", _provision_raises)

    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()
    err = capsys.readouterr().err
    assert "Postgres binaries not found" in err
