# SPDX-License-Identifier: AGPL-3.0-or-later
"""A postmaster started with ``-k <dir>`` puts its unix socket there, and a path past ``sun_path``
makes Postgres refuse to start with only ``pg_ctl: could not start server`` to show for it.
A hand-run shard on chas whose tempdir was 71 bytes reported 883 such errors (labrun-pex). The
tmpfs root and an ``NX_TEST_PG_ROOT`` override were length-checked when chosen; the tempdir
fallback and the integration fixtures' own clusters were not. :func:`tests._pg_ctl.pg_ctl_start`
is where every ``-k`` start passes, so the check is there."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests import _pg_ctl

pytestmark = pytest.mark.skipif(os.name == "nt", reason="no -k unix socket on Windows")


def _fake_pg_ctl(tmp_path: Path) -> tuple[Path, Path]:
    ran = tmp_path / "pg_ctl-ran"
    exe = tmp_path / "pg_ctl"
    exe.write_text(f"#!/bin/sh\ntouch {ran}\nexit 0\n")
    exe.chmod(0o755)
    return exe, ran


def _pgdata(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def test_a_socket_dir_past_sun_path_fails_loud_before_pg_ctl_runs(tmp_path, monkeypatch) -> None:
    exe, ran = _fake_pg_ctl(tmp_path)
    pgdata = _pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", len(os.fsencode(str(pgdata))) + 10)
    with pytest.raises(ValueError, match="sun_path") as err:
        _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", f"-p 5555 -k {pgdata}", check=False)
    assert "TMPDIR" in str(err.value) and "NX_TEST_PG_ROOT" in str(err.value)
    assert not ran.exists(), "pg_ctl ran for a socket path Postgres would refuse"


def test_a_socket_dir_that_fits_starts_as_before(tmp_path, monkeypatch) -> None:
    exe, ran = _fake_pg_ctl(tmp_path)
    pgdata = _pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", len(os.fsencode(str(pgdata))) + len("/.s.PGSQL.65535"))
    proc = _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", f"-k {pgdata}", check=False)
    assert proc.returncode == 0 and ran.exists()


def test_a_tcp_only_start_without_k_is_not_checked(tmp_path, monkeypatch) -> None:
    """``provision()``-style clusters run with no socket; a long data dir is fine for them."""
    exe, ran = _fake_pg_ctl(tmp_path)
    pgdata = _pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", 1)
    _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", "-p 5555", check=False)
    assert ran.exists()


@pytest.mark.parametrize("options", ["-p 1 -k /x/y", "-k/x/y -p 1", "-c a=b -k '/x/y'"])
def test_the_socket_dir_is_read_from_every_k_spelling(options) -> None:
    assert _pg_ctl._socket_dir(options) == "/x/y"


def test_the_limit_counts_bytes_not_characters(monkeypatch) -> None:
    sock_dir = "/" + "é" * 10  # 21 bytes, 11 characters
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", 11 + len("/.s.PGSQL.65535"))
    with pytest.raises(ValueError, match="sun_path"):
        _pg_ctl.check_socket_dir(f"-k {sock_dir}")


def test_the_limit_is_the_platforms() -> None:
    assert _pg_ctl.SUN_PATH_MAX == (103 if sys.platform == "darwin" else 107)
