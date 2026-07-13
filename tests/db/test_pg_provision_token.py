# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the persistent root token (gmiaf.32.5) in pg_provision.

Covers the pure-function credential surfaces without a real PostgreSQL
cluster: ``_write_credentials`` includes NX_SERVICE_TOKEN, and
``_persist_service_token`` backfills it atomically at 0600 while preserving
every existing line.
"""
from __future__ import annotations

import stat
from pathlib import Path

from nexus.db.pg_provision import (
    _persist_service_token,
    _read_credentials,
    _write_credentials,
)


def test_write_credentials_includes_service_token(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    _write_credentials(creds_path, tmp_path / "postgres", 15999,
                       "adminpw", "svcpw", "root-token-deadbeef", "diagpw")
    creds = _read_credentials(creds_path)
    assert creds["NX_SERVICE_TOKEN"] == "root-token-deadbeef"
    # Decoupling: the token is independent of the DB passwords.
    assert creds["NX_DB_PASS"] == "svcpw"
    assert creds["NX_DB_ADMIN_PASS"] == "adminpw"
    # RDR-182 P2.1: the diagnostic role's credentials ride the same file.
    assert creds["NX_DB_DIAG_USER"] == "nexus_diag"
    assert creds["NX_DB_DIAG_PASS"] == "diagpw"


def test_write_credentials_is_0600(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    _write_credentials(creds_path, tmp_path / "postgres", 15999,
                       "adminpw", "svcpw", "tok", "diagpw")
    mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert mode == 0o600


def test_persist_service_token_backfills_and_preserves(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    # Simulate a pre-gmiaf.32.5 file with no NX_SERVICE_TOKEN.
    creds_path.write_text(
        "PG_DATA=/x/postgres\nPG_PORT=15999\nNX_DB_PASS=svcpw\n"
    )
    creds_path.chmod(0o600)

    _persist_service_token(creds_path, "backfilled-token-cafe")

    creds = _read_credentials(creds_path)
    assert creds["NX_SERVICE_TOKEN"] == "backfilled-token-cafe"
    # Every prior key survives the rewrite.
    assert creds["PG_DATA"] == "/x/postgres"
    assert creds["PG_PORT"] == "15999"
    assert creds["NX_DB_PASS"] == "svcpw"
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


def test_persist_service_token_is_idempotent(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    creds_path.write_text("PG_PORT=15999\nNX_DB_PASS=svcpw\n")
    creds_path.chmod(0o600)

    _persist_service_token(creds_path, "first-token")
    # A second call with a DIFFERENT token must NOT append a second line; the
    # original token survives (no silent shadow-write).
    _persist_service_token(creds_path, "second-token-different")

    text = creds_path.read_text()
    assert text.count("NX_SERVICE_TOKEN=") == 1
    assert _read_credentials(creds_path)["NX_SERVICE_TOKEN"] == "first-token"


def test_persist_service_token_handles_missing_trailing_newline(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    creds_path.write_text("PG_PORT=15999")  # no trailing newline
    creds_path.chmod(0o600)

    _persist_service_token(creds_path, "tok2")

    creds = _read_credentials(creds_path)
    assert creds["PG_PORT"] == "15999"
    assert creds["NX_SERVICE_TOKEN"] == "tok2"


def test_load_service_credentials_into_env(tmp_path: Path, monkeypatch) -> None:
    # RDR-002 ez5.13: the one-command guided upgrade self-loads pg_credentials.
    from nexus.db.pg_provision import (
        _write_credentials,
        load_service_credentials_into_env,
    )

    creds_path = tmp_path / "pg_credentials"
    _write_credentials(creds_path, tmp_path / "postgres", 15999,
                       "adminpw", "svcpw", "root-token-deadbeef", "diagpw")

    for k in ("NX_SERVICE_TOKEN", "NX_STORAGE_BACKEND", "NX_DB_USER"):
        monkeypatch.delenv(k, raising=False)

    loaded = load_service_credentials_into_env(tmp_path)
    assert loaded is True
    import os
    assert os.environ["NX_SERVICE_TOKEN"] == "root-token-deadbeef"
    assert os.environ["NX_STORAGE_BACKEND"] == "service"


def test_load_service_credentials_does_not_clobber_existing_token(
    tmp_path: Path, monkeypatch
) -> None:
    from nexus.db.pg_provision import (
        _write_credentials,
        load_service_credentials_into_env,
    )

    creds_path = tmp_path / "pg_credentials"
    _write_credentials(creds_path, tmp_path / "postgres", 15999,
                       "adminpw", "svcpw", "file-token", "diagpw")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "user-exported-token")

    assert load_service_credentials_into_env(tmp_path) is True
    import os
    # setdefault: a user-exported token wins over the file's.
    assert os.environ["NX_SERVICE_TOKEN"] == "user-exported-token"


def test_load_service_credentials_no_file_reports_token_absence(
    tmp_path: Path, monkeypatch
) -> None:
    from nexus.db.pg_provision import load_service_credentials_into_env

    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    assert load_service_credentials_into_env(tmp_path) is False
