# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1381 / nexus-yv5m4: bare ``nx init`` / ``nx guided-upgrade`` ALWAYS
provision from the signed PG bundle — no host-PostgreSQL probing, no fallback.

Policy (locked 2026-07-07): nexus installs its own self-contained PostgreSQL
bundle (pgvector baked in) unconditionally. Host PostgreSQL is never probed or
silently used; there is no Homebrew / build-pgvector-from-source path. The
only override is an explicit ``NEXUS_PG_BIN``; the only skip is a cluster that
is already provisioned and serving (existing installs keep whatever
PostgreSQL created them).

Tests :func:`nexus.commands.init._acquire_pg_bundle_step` in isolation plus
its wiring inside :func:`nexus.commands.init._provision_postgres_step` —
throwaway ``config_dir`` (``tmp_path``), ``install_pg_bundle`` monkeypatched
so no network call and no live daemon/state is touched (mem:
feedback_dont_break_live_nexus_install). The synthetic bundle archive is REAL
(``make_pg_bundle_txz``): extraction + selection run the genuine
``ensure_pg_bundle`` path, only the download is faked.

Contract:
- pinned tag + no bundle on disk → download via the verified seam, extract,
  select (``NEXUS_PG_BIN`` exported) BEFORE ``provision`` runs — regardless
  of any host PostgreSQL;
- an explicit ``NEXUS_PG_BIN`` override → never downloads (a deliberate, even
  broken, override is surfaced, not silently papered over);
- an existing cluster data directory (serving OR stopped) → never downloads —
  established installs keep whatever PostgreSQL created them;
- no pinned/env tag → SystemExit with the explicit remedies (no host probe);
- download failure / extraction failure → SystemExit, no fallback.

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


def test_no_pin_fails_loud_with_remedies(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)
    with pytest.raises(SystemExit):
        init_mod._acquire_pg_bundle_step(tmp_path)
    err = capsys.readouterr().err
    assert "NEXUS_SERVICE_TAG" in err
    assert "install-binary" in err
    assert "NEXUS_PG_BIN" in err


def test_pinned_tag_downloads_extracts_selects(tmp_path, monkeypatch, make_pg_bundle_txz):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")
    calls: dict = {}
    monkeypatch.setattr(
        binary_install,
        "install_pg_bundle",
        _fake_install_pg_factory(make_pg_bundle_txz, tmp_path / "dl", calls),
    )

    bin_dir = init_mod._acquire_pg_bundle_step(tmp_path)

    assert (bin_dir / "initdb").is_file()
    assert calls["tag"] == "engine-service-v0.1.32"
    assert calls["config_dir"] == tmp_path
    # Selected for provisioning: discovery must resolve the bundle first.
    assert os.environ["NEXUS_PG_BIN"] == str(bin_dir)


def test_download_failure_fails_loud_no_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _fail(tag, config_dir, *, installed_by=""):
        raise binary_install.BinaryVerificationError("sha256 mismatch")

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fail)

    with pytest.raises(SystemExit):
        init_mod._acquire_pg_bundle_step(tmp_path)
    err = capsys.readouterr().err
    assert "sha256 mismatch" in err
    assert "install-binary engine-service-v0.1.32" in err


def test_downloaded_bundle_extraction_failure_fails_loud(
    tmp_path, monkeypatch, capsys
):
    """A verified download that fails to EXTRACT (corrupt archive, disk full)
    must exit with the cause on stderr — never a raw traceback, no fallback."""
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _fake_corrupt(tag, config_dir, *, installed_by=""):
        dest = binary_install.pg_bundle_dest(config_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a tarball")
        return dest, {"asset": dest.name, "sha256": "0" * 64}

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fake_corrupt)

    with pytest.raises(SystemExit):
        init_mod._acquire_pg_bundle_step(tmp_path)
    assert "could not be extracted" in capsys.readouterr().err


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


def test_provision_step_always_acquires_bundle(
    tmp_path, monkeypatch, make_pg_bundle_txz
):
    """Fresh machine, pinned tag → the bundle is downloaded + selected BEFORE
    provision() runs, with NO host-PostgreSQL probing (discovery is a boom)."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _must_not_probe():
        raise AssertionError("host PostgreSQL must never be probed")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", _must_not_probe)
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


def test_existing_cluster_present_detects_marker(tmp_path):
    """Direct unit test: PG_VERSION marker present (serving OR stopped) → True;
    absent → False. This is the stopped-cluster guard — an existing pgdata is
    never pg_ctl-started with freshly downloaded, possibly different-major
    binaries (code-review High)."""
    assert pg_provision.existing_cluster_present(tmp_path) is False
    pgdata = tmp_path / "postgres"
    pgdata.mkdir()
    assert pg_provision.existing_cluster_present(tmp_path) is False
    (pgdata / "PG_VERSION").write_text("17\n")
    assert pg_provision.existing_cluster_present(tmp_path) is True


def test_provision_step_skips_download_when_cluster_exists(tmp_path, monkeypatch):
    """An existing cluster data directory (even STOPPED — no port listening)
    keeps whatever PostgreSQL created it — re-running nx init must not
    download a bundle over it. Real marker file, no predicate mocking."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    pgdata = tmp_path / "postgres"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n")  # e.g. a legacy host-PG cluster

    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)
    seen: dict = {}
    monkeypatch.setattr(pg_provision, "provision", _fake_provision_factory(seen))

    init_mod._provision_postgres_step()

    assert seen["nexus_pg_bin"] is None  # existing cluster, no bundle selected


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


def test_provision_step_reuses_bundle_already_on_disk(
    tmp_path, monkeypatch, make_pg_bundle_txz
):
    """A bundle archive already at <config>/service/ is extracted + selected
    with no re-download (idempotent re-run)."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    dest = binary_install.pg_bundle_dest(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive = make_pg_bundle_txz(tmp_path / "stage", name=dest.name)
    shutil.copy2(archive, dest)

    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)
    seen: dict = {}
    monkeypatch.setattr(pg_provision, "provision", _fake_provision_factory(seen))

    init_mod._provision_postgres_step()

    assert seen["nexus_pg_bin"], "on-disk bundle must be selected"


def test_provision_step_no_pin_fails_loud(tmp_path, monkeypatch, capsys):
    """No pinned/env tag and nothing on disk → SystemExit with the explicit
    remedies; host PostgreSQL is not probed."""
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path)

    def _must_not_probe():
        raise AssertionError("host PostgreSQL must never be probed")

    monkeypatch.setattr(pg_provision, "discover_pg_binaries", _must_not_probe)
    monkeypatch.setattr(binary_install, "install_pg_bundle", _boom_install_pg)

    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()
    assert "install-binary" in capsys.readouterr().err
