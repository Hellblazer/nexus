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
                       "adminpw", "svcpw", "root-token-deadbeef")
    creds = _read_credentials(creds_path)
    assert creds["NX_SERVICE_TOKEN"] == "root-token-deadbeef"
    # Decoupling: the token is independent of the DB passwords.
    assert creds["NX_DB_PASS"] == "svcpw"
    assert creds["NX_DB_ADMIN_PASS"] == "adminpw"


def test_write_credentials_is_0600(tmp_path: Path) -> None:
    creds_path = tmp_path / "pg_credentials"
    _write_credentials(creds_path, tmp_path / "postgres", 15999,
                       "adminpw", "svcpw", "tok")
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
