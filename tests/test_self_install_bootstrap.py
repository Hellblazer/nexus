# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-228 Phase 2 Step 1 (nexus-sv2wx.14): ``nx self install --bootstrap``.

The first install of the CLI, in one command and one generation, from a
machine that has only ``uv``: the command the plugin runs is
``uv tool run --python 3.12 --from conexus==<version> nx self install --bootstrap``,
and the program that runs it is a throwaway ``uv`` environment, which is not a
generation and not a ``uv tool`` install.

These tests drive the real command in a real subprocess whose ``sys.prefix`` is
a throwaway virtual environment (made with the standard library, so it needs no
network), against a LOCAL package index: a directory of hand-built stand-in
``conexus`` wheels that ``uv`` reads through ``UV_FIND_LINKS`` with
``UV_NO_INDEX`` set. The index holds the running program's own version and a
higher one, so a build that asked for "the latest" instead of the running
version installs the higher stand-in and fails the version test.

The stand-in wheel is not the real package. What it proves is the install
machinery (generation, ``current``, shims, receipt, cache), which is what this
bead adds; the Windows host run in the bead's Done-when builds the real wheel.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import subprocess
import sys
import sysconfig
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus
from nexus import install_layout
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
    index.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(index / f"conexus-{version}-py3-none-any.whl", "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)


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
            if name.startswith(("NX_", "UV_")) or name in ("VIRTUAL_ENV", "PYTHONHOME"):
                del env[name]
        site = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
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
            "UV_FIND_LINKS": str(self.index),
            "UV_NO_INDEX": "1",
            # The refusal text holds an em dash; a Windows child would otherwise
            # write it in the console code page and this test would read mojibake.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.pathsep.join([str(Path(nexus.__file__).resolve().parents[1]), *sorted(site)]),
        })
        return env

    def run(self, python: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
        """``nx <args>``, run by *python* (a program that imports this checkout)."""
        return subprocess.run(  # noqa: S603 — fixed argv
            [str(python), "-c", _DRIVER, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env(), timeout=600, check=False,
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
    return world


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


def _seed_generation(world: World) -> None:
    generation = world.tools / "gen-20260101T000000Z"
    generation.mkdir()
    receipt = lc.Receipt(
        version="1.0.0", spec="conexus==1.0.0", source_kind="registry", source="conexus",
        python="3.12.0", base_interpreter=str(Path(sys.base_prefix)), created_at="2026-01-01T00:00:00Z",
    )
    (generation / lc.RECEIPT_NAME).write_text(receipt.to_json())


def _seed_uv_tool_install(world: World) -> None:
    (lc.venv_bin(world.uv_tools / "conexus")).mkdir(parents=True)


class TestAnExistingInstall:
    """A reading of the record, not a statement in it (checked by P2.R2,
    nexus-sv2wx.22): the flag is for a first install, so with a complete install
    present it refuses and names the plain command, which is the reading that
    cannot overwrite anything."""

    @pytest.mark.parametrize("seed", [_seed_generation, _seed_uv_tool_install], ids=["generation", "uv-tool"])
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

    def test_a_dry_run_prints_the_pinned_build_and_changes_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = world.snapshot()
        result = self._invoke(world, monkeypatch, "--dry-run")
        assert result.exit_code == 0, result.output
        assert "conexus" in result.output and _running_version() in result.output
        assert "--extras" not in result.output
        assert world.snapshot() == before
