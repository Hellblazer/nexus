# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows dispatchers on the user PATH (nexus-f9bgu.47, RedirectionGuard).

``<tools>\\bin`` holds one fixed ``<name>.exe`` per launcher; each reads the
pointer file ``<tools>\\current`` and runs ``<generation>\\Scripts\\<name>.exe``
(``src/nexus/_install/_nx_dispatch.py``), so a flip replaces no launcher: a
running generation launcher cannot be renamed or replaced (measured), and a path
through a junction is refused by RedirectionGuard (measured).

Runs on any host. ``uv`` is faked (:class:`FakeShimUv` writes the launcher a real
``uv pip install`` of the dispatcher wheel would); the real build, and the real
dispatcher run through a real launcher, are in
``tests/test_install_generation_real_windows.py``.
"""
from __future__ import annotations

import ast
import hashlib
import base64
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from nexus._install import _nx_dispatch as dispatch
from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc
from tests.test_install_generation_windows import FakeUv

WIN = "win32"


FakeShimUv = FakeUv  # the fake grew the dispatcher venv and its wheels (tests/test_install_generation_windows.py)


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    return root


@pytest.fixture
def generation(tools: Path) -> Path:
    return gen_core.build_generation(
        "conexus", tools=tools, run=FakeUv(), platform=WIN, now=lambda: 1_790_000_000.0, version="7.1.0",
    )


def _bin(tools: Path) -> Path:
    return gen_core.current_launcher_dir(tools)


class TestLauncherDirectory:
    def test_the_path_entry_is_a_real_directory_beside_the_pointers_not_through_one(self, tools: Path) -> None:
        assert gen_core.current_launcher_dir(tools) == tools / "bin"
        assert gen_core.retired_launcher_dir(tools) == tools / "current" / "bin"


class TestDispatchWheel:
    def test_it_installs_one_console_script_that_runs_the_dispatcher_module(self, tmp_path: Path) -> None:
        wheel = gen_core.dispatch_wheel(tmp_path, "nx-mcp")
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            entry = z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
            module_name = next(n for n in names if n.endswith(".py"))
            assert z.read(module_name).decode() == gen_core.dispatch_source()
        assert entry == "[console_scripts]\nnx-mcp = nxdispatch_nx_mcp:console_main\n"
        assert module_name == "nxdispatch_nx_mcp.py"

    def test_the_record_hashes_match_the_content(self, tmp_path: Path) -> None:
        wheel = gen_core.dispatch_wheel(tmp_path, "nx")
        with zipfile.ZipFile(wheel) as z:
            record = z.read(next(n for n in z.namelist() if n.endswith("RECORD"))).decode().splitlines()
            for line in record:
                path, digest, size = line.split(",")
                if not digest:
                    continue
                data = z.read(path)
                want = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                assert digest == f"sha256={want}" and int(size) == len(data), path

    def test_the_same_inputs_make_the_same_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        one = gen_core.dispatch_wheel(tmp_path / "a", "nx")
        two = gen_core.dispatch_wheel(tmp_path / "b", "nx")
        assert one.read_bytes() == two.read_bytes()


class TestEnsureDispatchers:
    def test_it_builds_the_venv_once_and_puts_a_dispatcher_in_the_path_directory_per_name(
        self, tools: Path, generation: Path,
    ) -> None:
        uv = FakeShimUv()
        lines = gen_core.ensure_dispatchers(tools, generation, run=uv, platform=WIN)
        assert sorted(p.name for p in _bin(tools).iterdir()) == ["nx-mcp.exe", "nx.exe"]
        assert (_bin(tools) / "nx.exe").read_bytes().startswith(b"dispatcher:nx:")
        venvs = [c for c in uv.calls if c[:2] == ["uv", "venv"]]
        assert len(venvs) == 1 and venvs[0][-1] == str(tools / "shim")
        assert any("dispatchers for nx, nx-mcp" in line for line in lines)
        assert (tools / "shim" / ".nx-dispatch-version").read_text().strip() == gen_core.DISPATCH_VERSION

    def test_the_dispatcher_venv_is_made_from_the_generations_base_interpreter(
        self, tools: Path, generation: Path, tmp_path: Path,
    ) -> None:
        home = tmp_path / "py313"
        home.mkdir()
        (home / "python.exe").write_bytes(b"x")
        cfg = generation / "pyvenv.cfg"
        cfg.write_text(f"home = {home}\nversion = 3.13.1\n", encoding="utf-8")
        uv = FakeShimUv()
        gen_core.ensure_dispatchers(tools, generation, run=uv, platform=WIN)
        venv = next(c for c in uv.calls if c[:2] == ["uv", "venv"])
        assert venv[venv.index("--python") + 1] == str(home / "python.exe")

    def test_a_second_call_runs_no_command_and_writes_nothing(self, tools: Path, generation: Path) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        before = {p.name: p.stat().st_mtime_ns for p in _bin(tools).iterdir()}
        uv = FakeShimUv()
        assert gen_core.ensure_dispatchers(tools, generation, run=uv, platform=WIN) == []
        assert uv.calls == []
        assert {p.name: p.stat().st_mtime_ns for p in _bin(tools).iterdir()} == before

    def test_a_flip_to_a_generation_with_the_same_names_changes_no_dispatcher(self, tools: Path, generation: Path) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        before = {p.name: p.read_bytes() for p in _bin(tools).iterdir()}
        newer = gen_core.build_generation(
            "conexus", tools=tools, run=FakeUv(), platform=WIN, now=lambda: 1_790_000_100.0, version="7.2.0",
        )
        gen_core.ensure_dispatchers(tools, newer, run=FakeShimUv(), platform=WIN)
        assert {p.name: p.read_bytes() for p in _bin(tools).iterdir()} == before

    def test_a_release_that_adds_a_launcher_gets_only_that_one_file(self, tools: Path, generation: Path) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        before = {p.name: p.read_bytes() for p in _bin(tools).iterdir()}
        newer = gen_core.build_generation(
            "conexus", tools=tools, run=FakeUv(scripts=("nx", "nx-mcp", "nx-extra")), platform=WIN,
            now=lambda: 1_790_000_100.0, version="7.2.0",
        )
        uv = FakeShimUv()
        gen_core.ensure_dispatchers(tools, newer, run=uv, platform=WIN)
        after = {p.name: p.read_bytes() for p in _bin(tools).iterdir()}
        assert sorted(after) == ["nx-extra.exe", "nx-mcp.exe", "nx.exe"]
        assert {k: v for k, v in after.items() if k != "nx-extra.exe"} == before
        installs = [a for c in uv.shim_installs for a in c if a.endswith(".whl")]
        assert len(installs) == 1 and "nx_extra" in installs[0]
        assert not any(c[:2] == ["uv", "venv"] for c in uv.calls), "the venv already exists"

    def test_a_new_dispatcher_version_refreshes_every_file_and_reinstalls(
        self, tools: Path, generation: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        monkeypatch.setattr(gen_core, "DISPATCH_VERSION", "2")
        uv = FakeShimUv()
        gen_core.ensure_dispatchers(tools, generation, run=uv, platform=WIN)
        assert "--reinstall" in uv.shim_installs[0]
        assert (tools / "shim" / ".nx-dispatch-version").read_text().strip() == "2"

    def test_a_running_dispatcher_is_renamed_aside_not_replaced_in_place(
        self, tools: Path, generation: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        monkeypatch.setattr(gen_core, "DISPATCH_VERSION", "2")
        real_replace = os.replace

        def refuse_running(src, dst, *a, **k):
            if str(dst).endswith("nx.exe"):
                raise PermissionError(5, "Access is denied")  # the image is running
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", refuse_running)
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        monkeypatch.undo()
        assert (_bin(tools) / "nx.exe").read_bytes().endswith(b"nxdispatch_nx-2-py3-none-any.whl")
        litter = [p.name for p in _bin(tools).iterdir() if p.name.startswith(".")]
        assert len(litter) == 1 and ".old." in litter[0], litter

    def test_litter_of_an_earlier_refresh_is_swept(self, tools: Path, generation: Path) -> None:
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        (_bin(tools) / ".nx.exe.old.4242").write_bytes(b"old")
        gen_core.ensure_dispatchers(tools, generation, run=FakeShimUv(), platform=WIN)
        assert not [p for p in _bin(tools).iterdir() if p.name.startswith(".")]

    def test_a_failed_install_is_a_generation_error_and_leaves_no_launcher_and_no_staging(
        self, tools: Path, generation: Path,
    ) -> None:
        uv = FakeShimUv()
        uv.fail_shim = "index unreachable"
        with pytest.raises(gen_core.GenerationError, match="index unreachable"):
            gen_core.ensure_dispatchers(tools, generation, run=uv, platform=WIN)
        assert [p.name for p in _bin(tools).iterdir()] == []
        assert not [p for p in tools.iterdir() if p.name.startswith(".dispatch-wheels")]

    def test_a_generation_that_cannot_say_what_it_declares_gets_no_dispatchers(self, tools: Path) -> None:
        bare = tools / "gen-bare"
        (bare / "Scripts").mkdir(parents=True)
        uv = FakeShimUv()
        with pytest.raises(gen_core.GenerationError, match="partial launcher set"):
            gen_core.ensure_dispatchers(tools, bare, run=uv, platform=WIN)
        assert uv.calls == []


class TestUserPathMigration:
    def _store(self, tmp_path: Path, value: str) -> gen_core.FileUserPath:
        path = tmp_path / "userpath.txt"
        path.write_text(value + "\n", encoding="utf-8")
        return gen_core.FileUserPath(path)

    def test_the_retired_entry_comes_off_and_the_new_one_goes_first(self, tools: Path, tmp_path: Path) -> None:
        store = self._store(tmp_path, f"{tools / 'current' / 'bin'};C:\\Windows;C:\\uv")
        env = {"PATH": f"{tools / 'current' / 'bin'};C:\\Windows"}
        result = gen_core.ensure_launcher_path(tools, uv_bin="C:\\uv", store=store, environ=env)
        value, _ = store.read()
        assert value == f"{tools / 'bin'};C:\\Windows;C:\\uv"
        assert result.changed

    def test_an_already_migrated_path_is_left_alone(self, tools: Path, tmp_path: Path) -> None:
        store = self._store(tmp_path, f"{tools / 'bin'};C:\\Windows")
        result = gen_core.ensure_launcher_path(tools, uv_bin="C:\\uv", store=store, environ={"PATH": ""})
        assert not result.changed
        assert store.read()[0] == f"{tools / 'bin'};C:\\Windows"

    def test_the_doctor_reads_the_retired_entry_as_a_problem(self, tools: Path, tmp_path: Path) -> None:
        _bin(tools).mkdir()
        (_bin(tools) / "nx.exe").write_bytes(b"x")
        store = self._store(tmp_path, f"{tools / 'bin'};{tools / 'current' / 'bin'}")
        problems = gen_core.inspect_user_path(_bin(tools), store=store, environ={})
        assert len(problems) == 1 and "still on the user PATH" in problems[0]

    def test_a_path_entry_that_raises_448_on_probe_is_not_an_exception(
        self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RedirectionGuard: ``Path.is_file`` on a path through an untrusted junction raises
        OSError 448 where the probe is meant to answer no."""
        _bin(tools).mkdir()
        (_bin(tools) / "nx.exe").write_bytes(b"x")
        dead = tmp_path / "dead"
        store = self._store(tmp_path, f"{dead};{tools / 'bin'}")
        real_is_file = Path.is_file

        def raising(self: Path, *a, **k):
            if str(self).startswith(str(dead)):
                raise OSError(448, "untrusted mount point")
            return real_is_file(self, *a, **k)

        monkeypatch.setattr(Path, "is_file", raising)
        assert gen_core.inspect_user_path(_bin(tools), store=store, environ={}) == []


