# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx self install`` builds with the TARGET release's installer (nexus-2k66t.4.3).

The lock pin (nexus-2k66t.4.1) lives in the installer a release ships. An
upgrade built by the RUNNING release's installer resolves the new release
fresh; these tests pin that the build runs the installer of the release being
installed, that a target without a usable installer falls back with one stated
line, and that a failed fetch leaves ``current`` where it was.

The targets are fake releases: a wheel (or checkout directory) whose
``install_generation.sh`` writes a marker and prints a generation directory, so
a marker proves WHICH installer ran. The registry case goes through a real
``uv`` against a local ``file://`` simple index.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import click
import pytest
import structlog
import structlog.testing

from nexus import install_layout, target_installer
from nexus.commands import self_cmd

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX install scripts; the Windows twin is tested below")


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every scratch directory the code makes lands in THIS test's own tempdir, so the
    "scratch is removed" assertions cannot see another xdist worker's."""
    scratch = tmp_path / "scratch-root"
    scratch.mkdir()
    # tempfile.tempdir alone: the scratch dirs are made in-process by tempfile.mkdtemp,
    # and a TMPDIR override must go through tests._tmp_root (its census forbids a bare one).
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch

TARGET_SCRIPT = """#!/usr/bin/env bash
set -eu
SOURCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --source)  SOURCE="$2"; shift 2 ;;
        --version) shift 2 ;;
        --extras)  shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
GEN="$NX_TOOLS_DIR/gen-20261024T000000Z"
mkdir -p "$GEN"
echo "target-installer source=$SOURCE" > "$NX_TEST_MARKER"
echo "$GEN"
"""

#: An installer that predates --extras: the running code passes it when extras are asked for.
OLD_SCRIPT = TARGET_SCRIPT.replace("        --extras)  shift 2 ;;\n", "")

SUPPORT = {
    "nexus/_install/layout.sh": "# stand-in\n",
    "nexus/_install/layout_core.py": "# stand-in\n",
    "nexus/_install/overrides.txt": "",
}


