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
from tests._child_encoding import PYTHON_CHILD_ENCODING

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
        (one / "who").write_text("one", encoding="utf-8")
        (two / "who").write_text("two", encoding="utf-8")
        link = tmp_path / "current"
        ops = gen_core.LinkOps()

        gen_core.swap_link(str(one), link, ops)
        assert lc.is_link(link)
        assert not link.is_symlink(), "a junction is not a symlink to pathlib; that is the whole point"
        assert os.path.isjunction(link)
        read = lc.read_link(link)
        assert not read.startswith("\\\\?\\")
        assert lc.compare_key(read) == lc.compare_key(str(one))
        assert (link / "who").read_text(encoding="utf-8") == "one"

        gen_core.swap_link(str(two), link, ops)
        assert (link / "who").read_text(encoding="utf-8") == "two"
        assert one.is_dir() and (one / "who").read_text(encoding="utf-8") == "one", "the old target must survive the swap"
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
            (gen / "nexus-install.json").write_text("{}\n", encoding="utf-8")
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
        (gen / "nexus-install.json").write_text("{}\n", encoding="utf-8")
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
        (target / "data.txt").write_text("data", encoding="utf-8")
        pointer = tools / "gen-rogue"
        gen_core.LinkOps().create(str(target), pointer)
        assert lc.is_link(pointer)
        errors: list[str] = []
        assert gc_core._reap(pointer, errors.append) == []
        assert (target / "data.txt").read_text(encoding="utf-8") == "data"
        assert any("unrecognised generation symlink" in e for e in errors)

    # -- the installer-rebuild rollback on real junctions (nexus-ff366) --------

    @staticmethod
    def _flipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """``current`` on a rebuilt generation and ``previous`` on the one it replaced, both
        real junctions; the user PATH and the logon task are the machine's, so the Windows flip
        step is the flip alone."""
        from nexus.commands import self_cmd  # noqa: PLC0415

        tools = tmp_path / "tools"
        tools.mkdir()
        older, original, rebuilt = (tools / f"gen-2026100{n}T000000Z" for n in (1, 2, 3))
        for generation in (older, original, rebuilt):
            generation.mkdir()
        gen_core.flip_current(older, tools)
        gen_core.flip_current(original, tools)
        gen_core.flip_current(rebuilt, tools)  # current -> rebuilt, previous -> original
        monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
        monkeypatch.setattr(
            self_cmd, "_windows_flip_and_path",
            lambda generation, *, tools, bootstrap=False: gen_core.flip_current(generation, tools, platform="win32"),
        )
        return tools, older, original, rebuilt

    def test_remove_link_drops_a_junction_and_never_what_it_names(self, tmp_path: Path) -> None:
        target = tmp_path / "precious"
        target.mkdir()
        (target / "data.txt").write_text("data", encoding="utf-8")
        link = tmp_path / "previous"
        gen_core.LinkOps().create(str(target), link)
        assert lc.is_link(link)
        lc.remove_link(link)
        assert not os.path.lexists(link), "the junction is gone"
        assert (target / "data.txt").read_text(encoding="utf-8") == "data", "its target is untouched"
        real = tmp_path / "real-dir"
        real.mkdir()
        (real / "keep.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(lc.LayoutError, match="not a link"):
            lc.remove_link(real)
        assert (real / "keep.txt").exists(), "a real directory is refused, not emptied"

    def test_restore_current_generation_drops_previous_when_its_target_is_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``keep_previous=False`` branch: ``layout_core.remove_link`` on the junction the
        flip left as ``previous``, with the generation it names still in place."""
        from nexus.commands import self_cmd  # noqa: PLC0415

        tools, older, original, rebuilt = self._flipped(tmp_path, monkeypatch)
        shutil.rmtree(older)
        self_cmd.restore_current_generation(original, previous=str(older))
        assert lc.compare_key(lc.read_link(tools / "current")) == lc.compare_key(str(original))
        assert not os.path.lexists(tools / "previous"), "previous is dropped, not left on the flip's generation"
        assert rebuilt.is_dir() and original.is_dir(), "no generation was deleted through a junction"

    def test_put_current_back_on_real_junctions_restores_both_pointers_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus import pdf_stack  # noqa: PLC0415

        tools, older, original, rebuilt = self._flipped(tmp_path, monkeypatch)
        assert pdf_stack._put_current_back(original, str(older)) == f"; current is back on {original.name}"
        assert lc.compare_key(lc.read_link(tools / "current")) == lc.compare_key(str(original))
        assert lc.compare_key(lc.read_link(tools / "previous")) == lc.compare_key(str(older))

    def test_put_current_back_reads_one_directory_spelled_in_another_case_as_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``current`` is compared with ``replaced`` by ``Path ==`` and ``previous`` by
        ``compare_key``. Measured on native Windows: a junction written from a target spelled in
        another case reads back in that case, and both comparisons fold it, so the account says
        nothing false about a pointer that did move. Other spellings do not survive a junction
        (an extended-length prefix reads back unstripped, a ``..`` segment is stored raw), so the
        two comparisons cannot disagree on anything a real pointer holds, and no change to
        ``_put_current_back`` was needed."""
        from nexus import pdf_stack  # noqa: PLC0415

        tools, older, original, rebuilt = self._flipped(tmp_path, monkeypatch)
        replaced = Path(str(original).upper())
        assert str(replaced) != str(original), "the fixture must spell the directory differently"
        line = pdf_stack._put_current_back(replaced, str(older).upper())
        assert lc.compare_key(lc.read_link(tools / "current")) == lc.compare_key(str(original))
        assert lc.compare_key(lc.read_link(tools / "previous")) == lc.compare_key(str(older))
        assert line == f"; current is back on {replaced.name}", line


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
            [str(current / "bin" / "nxdemo.exe")], check=True, capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
        )
        printed = done.stdout.strip()
        assert printed, "the launcher printed nothing"
        assert lc.compare_key(printed) == lc.compare_key(str(gen)), printed
        assert "current" not in Path(printed).parts, "sys.prefix leaked the junction"
