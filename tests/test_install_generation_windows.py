# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows generation installer, ``_install/generation_core.py``
(RDR-224, nexus-f9bgu.47).

Injected-platform tests: ``platform="win32"`` selects the Windows reading, a
fake ``uv`` stands in for the real one, and :class:`SymlinkOps` makes a symlink
where Windows makes a junction. They use ``#!/bin/sh`` interpreter stubs and
symlinks, so they run on POSIX hosts and are NOT part of the Windows rehearsal's
test set. What an injected seam cannot prove (real junctions, a running
launcher, a file held open) is ``tests/test_install_generation_real_windows.py``.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from nexus._install import gc_core
from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc

WIN = "win32"


class SymlinkOps(gen_core.LinkOps):
    """Where Windows makes a junction, a symlink; the logic above it is the same."""

    def create(self, target: str, link: Path) -> None:
        os.symlink(target, link, target_is_directory=True)

    def pid_alive(self, pid: int) -> bool:
        """Litter pids in these tests belong to processes that are gone."""
        return False


class FakeUv:
    """Answers ``uv venv`` / ``uv pip install`` / ``uv tool dir`` the way the real
    one leaves a tree: a pyvenv.cfg, and a Windows ``Scripts`` directory with an
    interpreter stub that declares the console scripts."""

    def __init__(
        self, *, scripts=("nx", "nx-mcp"), home="C:\\py\\cpython-3.12", cfg_version="",
        fail_install: str | None = None, tool_dir: Path | None = None,
        uv_bin: Path | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.scripts = scripts
        self.home = home
        self.cfg_version = cfg_version
        self.fail_install = fail_install
        self.tool_dir = tool_dir
        self.uv_bin = uv_bin

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[:4] == ["uv", "tool", "dir", "--bin"]:
            if self.uv_bin is None:
                return subprocess.CompletedProcess(argv, 1, "", "no uv bin dir in this fake")
            return subprocess.CompletedProcess(argv, 0, f"{self.uv_bin}\n", "")
        if argv[:3] == ["uv", "tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.tool_dir}\n", "")
        if argv[:2] == ["uv", "venv"]:
            gen = Path(argv[-1])
            (gen / "Scripts").mkdir(parents=True, exist_ok=True)
            cfg = f"home = {self.home}\nversion_info = 3.12.8\n"
            if self.cfg_version:
                cfg += f"version = {self.cfg_version}\n"
            if self.home:
                (gen / "pyvenv.cfg").write_text(cfg)
            else:
                (gen / "pyvenv.cfg").write_text("version_info = 3.12.8\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["uv", "pip", "install"]:
            if self.fail_install is not None:
                return subprocess.CompletedProcess(argv, 1, "", self.fail_install)
            python = Path(argv[argv.index("--python") + 1])
            scripts = python.parent
            python.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(self.scripts) + "\n")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            for name in self.scripts:
                (scripts / f"{name}.exe").write_bytes(f"#!{python}\nlauncher:{name}".encode())
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    return root


def _build(tools: Path, uv: FakeUv, **kw) -> Path:
    kw.setdefault("source", "conexus")
    return gen_core.build_generation(
        tools=tools, run=uv, platform=WIN, now=kw.pop("now", lambda: 1_790_000_000.0), **kw,
    )


def _gen(tools: Path, name: str) -> Path:
    gen = tools / name
    (gen / "Scripts").mkdir(parents=True)
    return gen


class TestBuildGeneration:
    def test_builds_a_venv_at_its_final_path_and_writes_the_receipt_last(self, tools: Path) -> None:
        uv = FakeUv()
        gen = _build(tools, uv, version="7.1.0", extras=["local"])
        assert gen.parent == tools and gen.name.startswith("gen-")
        receipt = lc.read_receipt(gen)
        assert receipt.spec == "conexus[local]==7.1.0"
        assert receipt.source_kind == "registry" and receipt.source == "conexus"
        assert receipt.extras == ["local"] and receipt.version == "7.1.0"
        assert receipt.base_interpreter == "C:\\py\\cpython-3.12"
        assert lc.list_generations(tools=tools) == [gen]

    def test_the_commands_name_the_windows_interpreter_and_the_packaged_overrides(self, tools: Path) -> None:
        uv = FakeUv()
        gen = _build(tools, uv, python_version="3.12")
        venv, install = uv.calls
        assert venv == ["uv", "venv", "--allow-existing", "--python", "3.12", str(gen)]
        assert install[:3] == ["uv", "pip", "install"]
        assert install[install.index("--python") + 1] == str(gen / "Scripts" / "python.exe")
        overrides = Path(install[install.index("--overrides") + 1])
        assert overrides.name == "overrides.txt" and overrides.is_file()
        assert install[-1] == "conexus"
        assert "--torch-backend" not in install  # Linux-only

    def test_constraints_are_passed_through(self, tools: Path, tmp_path: Path) -> None:
        pins = tmp_path / "pins.txt"
        pins.write_text("x==1\n")
        uv = FakeUv()
        _build(tools, uv, constraints=str(pins))
        install = uv.calls[1]
        assert install[install.index("--constraints") + 1] == str(pins)

    def test_a_windows_build_fills_a_launcher_dir_with_copies_of_its_own_launchers(self, tools: Path) -> None:
        """<gen>\\bin holds a byte copy of each owned Scripts launcher, so each
        still embeds THIS generation's interpreter path: a process started
        through <tools>\\current\\bin\\nx.exe runs from the real generation."""
        gen = _build(tools, FakeUv(scripts=("nx", "nx-mcp", "mineru")))
        bin_dir = gen_core.launcher_dir(gen)
        assert sorted(p.name for p in bin_dir.iterdir()) == ["mineru.exe", "nx-mcp.exe", "nx.exe"]
        for name in ("nx", "nx-mcp", "mineru"):
            source = lc.venv_script(gen, name, platform=WIN)
            copy = bin_dir / f"{name}.exe"
            assert copy.read_bytes() == source.read_bytes()
            assert f"#!{gen / 'Scripts' / 'python.exe'}".encode() in copy.read_bytes()
        assert gen_core.current_launcher_dir(tools) == tools / "current" / "bin"

    def test_never_shimmed_names_get_no_launcher(self, tools: Path) -> None:
        gen = _build(tools, FakeUv(scripts=("nx", "pip", "uvx")))
        assert [p.name for p in gen_core.launcher_dir(gen).iterdir()] == ["nx.exe"]

    def test_a_dependency_script_that_was_never_built_gets_none(self, tools: Path) -> None:
        gen = _build(tools, FakeUv(scripts=("nx",)))
        assert not (gen_core.launcher_dir(gen) / "mineru.exe").exists()

    def test_a_generation_that_cannot_say_what_it_declares_builds_nothing(self, tools: Path) -> None:
        class Mute(FakeUv):
            def __call__(self, argv, **kw):
                done = super().__call__(argv, **kw)
                if argv[:3] == ["uv", "pip", "install"]:
                    python = Path(argv[argv.index("--python") + 1])
                    python.write_text("#!/bin/sh\nexit 3\n")
                return done

        with pytest.raises(gen_core.GenerationError, match="partial launcher set"):
            _build(tools, Mute())
        assert list(tools.iterdir()) == []

    def test_an_unwritable_root_is_a_generation_error_with_the_path(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = os.mkdir

        def mkdir(path, *a, **kw):
            if Path(path).name.startswith("gen-"):
                raise PermissionError(13, "Access is denied")
            return real(path, *a, **kw)

        monkeypatch.setattr(os, "mkdir", mkdir)
        with pytest.raises(gen_core.GenerationError, match="could not create the generation directory") as raised:
            _build(tools, FakeUv())
        assert str(tools) in str(raised.value)

    def test_the_receipt_write_failing_is_a_generation_error_and_leaves_no_tree(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = os.replace

        def replace(src, dst, *a, **kw):
            if Path(dst).name == "nexus-install.json":
                raise OSError(28, "No space left on device")
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(gen_core.GenerationError, match="failed"):
            _build(tools, FakeUv())
        assert list(tools.iterdir()) == []

    def test_the_building_marker_is_left_in_place(self, tools: Path) -> None:
        gen = _build(tools, FakeUv())
        assert (gen / lc.BUILDING_MARKER_NAME).is_file()

    def test_python_full_comes_from_the_version_line_else_the_requested_version(self, tools: Path) -> None:
        assert lc.read_receipt(_build(tools, FakeUv())).python == "3.12"
        assert lc.read_receipt(_build(tools, FakeUv(cfg_version="3.12.8"), now=lambda: 1_790_000_100.0)).python == "3.12.8"

    def test_two_builds_in_one_second_never_share_a_tree(self, tools: Path) -> None:
        first = _build(tools, FakeUv())
        (first / "fingerprint").write_text("first")
        second = _build(tools, FakeUv())
        third = _build(tools, FakeUv())
        assert len({first, second, third}) == 3
        assert second.name == first.name + "a" and third.name == first.name + "b"
        assert (first / "fingerprint").read_text() == "first"

    def test_a_failed_install_raises_with_uvs_words_and_leaves_no_tree(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="No solution found"):
            _build(tools, FakeUv(fail_install="x No solution found when resolving"))
        assert list(tools.iterdir()) == []

    def test_a_missing_uv_says_so_and_leaves_no_tree(self, tools: Path) -> None:
        def no_uv(argv, **_kw):
            raise FileNotFoundError(2, "not found")

        with pytest.raises(gen_core.GenerationError, match="uv was not found"):
            gen_core.build_generation("conexus", tools=tools, run=no_uv, platform=WIN)
        assert list(tools.iterdir()) == []

    def test_a_venv_with_no_home_line_is_refused_and_removed(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="no 'home ='"):
            _build(tools, FakeUv(home=""))
        assert list(tools.iterdir()) == []

    def test_a_missing_constraints_file_is_refused_before_a_tree_is_claimed(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="constraints"):
            _build(tools, FakeUv(), constraints=str(tools / "nope.txt"))
        assert list(tools.iterdir()) == []

    def test_an_empty_source_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="--source is required"):
            gen_core.build_generation("", tools=tools, run=FakeUv(), platform=WIN)

    def test_a_directory_source_is_made_absolute_and_recorded(
        self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.chdir(checkout)
        gen = _build(tools, FakeUv(), source=".")
        receipt = lc.read_receipt(gen)
        assert receipt.source_kind == "directory"
        assert receipt.source == str(checkout.resolve()) and receipt.spec == str(checkout.resolve())

    def test_a_backslash_source_is_a_directory_not_a_registry_name(self, tools: Path) -> None:
        """Without the nt-only source_kind rule ``C:\\src\\nexus`` is a registry
        spec and uv is asked to resolve it as a package name."""
        uv = FakeUv()
        with pytest.raises(gen_core.GenerationError, match="directory source does not exist"):
            _build(tools, uv, source="C:\\src\\nexus")
        assert uv.calls == []
        assert list(tools.iterdir()) == []


class TestFlipAndRollback:
    def _flip(self, gen: Path, tools: Path) -> None:
        gen_core.flip_current(gen, tools, ops=SymlinkOps(), platform="linux")

    def test_the_first_flip_points_current_and_records_no_previous(self, tools: Path) -> None:
        gen = _gen(tools, "gen-A")
        self._flip(gen, tools)
        assert os.readlink(tools / "current") == str(gen)
        assert not os.path.lexists(tools / "previous")

    def test_the_second_flip_records_the_outgoing_generation_as_previous(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        assert os.readlink(tools / "current") == str(b)
        assert os.readlink(tools / "previous") == str(a)
        assert sorted(p.name for p in tools.iterdir()) == ["current", "gen-A", "gen-B", "previous"]

    def test_flipping_to_the_generation_already_current_leaves_previous_alone(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        self._flip(b, tools)
        assert os.readlink(tools / "previous") == str(a)

    def test_rollback_is_itself_reversible(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "current") == str(a)
        assert os.readlink(tools / "previous") == str(b)
        gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "current") == str(b)

    def test_rollback_with_nothing_recorded_or_a_reaped_previous_refuses(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="nothing to roll back"):
            gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        shutil.rmtree(a)
        with pytest.raises(gen_core.GenerationError, match="previous generation is gone"):
            gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")

    def test_a_relative_or_missing_target_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="absolute"):
            gen_core.flip_current("gen-A", tools, ops=SymlinkOps())
        with pytest.raises(gen_core.GenerationError, match="not a directory"):
            gen_core.flip_current(tools / "gen-nope", tools, ops=SymlinkOps())

    def test_a_real_directory_named_current_is_never_replaced(self, tools: Path) -> None:
        (tools / "current").mkdir()
        (tools / "current" / "keep").write_text("k")
        with pytest.raises(gen_core.GenerationError, match="not a link"):
            self._flip(_gen(tools, "gen-A"), tools)
        assert (tools / "current" / "keep").read_text() == "k"

    def test_a_failed_second_rename_restores_the_original_pointer(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        real = os.rename

        def rename(src, dst, *args, **kw):
            if Path(src).name.startswith(".current.new."):
                raise PermissionError(5, "denied")
            return real(src, dst, *args, **kw)

        monkeypatch.setattr(os, "rename", rename)
        with pytest.raises(gen_core.GenerationError, match="could not repoint"):
            self._flip(b, tools)
        assert os.readlink(tools / "current") == str(a)
        assert [p.name for p in tools.iterdir() if p.name.startswith(".")] == []

    def test_a_pointer_that_cannot_be_created_is_a_generation_error_and_changes_nothing(
        self, tools: Path,
    ) -> None:
        class Broken(SymlinkOps):
            def create(self, target: str, link: Path) -> None:
                raise OSError(1314, "A required privilege is not held by the client")

        a = _gen(tools, "gen-A")
        self._flip(a, tools)
        with pytest.raises(gen_core.GenerationError, match="could not create a pointer"):
            gen_core.flip_current(_gen(tools, "gen-B"), tools, ops=Broken(), platform="linux")
        assert os.readlink(tools / "current") == str(a)

    def test_litter_from_an_interrupted_swap_is_swept(self, tools: Path) -> None:
        a = _gen(tools, "gen-A")
        self._flip(a, tools)
        os.symlink(str(a), tools / ".current.new.999")
        os.symlink(str(a), tools / ".current.old.999")
        self._flip(a, tools)
        assert [p.name for p in tools.iterdir() if p.name.startswith(".")] == []
        assert os.readlink(tools / "current") == str(a)

    def test_a_crash_between_the_two_renames_is_recovered_by_restoring_the_old_pointer(
        self, tools: Path,
    ) -> None:
        """Crash window: ``current`` was renamed aside and the new one never took
        its place. Deleting the litter (the first version) left no ``current``."""
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        os.symlink(str(a), tools / ".current.old.999")  # what the crashed swap held
        assert not os.path.lexists(tools / "current")
        self._flip(b, tools)
        assert os.readlink(tools / "current") == str(b)
        assert os.readlink(tools / "previous") == str(a), "the restored pointer was the outgoing one"
        assert [p.name for p in tools.iterdir() if p.name.startswith(".")] == []

    def test_a_live_processs_in_flight_litter_is_left_alone(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        os.symlink(str(b), tools / ".current.new.4242")
        os.symlink(str(a), tools / ".current.old.4242")
        monkeypatch.setattr(SymlinkOps, "pid_alive", lambda self, pid: pid == 4242)
        self._flip(a, tools)
        assert os.path.lexists(tools / ".current.new.4242") and os.path.lexists(tools / ".current.old.4242")
        monkeypatch.setattr(SymlinkOps, "pid_alive", lambda self, pid: False)
        self._flip(a, tools)
        assert not os.path.lexists(tools / ".current.new.4242")
        assert not os.path.lexists(tools / ".current.old.4242")

    def test_the_pid_probe_never_signals_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """os.kill(pid, 0) calls TerminateProcess on Windows. The Windows reading
        asks the process table, so on that reading os.kill must not be reached."""
        calls: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(pid))
        winproc = gen_core._sibling("winproc_core")
        monkeypatch.setattr(winproc, "ctypes_win_info_api", lambda: object())
        monkeypatch.setattr(winproc, "process_age_seconds", lambda pid, api: 5.0 if pid == 7 else None)
        ops = gen_core.LinkOps("win32")
        assert ops.pid_alive(7) is True and ops.pid_alive(8) is False
        assert calls == []

    def test_the_pointer_comparison_is_the_windows_key_not_the_string(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flipping to the generation current already names, spelt in another
        case, must not rewrite previous to name that same generation."""
        monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        gen_core.flip_current(a, tools, ops=SymlinkOps(), platform=WIN)
        gen_core.flip_current(b, tools, ops=SymlinkOps(), platform=WIN)
        assert os.readlink(tools / "previous") == str(a)


class TestRegisterLegacy:
    def test_registers_idempotently(self, tools: Path, tmp_path: Path) -> None:
        legacy = tmp_path / "uv" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        pointer = gen_core.register_legacy(legacy, tools, ops=SymlinkOps(), platform="linux")
        assert pointer == tools / "gen-legacy-uv-tool"
        assert os.readlink(pointer) == str(legacy)
        before = os.lstat(pointer).st_ino
        gen_core.register_legacy(legacy, tools, ops=SymlinkOps(), platform="linux")
        assert os.lstat(pointer).st_ino == before

    def test_refreshes_a_pointer_that_names_something_else(self, tools: Path, tmp_path: Path) -> None:
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir()
        new.mkdir()
        gen_core.register_legacy(old, tools, ops=SymlinkOps(), platform="linux")
        gen_core.register_legacy(new, tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(new)
        assert old.is_dir()

    def test_a_relative_path_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="absolute"):
            gen_core.register_legacy("uv/conexus", tools, ops=SymlinkOps())


class TestLegacyExtras:
    def _receipt(self, tmp_path: Path, body: str) -> Path:
        (tmp_path / "uv-receipt.toml").write_text(body)
        return tmp_path

    def test_reads_sorted_extras_and_drops_mineru(self, tmp_path: Path) -> None:
        legacy = self._receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "conexus", extras = [\n  "voyage",\n  "local",\n"mineru" ] }]\n',
        )
        assert gen_core.legacy_extras(legacy) == ["local", "voyage"]

    def test_no_receipt_or_no_extras_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert gen_core.legacy_extras(tmp_path) == []
        assert gen_core.legacy_extras(self._receipt(tmp_path, '[tool]\nrequirements = [{ name = "conexus" }]\n')) == []


class TestEnsureUserPath:
    """The Windows shim step. A file stands in for HKCU\\Environment through the
    store seam; the real registry is never touched."""

    UV = "C:\\Users\\Sam\\.local\\bin"
    ENTRY = "C:\\nx\\tools\\current\\bin"

    def _store(self, tmp_path: Path, value: str) -> gen_core.FileUserPath:
        store = gen_core.FileUserPath(tmp_path / "userpath.txt")
        if value is not None:
            store.write(value, 2)
        return store

    def _ensure(self, store, env=None, **kw):
        return gen_core.ensure_user_path(self.ENTRY, uv_bin=self.UV, store=store, environ=env if env is not None else {}, **kw)

    def test_an_absent_entry_goes_first_and_everything_else_is_untouched(self, tmp_path: Path) -> None:
        original = "C:\\Windows;%USERPROFILE%\\bin;" + self.UV + ";D:\\tools\\"
        store = self._store(tmp_path, original)
        result = self._ensure(store)
        assert result.changed
        assert store.read()[0] == self.ENTRY + ";" + original  # every other entry, byte for byte

    def test_an_entry_already_ahead_of_uv_is_left_alone_and_nothing_is_written(self, tmp_path: Path) -> None:
        value = "C:\\Windows;" + self.ENTRY + ";" + self.UV
        store = self._store(tmp_path, value)
        before = store.path.stat().st_mtime_ns
        result = self._ensure(store)
        assert not result.changed
        assert store.path.stat().st_mtime_ns == before, "an unchanged PATH is not rewritten"

    def test_an_entry_behind_uv_is_moved_to_the_front(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, "C:\\a;" + self.UV + ";C:\\b;" + self.ENTRY + ";C:\\c")
        self._ensure(store)
        assert store.read()[0] == f"{self.ENTRY};C:\\a;{self.UV};C:\\b;C:\\c"

    def test_entries_compare_by_case_expansion_and_trailing_separator(self, tmp_path: Path) -> None:
        env = {"USERPROFILE": "C:\\Users\\Sam"}
        store = self._store(tmp_path, "c:\\NX\\Tools\\Current\\Bin\\;%userprofile%\\.local\\bin")
        result = self._ensure(store, env)
        assert not result.changed

    def test_the_uv_bin_is_matched_through_percent_expansion(self, tmp_path: Path) -> None:
        env = {"USERPROFILE": "C:\\Users\\Sam"}
        store = self._store(tmp_path, "%USERPROFILE%\\.local\\bin;" + self.ENTRY)
        assert self._ensure(store, env).changed  # entry is behind uv's bin once expanded

    def test_an_empty_or_missing_path_gets_just_the_entry(self, tmp_path: Path) -> None:
        store = gen_core.FileUserPath(tmp_path / "absent.txt")
        assert self._ensure(store).changed
        assert store.read()[0] == self.ENTRY

    def test_the_registry_type_is_preserved_on_write(self, tmp_path: Path) -> None:
        class Spy(gen_core.UserPathStore):
            kind = "spy"
            written: tuple | None = None

            def read(self):
                return "C:\\Windows", 1  # REG_SZ

            def write(self, value, reg_type):
                self.written = (value, reg_type)

        spy = Spy()
        self._ensure(spy)
        assert spy.written == (self.ENTRY + ";C:\\Windows", 1)

    def test_it_broadcasts_after_a_change_and_not_otherwise(self, tmp_path: Path) -> None:
        class Spy(gen_core.FileUserPath):
            broadcasts = 0

            def broadcast(self):
                Spy.broadcasts += 1

        store = Spy(tmp_path / "p.txt")
        self._ensure(store)
        assert Spy.broadcasts == 1
        self._ensure(store)
        assert Spy.broadcasts == 1

    def test_a_failing_broadcast_does_not_fail_the_install(self, tmp_path: Path) -> None:
        class Hung(gen_core.FileUserPath):
            def broadcast(self):
                raise OSError("a window is hung")

        assert self._ensure(Hung(tmp_path / "p.txt")).changed

    def test_the_process_path_is_prepended_too(self, tmp_path: Path) -> None:
        env = {"PATH": "C:\\Windows;" + self.UV}
        result = self._ensure(self._store(tmp_path, ""), env)
        assert result.process_changed
        assert env["PATH"] == f"{self.ENTRY};C:\\Windows;{self.UV}"
        again = self._ensure(self._store(tmp_path, ""), env)
        assert not again.process_changed

    def test_an_unreadable_or_unwritable_store_is_a_generation_error(self, tmp_path: Path) -> None:
        class Locked(gen_core.UserPathStore):
            kind = "locked"

            def read(self):
                raise PermissionError(5, "denied")

        with pytest.raises(gen_core.GenerationError, match="could not read the user PATH"):
            self._ensure(Locked())

        class NoWrite(gen_core.FileUserPath):
            def write(self, value, reg_type):
                raise PermissionError(5, "denied")

        with pytest.raises(gen_core.GenerationError, match="could not write the user PATH"):
            self._ensure(NoWrite(tmp_path / "x.txt"))

    def test_the_store_env_override_selects_a_file_not_the_registry(self, tmp_path: Path) -> None:
        target = tmp_path / "sandbox-path.txt"
        store = gen_core.default_user_path_store({gen_core.USER_PATH_STORE_ENV: str(target)})
        assert isinstance(store, gen_core.FileUserPath) and store.path == target
        assert isinstance(gen_core.default_user_path_store({}), gen_core.RegistryUserPath)
        gen_core.ensure_user_path(self.ENTRY, uv_bin=self.UV, environ={gen_core.USER_PATH_STORE_ENV: str(target)})
        assert target.read_text().strip() == self.ENTRY

    def test_a_shadowing_nx_exe_ahead_of_the_entry_moves_the_entry_first(self, tmp_path: Path) -> None:
        shadow = tmp_path / "pipbin"
        shadow.mkdir()
        (shadow / "nx.exe").write_bytes(b"pip's")
        store = self._store(tmp_path, f"{shadow};{self.ENTRY};{self.UV}")
        assert self._ensure(store).changed
        assert store.read()[0].split(";")[0] == self.ENTRY

    def test_the_uv_bin_dir_comes_from_uv_then_the_env_then_the_default(self, tmp_path: Path) -> None:
        assert gen_core.uv_bin_dir(run=FakeUv(uv_bin=tmp_path / "ub"), environ={}) == tmp_path / "ub"
        assert gen_core.uv_bin_dir(run=FakeUv(), environ={"UV_TOOL_BIN_DIR": "D:\\ub"}) == Path("D:\\ub")
        assert gen_core.uv_bin_dir(run=FakeUv(), environ={}) == Path.home() / ".local" / "bin"


class TestInspectUserPath:
    ENTRY_NAME = "current-bin"

    def _entry(self, tmp_path: Path, *, with_nx: bool = True) -> Path:
        entry = tmp_path / self.ENTRY_NAME
        entry.mkdir()
        if with_nx:
            (entry / "nx.exe").write_bytes(b"nx")
        return entry

    def _store(self, tmp_path: Path, value: str) -> gen_core.FileUserPath:
        store = gen_core.FileUserPath(tmp_path / "p.txt")
        store.write(value, 2)
        return store

    def test_healthy(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path)
        store = self._store(tmp_path, f"{entry};C:\\Windows")
        assert gen_core.inspect_user_path(entry, store=store, environ={}) == []

    def test_a_missing_launcher_is_reported(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path, with_nx=False)
        store = self._store(tmp_path, str(entry))
        [problem] = gen_core.inspect_user_path(entry, store=store, environ={})
        assert "nx.exe does not exist" in problem

    def test_an_entry_missing_from_the_user_path_is_reported(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path)
        [problem] = gen_core.inspect_user_path(entry, store=self._store(tmp_path, "C:\\Windows"), environ={})
        assert "is not on the user PATH" in problem

    def test_a_directory_ahead_that_provides_nx_exe_is_reported_by_name(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path)
        other = tmp_path / "uvbin"
        other.mkdir()
        (other / "nx.exe").write_bytes(b"uv's")
        store = self._store(tmp_path, f"{other};{entry}")
        [problem] = gen_core.inspect_user_path(entry, store=store, environ={})
        assert str(other) in problem and "ahead" in problem

    def test_a_directory_ahead_without_nx_exe_is_fine(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        assert gen_core.inspect_user_path(entry, store=self._store(tmp_path, f"{empty};{entry}"), environ={}) == []

    def test_the_persisted_path_is_read_not_the_process_path(self, tmp_path: Path) -> None:
        entry = self._entry(tmp_path)
        store = self._store(tmp_path, "C:\\Windows")
        problems = gen_core.inspect_user_path(entry, store=store, environ={"PATH": str(entry)})
        assert any("not on the user PATH" in p for p in problems)


class TestUvLaunchers:
    def test_names_come_from_the_receipts_entrypoints_and_pass_the_allowlist(self, tmp_path: Path) -> None:
        (tmp_path / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conexus", extras = ["local"] }]\n'
            "entrypoints = [\n"
            '  { name = "nx", install-path = "C:\\\\u\\\\nx.exe", from = "conexus" },\n'
            '  { name = "nx-mcp", install-path = "C:\\\\u\\\\nx-mcp.exe", from = "conexus" },\n'
            '  { name = "..\\\\evil", install-path = "x", from = "conexus" },\n'
            "]\n"
        )
        assert gen_core.legacy_launcher_names(tmp_path) == ["nx", "nx-mcp"]
        assert gen_core.legacy_launcher_names(tmp_path / "absent") == []

    def test_only_named_launchers_with_a_replacement_are_removed(self, tmp_path: Path) -> None:
        uv_bin, current_bin = tmp_path / "uvbin", tmp_path / "current-bin"
        uv_bin.mkdir()
        current_bin.mkdir()
        for name in ("nx", "nx-mcp", "uv", "other-tool"):
            (uv_bin / f"{name}.exe").write_bytes(b"x")
        (current_bin / "nx.exe").write_bytes(b"new")
        lines = gen_core.remove_uv_launchers(uv_bin, ["nx", "nx-mcp"], current_bin, platform=WIN)
        assert f"removed {uv_bin / 'nx.exe'}" in lines
        assert any(line.startswith(f"kept {uv_bin / 'nx-mcp.exe'}: no replacement") for line in lines)
        assert sorted(p.name for p in uv_bin.iterdir()) == ["nx-mcp.exe", "other-tool.exe", "uv.exe"]

    def test_a_locked_launcher_is_kept_in_use_and_dry_run_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        uv_bin, current_bin = tmp_path / "uvbin", tmp_path / "current-bin"
        uv_bin.mkdir()
        current_bin.mkdir()
        (uv_bin / "nx.exe").write_bytes(b"x")
        (current_bin / "nx.exe").write_bytes(b"new")
        assert gen_core.remove_uv_launchers(uv_bin, ["nx"], current_bin, dry_run=True, platform=WIN) == [
            f"would remove {uv_bin / 'nx.exe'}",
        ]
        assert (uv_bin / "nx.exe").exists()
        real = Path.unlink

        def unlink(self, *a, **kw):
            if self.name == "nx.exe" and self.parent == uv_bin:
                raise PermissionError(32, "in use")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert gen_core.remove_uv_launchers(uv_bin, ["nx"], current_bin, platform=WIN) == [
            f"kept {uv_bin / 'nx.exe'}: in use",
        ]
        assert (uv_bin / "nx.exe").exists()

    def test_declared_names_exclude_dependency_scripts(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen-A"
        (gen / "Scripts").mkdir(parents=True)
        python = gen / "Scripts" / "python.exe"
        python.write_text("#!/bin/sh\nprintf '%s\\n' nx nx-mcp pip\n")
        python.chmod(0o755)
        assert gen_core.declared_launcher_names(gen, platform=WIN) == ["nx", "nx-mcp"]
        assert gen_core.declared_launcher_names(tmp_path / "nowhere", platform=WIN) == []


class TestMigrateLegacy:
    def _legacy(self, tmp_path: Path, extras: str = '"local", "mineru"') -> Path:
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        (legacy / "Scripts" / "nx.exe").write_bytes(b"uv's launcher")
        (legacy / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "conexus", extras = [{extras}] }}]\n'
        )
        return legacy

    def _migrate(self, tools: Path, tmp_path: Path, legacy: Path, **kw):
        store = gen_core.FileUserPath(tmp_path / "userpath.txt")
        store.write("C:\\Windows;C:\\uvbin", 2)
        paths: list = []
        gen = gen_core.migrate_legacy(
            "conexus", legacy_venv=legacy, tools=tools, run=kw.pop("run", FakeUv()), ops=SymlinkOps(),
            platform=WIN, store=store, uv_bin="C:\\uvbin", environ={}, on_path=paths.append, **kw,
        )
        return gen, store, paths

    def test_builds_flips_ensures_the_path_and_registers_without_touching_the_legacy_tree(
        self, tools: Path, tmp_path: Path,
    ) -> None:
        legacy = self._legacy(tmp_path)
        gen, store, paths = self._migrate(tools, tmp_path, legacy)
        assert gen is not None and lc.read_receipt(gen).extras == ["local"]
        assert os.readlink(tools / "current") == str(gen)
        assert sorted(p.name for p in gen_core.launcher_dir(gen).iterdir()) == ["nx-mcp.exe", "nx.exe"]
        entry = str(tools / "current" / "bin")
        assert store.read()[0] == f"{entry};C:\\Windows;C:\\uvbin"
        assert len(paths) == 1 and paths[0].changed
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        # Never uninstalled, never reaped, and no file written into uv's bin dir.
        assert (legacy / "Scripts" / "nx.exe").read_bytes() == b"uv's launcher"

    def test_no_legacy_tree_is_a_clean_no_op(self, tools: Path, tmp_path: Path) -> None:
        uv = FakeUv()
        got = gen_core.migrate_legacy(
            "conexus", legacy_venv=tmp_path / "absent", tools=tools, run=uv, platform=WIN,
        )
        assert got is None and uv.calls == [] and list(tools.iterdir()) == []

    def test_the_legacy_dir_is_asked_of_uv_when_not_given(self, tools: Path, tmp_path: Path) -> None:
        self._legacy(tmp_path)
        uv = FakeUv(tool_dir=tmp_path / "uvtools", uv_bin=tmp_path / "uvbin")
        store = gen_core.FileUserPath(tmp_path / "userpath.txt")
        gen = gen_core.migrate_legacy(
            "conexus", tools=tools, run=uv, ops=SymlinkOps(), platform=WIN,
            store=store, environ={},
        )
        assert gen is not None
        assert uv.calls[0] == ["uv", "tool", "dir"]
        assert ["uv", "tool", "dir", "--bin"] in uv.calls

    def test_an_unresolvable_uv_is_an_error_not_a_no_op(self, tools: Path) -> None:
        def broken(argv, **_kw):
            return subprocess.CompletedProcess(argv, 1, "", "uv exploded")

        with pytest.raises(gen_core.GenerationError, match="uv exploded"):
            gen_core.migrate_legacy("conexus", tools=tools, run=broken, platform=WIN)

    def test_a_missing_source_is_refused(self, tools: Path, tmp_path: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="--source is required"):
            gen_core.migrate_legacy("", legacy_venv=self._legacy(tmp_path), tools=tools, platform=WIN)


class TestRepairLayout:
    """The state the first implementation left on a real box: a generation built
    and ``current`` flipped, then a failure before the PATH and the ledger."""

    def _half_migrated(self, tools: Path, tmp_path: Path):
        uv = FakeUv()
        gen = _build(tools, uv)
        gen_core.flip_current(gen, tools, ops=SymlinkOps(), platform=WIN)
        shutil.rmtree(gen_core.launcher_dir(gen))  # built before launcher directories existed
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        store = gen_core.FileUserPath(tmp_path / "userpath.txt")
        store.write("C:\\Windows;C:\\uvbin", 2)
        return gen, legacy, store

    def _repair(self, tools: Path, store, **kw):
        return gen_core.repair_layout(
            tools, ops=SymlinkOps(), platform=WIN, store=store, uv_bin="C:\\uvbin", environ={}, **kw,
        )

    def test_finishes_the_job_instead_of_refusing_or_building_another_layout(
        self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gen, legacy, store = self._half_migrated(tools, tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(legacy.parent))
        before = sorted(p.name for p in tools.iterdir())
        lines = self._repair(tools, store)
        assert any("building the launcher directory" in line for line in lines), lines
        assert (gen_core.launcher_dir(gen) / "nx.exe").is_file()
        assert store.read()[0].split(";")[0] == str(tools / "current" / "bin")
        assert any("restart" in line.lower() for line in lines)
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        after = sorted(p.name for p in tools.iterdir())
        assert set(after) - set(before) == {"gen-legacy-uv-tool"}, "no other generation was built"
        assert os.readlink(tools / "current") == str(gen)

    def test_it_is_idempotent(self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gen_, legacy, store = self._half_migrated(tools, tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(legacy.parent))
        self._repair(tools, store)
        assert self._repair(tools, store) == []

    def test_dry_run_changes_nothing(self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, legacy, store = self._half_migrated(tools, tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(legacy.parent))
        lines = self._repair(tools, store, dry_run=True)
        assert lines
        assert not gen_core.launcher_dir(gen).exists()
        assert store.read()[0] == "C:\\Windows;C:\\uvbin"
        assert not os.path.lexists(tools / "gen-legacy-uv-tool")

    def test_no_layout_is_nothing_to_repair(self, tools: Path, tmp_path: Path) -> None:
        store = gen_core.FileUserPath(tmp_path / "p.txt")
        assert self._repair(tools, store) == []
        assert not store.path.exists()


class TestReap:
    def test_reaps_through_gc_core_with_rule_d(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)
        gens = [_gen(tools, f"gen-{n}") for n in "ABCD"]
        for gen in gens:
            (gen / "nexus-install.json").write_text("{}\n")
            os.utime(gen, (1_000_000_000, 1_000_000_000))
            for sub in gen.rglob("*"):
                os.utime(sub, (1_000_000_000, 1_000_000_000))
        monkeypatch.setattr(gc_core._census(), "ps_snapshot", lambda **_k: "")
        lines = gen_core.reap(tools, keep=1, self_generation=gens[0], dry_run=True, platform=WIN)
        would = {Path(line.split(" ", 2)[2]).name for line in lines if line.startswith("would reap")}
        assert would == {"gen-B", "gen-C"}, lines

    def test_keep_zero_is_refused_on_stderr_and_deletes_nothing(
        self, tools: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        gen = _gen(tools, "gen-A")
        assert gen_core.reap(tools, keep=0, platform=WIN) == []
        assert "would retain no generations" in capsys.readouterr().err
        assert gen.exists()