def _wheel(path: Path, files: dict[str, str], *, version: str = "99.0.0") -> Path:
    dist_info = f"conexus-{version}.dist-info"
    body = dict(files)
    body[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.1\nName: conexus\nVersion: {version}\n"
    body[f"{dist_info}/WHEEL"] = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = [f"{n},sha256={hashlib.sha256(b.encode()).hexdigest()},{len(b.encode())}" for n, b in body.items()]
    record.append(f"{dist_info}/RECORD,,")
    body[f"{dist_info}/RECORD"] = "\n".join(record) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in body.items():
            zf.writestr(name, text)
    return path


def _release_files(script: str | None = TARGET_SCRIPT) -> dict[str, str]:
    files = dict(SUPPORT)
    if script is not None:
        files["nexus/_install/install_generation.sh"] = script
    return files


def _index(root: Path, wheel: Path) -> Path:
    """A local PEP 503 index holding *wheel* as ``conexus``."""
    page = root / "index" / "conexus"
    page.mkdir(parents=True)
    shutil.copy(wheel, page / wheel.name)
    (page / "index.html").write_text(
        f'<!DOCTYPE html><html><body><a href="{wheel.name}">{wheel.name}</a></body></html>\n', encoding="utf-8",
    )
    return root / "index"


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.tools = tmp_path / "tools"
        self.marker = tmp_path / "marker"
        self.tools.mkdir()
        monkeypatch.setenv("NX_TOOLS_DIR", str(self.tools))
        monkeypatch.setenv("NX_BIN_DIR", str(tmp_path / "bin"))
        monkeypatch.setenv("NX_TEST_MARKER", str(self.marker))
        monkeypatch.delenv("NX_INSTALL_RUNNING_INSTALLER", raising=False)
        self.builds: list[list[str]] = []
        self.flips: list[Path] = []
        self.host = self._generation("20260101T000000Z", "conexus")
        monkeypatch.setattr(self_cmd, "running_generation", lambda: self.host)
        # The post-build steps are the running code's own and have their tests; this
        # file is about WHICH installer builds, so they are recorded, not run.
        monkeypatch.setattr(self_cmd, "_flip_and_shim", lambda gen, **kw: self.flips.append(gen))
        monkeypatch.setattr(self_cmd, "_reap_generations", lambda *a, **kw: [])
        monkeypatch.setattr(self_cmd, "_register_legacy_tree_if_present", lambda *a, **kw: None)
        self.real_run_build = self_cmd._run_build

    def _generation(self, stamp: str, source: str) -> Path:
        gen = self.tools / f"gen-{stamp}"
        gen.mkdir()
        kind = "directory" if "/" in source else "registry"
        (gen / "nexus-install.json").write_text(install_layout.Receipt(
            version="7.1.0", spec=source, source_kind=kind, source=source,
            python="3.12.8", base_interpreter="/py", created_at="2026-10-01T00:00:00Z", extras=[],
        ).to_json())
        return gen

    def point_host_at(self, source: str) -> None:
        shutil.rmtree(self.host)
        self.host = self._generation("20260101T000000Z", source)

    def capture_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Record the build argv instead of running it."""
        def fake(argv, *, progress):
            self.builds.append(list(argv))
            import subprocess  # noqa: PLC0415
            return subprocess.CompletedProcess(argv, 0, f"{self.tools}/gen-20261024T000000Z\n", "")
        monkeypatch.setattr(self_cmd, "_run_build", fake)

    @property
    def current(self) -> str | None:
        link = self.tools / "current"
        return os.readlink(link) if link.is_symlink() else None


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


class TestTargetInstallerRuns:
    def test_a_wheel_source_builds_with_the_wheels_own_installer(
        self, world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files())
        world.point_host_at(str(wheel))
        generation = self_cmd.perform_self_install()
        assert generation is not None and generation.name == "gen-20261024T000000Z"
        assert world.marker.read_text() == f"target-installer source={wheel}\n"
        assert world.flips == [generation]
        assert f"building with the installer the wheel {wheel.name} ships" in capsys.readouterr().err

    def test_a_checkout_source_builds_with_the_checkouts_installer(
        self, world: World, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        for name, body in _release_files().items():
            target = checkout / "src" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        world.point_host_at(str(checkout))
        self_cmd.perform_self_install()
        assert world.marker.read_text() == f"target-installer source={checkout}\n"

    def test_a_registry_target_is_fetched_and_its_installer_builds(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert shutil.which("uv"), "uv is required: this test fetches through it"
        wheel = _wheel(tmp_path / "wheels" / "conexus-99.0.0-py3-none-any.whl", _release_files())
        monkeypatch.setenv("UV_INDEX_URL", _index(tmp_path, wheel).as_uri())
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvcache"))
        generation = self_cmd.perform_self_install(version="99.0.0")
        assert generation is not None
        assert world.marker.read_text() == "target-installer source=conexus\n"
        assert "building with the installer the conexus 99.0.0 package ships" in capsys.readouterr().err
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*")), "the scratch directory is removed"

    def test_the_running_version_needs_no_fetch_and_uses_the_running_installer(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from importlib.metadata import version  # noqa: PLC0415

        monkeypatch.setenv("UV_INDEX_URL", (world.tmp / "no-such-index").as_uri())  # a fetch would fail
        world.capture_builds(monkeypatch)
        self_cmd.perform_self_install(version=version("conexus"))
        assert world.builds[0][1] == str(self_cmd.packaged_install_dir() / "install_generation.sh")


class TestFallbackToTheRunningInstaller:
    def test_a_target_with_no_installer_falls_back_with_one_line(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", {"conexus_stub/__init__.py": ""})
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        self_cmd.perform_self_install()
        assert world.builds[0][1] == str(self_cmd.packaged_install_dir() / "install_generation.sh")
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "running installer" in ln]
        assert lines == [
            f"building with the running installer: the wheel {wheel.name} ships no installer (it predates one)"
        ]

    def test_a_target_whose_installer_lacks_a_flag_the_code_passes_falls_back(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files(OLD_SCRIPT))
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        self_cmd.perform_self_install(add_extras=("voyage",))
        assert world.builds[0][1] == str(self_cmd.packaged_install_dir() / "install_generation.sh")
        assert "its installer does not accept --extras" in capsys.readouterr().err

    def test_the_same_old_installer_is_used_when_the_flag_is_not_passed(
        self, world: World, tmp_path: Path,
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files(OLD_SCRIPT))
        world.point_host_at(str(wheel))
        self_cmd.perform_self_install()
        assert world.marker.exists(), "no extras asked for, so the target's installer is compatible"

    def test_a_target_missing_a_support_file_falls_back(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        files = _release_files()
        del files["nexus/_install/overrides.txt"]
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", files)
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        self_cmd.perform_self_install()
        assert "its installer lacks overrides.txt" in capsys.readouterr().err
        assert world.builds[0][1] == str(self_cmd.packaged_install_dir() / "install_generation.sh")

    def test_the_operator_knob_builds_with_the_running_installer_without_a_fetch(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files())
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        monkeypatch.setenv("NX_INSTALL_RUNNING_INSTALLER", "1")
        self_cmd.perform_self_install()
        assert world.builds[0][1] == str(self_cmd.packaged_install_dir() / "install_generation.sh")
        assert not world.marker.exists()


class TestAFailedFetchChangesNothing:
    def test_current_is_untouched_and_nothing_is_built(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os.symlink(world.host.name, world.tools / "current")
        before = (world.current, sorted(p.name for p in world.tools.iterdir()))
        world.capture_builds(monkeypatch)
        monkeypatch.setenv("UV_INDEX_URL", (tmp_path / "no-such-index").as_uri())
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvcache"))
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install(version="99.0.0")
        message = raised.value.message
        assert "could not fetch conexus==99.0.0 to read the installer it ships; nothing was built or changed" in message
        assert "NX_INSTALL_RUNNING_INSTALLER=1" in message
        assert world.builds == [] and world.flips == []
        assert (world.current, sorted(p.name for p in world.tools.iterdir())) == before
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*"))

    def test_a_wheel_with_an_escaping_member_is_refused_before_anything_is_built(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files())
        with zipfile.ZipFile(wheel, "a") as zf:
            member = zipfile.ZipInfo("placeholder")
            member.filename = "nexus/_install/../../../escaped"  # ZipInfo's constructor would tidy the name
            zf.writestr(member, "x")
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        with pytest.raises(click.ClickException):
            self_cmd.perform_self_install()
        assert world.builds == []
        assert not (Path(tempfile_root()) / "escaped").exists()
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*"))


def tempfile_root() -> str:
    return tempfile.gettempdir()


# ── Windows: the target's generation_core builds ────────────────────────────

FAKE_CORE = '''
import os
from pathlib import Path


class GenerationError(Exception):
    pass


def build_generation(source, *, version="", extras=(), python_version="3.12", constraints="",
                     tools=None, run=None, platform=None, now=None):
    Path(os.environ["NX_TEST_MARKER"]).write_text(f"target-core source={source} extras={list(extras)}\\n")
    if os.environ.get("NX_TEST_CORE_FAILS"):
        raise GenerationError("the target core failed")
    gen = Path(tools) / "gen-20261024T000000Z"
    gen.mkdir(parents=True, exist_ok=True)
    return gen
'''

WINDOWS_SUPPORT = {
    "nexus/_install/layout_core.py": "# stand-in\n",
    "nexus/_install/gc_core.py": "# stand-in\n",
    "nexus/_install/winproc_core.py": "# stand-in\n",
    "nexus/_install/overrides.txt": "",
}


def _windows_release(tmp_path: Path, core: str | None, *, layout_mark: str = "stand-in") -> Path:
    files = dict(WINDOWS_SUPPORT)
    files["nexus/_install/layout_core.py"] = f"MARK = {layout_mark!r}\n"
    if core is not None:
        files["nexus/_install/generation_core.py"] = core
    return _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", files)


#: A core that reaches a sibling the way the real one does (``_sibling``, by path, under the
#: ``_nx_install_`` key) and reports WHICH sibling it was served.
SIBLING_CORE = FAKE_CORE + '''

import sys


def _sibling(name):
    key = f"_nx_install_{name}"
    cached = sys.modules.get(key)
    if cached is not None:
        return cached
    import importlib.util
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def report():
    layout = _sibling("layout_core")
    return layout.MARK, layout.__file__
'''

#: A core that also has the legacy convergence entry point.
MIGRATE_CORE = FAKE_CORE + '''

def migrate_legacy(source, *, version="", python_version="", legacy_venv=None, tools=None, run=None,
                   ops=None, platform=None, store=None, uv_bin=None, environ=None, on_path=None):
    Path(os.environ["NX_TEST_MARKER"]).write_text(f"target-migrate source={source} version={version}\\n")
    gen = Path(tools) / "gen-20261024T000000Z"
    gen.mkdir(parents=True, exist_ok=True)
    return gen
'''


class TestWindowsTargetCore:
    def _stage(self, wheel: Path, *, with_extras: bool = True, version: str | None = None):
        return target_installer.staged_installer(
            str(wheel), version=version, running_version="1.0.0", windows=True,
            with_extras=with_extras, env=dict(os.environ), platform=None,
        )

    def test_the_targets_generation_core_is_loaded_and_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_TEST_MARKER", str(tmp_path / "marker"))
        wheel = _windows_release(tmp_path, FAKE_CORE)
        monkeypatch.setattr(self_cmd, "_is_windows", lambda platform=None: True)
        monkeypatch.setattr(self_cmd, "_windows_flip_and_path", lambda *a, **kw: None)
        tools = tmp_path / "tools"
        tools.mkdir()
        with self._stage(wheel) as staged:
            assert staged.core is not None and staged.install_dir is not None
            build = self_cmd._build_request(
                staged.install_dir, self_cmd._RegistryReceipt(source=str(wheel), extras=("pdf",)),
                version=None, extras=["pdf"], core=staged.core,
            )
            generation = self_cmd._windows_build_flip_shims(build, tools=tools, bin_dir=tmp_path / "bin")
        assert generation.name == "gen-20261024T000000Z"
        assert (tmp_path / "marker").read_text() == f"target-core source={wheel} extras=['pdf']\n"
        assert target_installer._CORE_KEY not in sys.modules, "the target's modules are not left loaded"
        assert not [k for k in sys.modules if k.startswith("_nx_install_")]

    def test_the_targets_own_error_class_is_translated_to_a_clean_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_TEST_MARKER", str(tmp_path / "marker"))
        monkeypatch.setenv("NX_TEST_CORE_FAILS", "1")
        wheel = _windows_release(tmp_path, FAKE_CORE)
        monkeypatch.setattr(self_cmd, "_is_windows", lambda platform=None: True)
        flipped: list[object] = []
        monkeypatch.setattr(self_cmd, "_windows_flip_and_path", lambda *a, **kw: flipped.append(a))
        with self._stage(wheel) as staged:
            build = self_cmd._build_request(
                staged.install_dir, self_cmd._RegistryReceipt(source=str(wheel)),
                version=None, extras=[], core=staged.core,
            )
            with pytest.raises(click.ClickException, match="generation build failed:\nthe target core failed"):
                self_cmd._windows_build_flip_shims(build, tools=tmp_path, bin_dir=tmp_path / "bin")
        assert flipped == []

    def test_a_core_that_does_not_take_a_keyword_falls_back(self, tmp_path: Path) -> None:
        no_extras = FAKE_CORE.replace("extras=(), ", "")
        wheel = _windows_release(tmp_path, no_extras)
        with self._stage(wheel) as staged:
            assert staged.core is None and staged.install_dir is None
            assert "its build_generation does not accept extras" in staged.note
            assert staged.note.startswith("building with the running installer:")

    def test_a_target_with_no_generation_core_falls_back(self, tmp_path: Path) -> None:
        wheel = _windows_release(tmp_path, None)
        with self._stage(wheel) as staged:
            assert staged.core is None
            assert "its installer lacks generation_core.py" in staged.note

    def test_a_second_staging_in_one_process_loads_its_own_siblings(self, tmp_path: Path) -> None:
        """The first target's ``layout_core`` must not be served to the second: the staging
        clears the ``_nx_install_*`` modules on both sides (a stale one would report "first")."""
        seen: list[tuple[str, str]] = []
        for which in ("first", "second"):
            wheel = _windows_release(tmp_path / which, SIBLING_CORE, layout_mark=which)
            with self._stage(wheel) as staged:
                assert staged.core is not None
                mark, loaded_from = staged.core.report()
                seen.append((mark, loaded_from))
                assert Path(loaded_from).parent == staged.install_dir.resolve()
        assert [mark for mark, _ in seen] == ["first", "second"]
        assert seen[0][1] != seen[1][1]
        assert not [k for k in sys.modules if k.startswith("_nx_install_")]

    def test_a_core_that_will_not_load_falls_back(self, tmp_path: Path) -> None:
        wheel = _windows_release(tmp_path, "raise RuntimeError('boom')\n")
        with self._stage(wheel) as staged:
            assert staged.core is None
            assert "could not be loaded (RuntimeError: boom)" in staged.note
        assert target_installer._CORE_KEY not in sys.modules


# ── the other routes that build, and what a failure says ────────────────────


def _index_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str], version: str = "99.0.0") -> None:
    assert shutil.which("uv"), "uv is required: these tests fetch through it"
    wheel = _wheel(tmp_path / "wheels" / f"conexus-{version}-py3-none-any.whl", files, version=version)
    monkeypatch.setenv("UV_INDEX_URL", _index(tmp_path, wheel).as_uri())
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvcache"))


def _receipt_script(version: str) -> str:
    """The target installer, but its generation carries a receipt naming *version*."""
    receipt = install_layout.Receipt(
        version=version, spec="conexus", source_kind="registry", source="conexus",
        python="3.12.8", base_interpreter="/py", created_at="2026-10-24T00:00:00Z", extras=[],
    ).to_json()
    return TARGET_SCRIPT.replace(
        'echo "$GEN"\n', f"cat > \"$GEN/nexus-install.json\" <<'RECEIPT'\n{receipt}\nRECEIPT\necho \"$GEN\"\n",
    )


class TestTheTakeoverRepairUsesTheUvTreesOwnInstaller:
    def test_the_installer_is_read_from_the_tree_on_disk_and_nothing_is_fetched(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        os.symlink(str(world.host), world.tools / "current")  # absolute, as the layout requires
        uv_tools = tmp_path / "uv-tools"
        legacy = uv_tools / "conexus"
        (legacy / "bin").mkdir(parents=True)
        site = legacy / "lib" / "python3.12" / "site-packages"
        for name, body in _release_files().items():
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_text(body)
        monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
        monkeypatch.setenv("UV_INDEX_URL", (tmp_path / "no-such-index").as_uri())  # a fetch would fail
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvcache"))
        monkeypatch.setattr(
            self_cmd, "_installed_version", lambda venv: "7.2.0" if Path(venv) == legacy else "7.1.0",
        )
        lines = self_cmd.repair_uv_takeover()
        assert world.marker.read_text() == "target-installer source=conexus\n"
        assert any(line.startswith("installed gen-") for line in lines)
        assert "building with the installer the installed conexus 7.2.0 package ships" in capsys.readouterr().err
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*"))


class TestTheLegacyConvergenceUsesTheTargetsInstaller:
    FAKE_MIGRATE = """#!/usr/bin/env bash
echo "script=${NX_MIGRATE_BUILD_SCRIPT-unset}" > "$NX_TEST_MARKER"
mkdir -p "$NX_TOOLS_DIR/gen-20261024T000000Z"
echo "$NX_TOOLS_DIR/gen-20261024T000000Z"
"""

    def _packaged(self, tmp_path: Path) -> Path:
        install_dir = tmp_path / "packaged"
        install_dir.mkdir()
        (install_dir / "migrate_legacy.sh").write_text(self.FAKE_MIGRATE)
        return install_dir

    def test_the_script_is_handed_the_fetched_install_generation_sh(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _index_for(tmp_path, monkeypatch, _release_files())
        generation = self_cmd._converge_legacy_install(
            self._packaged(tmp_path), world.tools, version="99.0.0", dry_run=False,
        )
        assert generation is not None
        recorded = world.marker.read_text().strip()
        assert recorded.startswith("script=") and "nx-target-installer-" in recorded
        assert recorded.endswith("/pkg/nexus/_install/install_generation.sh")
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*"))

    def test_with_no_version_the_newest_release_is_fetched(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _index_for(tmp_path, monkeypatch, _release_files())
        self_cmd._converge_legacy_install(self._packaged(tmp_path), world.tools, version=None, dry_run=False)
        assert world.marker.read_text().strip().endswith("/pkg/nexus/_install/install_generation.sh")

    def test_a_target_without_the_flags_the_script_passes_leaves_the_running_script(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # migrate_legacy.sh always passes --extras (it adds pdf), so an installer without it cannot be used.
        _index_for(tmp_path, monkeypatch, _release_files(OLD_SCRIPT))
        self_cmd._converge_legacy_install(self._packaged(tmp_path), world.tools, version="99.0.0", dry_run=False)
        assert world.marker.read_text().strip() == "script=unset"
        assert "its installer does not accept --extras" in capsys.readouterr().err

    def test_windows_runs_the_targets_migrate_legacy(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _index_for(tmp_path, monkeypatch, {
            **WINDOWS_SUPPORT, "nexus/_install/generation_core.py": MIGRATE_CORE,
        })
        monkeypatch.setattr(self_cmd, "_is_windows", lambda platform=None: True)
        monkeypatch.setattr(self_cmd, "_retarget_autostart_task", lambda gen: None)
        generation = self_cmd._converge_legacy_install(
            tmp_path / "unused", world.tools, version="99.0.0", dry_run=False,
        )
        assert generation is not None
        assert world.marker.read_text() == "target-migrate source=conexus version=99.0.0\n"
        assert target_installer._CORE_KEY not in sys.modules


class TestAfterTheBuild:
    def test_a_version_the_index_moved_to_is_warned_about_by_both_numbers(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _index_for(tmp_path, monkeypatch, _release_files(_receipt_script("99.0.1")))
        self_cmd.perform_self_install()  # newest: fetched as 99.0.0, built as 99.0.1
        err = capsys.readouterr().err
        assert "installer was read from conexus 99.0.0 but the generation holds 99.0.1" in err

    def test_matching_versions_are_silent(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _index_for(tmp_path, monkeypatch, _release_files(_receipt_script("99.0.0")))
        self_cmd.perform_self_install()
        assert "the generation holds" not in capsys.readouterr().err

    def test_the_fetch_uses_the_python_the_build_uses(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, str] = {}

        def fetch(spec: str, dest: Path, *, uv_python: str, env) -> None:
            seen["python"] = uv_python
            raise target_installer.TargetFetchError("stop here")

        monkeypatch.setattr(target_installer, "_fetch_registry", fetch)
        with pytest.raises(click.ClickException):
            self_cmd.perform_self_install(version="99.0.0")
        assert seen["python"] == "3.12" == target_installer.BUILD_PYTHON

    def test_build_python_is_the_installer_scripts_own_default(self) -> None:
        """BUILD_PYTHON duplicates install_generation.sh's PYTHON_VERSION default; a drift
        between them would fetch the installer for a different Python than the build uses."""
        script = (Path(target_installer.__file__).parent / "_install" / "install_generation.sh").read_text()
        (default,) = re.findall(r'^PYTHON_VERSION="([^"]+)"$', script, re.M)
        assert default == target_installer.BUILD_PYTHON

    def test_the_fetch_resolves_for_the_build_python_without_needing_that_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(argv, *, timeout, env):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(target_installer, "run_bounded", fake_run)
        target_installer._fetch_registry("conexus==99.0.0", tmp_path / "pkg", uv_python="3.12", env={})
        (argv,) = seen
        assert argv[argv.index("--python") + 1] == sys.executable
        assert argv[argv.index("--python-version") + 1] == "3.12"

    def test_a_failed_build_by_the_targets_installer_names_both_escape_hatches(
        self, world: World, tmp_path: Path,
    ) -> None:
        failing = TARGET_SCRIPT.replace('GEN="$NX_TOOLS_DIR', 'echo "no solution found" >&2; exit 1\nGEN="$NX_TOOLS_DIR', 1)
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", _release_files(failing))
        world.point_host_at(str(wheel))
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install()
        message = raised.value.message
        assert message.startswith("generation build failed")
        assert "NX_INSTALL_RUNNING_INSTALLER=1" in message and "NX_INSTALL_UNPINNED=1" in message
        assert world.flips == []

    def test_a_failed_build_by_the_running_installer_does_not_blame_a_target(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", {"conexus_stub/__init__.py": ""})
        world.point_host_at(str(wheel))
        monkeypatch.setattr(
            self_cmd, "_run_build", lambda argv, *, progress: subprocess.CompletedProcess(argv, 1, "", "boom"),
        )
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install()
        assert "NX_INSTALL_RUNNING_INSTALLER" not in raised.value.message

    def test_a_fallback_is_logged_at_warning_level_as_well(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wheel = _wheel(tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl", {"conexus_stub/__init__.py": ""})
        world.point_host_at(str(wheel))
        world.capture_builds(monkeypatch)
        with structlog.testing.capture_logs() as logs:
            self_cmd.perform_self_install()
        warnings = [e for e in logs if e["event"] == "target_installer_fallback"]
        assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
        assert "ships no installer" in warnings[0]["note"]


class TestFetchFailureWording:
    def test_a_network_failure_gets_the_index_hint(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fetch(spec: str, dest: Path, *, uv_python: str, env) -> None:
            raise target_installer.TargetFetchError(
                "  Failed to fetch: `https://pypi.example/simple/conexus/`\n  dns error: failed to lookup address"
            )

        monkeypatch.setattr(target_installer, "_fetch_registry", fetch)
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install(version="99.0.0")
        assert "uv could not reach the package index" in raised.value.message
        assert "could not fetch conexus==99.0.0" in raised.value.message

    def test_an_unreadable_local_wheel_is_not_called_a_fetch(
        self, world: World, tmp_path: Path,
    ) -> None:
        wheel = tmp_path / "w" / "conexus-99.0.0-py3-none-any.whl"
        wheel.parent.mkdir()
        wheel.write_text("this is not a zip file")
        world.point_host_at(str(wheel))
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install()
        message = raised.value.message
        assert "could not read the installer of" in message and "could not be read as a wheel" in message
        assert "fetch" not in message.split("NX_INSTALL_RUNNING_INSTALLER")[0]
        assert "package index" not in message
        assert not list(Path(tempfile_root()).glob("nx-target-installer-*"))


class TestTheRealPackagedInstaller:
    """The selection reads ``install_generation.sh`` by regex. Run it over the script this
    package really ships, with every flag the code really passes, so a reworded arm turns
    this red instead of silently sending every upgrade to the running installer."""

    def _real(self) -> Path:
        return self_cmd.packaged_install_dir()

    def test_every_flag_the_build_argv_carries_is_accepted(self) -> None:
        receipt = self_cmd._RegistryReceipt(source="conexus", extras=("pdf",))
        argv = self_cmd._build_argv(self._real(), receipt, version="1.2.3", extras=["pdf"])
        flags = {a for a in argv if a.startswith("--")}
        assert flags == {"--source", "--extras", "--version"}
        arms = target_installer._flag_arms(self._real() / "install_generation.sh")
        assert flags <= arms, f"install_generation.sh has no arm for {sorted(flags - arms)}"
        assert target_installer._unusable_reason(
            self._real(), windows=False, with_extras=True, with_version=True,
        ) is None

    def test_the_legacy_scripts_flags_are_accepted_by_the_install_script_it_runs(self) -> None:
        text = (self._real() / "migrate_legacy.sh").read_text()
        passed = set(re.findall(r"_build_args\+?=\((--[a-z-]+)", text))
        arms = target_installer._flag_arms(self._real() / "install_generation.sh")
        assert {"--source", "--version", "--extras"} <= passed, passed
        assert passed <= arms | {"--python"}

    def test_the_windows_cores_signatures_accept_what_the_code_passes(self) -> None:
        try:
            for migrate in (False, True):
                assert target_installer._unusable_reason(
                    self._real(), windows=True, with_extras=True, with_version=True, migrate=migrate,
                ) is None
        finally:
            target_installer._clear_loaded_cores()


def test_progress_is_passed_only_to_a_core_that_takes_it() -> None:
    """A target release's generation_core older than nexus-2k66t.4.7 has no `progress`
    keyword; it builds quietly instead of failing with a TypeError or being refused."""
    def old(name, *, tools, platform):  # noqa: ARG001 — signature is the point
        return None

    def new(name, *, tools, platform, progress=False):  # noqa: ARG001
        return None

    assert self_cmd._progress_kwarg(old, True) == {}
    assert self_cmd._progress_kwarg(new, True) == {"progress": True}

    def open_ended(name, **kw):  # noqa: ARG001
        return None

    assert self_cmd._progress_kwarg(open_ended, False) == {"progress": False}