class TestRepairFromAJunctionEraBox:
    def test_it_builds_the_dispatchers_converts_the_pointers_and_fixes_the_path(
        self, tools: Path, generation: Path, tmp_path: Path,
    ) -> None:
        older = gen_core.build_generation(
            "conexus", tools=tools, run=FakeUv(), platform=WIN, now=lambda: 1_789_000_000.0, version="7.0.0",
        )
        # The state an install of the era before this change is in: both pointers are directory links.
        os.symlink(generation, tools / "current", target_is_directory=True)
        os.symlink(older, tools / "previous", target_is_directory=True)
        store = gen_core.FileUserPath(tmp_path / "p.txt")
        store.write(f"{tools / 'current' / 'bin'};C:\\Windows", 2)

        class Ops(gen_core.LinkOps):
            def pid_alive(self, pid: int) -> bool:
                return False

        lines = gen_core.repair_layout(
            tools, ops=Ops(WIN), platform=WIN, store=store, uv_bin="C:\\uv", run=FakeShimUv(), environ={"PATH": ""},
        )
        assert (tools / "current").is_file() and (tools / "previous").is_file()
        assert lc.read_link(tools / "current", platform=WIN) == str(generation)
        assert lc.read_link(tools / "previous", platform=WIN) == str(older)
        assert (_bin(tools) / "nx.exe").is_file()
        assert store.read()[0] == f"{tools / 'bin'};C:\\Windows"
        assert any("dispatchers" in line for line in lines)
        assert gen_core.repair_layout(
            tools, ops=Ops(WIN), platform=WIN, store=store, uv_bin="C:\\uv", run=FakeShimUv(), environ={"PATH": ""},
        ) == [], "a second repair finds nothing to do"


