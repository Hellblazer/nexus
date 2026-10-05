# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows PG bundle build script and relocation smoke (RDR-224 P2.1, nexus-f9bgu.12).

Both scripts are Python with the platform and the process runner injected, so
every branch here runs on every OS: nothing skip-passes on a laptop. The real
MSVC build and the real Windows smoke ran on qwentescence (recorded on the
bead); what this file pins is the decisions that burned the 2026-09-29 spike
and the contract the client (nexus-f9bgu.15) extracts against.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

import build_pg_bundle_windows as bw
import pg_bundle_windows_smoke as sm

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build_pg_bundle_windows.py"
WORKFLOWS = REPO / ".github" / "workflows"


# --------------------------------------------------------------------------- #
# Pins and the cache key
# --------------------------------------------------------------------------- #


def test_defaults_equal_the_shell_script_and_the_workflow_pins() -> None:
    sh = (REPO / "scripts" / "build_pg_bundle.sh").read_text()
    sh_pg = re.search(r'PG_VERSION="\$\{PG_VERSION:-([^}]+)\}"', sh)
    sh_pgv = re.search(r'PGVECTOR_VERSION="\$\{PGVECTOR_VERSION:-([^}]+)\}"', sh)
    assert sh_pg and sh_pgv
    assert bw.DEFAULT_PG_VERSION == sh_pg.group(1)
    assert bw.DEFAULT_PGVECTOR_VERSION == sh_pgv.group(1)
    for wf in ("engine-service-release.yml", "pg-bundle-cache-seed.yml"):
        text = (WORKFLOWS / wf).read_text()
        assert f'PG_VERSION: "{bw.DEFAULT_PG_VERSION}"' in text, wf
        assert f'PGVECTOR_VERSION: "{bw.DEFAULT_PGVECTOR_VERSION}"' in text, wf


def test_pins_from_env_overrides_and_falls_back() -> None:
    assert bw.pins_from_env({}) == bw.Pins("17.5", "v0.8.2")
    assert bw.pins_from_env({"PG_VERSION": "17.6", "PGVECTOR_VERSION": "v0.9.0"}) == bw.Pins(
        "17.6", "v0.9.0"
    )
    assert bw.pins_from_env({"PG_VERSION": ""}).pg_version == "17.5"


def test_cache_key_has_the_shape_of_the_other_bundles() -> None:
    key = bw.cache_key(bw.Pins("17.5", "v0.8.2"), "qwentescence", "ab" * 32)
    assert key == (
        "pg-bundle-windows-x64-qwentescence-pg17.5-pgvectorv0.8.2-img-native-" + "ab" * 32
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, r, h: (bw.Pins("17.6", p.pgvector_version), r, h),
        lambda p, r, h: (bw.Pins(p.pg_version, "v0.9.0"), r, h),
        lambda p, r, h: (p, "windows-latest", h),
        lambda p, r, h: (p, r, "cd" * 32),
    ],
    ids=["pg-pin", "pgvector-pin", "runner", "script-hash"],
)
def test_every_input_changes_the_key(mutate) -> None:
    base = (bw.Pins("17.5", "v0.8.2"), "qwentescence", "ab" * 32)
    assert bw.cache_key(*mutate(*base)) != bw.cache_key(*base)


def test_cache_key_is_deterministic_and_refuses_a_bad_runner() -> None:
    args = (bw.Pins("17.5", "v0.8.2"), "qwentescence", "ab" * 32)
    assert bw.cache_key(*args) == bw.cache_key(*args)
    for bad in ("", "has space", "a/b"):
        with pytest.raises(bw.BuildError):
            bw.cache_key(args[0], bad, args[2])


def test_cache_key_cli_hashes_this_very_script(capsys: pytest.CaptureFixture[str]) -> None:
    rc = bw.main(["cache-key", "--runner", "qwentescence"], env={})
    assert rc == 0
    digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert capsys.readouterr().out.strip() == (
        f"pg-bundle-windows-x64-qwentescence-pg17.5-pgvectorv0.8.2-img-native-{digest}"
    )


def test_cache_key_cli_takes_pins_and_runner_from_env(capsys: pytest.CaptureFixture[str]) -> None:
    env = {"PG_VERSION": "17.9", "PGVECTOR_VERSION": "v1.0.0", "PG_BUNDLE_RUNNER": "r1"}
    assert bw.main(["cache-key"], env=env) == 0
    assert "-r1-pg17.9-pgvectorv1.0.0-" in capsys.readouterr().out


def test_cache_key_cli_without_a_runner_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    assert bw.main(["cache-key"], env={}) == 1
    assert "runner" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Hazard planners
# --------------------------------------------------------------------------- #

LISTING_20 = "\n".join(
    f"{p}: CUSTOM_COMMAND"
    for p in (
        "src/fe_utils/psqlscan.c", "src/backend/bootstrap/bootscanner.c",
        "src/backend/bootstrap/bootparse.c", "src/backend/parser/scan.c",
        "src/backend/parser/gram.c", "src/backend/replication/repl_scanner.c",
        "src/backend/replication/repl_gram.c", "src/backend/replication/syncrep_scanner.c",
        "src/backend/replication/syncrep_gram.c", "src/backend/utils/adt/jsonpath_scan.c",
        "src/backend/utils/adt/jsonpath_gram.c", "src/bin/pgbench/exprscan.c",
        "src/bin/pgbench/exprparse.c", "src/pl/plpgsql/src/pl_gram.c",
        "contrib/cube/cubescan.c", "contrib/cube/cubeparse.c", "contrib/seg/segscan.c",
        "contrib/seg/segparse.c", "src/test/isolation/specscanner.c",
        "src/test/isolation/specparse.c",
    )
)
NOISE = "src/backend/postgres.exe: c_LINKER\nsrc/common/foo.c: c_COMPILER\nall: phony\n"


