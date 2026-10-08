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
from tests._module_seam import setattr_in

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build_pg_bundle_windows.py"
WORKFLOWS = REPO / ".github" / "workflows"

#: The real reader, kept before the fixture below replaces it, so one test can prove that off
#: Windows it refuses rather than passes.
REAL_READ_AUTHENTICODE = bw.read_authenticode
#: The subject the real VS 2022 redist DLLs carry (measured on qwentescence, 2026-10-05, redist 14.44.35112).
MS_SUBJECT = "CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"


def _microsoft_signed(path: Path) -> bw.Signature:
    return bw.Signature("Valid", MS_SUBJECT)


@pytest.fixture(autouse=True)
def _runtime_dlls_are_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests stage fake DLLs on whatever OS runs them; Authenticode is asked of a real file on
    Windows only. The signature check itself is pinned by its own tests below, which inject readers."""
    monkeypatch.setattr(bw, "read_authenticode", _microsoft_signed)


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


def stage_bundle(prefix: Path, *, runtime: bool = True, build_only: bool = False) -> Path:
    """A bundle tree. ``build_only`` adds what ``ninja install`` and pgvector's
    install leave that the build then prunes (headers, .lib files, pgxs, pkgconfig)."""
    for d in ("bin", "lib/postgresql", "share/postgresql/extension"):
        (prefix / d).mkdir(parents=True, exist_ok=True)
    if build_only:
        for d in ("include/postgresql/server", "lib/postgresql/pgxs/src", "lib/pkgconfig"):
            (prefix / d).mkdir(parents=True, exist_ok=True)
        (prefix / "include" / "libpq-fe.h").write_text("h")
        (prefix / "lib" / "libpq.lib").write_bytes(b"lib")
        (prefix / "lib" / "libpgcommon.lib").write_bytes(b"lib")
        (prefix / "lib" / "postgresql" / "vector.lib").write_bytes(b"lib")
        (prefix / "lib" / "pkgconfig" / "libpq.pc").write_text("pc")
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


PGVECTOR_COMMIT = "cab9da72c04353f143bb06b42ab70a403daac64a"
FAKE_PG_BODY = b"pg tarball"


class FakeBuildRunner(bw.Runner):
    def __init__(self, prefix: Path, *, pgvector_head: str = PGVECTOR_COMMIT) -> None:
        self.prefix = prefix
        self.pgvector_head = pgvector_head
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def run(self, argv, *, cwd, env, log):  # type: ignore[override]
        argv = list(argv)
        self.calls.append(("run", argv, dict(env)))
        if argv[0] == "ninja" and argv[-1] == "install":
            stage_bundle(self.prefix, runtime=False, build_only=True)
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
        if argv[0] == "git" and argv[-2:] == ["rev-parse", "HEAD"]:
            return self.pgvector_head + "\n"
        return PG_DIRS[argv[-1].removeprefix("--")] + "\r\n"


def _host(runner: FakeBuildRunner, *, corrupt: bool = False, pg_pin: str | None = None) -> bw.Host:
    body = FAKE_PG_BODY

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

    return bw.Host(
        runner=runner, fetch=fetch, unpack=unpack,
        pg_sha256_pins={"17.5": pg_pin or hashlib.sha256(body).hexdigest()},
        pgvector_commit_pins={"v0.8.2": PGVECTOR_COMMIT},
    )


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
    runner = FakeBuildRunner(prefix, pgvector_head=kw.get("pgvector_head", PGVECTOR_COMMIT))
    kw.get("runners", []).append(runner)
    bw.build(
        prefix=kw.get("prefix", prefix), work=work, pins=kw.get("pins", bw.Pins("17.5", "v0.8.2")), jobs=6,
        env={}, vs_env=vs_env, flex_bison_dir=flex, redist=redist,
        host=_host(runner, corrupt=kw.get("corrupt", False), pg_pin=kw.get("pg_pin")),
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
    assert shapes[22:26] == ["ninja-build", "ninja-install", "run:git", "capture:git"]
    assert shapes.count("capture:pg_config") == 5  # pg_config asked, not guessed
    assert shapes[-2:] == ["nmake-build", "nmake-install"]
    # The parallel build is the capped one, and it comes after every generation.
    build_call = next(c for c in runner.calls if _shape(c) == "ninja-build")
    assert "-j6" in build_call[1]
    assert bw.verify_layout(prefix) == []
    # The build-only files ninja install left are gone; the runtime ones stay.
    assert not (prefix / "include").exists() and not (prefix / "lib" / "pkgconfig").exists()
    assert not (prefix / "lib" / "postgresql" / "pgxs").exists()
    assert not list((prefix / "lib").rglob("*.lib"))
    assert (prefix / "lib" / "postgresql" / "vector.dll").is_file()
    assert (prefix / "bin" / "libpq.dll").is_file() and (prefix / "bin" / "pg_config.exe").is_file()
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


def test_runtime_presence_check_refuses_a_bundle_without_the_notice(tmp_path: Path) -> None:
    """The licence condition (P0.6): the four DLLs ship with THIRD-PARTY-NOTICES.txt or not at all."""
    root = _smoke_root(tmp_path, windows=True)
    sm.check_runtime_present(root)
    (root / "THIRD-PARTY-NOTICES.txt").unlink()
    with pytest.raises(sm.SmokeError, match="THIRD-PARTY-NOTICES.txt missing"):
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

    setattr_in(monkeypatch, sm, "os.mkdir", spy)
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


# --------------------------------------------------------------------------- #
# nexus-f9bgu.27: the Authenticode check on the shipped runtime (code review S4)
# --------------------------------------------------------------------------- #


def _reader(status: str, subject: str):
    def read(path: Path) -> bw.Signature:
        return bw.Signature(status, subject)

    return read


@pytest.mark.parametrize(
    "status", ["NotSigned", "HashMismatch", "NotTrusted", "UnknownError", "Incompatible"],
)
def test_a_runtime_dll_without_a_valid_signature_is_refused_and_named(tmp_path: Path, status: str) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    with pytest.raises(bw.BuildError, match=rf"vcruntime140\.dll.*{status}"):
        bw.copy_runtime_dlls(tmp_path / "bin", redist, signature_reader=_reader(status, MS_SUBJECT))


@pytest.mark.parametrize(
    "subject",
    [
        "CN=Contoso Ltd, O=Contoso Ltd, C=US",
        "CN=Microsoft Corporation, O=Microsoft Corporation Evil, C=US",  # a Microsoft-looking CN is not the organisation
        "CN=Microsoft Windows, O=Contoso Ltd, C=US",
        "CN=Contoso, O=Microsoft Corporation, C=US",  # the organisation without a Microsoft certificate name
        "O=Microsoft Corporation, C=US",  # no CN at all
        "CN=Microsoft Windows",  # no organisation
        "CN=Not Microsoft, OU=O=Microsoft Corporation",
        "",
    ],
)
def test_a_runtime_dll_signed_by_anyone_but_microsoft_corporation_is_refused(tmp_path: Path, subject: str) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    with pytest.raises(bw.BuildError, match="not by 'Microsoft Corporation'"):
        bw.copy_runtime_dlls(tmp_path / "bin", redist, signature_reader=_reader("Valid", subject))


def test_each_shipped_dll_is_checked_and_its_signer_is_printed(tmp_path: Path) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    seen: list[str] = []
    out: list[str] = []

    def read(path: Path) -> bw.Signature:
        seen.append(path.name)
        return bw.Signature("Valid", MS_SUBJECT)

    bw.copy_runtime_dlls(tmp_path / "bin", redist, signature_reader=read, emit=out.append)
    assert seen == list(bw.VC_RUNTIME_DLLS)
    assert [ln.split(":")[0] for ln in out] == [f"signature {d}" for d in bw.VC_RUNTIME_DLLS]
    assert all(MS_SUBJECT in ln for ln in out)


@pytest.mark.parametrize(
    ("subject", "key", "value"),
    [
        ("CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond", "CN", "Microsoft Windows"),
        ("CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond", "O", "Microsoft Corporation"),
        ('O=Acme, CN="Acme, Inc.", C=US', "CN", "Acme, Inc."),
        ("C=US, CN=x", "CN", "x"),
        ("O=Only", "CN", None),
        ("OU=Only", "O", None),  # OU is not O
    ],
)
def test_subject_attribute_parses_the_subject(subject: str, key: str, value: str | None) -> None:
    assert bw.subject_attribute(subject, key) == value


def test_the_real_redist_subject_and_the_older_microsoft_corporation_cn_are_both_accepted(tmp_path: Path) -> None:
    """The real DLLs carry CN=Microsoft Windows (the first draft of this check demanded CN=Microsoft
    Corporation and refused them on the first real run). A CN of Microsoft Corporation, as the VS
    installer's own files carry, passes too."""
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    for subject in (MS_SUBJECT, "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"):
        bw.copy_runtime_dlls(tmp_path / "bin", redist, signature_reader=_reader("Valid", subject), emit=lambda m: None)


