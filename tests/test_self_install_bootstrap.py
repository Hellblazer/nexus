# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-228 Phase 2 Step 1 (nexus-sv2wx.14): ``nx self install --bootstrap``.

The first install of the CLI, in one command and one generation, from a
machine that has only ``uv``: the command the plugin runs is
``uv tool run --python 3.12 --from conexus==<version> nx self install --bootstrap``,
and the program that runs it is a throwaway ``uv`` environment, which is not a
generation and not a ``uv tool`` install.

These tests drive the real command in a real subprocess whose ``sys.prefix`` is
a throwaway virtual environment (made with the standard library, so it needs no
network), against a LOCAL package index: a ``file://`` simple-API index of hand-built stand-in
``conexus`` wheels, handed to ``uv`` as ``UV_INDEX_URL``. (``UV_NO_INDEX`` is not
enough: uv 0.8.0 ignored it and read the real PyPI, where the real wheel of the
running version exists, so the first version of these tests installed that.) The index holds the running program's own version and a
higher one, so a build that asked for "the latest" instead of the running
version installs the higher stand-in and fails the version test.

The stand-in wheel is not the real package. What it proves is the install
machinery (generation, ``current``, shims, receipt, cache), which is what this
bead adds; the Windows host run in the bead's Done-when builds the real wheel.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import nexus
from nexus import install_layout
from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc
from nexus.commands import self_cmd

ON_WINDOWS = os.name == "nt"

#: Higher than any real release: a build that does not pin the running
#: program's version resolves to this stand-in.
HIGHER = "999.0.0"

#: The message this command printed before the flag existed, for a program that
#: is neither a generation nor a packaged install. ``{host}`` is that program's
#: ``sys.prefix`` and ``{tools}`` the generation root.
TODAYS_REFUSAL = (
    "Error: this nx is running from {host}, which is not a generation under "
    "{tools} — `nx self install` upgrades a generation install in "
    "place-safe fashion and has nothing to do from a dev checkout.\n"
    "For a checkout, use: scripts/reinstall-tool.sh"
)

_DRIVER = (
    "import sys\n"
    "from nexus.cli import main\n"
    "sys.argv[0] = 'nx'\n"
    "main()\n"
)


def _running_version() -> str:
    return importlib.metadata.version("conexus")