def test_generated_targets_picks_the_20_and_ignores_the_rest() -> None:
    got = bw.generated_targets(NOISE + LISTING_20 + "\n" + NOISE)
    assert len(got) == 20
    assert "src/backend/parser/gram.c" in got and "contrib/seg/segparse.c" in got
    assert not any("postgres.exe" in t or t.endswith("foo.c") for t in got)


def test_generated_targets_refuses_a_drifted_count() -> None:
    short = "\n".join(LISTING_20.splitlines()[:-1])
    with pytest.raises(bw.BuildError, match="expected 20.*found 19"):
        bw.generated_targets(short)
    with pytest.raises(bw.BuildError):
        bw.generated_targets("")


def test_generation_is_one_ninja_at_a_time_with_j1() -> None:
    targets = bw.generated_targets(LISTING_20)
    cmds = bw.ninja_generate_cmds(Path("B"), targets)
    assert len(cmds) == 20
    assert all(c[:2] == ["ninja", "-j1"] and c[-1] in targets for c in cmds)


def test_meson_setup_is_lean_and_has_the_prefix() -> None:
    cmd = bw.meson_setup_cmd(Path("S"), Path("B"), Path("P"))
    assert cmd[:2] == ["meson", "setup"] and "--prefix=P" in cmd
    for opt in ("-Dauto_features=disabled", "-Dssl=none", "-Dicu=disabled",
                "-Dzlib=disabled", "-Dreadline=disabled", "-Dbuildtype=release"):
        assert opt in cmd


PG_DIRS = {
    "includedir": "C:/b/include/postgresql",
    "includedir-server": "C:/b/include/postgresql/server",
    "pkglibdir": "C:/b/lib/postgresql",
    "sharedir": "C:/b/share/postgresql",
    "libdir": "C:/b/lib",
}


def test_pgvector_gets_pg_configs_directories_and_installs_second() -> None:
    build_cmd, install_cmd = bw.pgvector_make_cmds(Path("C:/b"), PG_DIRS)
    assert build_cmd[:4] == ["nmake", "/NOLOGO", "/F", "Makefile.win"]
    for name, key in (("INCLUDEDIR", "includedir"), ("INCLUDEDIR_SERVER", "includedir-server"),
                      ("PKGLIBDIR", "pkglibdir"), ("SHAREDIR", "sharedir"), ("LIBDIR", "libdir")):
        assert f"{name}={PG_DIRS[key]}" in build_cmd
    assert any(a.startswith("PGROOT=") for a in build_cmd)
    assert install_cmd == [*build_cmd, "install"]


def _flex_root(tmp_path: Path, name: str, *, data: bool) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "win_bison.exe").write_bytes(b"x")
    if data:
        (d / "data").mkdir()
    return d


def test_flex_bison_dir_is_the_package_dir_not_the_links_shim(tmp_path: Path) -> None:
    packages = tmp_path / "Packages"
    packages.mkdir()
    _flex_root(packages, "WinFlexBison.win_flex_bison_Microsoft.Winget.Source_x", data=True)
    links = _flex_root(tmp_path, "Links", data=False)  # the shim: stub, no data/
    found = bw.find_flex_bison_dir({}, search_roots=[packages])
    assert found.name.startswith("WinFlexBison.win_flex_bison_")
    assert (found / "data").is_dir()
    # Non-vacuity: the shim alone is refused, not accepted for having win_bison.exe.
    with pytest.raises(bw.BuildError, match="Links shim"):
        bw.find_flex_bison_dir({"WIN_FLEX_BISON_DIR": str(links)}, search_roots=[])


def test_flex_bison_explicit_dir_wins(tmp_path: Path) -> None:
    good = _flex_root(tmp_path, "mine", data=True)
    assert bw.find_flex_bison_dir({"WIN_FLEX_BISON_DIR": str(good)}, search_roots=[]) == good


# --------------------------------------------------------------------------- #
# Visual Studio and the VC++ redist (P0.6 conditions)
# --------------------------------------------------------------------------- #


def _vs_tree(root: Path, *, versions=("14.38.33135", "14.44.35208"), marker: str | None = "14.38.33135",
             crt="Microsoft.VC143.CRT", dlls=bw.VC_RUNTIME_DLLS) -> Path:
    for v in versions:
        d = root / "VC" / "Redist" / "MSVC" / v / "x64" / crt
        d.mkdir(parents=True)
        for dll in dlls:
            (d / dll).write_bytes(f"{v}:{dll}".encode())
        dbg = root / "VC" / "Redist" / "MSVC" / v / "debug_nonredist" / "x64" / "Microsoft.VC143.DebugCRT"
        dbg.mkdir(parents=True)
        (dbg / "vcruntime140d.dll").write_bytes(b"debug")
    build = root / "VC" / "Auxiliary" / "Build"
    build.mkdir(parents=True)
    if marker:
        (build / "Microsoft.VCRedistVersion.default.txt").write_text(marker + "\r\n")
    return root


def test_redist_follows_the_installs_own_marker_and_never_debug(tmp_path: Path) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path))
    assert redist.version == "14.38.33135"  # the marker, not the newest folder
    assert redist.directory.name == "Microsoft.VC143.CRT"
    assert "debug_nonredist" not in redist.directory.parts