def test_the_default_reader_refuses_off_windows_instead_of_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    monkeypatch.setattr(bw, "sys", types.SimpleNamespace(platform="linux"))
    with pytest.raises(bw.BuildError, match="cannot read the Authenticode signature"):
        REAL_READ_AUTHENTICODE(Path("vcruntime140.dll"))


def test_the_windows_reader_passes_the_path_in_the_environment_and_parses_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    calls: list[dict] = []

    def fake_run(argv, **kw):  # noqa: ANN001
        calls.append({"argv": list(argv), "env": kw["env"]})
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"Status": "Valid", "Subject": MS_SUBJECT}), stderr="")

    monkeypatch.setattr(bw, "sys", types.SimpleNamespace(platform="win32"))
    setattr_in(monkeypatch, bw, "subprocess.run", fake_run)
    monkeypatch.setenv("PSMODULEPATH", r"C:\Program Files\PowerShell\7\Modules")
    sig = REAL_READ_AUTHENTICODE(Path(r"C:\it's here\vcruntime140.dll"))
    assert sig == bw.Signature("Valid", MS_SUBJECT)
    (call,) = calls
    assert call["env"]["NX_SIGCHECK_PATH"] == r"C:\it's here\vcruntime140.dll"
    assert not [k for k in call["env"] if k.upper() == "PSMODULEPATH"], (
        "pwsh 7's module path breaks Windows PowerShell's Get-AuthenticodeSignature (rehearsal run 37410530509)"
    )
    script = call["argv"][-1]
    assert "Get-AuthenticodeSignature" in script and "it's here" not in script, "the path is never quoted into the script"
    for bad in (
        types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        types.SimpleNamespace(returncode=0, stdout="not json", stderr=""),
        types.SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        # The cmdlet failed to load: exit 0, empty Status (rehearsal run 37410530509).
        types.SimpleNamespace(returncode=0, stdout='{"Status":"","Subject":""}', stderr="module could not be loaded"),
    ):
        setattr_in(monkeypatch, bw, "subprocess.run", lambda *a, _b=bad, **k: _b)
        with pytest.raises(bw.BuildError):
            REAL_READ_AUTHENTICODE(Path("x.dll"))