def _write_standin_wheel(index: Path, version: str) -> None:
    """A minimal ``conexus`` wheel declaring the ``nx`` and ``nx-mcp`` scripts.

    Built with ``zipfile``: no build backend, no network. The RECORD lists every
    file so ``uv`` can install and later uninstall it.
    """
    dist_info = f"conexus-{version}.dist-info"
    files = {
        "conexus_stub/__init__.py": f"def main():\n    print('stand-in nx {version}')\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: conexus\nVersion: {version}\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\nnx = conexus_stub:main\nnx-mcp = conexus_stub:main\n"
        ),
    }
    record = [
        f"{name},sha256={hashlib.sha256(body.encode()).hexdigest()},{len(body.encode())}"
        for name, body in files.items()
    ]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record) + "\n"
    project = index / "conexus"
    project.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(project / f"conexus-{version}-py3-none-any.whl", "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)
    _write_index_page(index)


def _write_index_page(index: Path) -> None:
    """The PEP 503 page for ``conexus``: one link per wheel now in the directory."""
    project = index / "conexus"
    links = "".join(
        f'<a href="{wheel.name}">{wheel.name}</a><br>\n' for wheel in sorted(project.glob("*.whl"))
    )
    (project / "index.html").write_text(
        f"<!DOCTYPE html><html><body>\n{links}</body></html>\n", encoding="utf-8",
    )


def _drop_wheel(index: Path, version: str) -> None:
    (index / "conexus" / f"conexus-{version}-py3-none-any.whl").unlink()
    _write_index_page(index)


@dataclass
class World:
    """One throwaway machine: a home, the generation root, the bin directory,
    ``uv``'s cache and tool directories, the local index, and the throwaway
    environment the command runs from."""

    root: Path
    index: Path
    tools: Path
    bin: Path
    uv_cache: Path
    uv_tools: Path
    throwaway: Path
    path_store: Path

    @property
    def python(self) -> Path:
        return lc.venv_python(self.throwaway)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for name in list(env):
            if name.startswith(("NX_", "UV_")) or name in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
                del env[name]
        env.update({
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "NX_TOOLS_DIR": str(self.tools),
            "NX_BIN_DIR": str(self.bin),
            # The Windows build writes the user PATH; a file stands in for the
            # registry so the test never touches the real account.
            "NX_USER_PATH_STORE": str(self.path_store),
            "UV_CACHE_DIR": str(self.uv_cache),
            "UV_TOOL_DIR": str(self.uv_tools),
            "UV_INDEX_URL": self.index.as_uri(),
            # The refusal text holds an em dash; a Windows child would otherwise
            # write it in the console code page and this test would read mojibake.
            "PYTHONIOENCODING": "utf-8",
        })
        return env

    def run(
        self, python: Path | str, *args: str, **extra: str,
    ) -> subprocess.CompletedProcess[str]:
        """``nx <args>``, run by *python* (a program that imports this checkout).
        *extra* adds or replaces environment variables."""
        env = self.env()
        env.update(extra)
        return subprocess.run(  # noqa: S603 — fixed argv
            [str(python), "-c", _DRIVER, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=600, check=False,
        )

    def prefix_of(self, python: Path | str) -> str:
        out = subprocess.run(  # noqa: S603 — fixed argv
            [str(python), "-c", "import sys; print(sys.prefix)"],
            capture_output=True, text=True, env=self.env(), timeout=60, check=True,
        )
        return out.stdout.strip()

    def reported_version(self, python: Path | str) -> str:
        out = subprocess.run(  # noqa: S603 — fixed argv
            [str(python), "-c",
             "import importlib.metadata as m; print(m.version('conexus'))"],
            capture_output=True, text=True, env=self.env(), timeout=60, check=True,
        )
        return out.stdout.strip()

    def snapshot(self) -> dict[str, object]:
        """Everything the command could have written: names, sizes, times and
        file contents under the generation root, the bin directory, uv's tool
        directory and uv's cache."""
        seen: dict[str, object] = {}
        for base in (self.tools, self.bin, self.uv_tools, self.uv_cache, self.path_store.parent):
            for dirpath, dirnames, filenames in os.walk(base):
                for name in [*dirnames, *filenames]:
                    path = Path(dirpath) / name
                    key = str(path.relative_to(self.root))
                    info = path.lstat()
                    body = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
                    seen[key] = (info.st_size, info.st_mtime_ns, hashlib.sha256(body).hexdigest())
        return seen


def _make_world(root: Path) -> World:
    world = World(
        root=root, index=root / "index", tools=root / "tools", bin=root / "bin",
        uv_cache=root / "uvcache", uv_tools=root / "uvtools",
        throwaway=root / "throwaway", path_store=root / "state" / "user-path.txt",
    )
    (root / "home").mkdir(parents=True)
    for directory in (world.tools, world.bin, world.uv_cache, world.uv_tools, world.path_store.parent):
        directory.mkdir(parents=True)
    _write_standin_wheel(world.index, _running_version())
    _write_standin_wheel(world.index, HIGHER)
    venv.EnvBuilder(with_pip=False, symlinks=not ON_WINDOWS).create(world.throwaway)
    _let_the_throwaway_import_this_checkout(world)
    return world


def _let_the_throwaway_import_this_checkout(world: World) -> None:
    """Make the checkout and its dependencies importable by the throwaway
    environment's own interpreter, through a ``.pth`` file in ITS site-packages.

    Not ``PYTHONPATH``: that variable is inherited by every child, including the
    interpreter ``uv`` inspects when it builds the new generation, and a
    generation that sees this checkout's ``conexus`` on its path finds the
    requested version "already installed" and fetches nothing. The first version
    of these tests did that, so none of them reached the package index."""
    purelib = subprocess.run(  # noqa: S603 — fixed argv
        [str(world.python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()
    site = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    paths = [str(Path(nexus.__file__).resolve().parents[1]), *sorted(site)]
    Path(purelib, "checkout.pth").write_text("\n".join(paths) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _never_the_real_user_path(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every in-process call that reaches ``ensure_user_path`` writes a file,
    never the registry. On Windows a test without this wrote a sandbox entry
    into the real HKCU user PATH (nexus-sv2wx.14, native Windows run)."""
    store = tmp_path_factory.mktemp("user-path") / "user-path"
    monkeypatch.setenv("NX_USER_PATH_STORE", str(store))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return _make_world(tmp_path / "w")


@dataclass
class Bootstrapped:
    world: World
    result: subprocess.CompletedProcess[str]
    running_version: str
    marker: Path


@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory: pytest.TempPathFactory) -> Bootstrapped:
    """ONE real ``--bootstrap`` run from the throwaway environment, with a marker
    file in the cache directory. Shared by the tests that read its outcome."""
    world = _make_world(tmp_path_factory.mktemp("bootstrap"))
    marker = world.uv_cache / "MARKER-must-survive"
    marker.write_text("x")
    result = world.run(world.python, "self", "install", "--bootstrap")
    return Bootstrapped(world, result, world.reported_version(world.python), marker)


def _generations(world: World) -> list[Path]:
    return sorted(p for p in world.tools.glob("gen-*") if p.is_dir())


class TestTheBootstrapBuild:
    def test_it_builds_one_generation_and_points_current_at_it(self, bootstrapped: Bootstrapped) -> None:
        world, result = bootstrapped.world, bootstrapped.result
        assert result.returncode == 0, result.stdout + result.stderr
        generations = _generations(world)
        assert len(generations) == 1, generations
        current = install_layout.current_generation(tools=world.tools)
        assert lc.compare_key(current) == lc.compare_key(generations[0])
        assert (generations[0] / lc.RECEIPT_NAME).is_file()
        assert f"installed {generations[0].name}" in result.stdout

    def test_it_writes_the_command_shims(self, bootstrapped: Bootstrapped) -> None:
        """POSIX: a shim in the user's bin directory. Windows: a launcher in
        ``<tools>\\current\\bin``, which the user PATH names (nexus writes no
        launcher into uv's bin directory there)."""
        world = bootstrapped.world
        if ON_WINDOWS:
            shim = world.tools / "current" / "bin" / lc.exe_name("nx", platform="win32")
        else:
            shim = world.bin / "nx"
        assert shim.is_file(), f"no nx shim at {shim}"

    def test_the_receipt_lists_no_extras(self, bootstrapped: Bootstrapped) -> None:
        receipt = install_layout.read_receipt(_generations(bootstrapped.world)[0])
        assert receipt.extras == []
        assert receipt.spec == f"conexus=={bootstrapped.running_version}"
        assert (receipt.source, receipt.source_kind) == ("conexus", "registry")

    def test_the_installed_version_is_the_version_of_the_program_that_ran_the_command(
        self, bootstrapped: Bootstrapped,
    ) -> None:
        """The index also holds a higher version; the running program's own wins."""
        assert bootstrapped.running_version == _running_version()
        assert bootstrapped.running_version != HIGHER
        generation = _generations(bootstrapped.world)[0]
        probe = subprocess.run(  # noqa: S603 — fixed argv
            [str(lc.venv_python(generation)), "-c",
             "import importlib.metadata as m; print(m.version('conexus'))"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        assert probe.stdout.strip() == bootstrapped.running_version
        assert install_layout.read_receipt(generation).version == bootstrapped.running_version

    def test_the_cache_prune_does_not_run(self, bootstrapped: Bootstrapped) -> None:
        """``uv cache prune`` removes a file it does not know; the marker is still there."""
        assert bootstrapped.result.returncode == 0
        assert bootstrapped.marker.is_file(), "a cache prune ran: the marker is gone"
        assert "prune" not in (bootstrapped.result.stdout + bootstrapped.result.stderr).lower()

    def test_the_cache_marker_test_would_notice_a_prune(self, world: World) -> None:
        """Positive control: the same marker, under the same cache directory, IS
        removed by ``uv cache prune``, so its survival above says something."""
        marker = world.uv_cache / "MARKER"
        marker.write_text("x")
        subprocess.run(  # noqa: S603 — fixed argv
            ["uv", "cache", "prune"], env=world.env(), capture_output=True, check=True, timeout=120,
        )
        assert not marker.exists()


class TestTheRefusalsThatStay:
    def test_without_the_flag_a_throwaway_environment_refuses_with_todays_message(
        self, world: World,
    ) -> None:
        before = world.snapshot()
        result = world.run(world.python, "self", "install")
        assert result.returncode == 1
        expected = TODAYS_REFUSAL.format(host=world.prefix_of(world.python), tools=world.tools)
        assert result.stderr.strip().endswith(expected), result.stderr
        assert world.snapshot() == before
        assert _generations(world) == []

    def test_without_the_flag_a_development_checkout_refuses_with_todays_message(
        self, world: World,
    ) -> None:
        """The interpreter running this test is a checkout's environment."""
        before = world.snapshot()
        result = world.run(sys.executable, "self", "install")
        assert result.returncode == 1
        expected = TODAYS_REFUSAL.format(host=world.prefix_of(sys.executable), tools=world.tools)
        assert result.stderr.strip().endswith(expected), result.stderr
        assert world.snapshot() == before


def _receipted_generation(
    world: World, name: str = "gen-20260101T000000Z", version: str = "1.0.0",
) -> Path:
    generation = world.tools / name
    generation.mkdir()
    receipt = lc.Receipt(
        version=version, spec=f"conexus=={version}", source_kind="registry", source="conexus",
        python="3.12.0", base_interpreter=str(Path(sys.base_prefix)), created_at="2026-01-01T00:00:00Z",
    )
    (generation / lc.RECEIPT_NAME).write_text(receipt.to_json())
    return generation


def _point_current(world: World, target: Path) -> None:
    """``current`` -> *target*: a symlink on POSIX, a junction on Windows (where
    the generation module's own flip makes it)."""
    if ON_WINDOWS:
        gen_core.flip_current(target, world.tools, platform="win32")
    else:
        os.symlink(target, world.tools / "current")


def _seed_shims(world: World) -> None:
    """What the shim step leaves, for the generation ``current`` names. POSIX: our
    ``nx`` shim in the bin directory. Windows: ``nx.exe`` in ``<tools>\\current\\bin``
    and that directory on the user PATH (the sandbox store)."""
    if ON_WINDOWS:
        launcher = world.tools / "current" / "bin"
        launcher.mkdir(exist_ok=True)
        (launcher / "nx.exe").write_bytes(b"MZ")
        world.path_store.write_text(str(launcher) + "\n", encoding="utf-8")
    else:
        shim = world.bin / "nx"
        shim.write_text(lc.render_shim("nx", tools=world.tools))
        shim.chmod(0o755)


def _lose_the_shims(world: World) -> None:
    """The state a run that died AFTER the flip leaves: ``current`` stays, the
    shim step's output is gone."""
    if ON_WINDOWS:
        world.path_store.write_text("\n", encoding="utf-8")
    else:
        for entry in world.bin.iterdir():
            entry.unlink()


def _seed_current_generation(world: World) -> None:
    """A complete install: a receipted generation that ``current`` points at,
    with the shim step's output in place."""
    _point_current(world, _receipted_generation(world))
    _seed_shims(world)


def _seed_uv_tool_install(world: World) -> None:
    (lc.venv_bin(world.uv_tools / "conexus")).mkdir(parents=True)


def _remove_link(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    else:
        os.rmdir(path)


def _interrupt(world: World, *, keep_shims: bool = False) -> None:
    """Leave the state a run that died after the receipt leaves: the generation
    and its receipt, no ``current``, and (unless *keep_shims*) no shims."""
    _remove_link(world.tools / "current")
    if not keep_shims:
        for entry in world.bin.iterdir():
            entry.unlink()


def _shim_of(world: World) -> Path:
    if ON_WINDOWS:
        return world.tools / "current" / "bin" / lc.exe_name("nx", platform="win32")
    return world.bin / "nx"


class TestAnExistingInstall:
    """A reading of the record, not a statement in it (checked by P2.R2,
    nexus-sv2wx.22): the flag is for a first install, so with a COMPLETE install
    present it refuses and names the plain command, which is the reading that
    cannot overwrite anything. Complete means ``current`` resolves to a receipted
    generation, or a ``uv tool`` install of conexus exists."""

    @pytest.mark.parametrize(
        "seed", [_seed_current_generation, _seed_uv_tool_install], ids=["generation", "uv-tool"],
    )
    def test_it_refuses_in_one_line_naming_plain_self_install_and_changes_nothing(
        self, world: World, seed,
    ) -> None:
        seed(world)
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        message = result.stderr.strip()
        assert message.startswith("Error: "), message
        assert "\n" not in message, message
        assert "Run `nx self install`" in message
        assert world.snapshot() == before
        assert result.stdout == ""

    def test_a_second_bootstrap_after_a_real_one_changes_nothing(self, bootstrapped: Bootstrapped) -> None:
        world = bootstrapped.world
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Run `nx self install`" in result.stderr
        assert world.snapshot() == before
        assert len(_generations(world)) == 1

    def test_a_dry_run_with_an_existing_install_refuses_too(self, world: World) -> None:
        _seed_current_generation(world)
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap", "--dry-run")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Run `nx self install`" in result.stderr
        assert result.stdout == ""
        assert world.snapshot() == before


class TestAnInterruptedBootstrap:
    """The receipt is written when the build finishes, BEFORE ``current`` and the
    shims. A run that dies in between leaves a receipted generation and no
    ``current``, on a machine that has no ``nx`` on its PATH: the refusal's
    "Run `nx self install`" would be unrunnable there, so the same command
    finishes the install instead, without rebuilding."""

    @pytest.mark.parametrize("keep_shims", [False, True], ids=["no-shims", "our-shims-left"])
    def test_a_receipted_generation_with_no_current_is_finished_without_a_rebuild(
        self, world: World, keep_shims: bool,
    ) -> None:
        first = world.run(world.python, "self", "install", "--bootstrap")
        assert first.returncode == 0, first.stdout + first.stderr
        (generation,) = _generations(world)
        receipt_before = (generation / lc.RECEIPT_NAME).read_bytes()
        _interrupt(world, keep_shims=keep_shims)
        assert not (world.tools / "current").exists()

        again = world.run(world.python, "self", "install", "--bootstrap")

        assert again.returncode == 0, again.stdout + again.stderr
        assert _generations(world) == [generation], "a second generation was built"
        assert (generation / lc.RECEIPT_NAME).read_bytes() == receipt_before
        current = install_layout.current_generation(tools=world.tools)
        assert lc.compare_key(current) == lc.compare_key(generation)
        assert _shim_of(world).is_file()
        assert f"installed {generation.name}" in again.stdout
        assert "no rebuild" in again.stdout and generation.name in again.stdout

    def test_a_dangling_current_is_finished_the_same_way(self, world: World) -> None:
        first = world.run(world.python, "self", "install", "--bootstrap")
        assert first.returncode == 0, first.stdout + first.stderr
        (generation,) = _generations(world)
        _interrupt(world)
        elsewhere = world.root / "elsewhere"
        elsewhere.mkdir()
        _point_current(world, elsewhere)
        shutil.rmtree(elsewhere)

        again = world.run(world.python, "self", "install", "--bootstrap")

        assert again.returncode == 0, again.stdout + again.stderr
        assert _generations(world) == [generation]
        current = install_layout.current_generation(tools=world.tools)
        assert lc.compare_key(current) == lc.compare_key(generation)

    def _invoke_in_process(self, world: World, monkeypatch: pytest.MonkeyPatch, *args: str):
        for name in ("NX_TOOLS_DIR", "NX_BIN_DIR", "UV_TOOL_DIR"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NX_TOOLS_DIR", str(world.tools))
        monkeypatch.setenv("NX_BIN_DIR", str(world.bin))
        monkeypatch.setenv("UV_TOOL_DIR", str(world.uv_tools))
        return CliRunner().invoke(self_cmd.install_cmd, ["--bootstrap", *args])

    def test_the_newest_usable_generation_is_the_one_finished(self, world: World, monkeypatch) -> None:
        older = _receipted_generation(world, "gen-20260101T000000Z", version=_running_version())
        newer = _receipted_generation(world, "gen-20260202T000000Z", version=_running_version())
        finished: list[Path] = []
        monkeypatch.setattr(
            self_cmd, "_flip_and_shim", lambda generation, **_: finished.append(generation),
        )
        monkeypatch.setattr(self_cmd, "_shim_names_of", lambda generation: ["nx"])
        result = self._invoke_in_process(world, monkeypatch)
        assert result.exit_code == 0, result.output
        assert finished == [newer] and older not in finished

    def test_a_newer_generation_that_cannot_be_finished_is_passed_over_for_an_older_one(
        self, world: World, monkeypatch,
    ) -> None:
        older = _receipted_generation(world, "gen-20260101T000000Z", version=_running_version())
        newer = _receipted_generation(world, "gen-20260202T000000Z", version=_running_version())
        finished: list[Path] = []
        monkeypatch.setattr(
            self_cmd, "_flip_and_shim", lambda generation, **_: finished.append(generation),
        )
        def names(generation: Path) -> list[str]:
            if generation != older:
                raise lc.LayoutError("no interpreter")
            return ["nx"]
        monkeypatch.setattr(self_cmd, "_shim_names_of", names)
        result = self._invoke_in_process(world, monkeypatch)
        assert result.exit_code == 0, result.output
        assert finished == [older] and newer not in finished

    def test_a_dry_run_says_what_it_would_finish_and_changes_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generation = _receipted_generation(world, version=_running_version())
        monkeypatch.setattr(self_cmd, "_shim_names_of", lambda generation: ["nx"])
        before = world.snapshot()
        result = self._invoke_in_process(world, monkeypatch, "--dry-run")
        assert result.exit_code == 0, result.output
        assert generation.name in result.output and "no rebuild" in result.output
        assert _running_version() in result.output
        assert world.snapshot() == before


def _built(world: World) -> Path:
    """One real bootstrap, returning its generation."""
    first = world.run(world.python, "self", "install", "--bootstrap")
    assert first.returncode == 0, first.stdout + first.stderr
    (generation,) = _generations(world)
    return generation


def _record_version(generation: Path, version: str) -> None:
    path = generation / lc.RECEIPT_NAME
    data = json.loads(path.read_text())
    data["version"] = version
    path.write_text(json.dumps(data))


class TestAnInstallThatDiedAfterTheFlip:
    """The flip comes before the shim step, so a run that dies, fails or is
    killed in between leaves ``current`` at a receipted generation with nothing
    on the PATH. That is NOT a complete install: refusing it would send the reader
    to a `nx self install` that does not exist on the machine."""

    def test_current_set_and_no_shims_is_finished_without_a_rebuild(self, world: World) -> None:
        generation = _built(world)
        _lose_the_shims(world)
        assert install_layout.current_generation(tools=world.tools) == generation

        again = world.run(world.python, "self", "install", "--bootstrap")

        assert again.returncode == 0, again.stdout + again.stderr
        assert _generations(world) == [generation], "a second generation was built"
        assert _shim_of(world).is_file()
        if not ON_WINDOWS:
            assert (world.bin / "nx").read_text().startswith("#!")
        assert "no rebuild" in again.stdout and generation.name in again.stdout

    @pytest.mark.skipif(
        ON_WINDOWS or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="a read-only bin directory stops writes for a non-root POSIX user",
    )
    def test_a_shim_step_that_fails_leaves_a_state_the_same_command_finishes(self, world: World) -> None:
        world.bin.chmod(0o555)
        try:
            failed = world.run(world.python, "self", "install", "--bootstrap")
            assert failed.returncode != 0, failed.stdout + failed.stderr
            (generation,) = _generations(world)
            assert install_layout.current_generation(tools=world.tools) == generation, "the flip did not happen"
            assert not (world.bin / "nx").exists()
        finally:
            world.bin.chmod(0o755)

        again = world.run(world.python, "self", "install", "--bootstrap")

        assert again.returncode == 0, again.stdout + again.stderr
        assert _generations(world) == [generation]
        assert (world.bin / "nx").is_file()

    def test_a_complete_install_still_refuses_with_the_old_text(self, bootstrapped: Bootstrapped) -> None:
        world = bootstrapped.world
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stderr.strip() == (
            f"Error: --bootstrap is for a first install and generation "
            f"{_generations(world)[0].name} under {world.tools} is already installed. "
            "Run `nx self install` to update it. Nothing was changed."
        )
        assert world.snapshot() == before

    @pytest.mark.skipif(ON_WINDOWS, reason="nexus writes no file into the bin directory on Windows")
    def test_a_shim_that_is_not_ours_does_not_make_the_install_complete_or_get_replaced(
        self, world: World,
    ) -> None:
        _built(world)
        (world.bin / "nx").unlink()
        (world.bin / "nx").write_text("#!/bin/sh\necho somebody else's nx\n")
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        assert str(world.bin / "nx") in result.stderr and "not a nexus shim" in result.stderr
        assert (world.bin / "nx").read_text() == "#!/bin/sh\necho somebody else's nx\n"


class TestWhatCompleteMeansOnWindows:
    """The Windows reading, on any host: ``current\\bin\\nx.exe`` exists and its
    directory is on the user PATH (the sandbox store stands in for the registry)."""

    @pytest.fixture
    def windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
        world = _make_world(tmp_path / "w")
        monkeypatch.setattr(self_cmd, "_is_windows", lambda platform=None: True)
        monkeypatch.setenv("NX_USER_PATH_STORE", str(world.path_store))
        monkeypatch.setenv("UV_TOOL_DIR", str(world.uv_tools))
        generation = _receipted_generation(world)
        os.symlink(generation, world.tools / "current")
        (generation / "bin").mkdir()
        return world

    def _complete(self, world: World) -> bool:
        return self_cmd._existing_install(world.tools, world.bin) is not None

    def test_the_launcher_and_the_path_entry_make_it_complete(self, windows: World) -> None:
        (windows.tools / "current" / "bin" / "nx.exe").write_bytes(b"MZ")
        windows.path_store.write_text(str(windows.tools / "current" / "bin") + "\n", encoding="utf-8")
        assert self._complete(windows)

    def test_a_launcher_with_no_path_entry_is_unfinished(self, windows: World) -> None:
        (windows.tools / "current" / "bin" / "nx.exe").write_bytes(b"MZ")
        windows.path_store.write_text("C:\\other\n", encoding="utf-8")
        assert not self._complete(windows)

    def test_a_path_entry_with_no_launcher_is_unfinished(self, windows: World) -> None:
        windows.path_store.write_text(str(windows.tools / "current" / "bin") + "\n", encoding="utf-8")
        assert not self._complete(windows)


class TestAResumeNeedsTheRightVersionAndAHealthyGeneration:
    """A leftover generation is finished only when it is what a fresh build would
    produce: the running program's version, answering the console-script lookup
    the shim step makes. Anything else falls through to a fresh build, which is
    safe because no complete install exists."""

    def _fresh_build_took_over(self, world: World, old: Path, again) -> Path:
        assert again.returncode == 0, again.stdout + again.stderr
        generations = _generations(world)
        assert len(generations) == 2 and old in generations, generations
        (new,) = [g for g in generations if g != old]
        assert lc.compare_key(install_layout.current_generation(tools=world.tools)) == lc.compare_key(new)
        assert install_layout.read_receipt(new).version == _running_version()
        assert "no rebuild" not in again.stdout
        assert _shim_of(world).is_file()
        return new

    @pytest.mark.parametrize("flipped", [False, True], ids=["no-current", "current-set-no-shims"])
    def test_an_older_version_leftover_is_not_finished(self, world: World, flipped: bool) -> None:
        old = _built(world)
        _record_version(old, "0.0.1")
        if flipped:
            _lose_the_shims(world)
        else:
            _interrupt(world)
        again = world.run(world.python, "self", "install", "--bootstrap")
        self._fresh_build_took_over(world, old, again)

    @pytest.mark.parametrize("flipped", [False, True], ids=["no-current", "current-set-no-shims"])
    def test_a_leftover_whose_interpreter_is_gone_is_not_finished(self, world: World, flipped: bool) -> None:
        old = _built(world)
        lc.venv_python(old).unlink()
        if flipped:
            _lose_the_shims(world)
        else:
            _interrupt(world)
        again = world.run(world.python, "self", "install", "--bootstrap")
        self._fresh_build_took_over(world, old, again)

    def test_a_matching_healthy_leftover_is_finished_and_the_message_names_its_version(
        self, world: World,
    ) -> None:
        old = _built(world)
        _interrupt(world)
        again = world.run(world.python, "self", "install", "--bootstrap")
        assert again.returncode == 0, again.stdout + again.stderr
        assert _generations(world) == [old]
        assert "no rebuild" in again.stdout and _running_version() in again.stdout


class TestAnUnreadableCurrentIsNeverMoved:
    """``current`` names a directory that exists but whose receipt cannot be
    read: whatever runs from it is working or at least not ours to move, so the
    flag refuses in one line, naming the generation and the way to repair it from
    the generation's own ``nx`` (there may be none on the PATH)."""

    def _refused(self, world: World, generation: Path) -> None:
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        message = result.stderr.strip()
        assert message.startswith("Error: ") and "\n" not in message, message
        assert str(generation) in message
        assert f"{lc.venv_script(generation, 'nx')} self install" in message
        assert world.snapshot() == before
        assert result.stdout == ""

    def test_a_generation_without_a_receipt(self, world: World) -> None:
        generation = world.tools / "gen-20260101T000000Z"
        generation.mkdir()
        _point_current(world, generation)
        self._refused(world, generation)

    def test_a_generation_whose_receipt_is_garbage(self, world: World) -> None:
        generation = world.tools / "gen-20260101T000000Z"
        generation.mkdir()
        (generation / lc.RECEIPT_NAME).write_text("{not json")
        _point_current(world, generation)
        _seed_shims(world)
        self._refused(world, generation)

    def test_a_dry_run_refuses_too(self, world: World) -> None:
        generation = world.tools / "gen-20260101T000000Z"
        generation.mkdir()
        _point_current(world, generation)
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap", "--dry-run")
        assert result.returncode == 1 and str(generation) in result.stderr
        assert world.snapshot() == before


class TestWithoutDistributionMetadata:
    """``--bootstrap`` asks the running program which shim names it would write.
    A process with no conexus metadata must refuse in one line, never raise."""

    @pytest.fixture
    def no_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing():
            raise importlib.metadata.PackageNotFoundError("conexus")
        monkeypatch.setattr(self_cmd, "_conexus_entry_points", missing)

    def test_asked_for_the_names_it_refuses_in_one_line(self, no_metadata: None) -> None:
        with pytest.raises(click.ClickException) as exc:
            self_cmd._shim_names_to_write()
        assert "\n" not in exc.value.message and "conexus" in exc.value.message

    def test_given_a_generation_it_reports_that_generations_own_names(
        self, no_metadata: None, bootstrapped: Bootstrapped,
    ) -> None:
        generation = _generations(bootstrapped.world)[0]
        names = self_cmd._shim_names_to_write(generation)
        assert names == sorted(install_layout.owned_shim_names(generation, platform=self_cmd._plat()))
        assert "nx" in names and "nx-mcp" in names

    @pytest.mark.skipif(
        ON_WINDOWS,
        reason="Windows writes no shims into the bin directory, so it never asks "
        "for shim names and a bootstrap there needs no conexus metadata",
    )
    def test_a_bootstrap_with_no_metadata_refuses_in_one_line(
        self, world: World, no_metadata: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(self_cmd, "_running_version", lambda: "1.0.0")
        monkeypatch.setenv("NX_TOOLS_DIR", str(world.tools))
        monkeypatch.setenv("NX_BIN_DIR", str(world.bin))
        monkeypatch.setenv("UV_TOOL_DIR", str(world.uv_tools))
        monkeypatch.setenv("NX_USER_PATH_STORE", str(world.path_store))
        before = world.snapshot()
        result = CliRunner().invoke(self_cmd.install_cmd, ["--bootstrap"])
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output and len(result.output.strip().splitlines()) == 1
        assert world.snapshot() == before


class TestAFailedBuild:
    def test_a_build_that_fails_leaves_no_generation_and_no_current(self, world: World) -> None:
        """The index holds no wheel at the running version."""
        _drop_wheel(world.index, _running_version())
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "generation build failed" in result.stderr
        assert _generations(world) == []
        assert not (world.tools / "current").exists() and not (world.tools / "current").is_symlink()
        assert list(world.bin.iterdir()) == []

    def test_a_network_failure_on_the_bootstrap_path_names_the_bootstrap_rerun(self, world: World) -> None:
        """The index is an address nothing listens on, so uv fails the way an
        unreachable corporate index does. The message must not talk about an
        install that was not changed, nor tell the reader to run `nx`."""
        result = world.run(
            world.python, "self", "install", "--bootstrap", UV_INDEX_URL="http://127.0.0.1:9/simple",
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "re-run the same bootstrap command with UV_INDEX_URL=https://pypi.org/simple" in result.stderr
        assert "not changed" not in result.stderr
        assert "nx self install" not in result.stderr
        assert _generations(world) == []


@pytest.mark.skipif(ON_WINDOWS, reason="nexus writes no file into the bin directory on Windows")
class TestAForeignProgramInTheBinDirectory:
    """The shim step replaces whatever is at ``<bin>/nx``. ``--bootstrap`` runs
    where nothing of nexus's exists, which is exactly where a pipx or npm
    ``nx`` is most likely to be sitting, so it refuses before building."""

    def _refused(self, world: World, name: str) -> None:
        before = world.snapshot()
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 1, result.stdout + result.stderr
        message = result.stderr.strip()
        assert message.startswith("Error: ") and "\n" not in message, message
        assert str(world.bin / name) in message
        assert world.snapshot() == before
        assert _generations(world) == []
        assert result.stdout == ""

    def test_a_foreign_script_is_refused_and_left_alone(self, world: World) -> None:
        foreign = world.bin / "nx"
        foreign.write_text("#!/bin/sh\necho somebody else's nx\n")
        foreign.chmod(0o755)
        self._refused(world, "nx")
        assert foreign.read_text() == "#!/bin/sh\necho somebody else's nx\n"

    def test_a_foreign_symlink_is_refused_and_left_alone(self, world: World) -> None:
        elsewhere = world.root / "pipx" / "nx"
        elsewhere.parent.mkdir()
        elsewhere.write_text("#!/bin/sh\n")
        (world.bin / "nx").symlink_to(elsewhere)
        self._refused(world, "nx")
        assert (world.bin / "nx").is_symlink() and (world.bin / "nx").resolve() == elsewhere.resolve()

    def test_a_foreign_program_under_another_shim_name_is_refused_too(self, world: World) -> None:
        (world.bin / "nx-mcp").write_text("#!/bin/sh\n")
        self._refused(world, "nx-mcp")

    def test_a_dangling_uv_symlink_is_not_foreign(self, world: World) -> None:
        """What `uv tool install conexus` leaves once its tree is gone: ours to replace."""
        (world.bin / "nx").symlink_to(world.uv_tools / "conexus" / "bin" / "nx")
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (world.bin / "nx").is_symlink() and (world.bin / "nx").is_file()

    def test_a_stranger_file_that_is_not_a_shim_name_is_ignored(self, world: World) -> None:
        (world.bin / "ripgrep").write_text("not a nexus command\n")
        result = world.run(world.python, "self", "install", "--bootstrap")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (world.bin / "ripgrep").read_text() == "not a nexus command\n"


class TestTheFlagTakesNothingElse:
    def _invoke(self, world: World, monkeypatch: pytest.MonkeyPatch, *args: str):
        monkeypatch.setenv("NX_TOOLS_DIR", str(world.tools))
        monkeypatch.setenv("NX_BIN_DIR", str(world.bin))
        monkeypatch.setenv("UV_TOOL_DIR", str(world.uv_tools))
        return CliRunner().invoke(self_cmd.install_cmd, ["--bootstrap", *args])

    @pytest.mark.parametrize("args", [("--extras", "local"), ("--version", "1.2.3")], ids=["extras", "version"])
    def test_extras_and_a_version_are_refused(
        self, world: World, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...],
    ) -> None:
        before = world.snapshot()
        result = self._invoke(world, monkeypatch, *args)
        assert result.exit_code == 1
        assert "--bootstrap" in result.output and args[0] in result.output
        assert world.snapshot() == before

    def test_keep_is_accepted_and_changes_nothing_about_the_build(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing is reaped on this path, so the count has nothing to act on;
        the flag is still accepted, because the plain command's callers pass it."""
        plain = self._invoke(world, monkeypatch, "--dry-run")
        kept = self._invoke(world, monkeypatch, "--keep", "7", "--dry-run")
        assert kept.exit_code == 0, kept.output
        assert kept.output == plain.output

    def test_a_dry_run_prints_the_pinned_build_and_changes_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = world.snapshot()
        result = self._invoke(world, monkeypatch, "--dry-run")
        assert result.exit_code == 0, result.output
        assert "conexus" in result.output and _running_version() in result.output
        assert "--extras" not in result.output
        assert world.snapshot() == before


class TestTheWindowsTwin:
    """The Windows build, flip and PATH steps run through ``generation_core``;
    their failure texts must name the bootstrap command on the bootstrap path
    and keep today's words on the plain one."""

    def _core(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        core = self_cmd._generation()
        generation = tmp_path / "tools" / "gen-20260101T000000Z"
        generation.mkdir(parents=True)
        monkeypatch.setattr(core, "build_generation", lambda *a, **k: generation)
        monkeypatch.setattr(self_cmd, "_plat", lambda: "win32")
        monkeypatch.setattr(self_cmd, "_retarget_autostart_task", lambda *_: None)
        return core

    def _fail_flip(self, core, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a, **_k):
            raise core.GenerationError("boom")
        monkeypatch.setattr(core, "flip_current", boom)

    def test_a_failed_flip_on_the_bootstrap_path_names_the_bootstrap_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = self._core(monkeypatch, tmp_path)
        self._fail_flip(core, monkeypatch)
        build = self_cmd._WindowsBuild("conexus", [], "1.0.0")
        with pytest.raises(click.ClickException) as exc:
            self_cmd._windows_build_flip_shims(
                build, tools=tmp_path / "tools", bin_dir=tmp_path / "bin", bootstrap=True,
            )
        message = exc.value.message
        assert "re-run the same `--bootstrap` command" in message
        assert "`nx self install`" not in message and "boom" in message

    def test_a_failed_flip_on_the_plain_path_keeps_its_words(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = self._core(monkeypatch, tmp_path)
        self._fail_flip(core, monkeypatch)
        build = self_cmd._WindowsBuild("conexus", [], "1.0.0")
        with pytest.raises(click.ClickException) as exc:
            self_cmd._windows_build_flip_shims(build, tools=tmp_path / "tools", bin_dir=tmp_path / "bin")
        assert exc.value.message == (
            "flip or user PATH update failed (`nx self install` re-runs the missing steps):\nboom"
        )

    def test_a_failed_build_on_the_bootstrap_path_uses_the_bootstrap_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = self_cmd._generation()

        def unreachable(*_a, **_k):
            raise core.GenerationError(
                "Failed to fetch https://example.org/simple/conexus/\n  dns error: failed to lookup address"
            )
        monkeypatch.setattr(core, "build_generation", unreachable)
        monkeypatch.setattr(self_cmd, "_plat", lambda: "win32")
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        build = self_cmd._WindowsBuild("conexus", [], "1.0.0")
        with pytest.raises(click.ClickException) as exc:
            self_cmd._windows_build_flip_shims(
                build, tools=tmp_path / "tools", bin_dir=tmp_path / "bin", bootstrap=True,
            )
        assert "re-run the same bootstrap command" in exc.value.message
        assert "not changed" not in exc.value.message