def test_redist_without_a_marker_takes_the_highest_version(tmp_path: Path) -> None:
    assert bw.find_redist(_vs_tree(tmp_path, marker=None)).version == "14.44.35208"


def test_redist_refuses_a_crt_folder_missing_a_dll(tmp_path: Path) -> None:
    tree = _vs_tree(tmp_path, dlls=bw.VC_RUNTIME_DLLS[:3])
    with pytest.raises(bw.BuildError, match="msvcp140_1.dll"):
        bw.find_redist(tree)


def test_redist_refuses_an_install_without_one(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError, match="redist is not installed"):
        bw.find_redist(tmp_path)


class _Capture(bw.Runner):
    def __init__(self, out: str) -> None:
        self.out = out

    def capture(self, argv, *, cwd, env):  # type: ignore[override]
        return self.out


def _vswhere(tmp_path: Path) -> Path:
    p = tmp_path / "vswhere.exe"
    p.write_bytes(b"x")
    return p


def test_a_preview_visual_studio_is_refused(tmp_path: Path) -> None:
    out = json.dumps([{"installationPath": "C:/vs", "isPrerelease": True}])
    with pytest.raises(bw.BuildError, match="Preview"):
        bw.find_vs_install({}, _Capture(out), vswhere=_vswhere(tmp_path))


def test_a_released_visual_studio_is_taken(tmp_path: Path) -> None:
    out = json.dumps([{"installationPath": "C:/vs", "isPrerelease": False}])
    assert bw.find_vs_install({}, _Capture(out), vswhere=_vswhere(tmp_path)) == Path("C:/vs")
    with pytest.raises(bw.BuildError, match="no Visual Studio"):
        bw.find_vs_install({}, _Capture("[]"), vswhere=_vswhere(tmp_path))
    assert bw.find_vs_install({"VS_INSTALL_PATH": "D:/x"}, _Capture("")) == Path("D:/x")