def _stage(tools: Path, name: str, body: str) -> Path:
    gen = tools / "gen-X"
    scripts = gen / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    exe = scripts / f"{name}.exe"
    exe.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return gen


def _run_main(tools: Path, argv: list[str]) -> int:
    """The dispatcher as the dispatcher venv's launcher runs it: the interpreter at <tools>/shim/Scripts."""
    return dispatch.main(argv, executable=str(tools / "shim" / "Scripts" / "python.exe"))


@pytest.mark.skipif(os.name == "nt", reason="stages #!/bin/sh stand-ins for the generation's launchers")
class TestDispatcherRuns:
    def test_it_runs_the_current_generations_launcher_with_the_arguments_and_returns_its_status(
        self, tools: Path, capfd: pytest.CaptureFixture[str],
    ) -> None:
        gen = _stage(tools, "nx", 'echo "args:$#:$1:$2"; exit 7')
        lc.write_pointer(tools / "current", str(gen))
        assert _run_main(tools, ["/p/bin/nx.exe", "a b", "c"]) == 7
        assert capfd.readouterr().out.strip() == "args:2:a b:c"

    def test_the_launcher_name_is_the_name_of_the_dispatcher_that_ran(self, tools: Path) -> None:
        gen = _stage(tools, "nx-hook", "exit 3")
        lc.write_pointer(tools / "current", str(gen))
        assert _run_main(tools, ["nx-hook.exe"]) == 3

    def test_a_flip_between_two_runs_changes_what_runs(self, tools: Path) -> None:
        one = _stage(tools, "nx", "exit 1")
        lc.write_pointer(tools / "current", str(one))
        assert _run_main(tools, ["nx.exe"]) == 1
        two = tools / "gen-Y"
        (two / "Scripts").mkdir(parents=True)
        (two / "Scripts" / "nx.exe").write_text("#!/bin/sh\nexit 2\n")
        (two / "Scripts" / "nx.exe").chmod(0o755)
        lc.write_pointer(tools / "current", str(two))
        assert _run_main(tools, ["nx.exe"]) == 2

    def test_a_directory_link_pointer_of_the_old_era_is_still_followed(self, tools: Path) -> None:
        gen = _stage(tools, "nx", "exit 9")
        os.symlink(gen, tools / "current", target_is_directory=True)
        assert _run_main(tools, ["nx.exe"]) == 9

    def test_no_pointer_is_said_in_words_and_is_status_127(self, tools: Path, capfd: pytest.CaptureFixture[str]) -> None:
        assert _run_main(tools, ["nx.exe"]) == 127
        assert "no generation is installed" in capfd.readouterr().err

    def test_a_name_the_generation_does_not_have_is_said_in_words(self, tools: Path, capfd: pytest.CaptureFixture[str]) -> None:
        gen = _stage(tools, "nx", "exit 0")
        lc.write_pointer(tools / "current", str(gen))
        assert _run_main(tools, ["gone.exe"]) == 127
        assert "gone is not in the current generation" in capfd.readouterr().err

    def test_a_garbled_pointer_is_not_followed(self, tools: Path) -> None:
        (tools / "current").write_bytes(b"\xff\xfe junk")
        assert _run_main(tools, ["nx.exe"]) == 127


class TestDispatcherModule:
    def test_the_pointer_format_agrees_with_layout_core(self) -> None:
        assert dispatch.POINTER_MAGIC == lc.POINTER_MAGIC

    def test_the_two_readers_agree_on_what_a_pointer_names(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        lc.write_pointer(link, str(tmp_path))
        assert dispatch.read_pointer(str(link)) == lc.read_pointer(link) == str(tmp_path)

    def test_it_imports_only_the_standard_library_it_needs(self) -> None:
        """Every import is paid on every ``nx`` call; ``subprocess`` only when something runs."""
        tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
        top = {n.names[0].name for n in tree.body if isinstance(n, ast.Import)}
        nested = {
            n.names[0].name
            for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
            for n in ast.walk(fn) if isinstance(n, ast.Import)
        }
        assert top == {"os", "sys"}
        assert nested == {"subprocess"}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
