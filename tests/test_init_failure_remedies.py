# SPDX-License-Identifier: AGPL-3.0-or-later
"""F11 (nexus-f3yqd.6): each `nx init` failure names what to fix, and the log where one exists.

Five failures printed only the exception text: the bge model download, the storage-service start, the two
PostgreSQL bundle extractions and PostgreSQL provisioning. A first-run user saw the error and nothing to do.
The retry is not repeated here: `nx init`'s step reporter prints "Re-run `nx init` ..." under any failed
step, and the same steps also run from `nx upgrade`, where that line would be wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import init as init_mod
from nexus.daemon import binary_install
from nexus.db import pg_provision

NOT_HERE = "re-run `nx init`"  # the step reporter owns the retry line


def _no_platform_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_provision, "refuse_root", lambda: None)
    monkeypatch.setattr(pg_provision, "refuse_smart_app_control", lambda: None)


def test_the_bge_download_failure_says_check_the_network_and_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("nexus.commands.init._provision_postgres_step", lambda: None)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.commands.init._start_service_step", lambda cd: None)
    monkeypatch.setattr("nexus.commands.init._ensure_service_binary_step", lambda cd: True)

    def _offline(**_kw: object) -> Path:
        raise OSError("Temporary failure in name resolution")

    monkeypatch.setattr("nexus.db.service_bge_model.fetch_service_bge_onnx", _offline)
    result = CliRunner().invoke(init_mod.init_cmd, ["--service"])
    assert result.exit_code != 0
    out = " ".join(result.output.split())
    assert "Temporary failure in name resolution" in out
    assert "Check the network connection" in out, out


def test_the_service_start_failure_names_the_log_and_the_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(_cd: Path) -> object:
        raise RuntimeError("address already in use")

    monkeypatch.setattr("nexus.commands.daemon.ensure_storage_supervisor", _boom)
    with pytest.raises(SystemExit):
        init_mod._start_service_step(tmp_path)
    err = capsys.readouterr().err
    assert "address already in use" in err
    assert str(tmp_path / "logs" / "storage_service.log") in err and NOT_HERE not in err.lower(), err


def test_the_downloaded_bundle_extraction_failure_names_the_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NEXUS_SERVICE_TAG", "engine-service-v0.1.32")

    def _fake_corrupt(tag: str, config_dir: Path, *, installed_by: str = "") -> tuple[Path, dict[str, str]]:
        dest = binary_install.pg_bundle_dest(config_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a tarball")
        return dest, {"asset": dest.name, "sha256": "0" * 64}

    monkeypatch.setattr(binary_install, "install_pg_bundle", _fake_corrupt)
    with pytest.raises(SystemExit):
        init_mod._acquire_pg_bundle_step(tmp_path)
    err = capsys.readouterr().err
    assert "could not be extracted" in err and "free disk space" in err and str(tmp_path) in err, err


def test_the_bundled_postgres_extraction_failure_names_the_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _no_platform_guards(monkeypatch)

    def _broken(_cd: Path) -> Path:
        raise OSError("No space left on device")

    monkeypatch.setattr(init_mod, "_select_bundled_pg", _broken)
    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()
    err = capsys.readouterr().err
    assert "No space left on device" in err and "free disk space" in err and str(tmp_path) in err, err


def test_the_postgres_provisioning_failure_names_pg_log_and_the_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_PG_BIN", str(tmp_path / "pgbin"))  # an explicit override skips the download
    _no_platform_guards(monkeypatch)
    monkeypatch.setattr(init_mod, "_select_bundled_pg", lambda _cd: None)

    def _initdb_fails(*_a: object, **_kw: object) -> object:
        raise RuntimeError("initdb exited 1")

    monkeypatch.setattr(pg_provision, "provision", _initdb_fails)
    with pytest.raises(SystemExit):
        init_mod._provision_postgres_step()
    err = capsys.readouterr().err
    assert "initdb exited 1" in err
    assert str(tmp_path / "postgres" / "pg.log") in err and NOT_HERE not in err.lower(), err