def test_the_bundle_build_refuses_an_unsigned_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw, "read_authenticode", _reader("NotSigned", ""))
    with pytest.raises(bw.BuildError, match="NotSigned"):
        _build(tmp_path)


def test_refresh_refuses_an_unsigned_runtime_and_writes_no_notice(tmp_path: Path) -> None:
    redist = bw.find_redist(_vs_tree(tmp_path / "vs"))
    bundle = tmp_path / "bundle"
    with pytest.raises(bw.BuildError, match="HashMismatch"):
        bw.copy_runtime(bundle, redist, bw.Pins("17.5", "v0.8.2"), signature_reader=_reader("HashMismatch", MS_SUBJECT))
    assert not (bundle / bw.NOTICE_NAME).exists(), "no notice is written for a runtime that was refused"


# --------------------------------------------------------------------------- #
# nexus-f9bgu.27: pins (code review m5)
# --------------------------------------------------------------------------- #


def test_the_pins_are_the_literals_measured_on_2026_10_05() -> None:
    assert bw.PINNED_PG_SHA256 == {"17.5": "fcb7ab38e23b264d1902cb25e6adafb4525a6ebcbd015434aeef9eda80f528d8"}
    assert bw.PINNED_PGVECTOR_COMMITS == {"v0.8.2": "cab9da72c04353f143bb06b42ab70a403daac64a"}
    assert bw.DEFAULT_PG_VERSION in bw.PINNED_PG_SHA256 and bw.DEFAULT_PGVECTOR_VERSION in bw.PINNED_PGVECTOR_COMMITS
    host = bw.Host(runner=bw.Runner())
    assert host.pg_sha256_pins == bw.PINNED_PG_SHA256 and host.pgvector_commit_pins == bw.PINNED_PGVECTOR_COMMITS


def test_a_tarball_that_agrees_with_the_hosts_file_but_not_with_the_pin_is_refused(tmp_path: Path) -> None:
    """The host's .sha256 sits beside the tarball: it proves the download is intact, not that it is the
    one that was reviewed. The control builds with a matching pin, so the host check alone passes."""
    prefix, runner, _ = _build(tmp_path)
    assert bw.verify_layout(prefix) == []
    (tmp_path / "again").mkdir()
    with pytest.raises(bw.BuildError, match="pin in this script"):
        _build(tmp_path / "again", pg_pin="0" * 64)


