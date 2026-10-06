# SPDX-License-Identifier: AGPL-3.0-or-later
"""What no injected seam can prove about the Windows generation installer
(RDR-224, nexus-f9bgu.47): real directory junctions, a swap while a process runs
through one, a running launcher replaced by rename-aside, a generation held by
an open file kept by GC.

Windows-only, and in the rehearsal's Windows test set. No symlinks (the runner's
account holds no symlink privilege) and no POSIX stubs. The class never
skip-passes once selected on Windows: the rehearsal's junit floor lists
``tests.test_install_generation_real_windows.TestRealWindows`` under
``--require-passed``, so a run that skipped it fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from nexus._install import gc_core
from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc

ON_WINDOWS = os.name == "nt"


def _copy_of_cmd(destination: Path) -> Path:
    """A self-contained, runnable exe to stand in for a running launcher."""
    shutil.copyfile(Path(os.environ["COMSPEC"]), destination)
    return destination


def _sleeper(exe: Path) -> subprocess.Popen:
    """Run *exe* (a copy of cmd.exe) for about half a minute."""
    return subprocess.Popen([str(exe), "/c", "ping", "-n", "30", "127.0.0.1", ">nul"])


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows kernel's junctions and file locking")
class TestRealWindows:
    def test_a_real_junction_is_created_read_and_swapped(self, tmp_path: Path) -> None:
        one, two = tmp_path / "gen-1", tmp_path / "gen-2"
        one.mkdir()
        two.mkdir()
        (one / "who").write_text("one")
        (two / "who").write_text("two")
        link = tmp_path / "current"
        ops = gen_core.LinkOps()

        gen_core.swap_link(str(one), link, ops)
        assert lc.is_link(link)
        assert not link.is_symlink(), "a junction is not a symlink to pathlib; that is the whole point"
        assert os.path.isjunction(link)
        read = lc.read_link(link)
        assert not read.startswith("\\\\?\\")
        assert lc.compare_key(read) == lc.compare_key(str(one))
        assert (link / "who").read_text() == "one"

        gen_core.swap_link(str(two), link, ops)
        assert (link / "who").read_text() == "two"
        assert one.is_dir() and (one / "who").read_text() == "one", "the old target must survive the swap"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["current", "gen-1", "gen-2"]

    def test_a_swap_works_while_a_process_runs_through_the_junction(self, tmp_path: Path) -> None:
        one, two = tmp_path / "gen-1", tmp_path / "gen-2"
        one.mkdir()
        two.mkdir()
        _copy_of_cmd(one / "nx.exe")
        link = tmp_path / "current"
        gen_core.swap_link(str(one), link, gen_core.LinkOps())
        child = _sleeper(link / "nx.exe")  # executing through the junction, as a spawn does
        try:
            time.sleep(0.5)
            assert child.poll() is None
            gen_core.swap_link(str(two), link, gen_core.LinkOps())
            assert lc.compare_key(lc.read_link(link)) == lc.compare_key(str(two))
            assert child.poll() is None, "the swap must not disturb a running process"
        finally:
            child.kill()
            child.wait()

    def test_flip_and_rollback_on_real_junctions(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        a, b = tools / "gen-A", tools / "gen-B"
        a.mkdir()
        b.mkdir()
        gen_core.flip_current(a, tools)
        gen_core.flip_current(b, tools)
        assert lc.current_generation(tools=tools).name == "gen-B"
        assert lc.compare_key(lc.read_link(tools / "previous")) == lc.compare_key(str(a))
        gen_core.rollback_current(tools)
        assert lc.current_generation(tools=tools).name == "gen-A"

    def test_the_real_user_path_registry_value_is_readable_and_left_untouched(self) -> None:
        """Read-only: the store seam reads HKCU\\Environment without writing."""
        value, reg_type = gen_core.RegistryUserPath().read()
        assert isinstance(value, str)
        assert reg_type in (1, 2), reg_type  # REG_SZ or REG_EXPAND_SZ

    def test_gc_plan_protects_real_junction_current_and_previous(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        gens = {}
        for name in ("A", "B", "C", "D"):
            gen = tools / f"gen-{name}"
            gen.mkdir()
            (gen / "nexus-install.json").write_text("{}\n")
            gens[name] = gen
        gen_core.flip_current(gens["B"], tools)
        gen_core.flip_current(gens["C"], tools)  # current -> C, previous -> B, both junctions
        assert lc.is_link(tools / "current") and lc.is_link(tools / "previous")
        plan = {entry.name: (action, detail) for action, entry, detail in gc_core.plan(
            tools, keep=1, snapshot="",
        )}
        assert plan["gen-C"] == ("skip", "protected pointer"), plan
        assert plan["gen-B"] == ("skip", "protected pointer"), plan
        assert plan["gen-A"][0] == "reap", plan

    def test_a_generation_held_by_an_open_file_is_kept_then_reaped(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        gen = tools / "gen-A"
        (gen / "Lib").mkdir(parents=True)
        (gen / "nexus-install.json").write_text("{}\n")
        held = gen / "Lib" / "held.pyd"
        held.write_bytes(b"x")
        errors: list[str] = []
        with open(held, "rb"):  # Python opens without FILE_SHARE_DELETE
            lines = gc_core._reap(gen, errors.append)
            assert len(lines) == 1 and lines[0].startswith(f"kept {gen}: in use"), lines
            assert held.exists() and gen.exists()
        lines = gc_core._reap(gen, errors.append)
        assert lines == [f"reaped {gen}"] and not gen.exists()

    def test_gc_never_follows_a_real_junction_pointer_into_its_target(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        target = tmp_path / "precious"
        target.mkdir()
        (target / "data.txt").write_text("data")
        pointer = tools / "gen-rogue"
        gen_core.LinkOps().create(str(target), pointer)
        assert lc.is_link(pointer)
        errors: list[str] = []
        assert gc_core._reap(pointer, errors.append) == []
        assert (target / "data.txt").read_text() == "data"
        assert any("unrecognised generation symlink" in e for e in errors)


def _build_demo_wheel(directory: Path) -> Path:
    """A one-module wheel whose console script prints ``sys.prefix``: a real
    launcher without a network, a build backend or a package index."""
    wheel = directory / "nxdemo-1.0-py3-none-any.whl"
    info = "nxdemo-1.0.dist-info"
    files = {
        "nxdemo.py": "import sys\n\n\ndef main():\n    print(sys.prefix)\n",
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: nxdemo\nVersion: 1.0\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[console_scripts]\nnxdemo = nxdemo:main\n",
    }
    record = "".join(f"{name},,\n" for name in files) + f"{info}/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
        archive.writestr(f"{info}/RECORD", record)
    return wheel


class TestRealLauncherThroughJunction:
    """A launcher copied into ``<gen>\\bin`` and run through ``current`` reports
    the REAL generation as ``sys.prefix``, not the junction: the property the
    whole PATH-entry design depends on. Needs ``uv`` (present on the runner);
    skips with that reason where it is not, and is not in the floor's
    ``--require-passed`` set for that reason."""

    @pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows launcher and junctions")
    def test_sys_prefix_through_current_bin_is_the_real_generation(self, tmp_path: Path) -> None:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not on PATH, so no real venv can be built here")
        gen = tmp_path / "tools" / "gen-A"
        gen.parent.mkdir()
        wheel = _build_demo_wheel(tmp_path)
        subprocess.run([uv, "venv", "--python", sys.executable, str(gen)], check=True, capture_output=True)
        subprocess.run(
            [uv, "pip", "install", "--offline", "--no-deps", "--python", str(gen / "Scripts" / "python.exe"), str(wheel)],
            check=True, capture_output=True,
        )
        assert (gen / "Scripts" / "nxdemo.exe").is_file(), "the install made no launcher"
        gen_core.populate_launchers(gen, dist="nxdemo")
        assert (gen / "bin" / "nxdemo.exe").is_file()
        current = gen.parent / "current"
        gen_core.swap_link(str(gen), current, gen_core.LinkOps())
        done = subprocess.run(
            [str(current / "bin" / "nxdemo.exe")], check=True, capture_output=True, text=True,
        )
        printed = done.stdout.strip()
        assert printed, "the launcher printed nothing"
        assert lc.compare_key(printed) == lc.compare_key(str(gen)), printed
        assert "current" not in Path(printed).parts, "sys.prefix leaked the junction"
