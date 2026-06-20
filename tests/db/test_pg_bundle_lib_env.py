# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4mm24 — relocatable PG bundle binaries have no RPATH; _bundle_lib_env
points the loader at the bundle's sibling lib/ so initdb/pg_ctl find libpq.so.5
on a minimal base."""
from __future__ import annotations

import os
from pathlib import Path

from nexus.db.pg_provision import _bundle_lib_env


def _bundle(tmp_path: Path) -> Path:
    (tmp_path / "bundle" / "bin").mkdir(parents=True)
    (tmp_path / "bundle" / "lib").mkdir(parents=True)
    initdb = tmp_path / "bundle" / "bin" / "initdb"
    initdb.write_text("")
    return initdb


def test_sets_ld_library_path_to_sibling_lib(tmp_path: Path) -> None:
    initdb = _bundle(tmp_path)
    env = _bundle_lib_env([str(initdb)], {})
    assert env["LD_LIBRARY_PATH"] == str(tmp_path / "bundle" / "lib")


def test_prepends_to_existing_ld_library_path(tmp_path: Path) -> None:
    initdb = _bundle(tmp_path)
    env = _bundle_lib_env([str(initdb)], {"LD_LIBRARY_PATH": "/existing"})
    parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "bundle" / "lib")
    assert "/existing" in parts


def test_no_lib_dir_leaves_env_untouched(tmp_path: Path) -> None:
    # bin with no sibling lib/ (e.g. a system PG layout without ../lib)
    (tmp_path / "bin").mkdir()
    initdb = tmp_path / "bin" / "initdb"
    initdb.write_text("")
    env = _bundle_lib_env([str(initdb)], {"FOO": "bar"})
    assert "LD_LIBRARY_PATH" not in env
    assert env["FOO"] == "bar"


def test_defaults_to_os_environ_when_env_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SENTINEL_VAR", "present")
    initdb = _bundle(tmp_path)
    env = _bundle_lib_env([str(initdb)], None)
    assert env["SENTINEL_VAR"] == "present"  # inherited os.environ
    assert env["LD_LIBRARY_PATH"] == str(tmp_path / "bundle" / "lib")