def test_a_postgresql_version_without_a_pin_is_refused_before_meson(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError, match="no pinned sha256 for PostgreSQL 17.6"):
        _build(tmp_path, pins=bw.Pins("17.6", "v0.8.2"))


def test_pgvector_must_resolve_to_the_pinned_commit(tmp_path: Path) -> None:
    runners: list[FakeBuildRunner] = []
    with pytest.raises(bw.BuildError, match="tag moved"):
        _build(tmp_path, pgvector_head="f" * 40, runners=runners)
    shapes = [_shape(c) for c in runners[0].calls]
    assert "capture:git" in shapes and "nmake-build" not in shapes, "nothing was built past the check"
    (tmp_path / "other").mkdir()
    with pytest.raises(bw.BuildError, match="no pinned commit for pgvector v0.9.0"):
        _build(tmp_path / "other", pins=bw.Pins("17.5", "v0.9.0"))


def test_the_docstring_names_only_test_files_that_exist() -> None:
    named = set(re.findall(r"tests/test_[\w]+\.py", SCRIPT.read_text(encoding="utf-8")))
    assert named, "non-vacuity: the script names its pinning test"
    for name in named:
        assert (REPO / name).is_file(), f"{SCRIPT.name} names {name}, which does not exist"


# --------------------------------------------------------------------------- #
# nexus-f9bgu.27: the work directory does not outlive the build (code review S3)
# --------------------------------------------------------------------------- #


def _drive_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str, fail: bool = False,
                env: dict[str, str] | None = None) -> tuple[int, list[Path]]:
    """bw.main on 'win32' with every Windows-only step stubbed and the build recording its work dir."""
    import tempfile

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    seen: list[Path] = []

    def fake_build(*, work: Path, **_kw) -> None:
        seen.append(work)
        (work / "pgbuild").mkdir(parents=True)
        ro = work / "pgbuild" / "readonly.obj"
        ro.write_text("x")
        ro.chmod(0o444)
        if fail:
            raise bw.BuildError("compile failed")

    monkeypatch.setattr(bw, "find_vs_install", lambda e, r: tmp_path)
    monkeypatch.setattr(bw, "find_redist", lambda vs: None)
    monkeypatch.setattr(bw, "load_vs_env", lambda vs, e: {})
    monkeypatch.setattr(bw, "find_flex_bison_dir", lambda e: tmp_path)
    monkeypatch.setattr(bw, "build", fake_build)
    rc = bw.main(["build", "--prefix", str(tmp_path / "prefix"), *extra], env=env or {}, platform="win32")
    return rc, seen


def test_the_auto_made_work_dir_is_removed_after_a_build_and_after_a_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, seen = _drive_main(tmp_path, monkeypatch)
    assert rc == 0 and len(seen) == 1 and seen[0].name.startswith("pgbundle-") and not seen[0].exists()
    (tmp_path / "f").mkdir()
    rc, seen = _drive_main(tmp_path / "f", monkeypatch, fail=True)
    assert rc == 1 and len(seen) == 1 and not seen[0].exists(), "a failed build leaves no gigabytes behind either"


def test_keep_work_dir_keeps_it_and_a_named_work_dir_is_never_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, seen = _drive_main(tmp_path, monkeypatch, "--keep-work-dir")
    assert rc == 0 and seen[0].is_dir()
    (tmp_path / "n").mkdir()
    named = tmp_path / "n" / "mine"
    rc, seen = _drive_main(tmp_path / "n", monkeypatch, "--work-dir", str(named))
    assert rc == 0 and seen == [named.resolve()] and named.is_dir(), "a directory the caller named is the caller's"
    (tmp_path / "e").mkdir()
    named2 = tmp_path / "e" / "fromenv"
    rc, seen = _drive_main(tmp_path / "e", monkeypatch, env={"WORK_DIR": str(named2)})
    assert rc == 0 and named2.is_dir()