def test_runtime_dlls_are_the_four_and_the_smoke_agrees() -> None:
    assert bw.VC_RUNTIME_DLLS == (
        "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    )
    assert sm.VC_RUNTIME_DLLS == bw.VC_RUNTIME_DLLS


def test_copy_runtime_ships_the_dlls_unmodified_with_the_notice(tmp_path: Path) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    bundle = tmp_path / "bundle"
    digests = bw.copy_runtime(bundle, redist, bw.Pins("17.5", "v0.8.2"))
    for dll in bw.VC_RUNTIME_DLLS:
        assert (bundle / "bin" / dll).read_bytes() == (redist.directory / dll).read_bytes()
        assert digests[dll] == hashlib.sha256((redist.directory / dll).read_bytes()).hexdigest()
    notice = (bundle / bw.NOTICE_NAME).read_text()
    for needle in ("Microsoft's", "NOT covered by the AGPL", "unmodified", redist.version,
                   "reverse engineer", "separately", "aka.ms/vs/17/redistribution",
                   *bw.VC_RUNTIME_DLLS, *digests.values()):
        assert needle in notice, needle
    # Only the four: nothing else from the CRT folder (concrt140 etc.) leaks in.
    assert sorted(p.name for p in (bundle / "bin").iterdir()) == sorted(bw.VC_RUNTIME_DLLS)


def test_refresh_replaces_stale_dlls_from_the_current_redist(tmp_path: Path) -> None:
    old = bw.find_redist(_vs_tree(tmp_path / "old", versions=("14.38.33135",)))
    new = bw.find_redist(_vs_tree(tmp_path / "new", versions=("14.44.35208",), marker="14.44.35208"))
    bundle = tmp_path / "bundle"
    bw.copy_runtime(bundle, old, bw.Pins("17.5", "v0.8.2"))
    bw.copy_runtime(bundle, new, bw.Pins("17.5", "v0.8.2"))
    assert (bundle / "bin" / "vcruntime140.dll").read_bytes() == b"14.44.35208:vcruntime140.dll"
    assert "14.44.35208" in (bundle / bw.NOTICE_NAME).read_text()


# --------------------------------------------------------------------------- #
# Layout and packaging (the contract bead .15's client extracts against)
# --------------------------------------------------------------------------- #


def stage_bundle(prefix: Path, *, runtime: bool = True) -> Path:
    for d in ("bin", "include", "lib/postgresql", "share/postgresql/extension"):
        (prefix / d).mkdir(parents=True, exist_ok=True)
    for b in bw.REQUIRED_BINARIES:
        (prefix / "bin" / f"{b}.exe").write_bytes(b"MZ")
    (prefix / "bin" / "libpq.dll").write_bytes(b"MZ")
    (prefix / "lib" / "postgresql" / "vector.dll").write_bytes(b"MZ")
    for ctl in ("vector", "pg_trgm"):
        (prefix / "share" / "postgresql" / "extension" / f"{ctl}.control").write_text("x")
    (prefix / ".build_prefix").write_text("C:\\build\\bundle\n")
    if runtime:
        for dll in bw.VC_RUNTIME_DLLS:
            (prefix / "bin" / dll).write_bytes(b"MZ")
        (prefix / bw.NOTICE_NAME).write_text("notice")
    return prefix


def test_a_complete_tree_verifies_clean(tmp_path: Path) -> None:
    assert bw.verify_layout(stage_bundle(tmp_path / "bundle")) == []


@pytest.mark.parametrize(
    "victim,needle",
    [
        ("bin/initdb.exe", "initdb.exe"), ("bin/pg_ctl.exe", "pg_ctl.exe"),
        ("bin/postgres.exe", "postgres.exe"), ("bin/libpq.dll", "libpq.dll"),
        ("lib/postgresql/vector.dll", "vector.dll"),
        ("share/postgresql/extension/vector.control", "vector.control"),
        ("share/postgresql/extension/pg_trgm.control", "pg_trgm.control"),
        (".build_prefix", ".build_prefix"),
        ("bin/vcruntime140.dll", "vcruntime140.dll"), ("bin/vcruntime140_1.dll", "vcruntime140_1.dll"),
        ("bin/msvcp140.dll", "msvcp140.dll"), ("bin/msvcp140_1.dll", "msvcp140_1.dll"),
        (bw.NOTICE_NAME, bw.NOTICE_NAME),
    ],
)
def test_each_missing_piece_is_named(tmp_path: Path, victim: str, needle: str) -> None:
    bundle = stage_bundle(tmp_path / "bundle")
    (bundle / victim).unlink()
    problems = bw.verify_layout(bundle)
    assert problems and any(needle in p for p in problems), problems


def test_package_is_one_bundle_directory_with_a_sha256_file(tmp_path: Path) -> None:
    bundle = stage_bundle(tmp_path / "work" / "bundle")
    archive = bw.package(bundle, tmp_path / "dist")
    assert archive.name == "nexus-pg-windows-x64.txz"
    with tarfile.open(archive, "r:xz") as tf:
        names = tf.getnames()
        assert names and all(n == "bundle" or n.startswith("bundle/") for n in names)
        assert {"bundle/bin/initdb.exe", "bundle/bin/vcruntime140.dll", "bundle/.build_prefix",
                f"bundle/{bw.NOTICE_NAME}"} <= set(names)
        assert all(m.uid == 0 and m.gid == 0 for m in tf.getmembers())
        tf.extractall(tmp_path / "out", filter="data")  # the client's own extraction filter
    assert (tmp_path / "out" / "bundle" / "bin" / "pg_ctl.exe").is_file()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (tmp_path / "dist" / "nexus-pg-windows-x64.txz.sha256").read_text() == (
        f"{digest}  nexus-pg-windows-x64.txz\n"
    )


def test_package_refuses_an_incomplete_bundle(tmp_path: Path) -> None:
    bundle = stage_bundle(tmp_path / "bundle", runtime=False)
    with pytest.raises(bw.BuildError, match="incomplete"):
        bw.package(bundle, tmp_path / "dist")


# --------------------------------------------------------------------------- #
# The build, driven end to end through fakes
# --------------------------------------------------------------------------- #


class FakeBuildRunner(bw.Runner):
    def __init__(self, prefix: Path) -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def run(self, argv, *, cwd, env, log):  # type: ignore[override]
        argv = list(argv)
        self.calls.append(("run", argv, dict(env)))
        if argv[0] == "ninja" and argv[-1] == "install":
            stage_bundle(self.prefix, runtime=False)
            (self.prefix / ".build_prefix").unlink()
        if argv[0] == "git":
            dest = Path(argv[-1])
            dest.mkdir(parents=True)
            (dest / "LICENSE").write_text("pgvector license")
        if argv[0] == "nmake" and argv[-1] == "install":
            (self.prefix / "lib" / "postgresql" / "vector.dll").write_bytes(b"MZ")

    def capture(self, argv, *, cwd, env):  # type: ignore[override]
        argv = list(argv)
        self.calls.append(("capture", argv, dict(env)))
        if argv[0] == "ninja":
            return NOISE + LISTING_20
        return PG_DIRS[argv[-1].removeprefix("--")] + "\r\n"


def _host(runner: FakeBuildRunner, *, corrupt: bool = False) -> bw.Host:
    body = b"pg tarball"

    def fetch(url: str, dest: Path) -> None:
        if url.endswith(".sha256"):
            digest = "0" * 64 if corrupt else hashlib.sha256(body).hexdigest()
            dest.write_text(f"{digest}  postgresql-17.5.tar.bz2\n")
        else:
            dest.write_bytes(body)

    def unpack(archive: Path, dest: Path) -> None:
        src = dest / "postgresql-17.5"
        src.mkdir()
        (src / "COPYRIGHT").write_text("PostgreSQL license")

    return bw.Host(runner=runner, fetch=fetch, unpack=unpack)


def _toolchain(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    for t in bw.REQUIRED_TOOLS:
        for name in (t, f"{t}.exe"):
            f = tools / name
            f.write_text("#!/bin/sh\n")
            f.chmod(0o755)
    flex = tmp_path / "flexbison"
    flex.mkdir()
    return {"PATH": str(tools)}, flex


def _build(tmp_path: Path, **kw):
    prefix = tmp_path / "bundle"
    work = tmp_path / "work"
    vs_env, flex = _toolchain(tmp_path)
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    runner = FakeBuildRunner(prefix)
    bw.build(
        prefix=kw.get("prefix", prefix), work=work, pins=bw.Pins("17.5", "v0.8.2"), jobs=6,
        env={}, vs_env=vs_env, flex_bison_dir=flex, redist=redist,
        host=_host(runner, corrupt=kw.get("corrupt", False)),
    )
    return prefix, runner, flex


def _shape(call: tuple[str, list[str], dict[str, str]]) -> str:
    kind, argv, _ = call
    if argv[0] == "ninja":
        if "targets" in argv:
            return "ninja-list"
        if "-j1" in argv:
            return "ninja-gen"
        return "ninja-install" if argv[-1] == "install" else "ninja-build"
    if argv[0] == "nmake":
        return "nmake-install" if argv[-1] == "install" else "nmake-build"
    return f"{kind}:{Path(argv[0]).stem}"


def test_build_runs_the_steps_in_the_order_the_hazards_demand(tmp_path: Path) -> None:
    prefix, runner, flex = _build(tmp_path)
    shapes = [_shape(c) for c in runner.calls]
    assert shapes[:2] == ["run:meson", "ninja-list"]
    gen = [i for i, s in enumerate(shapes) if s == "ninja-gen"]
    assert len(gen) == 20 and gen == list(range(2, 22)), "20 serial generations right after listing"
    assert shapes[22:25] == ["ninja-build", "ninja-install", "run:git"]
    assert shapes.count("capture:pg_config") == 5  # pg_config asked, not guessed
    assert shapes[-2:] == ["nmake-build", "nmake-install"]
    # The parallel build is the capped one, and it comes after every generation.
    build_call = next(c for c in runner.calls if _shape(c) == "ninja-build")
    assert "-j6" in build_call[1]
    assert bw.verify_layout(prefix) == []
    assert (prefix / "licenses" / "postgresql-COPYRIGHT.txt").is_file()
    assert (prefix / "licenses" / "pgvector-LICENSE.txt").is_file()
    assert (prefix / ".build_prefix").read_text().strip() == os.path.realpath(prefix)


def test_flex_bison_dir_leads_path_for_every_step_and_cc_is_cl(tmp_path: Path) -> None:
    _, runner, flex = _build(tmp_path)
    for _, argv, env in runner.calls:
        assert env["PATH"].split(os.pathsep)[0] == str(flex), argv
        assert env["CC"] == "cl", argv


def test_pgvector_receives_pg_configs_directories(tmp_path: Path) -> None:
    _, runner, _ = _build(tmp_path)
    nmake = next(c[1] for c in runner.calls if _shape(c) == "nmake-build")
    assert f"PKGLIBDIR={PG_DIRS['pkglibdir']}" in nmake
    assert f"INCLUDEDIR_SERVER={PG_DIRS['includedir-server']}" in nmake


def test_a_tarball_that_misses_its_published_sha256_stops_before_meson(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError, match="published"):
        _build(tmp_path, corrupt=True)


def test_a_space_in_the_prefix_is_refused_up_front(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError, match="space"):
        _build(tmp_path, prefix=tmp_path / "with space" / "bundle")


def test_missing_tools_are_reported_together(tmp_path: Path) -> None:
    prefix = tmp_path / "bundle"
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    with pytest.raises(bw.BuildError) as exc:
        bw.build(
            prefix=prefix, work=tmp_path / "w", pins=bw.Pins("17.5", "v0.8.2"), jobs=2, env={},
            vs_env={"PATH": str(tmp_path / "empty")}, flex_bison_dir=tmp_path,
            redist=redist, host=_host(FakeBuildRunner(prefix)),
        )
    for tool in ("meson", "nmake", "cl"):
        assert tool in str(exc.value)


def test_build_and_refresh_refuse_off_windows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["build", "--prefix", str(tmp_path)], ["refresh-runtime", "--prefix", str(tmp_path)]):
        assert bw.main(argv, env={}, platform="linux") == 1
        assert "Windows only" in capsys.readouterr().err


def test_refresh_runtime_needs_an_existing_built_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refreshing an empty prefix used to succeed and leave a bundle of four DLLs."""
    assert bw.main(["refresh-runtime", "--prefix", str(tmp_path)], env={}, platform="win32") == 1
    assert "nothing to refresh" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_verify_and_package_cli_run_on_any_os(tmp_path: Path) -> None:
    bundle = stage_bundle(tmp_path / "bundle")
    assert bw.main(["verify", "--prefix", str(bundle)], env={}, platform="darwin") == 0
    assert bw.main(["package", "--prefix", str(bundle), "--out-dir", str(tmp_path / "d")],
                   env={}, platform="darwin") == 0
    assert (tmp_path / "d" / "nexus-pg-windows-x64.txz").is_file()
    (bundle / "bin" / "initdb.exe").unlink()
    assert bw.main(["verify", "--prefix", str(bundle)], env={}, platform="darwin") == 1


def _fake_tool(directory: Path, name: str, output: str) -> Path:
    """An executable *name* that prints *output*. On Windows shutil.which finds only PATHEXT
    suffixes, so the stand-in is a .cmd there; found on the first real Windows run of this file
    (nexus-f9bgu.9): the POSIX-only stand-ins were never found, which is the exact lookup under test."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        tool = directory / f"{name}.cmd"
        tool.write_text(f"@echo off\r\n{'echo ' + output if output else 'rem'}\r\n")
    else:
        tool = directory / name
        tool.write_text(f"#!/bin/sh\n{'echo ' + output if output else ':'}\n")
        tool.chmod(0o755)
    return tool


def test_a_tool_only_on_the_build_path_is_resolved_against_that_path(tmp_path: Path) -> None:
    """Windows' CreateProcess searches the parent's PATH, so nmake/cl (present only
    after vcvars) must be resolved by the script, not left to the loader."""
    tool = _fake_tool(tmp_path / "bin", "nmake", "")
    resolved = bw.resolve_exe(["nmake", "/NOLOGO"], {"PATH": str(tool.parent)})
    assert [os.path.normcase(resolved[0]), *resolved[1:]] == [os.path.normcase(str(tool)), "/NOLOGO"]
    with pytest.raises(bw.BuildError, match="nmake: not found"):
        bw.resolve_exe(["nmake"], {"PATH": str(tmp_path / "elsewhere")})


def test_runner_runs_a_tool_found_only_through_the_given_env(tmp_path: Path) -> None:
    tool = _fake_tool(tmp_path / "bin", "only-here", "from-build-path")
    env = {"PATH": str(tool.parent)}
    assert bw.Runner().capture(["only-here"], cwd=None, env=env).strip() == "from-build-path"
    log = tmp_path / "l.log"
    bw.Runner().run(["only-here"], cwd=None, env=env, log=log)
    assert "from-build-path" in log.read_text()


def test_a_failing_step_surfaces_the_log_tail(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "x.log"
    with pytest.raises(bw.BuildError, match="exited 3"):
        bw.Runner().run(
            [sys.executable, "-c", "import sys; print('boom line'); sys.exit(3)"],
            cwd=None, env=os.environ, log=log,
        )
    assert "boom line" in log.read_text()


# --------------------------------------------------------------------------- #
# The relocation smoke
# --------------------------------------------------------------------------- #


class FakeSmokeRunner(sm.Runner):
    def __init__(self, *, vector: str = "0.8.2", plan: str = "Index Scan using smoke_hnsw",
                 rows: str = "1\n2\n3\n4\n5", modules: list[str] | None = None,
                 fail_query: str | None = None) -> None:
        self.vector, self.plan, self.rows, self.modules = vector, plan, rows, modules or []
        self.fail_query = fail_query
        self.events: list[str] = []
        self.to_file_calls: list[tuple[list[str], Path]] = []
        self.envs: list[dict[str, str]] = []

    def to_file(self, argv, *, env, out):  # type: ignore[override]
        verb = argv[-1]
        self.events.append(f"pg_ctl-{verb}")
        self.to_file_calls.append((list(argv), out))
        self.envs.append(dict(env))
        return 0

    def capture(self, argv, *, env):  # type: ignore[override]
        self.envs.append(dict(env))
        name = Path(argv[0]).stem
        if name == "initdb":
            self.events.append("initdb")
            data = Path(argv[argv.index("-D") + 1])
            data.mkdir(parents=True)
            (data / "postmaster.pid").write_text("4242\n")
            return 0, ""
        if name == "powershell":
            self.events.append("modules")
            return 0, "\n".join(self.modules)
        q = argv[-1]
        self.events.append(f"sql:{q[:40]}")
        if self.fail_query and self.fail_query in q:
            return 1, "ERROR: simulated"
        if "extversion FROM pg_extension WHERE extname='vector'" in q:
            return 0, self.vector
        if "extname='pg_trgm'" in q:
            return 0, "1.6"
        if "similarity" in q:
            return 0, "t"
        if "EXPLAIN" in q:
            return 0, self.plan
        if "ORDER BY" in q:
            return 0, self.rows
        if "version()" in q:
            return 0, "PostgreSQL 17.5, compiled by Visual C++"
        return 0, ""


def _smoke_root(tmp_path: Path, *, windows: bool, build_prefix: Path | None = None) -> Path:
    root = tmp_path / "relocated" / "bundle"
    stage_bundle(root, runtime=windows)
    (root / ".build_prefix").write_text(str(build_prefix or tmp_path / "gone" / "bundle") + "\n")
    return root


def _smoke(tmp_path: Path, runner: FakeSmokeRunner, *, windows: bool, expect: str = "0.8.2") -> list[str]:
    root = _smoke_root(tmp_path, windows=windows)
    out: list[str] = []
    sm.smoke(root, tmp_path / "work", port=5999, expect_vector=expect,
             platform=sm.Platform(windows=windows), runner=runner,
             base_env={"SystemRoot": r"C:\Windows", "PATH": "/opt/vs/bin"}, emit=out.append)
    return out


def test_smoke_happy_path_posix_branch(tmp_path: Path) -> None:
    runner = FakeSmokeRunner()
    out = _smoke(tmp_path, runner, windows=False)
    assert out[-1] == "SMOKE pg_ctl stop: ok"
    assert any(line.startswith("SMOKE vector: 0.8.2") for line in out)
    assert any("hnsw" in line and "1,2,3,4,5" in line for line in out)
    assert "modules" not in runner.events  # no Windows module listing off Windows


def test_smoke_happy_path_windows_branch_checks_modules(tmp_path: Path) -> None:
    root_bin = tmp_path / "relocated" / "bundle" / "bin"
    mods = [str(root_bin / d) for d in sm.VC_RUNTIME_DLLS] + [r"C:\Windows\System32\kernel32.dll"]
    runner = FakeSmokeRunner(modules=mods)
    out = _smoke(tmp_path, runner, windows=True)
    assert "modules" in runner.events
    assert any(line.startswith("SMOKE runtime modules:") for line in out)


def test_smoke_orders_initdb_start_queries_stop(tmp_path: Path) -> None:
    runner = FakeSmokeRunner()
    _smoke(tmp_path, runner, windows=False)
    ev = runner.events
    assert ev[0] == "initdb" and ev[1] == "pg_ctl-start" and ev[-1] == "pg_ctl-stop"
    assert ev.index("pg_ctl-start") < next(i for i, e in enumerate(ev) if e.startswith("sql:"))


def test_pg_ctl_output_goes_to_a_file_never_a_pipe(tmp_path: Path) -> None:
    runner = FakeSmokeRunner()
    _smoke(tmp_path, runner, windows=False)
    assert len(runner.to_file_calls) == 2  # start and stop, both through to_file
    for argv, out in runner.to_file_calls:
        assert out.name == "pg_ctl.out" and out.parent == tmp_path / "work"


def test_pg_ctl_start_listens_on_loopback_only_on_the_given_port(tmp_path: Path) -> None:
    runner = FakeSmokeRunner()
    _smoke(tmp_path, runner, windows=False)
    start = next(a for a, _ in runner.to_file_calls if a[-1] == "start")
    opts = start[start.index("-o") + 1]
    assert "-p 5999" in opts and "listen_addresses=127.0.0.1" in opts and "-w" in start


def test_stop_runs_even_when_a_query_fails(tmp_path: Path) -> None:
    runner = FakeSmokeRunner(fail_query="CREATE EXTENSION pg_trgm")
    with pytest.raises(sm.SmokeError, match="pg_trgm"):
        _smoke(tmp_path, runner, windows=False)
    assert runner.events[-1] == "pg_ctl-stop"


def test_wrong_vector_version_fails(tmp_path: Path) -> None:
    with pytest.raises(sm.SmokeError, match="0.8.1.*0.8.2"):
        _smoke(tmp_path, FakeSmokeRunner(vector="0.8.1"), windows=False)


def test_a_plan_that_skips_the_hnsw_index_fails(tmp_path: Path) -> None:
    with pytest.raises(sm.SmokeError, match="HNSW"):
        _smoke(tmp_path, FakeSmokeRunner(plan="Seq Scan on smoke_items"), windows=False)


def test_an_hnsw_query_returning_too_few_rows_fails(tmp_path: Path) -> None:
    with pytest.raises(sm.SmokeError, match="expected 5"):
        _smoke(tmp_path, FakeSmokeRunner(rows="1\n2"), windows=False)


def test_a_vc_runtime_module_loaded_from_elsewhere_fails(tmp_path: Path) -> None:
    mods = [r"C:\Windows\System32\vcruntime140.dll"]
    with pytest.raises(sm.SmokeError, match="not from the bundle"):
        _smoke(tmp_path, FakeSmokeRunner(modules=mods), windows=True)


def test_a_module_listing_with_no_runtime_in_it_is_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(sm.SmokeError, match="saw nothing"):
        _smoke(tmp_path, FakeSmokeRunner(modules=[r"C:\Windows\System32\kernel32.dll"]), windows=True)


def test_children_never_see_the_build_hosts_toolchain_path(tmp_path: Path) -> None:
    runner = FakeSmokeRunner()
    _smoke(tmp_path, runner, windows=False)
    bin_dir = str(tmp_path / "relocated" / "bundle" / "bin")
    for env in runner.envs:
        assert "/opt/vs/bin" not in env["PATH"]
        # windows=False: scrubbed_env joins with ":" on every host (and a Windows drive letter holds one,
        # so the whole value is compared, not its first field)
        assert env["PATH"] == f"{bin_dir}:/usr/bin:/bin"


def test_scrubbed_env_windows_is_bin_plus_os_dirs_only() -> None:
    env = sm.scrubbed_env(
        Path(r"C:\x\bin"), sm.Platform(windows=True),
        {"SystemRoot": r"C:\Windows", "PATH": r"C:\VS\bin;C:\Strawberry\perl\bin", "TEMP": r"C:\t",
         "VCINSTALLDIR": r"C:\VS"},
    )
    assert env["PATH"].split(";") == [r"C:\x\bin", r"C:\Windows\System32", r"C:\Windows"]
    assert "VCINSTALLDIR" not in env and env["TEMP"] == r"C:\t"


def test_relocation_check_refuses_a_live_build_prefix_and_an_identity(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    root = _smoke_root(tmp_path, windows=False, build_prefix=live)
    with pytest.raises(sm.SmokeError, match="still exists"):
        sm.check_relocated(root, allow_prefix_present=False)
    assert sm.check_relocated(root, allow_prefix_present=True).startswith("WEAK")
    (root / ".build_prefix").write_text(str(root) + "\n")
    with pytest.raises(sm.SmokeError, match="nothing was relocated"):
        sm.check_relocated(root, allow_prefix_present=True)
    (root / ".build_prefix").unlink()
    with pytest.raises(sm.SmokeError, match="not a bundle"):
        sm.check_relocated(root, allow_prefix_present=True)


def test_gone_build_prefix_passes(tmp_path: Path) -> None:
    root = _smoke_root(tmp_path, windows=False)
    assert "is gone" in sm.check_relocated(root, allow_prefix_present=False)


def test_runtime_presence_check_names_the_missing_dll(tmp_path: Path) -> None:
    root = _smoke_root(tmp_path, windows=True)
    sm.check_runtime_present(root)
    (root / "bin" / "msvcp140_1.dll").unlink()
    with pytest.raises(sm.SmokeError, match="msvcp140_1.dll"):
        sm.check_runtime_present(root)


def test_materialise_extracts_the_archive_the_build_packages(tmp_path: Path) -> None:
    archive = bw.package(stage_bundle(tmp_path / "w" / "bundle"), tmp_path / "dist")
    root = sm.materialise(archive, archive=True, dest=tmp_path / "fresh")
    assert root == tmp_path / "fresh" / "bundle" and (root / "bin" / "initdb.exe").is_file()
    tree = sm.materialise(tmp_path / "w" / "bundle", archive=False, dest=tmp_path / "fresh2")
    assert (tree / "bin" / "initdb.exe").is_file()


def test_windows_workdir_inherits_its_parents_acl_and_posix_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tempfile.mkdtemp is owner-only on Windows (3.12+), which an elevated
    initdb's restricted-token postgres cannot read DLLs from (0xC0000135, seen
    on qwentescence). The Windows branch must call mkdir with NO mode."""
    calls: list[tuple] = []
    real = os.mkdir

    def spy(path, *args, **kwargs):
        calls.append((Path(path).name, args, kwargs))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sm.os, "mkdir", spy)
    win = sm.make_workdir(tmp_path / "a", sm.Platform(windows=True))
    posix = sm.make_workdir(tmp_path / "b", sm.Platform(windows=False))
    assert win.is_dir() and posix.is_dir() and win.name.startswith("pgsmoke-")
    by_name = {name: (a, k) for name, a, k in calls}
    assert by_name[win.name] == ((), {})
    assert by_name[posix.name] == ((0o700,), {})
    assert sm.make_workdir(tmp_path / "a", sm.Platform(windows=True)) != win


def test_run_demands_exactly_one_source(tmp_path: Path) -> None:
    kw = dict(workdir=tmp_path, port=1, expect_vector="0.8.2", allow_prefix_present=False,
              keep=False, platform=sm.Platform(False), runner=FakeSmokeRunner(), base_env={})
    with pytest.raises(sm.SmokeError, match="exactly one"):
        sm.run(archive=None, bundle=None, **kw)
    with pytest.raises(sm.SmokeError, match="exactly one"):
        sm.run(archive=tmp_path / "a", bundle=tmp_path / "b", **kw)


def test_run_end_to_end_from_an_archive_cleans_up_and_never_deletes_the_parent(tmp_path: Path) -> None:
    stage = stage_bundle(tmp_path / "w" / "bundle")
    (stage / ".build_prefix").write_text(str(tmp_path / "gone" / "bundle") + "\n")
    archive = bw.package(stage, tmp_path / "dist")
    parent = tmp_path / "smokes"
    runner = FakeSmokeRunner(modules=[str(Path("X") / d) for d in ()])
    out: list[str] = []
    # posix branch: the fake handles every command; the real archive is extracted for real
    sm.run(archive=archive, bundle=None, workdir=parent, port=5999, expect_vector="0.8.2",
           allow_prefix_present=False, keep=False, platform=sm.Platform(False), runner=runner,
           base_env={}, emit=out.append)
    assert out[-1] == "SMOKE PASSED"
    assert parent.is_dir() and list(parent.iterdir()) == []


def test_loaded_module_check_compares_case_and_slashes() -> None:
    bin_dir = Path("C:/x/bin")
    assert sm.check_loaded_modules(["c:\\X\\BIN\\VCRUNTIME140.DLL"], bin_dir)


def test_loaded_module_check_ignores_windows_own_msvcp_win_and_still_catches_a_versioned_runtime() -> None:
    """msvcp_win.dll is a Windows component (System32, loaded by ucrtbase), not the VC++ runtime
    this check polices; the first real run of the engine smoke (nexus-f9bgu.9, qwentescence) failed
    on it because the old filter was a bare 'msvcp' prefix. A versioned msvcp/vcruntime module from
    anywhere but the bundle must still fail."""
    bin_dir = Path("C:/x/bin")
    listing = [
        "C:\\x\\bin\\vcruntime140.dll",
        "C:\\WINDOWS\\System32\\msvcp_win.dll",
        "C:\\WINDOWS\\System32\\ucrtbase.dll",
    ]
    assert sm.check_loaded_modules(listing, bin_dir) == ["C:\\x\\bin\\vcruntime140.dll"]
    with pytest.raises(sm.SmokeError, match="msvcp140_2.dll"):
        sm.check_loaded_modules([*listing, "C:\\WINDOWS\\System32\\msvcp140_2.dll"], bin_dir)
    with pytest.raises(sm.SmokeError, match="saw nothing"):
        sm.check_loaded_modules(["C:\\WINDOWS\\System32\\msvcp_win.dll"], bin_dir)


# --------------------------------------------------------------------------- #
# pg_ctl's output handle must not make a caller wait (the real process, no fakes)
# --------------------------------------------------------------------------- #


def test_to_file_returns_while_a_grandchild_still_holds_the_output_handle(tmp_path: Path) -> None:
    """pg_ctl's postgres keeps pg_ctl's stdout open for its whole life. Through a
    pipe the reader waits for that; to a file nothing waits. Reproduced with a
    child that leaves a long-lived grandchild holding the inherited handle."""
    pidfile = tmp_path / "grandchild.pid"
    child = (
        "import subprocess, sys;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid));"
        "print('pg_ctl: server started')"
    )
    t0 = time.monotonic()
    try:
        rc = sm.Runner().to_file([sys.executable, "-c", child], env=os.environ, out=tmp_path / "out.txt")
        elapsed = time.monotonic() - t0
        assert rc == 0
        assert elapsed < 30, f"to_file waited {elapsed:.0f}s on the grandchild's handle"
        assert pidfile.is_file(), "non-vacuity: the grandchild really was started"
        # The output really landed in the FILE (a PIPE would leave it empty).
        assert "pg_ctl: server started" in (tmp_path / "out.txt").read_text()
    finally:
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text()), 15)
            except OSError:
                pass


def test_a_pipe_capture_really_would_wait_on_that_grandchild(tmp_path: Path) -> None:
    """The control for the test above: the SAME child, read through a pipe, does
    not return until the grandchild closes the handle (bounded by a timeout here)."""
    pidfile = tmp_path / "grandchild.pid"
    child = (
        "import subprocess, sys;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))"
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run([sys.executable, "-c", child], capture_output=True, timeout=3)
    finally:
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text()), 15)
            except OSError:
                pass
