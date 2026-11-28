# SPDX-License-Identifier: AGPL-3.0-or-later
"""What no injected seam can prove about the Windows generation installer
(RDR-224, nexus-f9bgu.47): the real pointer FILES the installer flips, a flip while
a process runs, the dispatchers on the user PATH run through a real launcher, an
install from before RedirectionGuard (real directory junctions) migrated and rolled
back, a generation held by an open file kept by GC.

RedirectionGuard (nexus-f9bgu.47). Windows 11 can refuse to traverse a directory
junction a non-elevated process made (WinError 448), and a user without Developer
Mode cannot make a symlink (WinError 1314), so the installer's pointers are plain
files. These tests hold on a host that enforces the mitigation and on one that does
not: nothing here traverses a pointer, and the junction-era tests read one only with
``readlink``, which does not traverse.

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
from tests._settle import settle

ON_WINDOWS = os.name == "nt"


class JunctionOps(gen_core.LinkOps):
    """The pointer of the era before nexus-f9bgu.47: a directory junction, which is what an
    install made before it has on disk. Used only to stand such an install up."""

    def create(self, target: str, link: Path) -> None:
        import _winapi  # noqa: PLC0415 -- exists only on Windows

        _winapi.CreateJunction(target, str(link))


def _copy_of_cmd(destination: Path) -> Path:
    """A self-contained, runnable exe to stand in for a running launcher."""
    shutil.copyfile(Path(os.environ["COMSPEC"]), destination)
    return destination


def _sleeper(exe: Path) -> subprocess.Popen:
    """Run *exe* (a copy of cmd.exe) for about half a minute."""
    return subprocess.Popen([str(exe), "/c", "ping", "-n", "30", "127.0.0.1", ">nul"])


@pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows kernel's junctions and file locking")
class TestRealWindows:
    def test_a_real_pointer_file_is_created_read_and_swapped_in_one_step(self, tmp_path: Path) -> None:
        one, two = tmp_path / "gen-1", tmp_path / "gen-2"
        one.mkdir()
        two.mkdir()
        link = tmp_path / "current"
        ops = gen_core.LinkOps()

        gen_core.swap_link(str(one), link, ops)
        assert lc.is_link(link)
        assert link.is_file() and not link.is_symlink() and not os.path.isjunction(link)
        assert lc.compare_key(lc.read_link(link)) == lc.compare_key(str(one))

        gen_core.swap_link(str(two), link, ops)
        assert lc.compare_key(lc.read_link(link)) == lc.compare_key(str(two))
        assert one.is_dir() and two.is_dir(), "a pointer swap touches no generation"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["current", "gen-1", "gen-2"]

    def test_a_swap_works_while_a_process_runs_from_the_old_generation(self, tmp_path: Path) -> None:
        one, two = tmp_path / "gen-1", tmp_path / "gen-2"
        one.mkdir()
        two.mkdir()
        _copy_of_cmd(one / "nx.exe")
        link = tmp_path / "current"
        gen_core.swap_link(str(one), link, gen_core.LinkOps())
        child = _sleeper(one / "nx.exe")  # a launcher of the generation current names
        try:
            time.sleep(0.5)
            assert child.poll() is None
            gen_core.swap_link(str(two), link, gen_core.LinkOps())
            assert lc.compare_key(lc.read_link(link)) == lc.compare_key(str(two))
            assert child.poll() is None, "the swap must not disturb a running process"
        finally:
            child.kill()
            child.wait()

    def test_a_pointer_is_replaced_while_another_process_has_it_open(self, tmp_path: Path) -> None:
        """A reader holds the pointer open for a moment (a dispatcher, a hook). The replace
        waits it out rather than failing the flip."""
        one, two = tmp_path / "gen-1", tmp_path / "gen-2"
        one.mkdir()
        two.mkdir()
        link = tmp_path / "current"
        gen_core.swap_link(str(one), link, gen_core.LinkOps())
        with open(link, "rb") as reader:  # Python opens without FILE_SHARE_DELETE
            import threading  # noqa: PLC0415

            threading.Timer(0.3, reader.close).start()
            gen_core.swap_link(str(two), link, gen_core.LinkOps())
        assert lc.compare_key(lc.read_link(link)) == lc.compare_key(str(two))

    def test_flip_and_rollback_on_real_pointer_files(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        a, b = tools / "gen-A", tools / "gen-B"
        a.mkdir()
        b.mkdir()
        gen_core.flip_current(a, tools)
        gen_core.flip_current(b, tools)
        assert (tools / "current").is_file() and (tools / "previous").is_file()
        assert lc.current_generation(tools=tools).name == "gen-B"
        assert lc.compare_key(lc.read_link(tools / "previous")) == lc.compare_key(str(a))
        gen_core.rollback_current(tools)
        assert lc.current_generation(tools=tools).name == "gen-A"

    def test_an_install_from_before_redirectionguard_migrates_and_rolls_back(self, tmp_path: Path) -> None:
        """Real junction pointers, as every box that installed before nexus-f9bgu.47 has. On a
        host that enforces RedirectionGuard each is refused on traversal (WinError 448) and
        the installer must never traverse one: ``readlink`` does not, ``rmdir`` does not."""
        tools = tmp_path / "tools"
        tools.mkdir()
        a, b, c = (tools / f"gen-{n}" for n in "ABC")
        for gen in (a, b, c):
            gen.mkdir()
        old = JunctionOps()
        gen_core.flip_current(a, tools, ops=old)
        gen_core.flip_current(b, tools, ops=old)
        assert os.path.isjunction(tools / "current") and os.path.isjunction(tools / "previous")
        assert lc.current_generation(tools=tools).name == "gen-B", "a junction is read without traversing it"
        assert lc.compare_key(lc.read_link(tools / "previous")) == lc.compare_key(str(a))

        # Rollback to a junction-era previous, then the next flip: both end as files.
        gen_core.rollback_current(tools)
        assert lc.current_generation(tools=tools).name == "gen-A"
        gen_core.flip_current(c, tools)
        for name, target in (("current", c), ("previous", a)):
            pointer = tools / name
            assert pointer.is_file() and not os.path.isjunction(pointer), name
            assert lc.compare_key(lc.read_link(pointer)) == lc.compare_key(str(target)), name
        assert a.is_dir() and b.is_dir() and c.is_dir(), "no generation was emptied through a junction"
        assert sorted(p.name for p in tools.iterdir() if p.name.startswith(".")) == [], "no litter"

    def test_repair_turns_the_junctions_of_an_old_install_into_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real junctions (current, previous and the legacy ledger) become pointer files. The dispatcher and
        launcher builds are stubbed (``TestRealDispatchers`` and the generation build tests run them for
        real); everything else, the conversion included, is the product's."""
        tools = tmp_path / "tools"
        tools.mkdir()
        old_gen, gen = tools / "gen-A", tools / "gen-B"
        old_gen.mkdir()
        gen.mkdir()
        (tmp_path / "uv" / "Scripts").mkdir(parents=True)
        legacy = tmp_path / "uv"
        gen_core.flip_current(old_gen, tools, ops=JunctionOps())
        gen_core.flip_current(gen, tools, ops=JunctionOps())
        JunctionOps().create(str(legacy), tools / "gen-legacy-uv-tool")
        for name in ("current", "previous", "gen-legacy-uv-tool"):
            assert os.path.isjunction(tools / name), name
        monkeypatch.setattr(gen_core, "populate_launchers", lambda *a, **k: [])
        monkeypatch.setattr(gen_core, "ensure_dispatchers", lambda *a, **k: [])
        monkeypatch.setattr(lc, "uv_conexus_venv", lambda **k: legacy)
        store = gen_core.FileUserPath(tmp_path / "userpath.txt")
        store.write("C:\\Windows", 2)
        lines = gen_core.repair_layout(tools, store=store, uv_bin="C:\\uvbin", environ={"PATH": ""})
        assert sum("into a pointer file" in line for line in lines) == 3, lines
        for name, target in (("current", gen), ("previous", old_gen), ("gen-legacy-uv-tool", legacy)):
            pointer = tools / name
            assert pointer.is_file() and not os.path.isjunction(pointer), name
            assert lc.compare_key(lc.read_link(pointer)) == lc.compare_key(str(target)), name
        assert gen.is_dir() and old_gen.is_dir() and legacy.is_dir(), "no tree was emptied through a junction"

    def test_the_real_user_path_registry_value_is_readable_and_left_untouched(self) -> None:
        """Read-only: the store seam reads HKCU\\Environment without writing."""
        value, reg_type = gen_core.RegistryUserPath().read()
        assert isinstance(value, str)
        assert reg_type in (1, 2), reg_type  # REG_SZ or REG_EXPAND_SZ

    def test_gc_plan_protects_real_pointer_file_current_and_previous(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        gens = {}
        for name in ("A", "B", "C", "D"):
            gen = tools / f"gen-{name}"
            gen.mkdir()
            (gen / "nexus-install.json").write_text("{}\n", encoding="utf-8")
            gens[name] = gen
        gen_core.flip_current(gens["B"], tools)
        gen_core.flip_current(gens["C"], tools)  # current -> C, previous -> B, both pointer files
        assert lc.is_link(tools / "current") and lc.is_link(tools / "previous")
        plan = {entry.name: (action, detail) for action, entry, detail in gc_core.plan(
            tools, keep=1, snapshot="999 C:/Windows/notepad.exe\n",  # a table that names none of them
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
            lambda generation, *, tools, bootstrap=False, restoring=False: gen_core.flip_current(generation, tools, platform="win32"),
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
        "nxdemo.py": (
            "import sys\nimport time\n\n\ndef main():\n"
            "    if sys.argv[1:2] == ['sleep']:\n        open(sys.argv[2], 'w').close()\n        time.sleep(30)\n        return\n"
            "    if sys.argv[1:2] == ['echo']:\n"
            "        print('args=%r' % (sys.argv[1:],))\n"
            "        print('stdin=' + sys.stdin.readline().strip())\n"
            "        raise SystemExit(3)\n"
            "    print(sys.prefix)\n"
        ),
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


def _need_uv() -> None:
    """``uv`` is what builds the dispatchers, so a run without it proved nothing. On a developer box
    that is a skip; on CI it is a FAILURE (a skip there passes with no evidence, and the workflow's
    ``--require-passed`` list is not ours to edit)."""
    if shutil.which("uv") is not None:
        return
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        pytest.fail("uv is not on PATH on CI: the dispatcher tests would pass having proved nothing")
    pytest.skip("uv is not on PATH, so no real venv can be built here")


class TestRealDispatchers:
    """The dispatchers on the user PATH, built by a real ``uv`` and run through a real launcher:
    ``<tools>\\bin\\nxdemo.exe`` runs the CURRENT generation's ``nxdemo.exe`` and reports that
    generation as ``sys.prefix``; a flip changes what it runs without replacing it, even while
    one is running; a rollback changes it back; a refresh of the dispatcher itself renames a
    RUNNING one aside. Needs ``uv``: skipped without it off CI, a failure on CI (:func:`_need_uv`)."""

    @staticmethod
    def _generation(tmp_path: Path, tools: Path, stamp: str, wheel: Path) -> Path:
        uv = shutil.which("uv")
        gen = tools / f"gen-{stamp}"
        subprocess.run([uv, "venv", "--python", sys.executable, str(gen)], check=True, capture_output=True)
        subprocess.run(
            [uv, "pip", "install", "--offline", "--no-deps", "--python", str(gen / "Scripts" / "python.exe"), str(wheel)],
            check=True, capture_output=True,
        )
        assert (gen / "Scripts" / "nxdemo.exe").is_file(), "the install made no launcher"
        return gen

    @staticmethod
    def _run(exe: Path, *args: str) -> str:
        done = subprocess.run(
            [str(exe), *args], check=True, capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
        )
        return done.stdout.strip()

    @pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows launcher")
    def test_a_dispatcher_runs_the_current_generation_across_a_flip_a_running_process_and_a_rollback(
        self, tmp_path: Path,
    ) -> None:
        _need_uv()
        tools = tmp_path / "tools"
        tools.mkdir()
        wheel = _build_demo_wheel(tmp_path)
        one = self._generation(tmp_path, tools, "A", wheel)
        two = self._generation(tmp_path, tools, "B", wheel)

        for generation in (one,):
            lines = gen_core.ensure_dispatchers(tools, generation, dist="nxdemo")
            assert any("dispatchers for nxdemo" in line for line in lines), lines
        dispatcher = gen_core.current_launcher_dir(tools) / "nxdemo.exe"
        assert dispatcher.is_file()
        before = dispatcher.read_bytes()
        gen_core.flip_current(one, tools)
        assert lc.compare_key(self._run(dispatcher)) == lc.compare_key(str(one)), "the dispatcher ran generation A"

        # A process is running THROUGH the dispatcher while the flip happens.
        ready = tmp_path / "ready"
        child = subprocess.Popen([str(dispatcher), "sleep", str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            settle(lambda: not ready.exists(), deadline=120.0, what="the demo launcher started")
            assert child.poll() is None, "the demo launcher should be sleeping"
            assert gen_core.ensure_dispatchers(tools, two, dist="nxdemo") == [], "nothing new to write"
            gen_core.flip_current(two, tools)
            assert dispatcher.read_bytes() == before, "a flip replaced no launcher"
            assert child.poll() is None
            assert lc.compare_key(self._run(dispatcher)) == lc.compare_key(str(two)), "the next run is generation B"
        finally:
            child.kill()
            child.wait()

        gen_core.rollback_current(tools)
        assert lc.compare_key(self._run(dispatcher)) == lc.compare_key(str(one)), "a rollback runs generation A again"
        assert dispatcher.read_bytes() == before

    @pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows launcher")
    def test_a_refresh_renames_a_running_real_dispatcher_aside_and_the_next_run_is_the_new_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The one place a dispatcher IS replaced (a ``DISPATCH_VERSION`` bump, a rebuilt environment).
        A real generation launcher cannot be renamed while it runs (WinError 32); this proves the
        dispatcher can, with a real uv-built one that is executing, so the first bump does not fail
        an upgrade made while ``nx-mcp`` runs."""
        _need_uv()
        tools = tmp_path / "tools"
        tools.mkdir()
        gen = self._generation(tmp_path, tools, "A", _build_demo_wheel(tmp_path))
        gen_core.ensure_dispatchers(tools, gen, dist="nxdemo")
        gen_core.flip_current(gen, tools)
        dispatcher = gen_core.current_launcher_dir(tools) / "nxdemo.exe"
        before = os.stat(dispatcher).st_ino  # the file's identity: the same bytes come out of a rebuild
        ready = tmp_path / "ready"
        child = subprocess.Popen([str(dispatcher), "sleep", str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            settle(lambda: not ready.exists(), deadline=120.0, what="the demo launcher started")
            monkeypatch.setattr(gen_core, "DISPATCH_VERSION", "2")  # a new version: every dispatcher is refreshed
            lines = gen_core.ensure_dispatchers(tools, gen, dist="nxdemo")
            assert any("dispatchers for nxdemo" in line for line in lines), lines
            assert child.poll() is None, "the running dispatcher kept running"
            asides = [p.name for p in dispatcher.parent.iterdir() if ".old." in p.name]
            assert len(asides) == 1, f"the running file is renamed aside, not replaced: {asides}"
            assert os.stat(dispatcher).st_ino != before, "a new file is in place under the old name"
            assert lc.compare_key(self._run(dispatcher)) == lc.compare_key(str(gen)), "and it works"
        finally:
            child.kill()
            child.wait()
        gen_core.ensure_dispatchers(tools, gen, dist="nxdemo")
        assert not [p for p in dispatcher.parent.iterdir() if ".old." in p.name], "the aside is swept once it is not running"

    @pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows launcher")
    def test_the_dispatcher_passes_arguments_stdin_and_the_exit_status(self, tmp_path: Path) -> None:
        _need_uv()
        tools = tmp_path / "tools"
        tools.mkdir()
        gen = self._generation(tmp_path, tools, "A", _build_demo_wheel(tmp_path))
        gen_core.ensure_dispatchers(tools, gen, dist="nxdemo")
        gen_core.flip_current(gen, tools)
        dispatcher = gen_core.current_launcher_dir(tools) / "nxdemo.exe"
        done = subprocess.run(
            [str(dispatcher), "echo", "a b", "c"], capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
            input="from stdin\n",
        )
        assert done.returncode == 3, done
        assert done.stdout.splitlines() == ["args=['echo', 'a b', 'c']", "stdin=from stdin"]

    @pytest.mark.skipif(not ON_WINDOWS, reason="needs the Windows launcher")
    def test_the_dispatcher_without_a_pointer_says_so_and_exits_127(self, tmp_path: Path) -> None:
        _need_uv()
        tools = tmp_path / "tools"
        tools.mkdir()
        gen = self._generation(tmp_path, tools, "A", _build_demo_wheel(tmp_path))
        gen_core.ensure_dispatchers(tools, gen, dist="nxdemo")
        done = subprocess.run(
            [str(gen_core.current_launcher_dir(tools) / "nxdemo.exe")], capture_output=True, text=True,
            encoding=PYTHON_CHILD_ENCODING,
        )
        assert done.returncode == 127 and "no generation is installed" in done.stderr, done
