# SPDX-License-Identifier: AGPL-3.0-or-later
"""pg_background in the Windows bundle build (nexus-f3yqd.2): what is installed, the pin refusals, the
licence notices and the default_version check. Split from tests/test_pg_bundle_windows.py, which is over its
size cap; it reuses that module's fake build runner and staged tree."""

from __future__ import annotations

from pathlib import Path

import pytest

import build_pg_bundle_windows as bw
from tests.test_pg_bundle_windows import (
    PG_BACKGROUND_DATA,
    PG_BACKGROUND_DECOYS,
    PG_BACKGROUND_MAKEFILE,
    FakeBuildRunner,
    _build,
    _microsoft_signed,
    stage_bundle,
)


@pytest.fixture(autouse=True)
def _runtime_dlls_are_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw, "read_authenticode", _microsoft_signed)


def test_the_makefile_data_list_is_parsed_as_upstream_writes_it() -> None:
    assert bw.makefile_data_files(PG_BACKGROUND_MAKEFILE) == list(PG_BACKGROUND_DATA)
    with pytest.raises(bw.BuildError, match="no DATA"):
        bw.makefile_data_files("MODULE_big = x\n")


def test_default_version_comes_from_the_control_file() -> None:
    assert bw.control_default_version("comment = 'c'\ndefault_version = '2.0'\n") == "2.0"
    assert bw.control_default_version("default_version = ''\n") is None
    assert bw.control_default_version("comment = 'c'\n") is None


def test_a_build_installs_exactly_the_makefile_data_files_and_no_base_script(tmp_path: Path) -> None:
    prefix, _, _ = _build(tmp_path)
    ext = prefix / "share" / "postgresql" / "extension"
    installed = sorted(f.name for f in ext.glob("pg_background*"))
    assert installed == sorted(["pg_background.control", *(Path(n).name for n in PG_BACKGROUND_DATA)])
    for decoy in PG_BACKGROUND_DECOYS:
        assert not (ext / Path(decoy).name).exists(), f"{decoy} is not in the Makefile's DATA list"


def test_install_refuses_a_data_list_without_the_default_version_script(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "extension").mkdir(parents=True)
    (src / "pg_background.dll").write_bytes(b"MZ")
    (src / "pg_background.control").write_text("default_version = '2.0'\n")
    (src / "Makefile").write_text("DATA = extension/pg_background--1.9--1.10.sql\n")
    (src / "extension" / "pg_background--1.9--1.10.sql").write_text("s")
    dirs = {"pkglibdir": str(tmp_path / "lib"), "sharedir": str(tmp_path / "share")}
    (tmp_path / "lib").mkdir()
    with pytest.raises(bw.BuildError, match=r"pg_background--2\.0\.sql.*not among"):
        bw.pg_background_install(src, dirs)


def test_a_pg_background_version_without_a_pin_is_refused_before_it_is_cloned(tmp_path: Path) -> None:
    runners: list[FakeBuildRunner] = []
    with pytest.raises(bw.BuildError, match="no pinned commit for pg_background v2.0.5"):
        _build(tmp_path, pins=bw.Pins("17.5", "v0.8.2", "v2.0.5"), runners=runners)
    assert not any(c[1][:2] == ["git", "clone"] and c[1][-1].endswith("pg_background") for c in runners[0].calls)


def test_pg_background_must_resolve_to_the_pinned_commit(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError, match="pg_background v2.0.4 resolved to commit f{40}.*the pin is c1db"):
        _build(tmp_path, pg_background_head="f" * 40)


def test_a_component_without_its_licence_file_stops_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = FakeBuildRunner.run

    def no_pg_background_licence(self, argv, *, cwd, env, log):  # type: ignore[no-untyped-def]
        real(self, argv, cwd=cwd, env=env, log=log)
        if argv[0] == "git" and Path(argv[-1]).name == "pg_background":
            (Path(argv[-1]) / "LICENSE").unlink()

    monkeypatch.setattr(FakeBuildRunner, "run", no_pg_background_licence)
    with pytest.raises(bw.BuildError, match="pg_background: none of LICENSE, LICENSE.txt.*without its licence"):
        _build(tmp_path)


def test_copy_license_takes_the_first_name_present_and_raises_when_none(tmp_path: Path) -> None:
    (tmp_path / "LICENSE.txt").write_text("text")
    bw.copy_license(tmp_path, ("LICENSE", "LICENSE.txt"), tmp_path / "out.txt", "x")
    assert (tmp_path / "out.txt").read_text() == "text"
    with pytest.raises(bw.BuildError, match="refusing to ship it"):
        bw.copy_license(tmp_path / "empty", ("LICENSE",), tmp_path / "o2.txt", "x")


@pytest.mark.parametrize("name", bw.LICENSE_FILES)
def test_verify_layout_requires_each_licence_file(tmp_path: Path, name: str) -> None:
    bundle = stage_bundle(tmp_path / "bundle")
    (bundle / bw.LICENSES_DIR / name).unlink()
    assert any(name in p for p in bw.verify_layout(bundle))


@pytest.mark.parametrize("ctl", ["vector", "pg_trgm", "pg_background"])
def test_verify_layout_derives_the_script_name_from_each_control_file(tmp_path: Path, ctl: str) -> None:
    bundle = stage_bundle(tmp_path / "bundle")
    ext = bundle / "share" / "postgresql" / "extension"
    (ext / f"{ctl}.control").write_text("default_version = '9.9'\n")  # the staged script is --1.0
    assert any(f"{ctl}--9.9.sql" in p for p in bw.verify_layout(bundle))
    (ext / f"{ctl}.control").write_text("comment = 'x'\n")
    assert any(f"{ctl}.control names no default_version" in p for p in bw.verify_layout(bundle))


def test_an_upgrade_script_ending_at_the_default_version_is_enough(tmp_path: Path) -> None:
    """PostgreSQL's own pg_trgm ships a base script plus upgrade scripts and no <ext>--<default_version>.sql."""
    bundle = stage_bundle(tmp_path / "bundle")
    ext = bundle / "share" / "postgresql" / "extension"
    (ext / "pg_trgm--1.0.sql").rename(ext / "pg_trgm--0.9--1.0.sql")
    assert bw.verify_layout(bundle) == []
    (ext / "pg_trgm--0.9--1.0.sql").unlink()
    assert any("pg_trgm--1.0.sql" in p for p in bw.verify_layout(bundle))