def test_the_auto_made_work_dir_lives_under_runner_temp_when_the_runner_sets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner empties RUNNER_TEMP between jobs, so a job killed mid-build leaves nothing."""
    rt = tmp_path / "runner-temp"
    rt.mkdir()
    rc, seen = _drive_main(tmp_path, monkeypatch, "--keep-work-dir", env={"RUNNER_TEMP": str(rt)})
    assert rc == 0 and seen[0].parent == rt.resolve()


def test_remove_tree_clears_read_only_files_and_never_raises(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    (tree / "d").mkdir(parents=True)
    ro = tree / "d" / "object"
    ro.write_text("x")
    ro.chmod(0o444)
    bw.remove_tree(tree)
    assert not tree.exists()
    bw.remove_tree(tmp_path / "never-existed")  # must not raise


# --------------------------------------------------------------------------- #
# nexus-f9bgu.27: the smoke extracts the way the client does, and compares long paths
# --------------------------------------------------------------------------- #


def test_materialise_grants_the_current_user_before_anything_is_extracted(tmp_path: Path) -> None:
    archive = bw.package(stage_bundle(tmp_path / "w" / "bundle"), tmp_path / "dist")
    seen: list[tuple[Path, list[str]]] = []

    def grant(path: Path) -> None:
        seen.append((path, sorted(p.name for p in path.iterdir())))

    root = sm.materialise(archive, archive=True, dest=tmp_path / "fresh", grant=grant)
    assert seen == [(tmp_path / "fresh", [])], "one grant, on the destination, while it was still empty"
    assert (root / "bin" / "initdb.exe").is_file()


def test_the_default_grant_is_the_clients_own_function_and_a_no_op_off_windows(tmp_path: Path) -> None:
    grant = sm.load_grant_user_tree_access()
    assert grant.__name__ == "grant_user_tree_access"
    if os.name != "nt":
        grant(tmp_path)  # POSIX: nothing to do, and it must not raise
    src = (REPO / "src" / "nexus" / "db" / "pg_bundle.py").read_text(encoding="utf-8")
    assert "grant_user_tree_access(dest)" in src, "premise: the client still grants before it extracts"


def test_the_work_dir_defaults_under_runner_temp(tmp_path: Path) -> None:
    rt = tmp_path / "runner-temp"
    work = sm.make_workdir(None, sm.Platform(windows=False), env={"RUNNER_TEMP": str(rt)})
    assert work.parent == rt and work.is_dir()


def test_a_short_form_temp_is_not_a_false_failure_once_both_sides_are_made_long() -> None:
    short_bin = Path("C:/Users/RUNNER~1/AppData/Local/Temp/pgsmoke/bundle/bin")
    modules = [r"C:\Users\runneradmin\AppData\Local\Temp\pgsmoke\bundle\bin\vcruntime140.dll"]

    def long_form(p: str) -> str:
        return p.replace("RUNNER~1", "runneradmin")

    assert sm.check_loaded_modules(modules, short_bin, long_path=long_form)
    # control: without the expansion the same listing is a (false) failure
    with pytest.raises(sm.SmokeError, match="not from the bundle"):
        sm.check_loaded_modules(modules, short_bin, long_path=lambda p: p)
    # a genuinely foreign module still fails under the expansion
    with pytest.raises(sm.SmokeError, match="not from the bundle"):
        sm.check_loaded_modules([r"C:\Windows\System32\vcruntime140.dll"], short_bin, long_path=long_form)


def test_resolve_long_path_normalises_dot_segments_on_any_os(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    messy = str(tmp_path / "a" / ".." / "b")
    assert sm.resolve_long_path(messy, windows=False) == os.path.realpath(tmp_path / "b")


# --------------------------------------------------------------------------- #
# Build-only files (headers, .lib, pgxs, pkgconfig) never ship
# --------------------------------------------------------------------------- #


def test_prune_build_only_removes_exactly_the_build_only_set(tmp_path: Path) -> None:
    bundle = stage_bundle(tmp_path / "bundle", build_only=True)
    removed = bw.prune_build_only(bundle)
    assert set(removed) == {
        "include/", "lib/postgresql/pgxs/", "lib/pkgconfig/",
        "lib/libpq.lib", "lib/libpgcommon.lib", "lib/postgresql/vector.lib",
    }
    assert bw.verify_layout(bundle) == []
    assert bw.prune_build_only(bundle) == []  # idempotent


@pytest.mark.parametrize("leftover", ["include/libpq-fe.h", "lib/pkgconfig/libpq.pc", "lib/libpq.lib",
                                      "lib/postgresql/vector.lib", "lib/postgresql/pgxs/src/x.mk"])
def test_verify_layout_names_a_build_only_leftover(tmp_path: Path, leftover: str) -> None:
    bundle = stage_bundle(tmp_path / "bundle")
    (bundle / leftover).parent.mkdir(parents=True, exist_ok=True)
    (bundle / leftover).write_text("x")
    problems = bw.verify_layout(bundle)
    assert problems and all("build-only" in p for p in problems), problems
