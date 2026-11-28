# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pointer file's failure modes and the layout's edges (nexus-f9bgu.47, review round 2).

A pointer is a file, so it can be torn (a crash before the bytes reached the disk, a restored
backup, an antivirus rewrite) in a way a junction, which has no content, could not. Nothing here
needs Windows: ``platform="win32"`` selects the Windows reading and a symlink stands in for the
junction of an install from before the pointer file.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install import dispatch_core
from nexus._install import generation_core as gen_core
from nexus._install import gc_core
from nexus._install import layout_core as lc
from tests._module_seam import setattr_in
from tests.test_install_generation_windows import FakeUv

WIN = "win32"
GEN = "nexus._install.generation_core"


class Ops(gen_core.LinkOps):
    def pid_alive(self, pid: int) -> bool:
        return False


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    return root


def _gen(tools: Path, name: str) -> Path:
    gen = tools / name
    (gen / "Scripts").mkdir(parents=True)
    return gen


def _flip(gen: Path, tools: Path) -> None:
    gen_core.flip_current(gen, tools, ops=Ops(WIN), platform=WIN)


class TestWritePointer:
    def test_it_writes_lf_only_on_every_host_and_syncs_before_the_caller_replaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        synced: list[int] = []
        real_fsync = os.fsync
        setattr_in(monkeypatch, "nexus._install.layout_core", "os.fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
        link = tmp_path / "current"
        lc.write_pointer(link, str(tmp_path))
        assert link.read_bytes() == f"nx-pointer 1\n{tmp_path}\n".encode()
        assert b"\r" not in link.read_bytes()
        assert len(synced) == 1, "the file is on the disk before os.replace can make it the pointer"


class TestATornPointerIsNotADeadEnd:
    """An empty or headerless ``current`` used to make swap_link refuse for ever ("exists and is not a
    link"), and ``nx`` itself could not run to repair it."""

    @pytest.mark.parametrize("body", [b"", b"nx-pointer 1\n", b"garbage with no header\n", b"nx-pointer 2\nC:\\x\n", b"\x00\x00\x00"])
    def test_flip_replaces_a_small_file_that_is_not_a_pointer(self, tools: Path, body: bytes) -> None:
        a = _gen(tools, "gen-A")
        (tools / "current").write_bytes(body)
        _flip(a, tools)
        assert lc.current_generation(tools=tools, platform=WIN) == a
        assert not [p for p in tools.iterdir() if p.name.startswith(".")]

    def test_flip_replaces_a_torn_previous_too(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        (tools / "previous").write_bytes(b"")
        _flip(b, tools)
        assert lc.read_link(tools / "previous", platform=WIN) == str(a)

    def test_a_directory_is_still_refused_and_says_what_to_do(self, tools: Path) -> None:
        (tools / "current").mkdir()
        (tools / "current" / "keep.txt").write_text("x", encoding="utf-8")
        with pytest.raises(gen_core.GenerationError, match="Move it aside and run `nx self install`"):
            _flip(_gen(tools, "gen-A"), tools)
        assert (tools / "current" / "keep.txt").exists()

    def test_a_file_bigger_than_a_pointer_is_still_refused(self, tools: Path) -> None:
        (tools / "current").write_bytes(b"x" * (dispatch_core.TORN_MAX_BYTES + 1))
        with pytest.raises(gen_core.GenerationError, match="bigger than a pointer"):
            _flip(_gen(tools, "gen-A"), tools)
        assert (tools / "current").stat().st_size == dispatch_core.TORN_MAX_BYTES + 1

    def test_the_legacy_ledger_pointer_recovers_the_same_way(self, tools: Path, tmp_path: Path) -> None:
        legacy = tmp_path / "uv" / "conexus"
        legacy.mkdir(parents=True)
        (tools / "gen-legacy-uv-tool").write_bytes(b"")
        pointer = gen_core.register_legacy(legacy, tools, ops=Ops(WIN), platform=WIN)
        assert lc.read_link(pointer, platform=WIN) == str(legacy)

    def test_a_partial_new_pointer_of_a_dead_swap_is_swept(self, tools: Path) -> None:
        a = _gen(tools, "gen-A")
        _flip(a, tools)
        (tools / ".current.new.99999").write_bytes(b"nx-poi")  # a write that stopped part way
        _flip(a, tools)
        assert not [p for p in tools.iterdir() if p.name.startswith(".")]

    def test_the_dispatcher_says_how_to_rebuild_a_damaged_pointer(self, tools: Path) -> None:
        from nexus._install import _nx_dispatch as dispatch

        (tools / "current").write_bytes(b"")
        program, problem = dispatch.resolve("nx", str(tools))
        assert program is None
        assert "uv tool run --from conexus nx self install --bootstrap" in problem and "damaged" in problem


class TestOnlyAConfirmedReadMakesAPointerTorn:
    """A valid pointer that cannot be read for a moment (an antivirus lock) used to look 'not a link',
    small and regular, and was deleted."""

    def test_a_valid_pointer_the_layout_cannot_read_just_now_is_never_deleted(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        before = (tools / "current").read_bytes()
        monkeypatch.setattr(lc, "read_pointer", lambda p: None)  # the layout's read fails this once
        real_open = open

        def locked(path, *args, **kwargs):
            if str(path).endswith("current"):
                raise PermissionError(13, "being used by another process")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(dispatch_core, "open", locked, raising=False)
        with pytest.raises(gen_core.GenerationError):
            _flip(b, tools)
        assert (tools / "current").read_bytes() == before

    def test_a_valid_pointer_is_not_torn_with_either_line_ending(self, tools: Path) -> None:
        for newline in ("\n", "\r\n"):
            pointer = tools / "current"
            pointer.write_bytes(f"nx-pointer 1{newline}C:\\tools\\gen-A{newline}".encode())
            assert not dispatch_core.is_torn_pointer(pointer), repr(newline)

    @pytest.mark.parametrize("body", [b"", b"nx-pointer 1", b"nx-pointer 1\n\n", b"junk\n", b"\xff\xfe\x00\x01"])
    def test_a_confirmed_empty_short_or_headerless_read_is_torn(self, tools: Path, body: bytes) -> None:
        pointer = tools / "current"
        pointer.write_bytes(body)
        assert dispatch_core.is_torn_pointer(pointer)

    def test_the_header_this_module_restates_is_layout_cores(self) -> None:
        assert dispatch_core.POINTER_MAGIC == lc.POINTER_MAGIC

    def test_a_torn_pointer_that_cannot_be_deleted_is_a_generation_error_with_the_way_back(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tools / "current").write_bytes(b"")

        def refused(path, *a, **k):
            raise PermissionError(5, "Access is denied")

        setattr_in(monkeypatch, GEN, "os.unlink", refused)
        with pytest.raises(gen_core.GenerationError, match=r"could not replace the damaged pointer.*nx self install --bootstrap"):
            _flip(_gen(tools, "gen-A"), tools)


class TestRepairHealsATornCurrent:
    def _box(self, tmp_path: Path) -> tuple[Path, Path, FakeUv]:
        tools = tmp_path / "tools"
        tools.mkdir()
        uv = FakeUv()
        gen = gen_core.build_generation("conexus", tools=tools, run=uv, platform=WIN, now=lambda: 1_790_000_000.0, version="7.1.0")
        return tools, gen, uv

    def _repair(self, tools: Path, uv: FakeUv, tmp_path: Path, **kw) -> list[str]:
        store = gen_core.FileUserPath(tmp_path / "p.txt")
        store.write("C:\\Windows", 2)
        return gen_core.repair_layout(
            tools, ops=Ops(WIN), platform=WIN, store=store, uv_bin="C:\\uv", run=uv, environ={"PATH": ""}, **kw,
        )

    def test_it_points_a_damaged_current_at_the_newest_complete_generation_and_finishes_the_repair(
        self, tmp_path: Path,
    ) -> None:
        tools, gen, uv = self._box(tmp_path)
        newer = gen_core.build_generation("conexus", tools=tools, run=uv, platform=WIN, now=lambda: 1_790_000_100.0, version="7.2.0")
        (tools / "current").write_bytes(b"")
        lines = self._repair(tools, uv, tmp_path)
        assert lc.current_generation(tools=tools, platform=WIN) == newer
        assert any("was damaged" in line and newer.name in line for line in lines), lines
        assert (tools / "bin" / "nx.exe").is_file(), "and the rest of the repair ran on it"
        assert gen.is_dir()

    def test_a_dry_run_says_so_and_changes_nothing(self, tmp_path: Path) -> None:
        tools, gen, uv = self._box(tmp_path)
        (tools / "current").write_bytes(b"")
        lines = self._repair(tools, uv, tmp_path, dry_run=True)
        assert any("was damaged" in line for line in lines)
        assert (tools / "current").read_bytes() == b""

    def test_a_current_that_is_a_directory_or_absent_is_not_ours_to_heal(self, tmp_path: Path) -> None:
        tools, gen, uv = self._box(tmp_path)
        assert self._repair(tools, uv, tmp_path) == []
        (tools / "current").mkdir()
        assert self._repair(tools, uv, tmp_path) == []
        assert (tools / "current").is_dir()

    def test_a_damaged_current_with_no_generation_to_name_is_left_alone(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "current").write_bytes(b"")
        assert self._repair(tools, FakeUv(), tmp_path) == []
        assert (tools / "current").read_bytes() == b""


class TestReplaceRetries:
    def test_a_reader_holding_the_pointer_open_is_waited_out(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        real_replace = os.replace
        refusals: list[int] = []

        def flaky(src, dst, *k, **kw):
            if len(refusals) < 3:
                refusals.append(1)
                raise PermissionError(32, "The process cannot access the file because it is being used by another process")
            return real_replace(src, dst, *k, **kw)

        setattr_in(monkeypatch, GEN, "os.replace", flaky)
        setattr_in(monkeypatch, GEN, "time.sleep", lambda _s: None)
        _flip(b, tools)
        assert len(refusals) >= 3 and lc.current_generation(tools=tools, platform=WIN) == b

    def test_a_reader_that_never_lets_go_fails_the_flip_and_leaves_the_old_pointer(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)

        def locked(*_a, **_k):
            raise PermissionError(32, "in use")

        setattr_in(monkeypatch, GEN, "os.replace", locked)
        setattr_in(monkeypatch, GEN, "time.sleep", lambda _s: None)
        with pytest.raises(gen_core.GenerationError):
            _flip(b, tools)
        assert lc.current_generation(tools=tools, platform=WIN) == a

    def test_the_replace_does_not_fall_back_to_two_renames_when_the_new_file_is_briefly_unreadable(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The decision is about the pointer in place, not the sibling: an antivirus scan holding the
        sibling must not turn an atomic replace into a window where ``current`` is absent."""
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        renames: list[str] = []
        real_rename = os.rename
        setattr_in(monkeypatch, GEN, "os.rename", lambda s, d, *k, **kw: (renames.append(str(d)), real_rename(s, d))[1])
        real_read = lc.read_pointer
        monkeypatch.setattr(lc, "read_pointer", lambda p: None if ".new." in str(p) else real_read(p))
        _flip(b, tools)
        assert not [r for r in renames if r.endswith("current")], renames


class TestFlipWarnsAboutAnOldGeneration:
    def test_a_generation_from_before_pointer_files_is_named(self, tools: Path, capsys: pytest.CaptureFixture[str]) -> None:
        old = _gen(tools, "gen-OLD")
        (old / "Lib" / "site-packages" / "nexus" / "_install").mkdir(parents=True)
        _flip(old, tools)
        assert "predates pointer files" in capsys.readouterr().err

    def test_a_current_generation_is_not_warned_about(self, tools: Path, capsys: pytest.CaptureFixture[str]) -> None:
        new = _gen(tools, "gen-NEW")
        install = new / "Lib" / "site-packages" / "nexus" / "_install"
        install.mkdir(parents=True)
        (install / "_nx_dispatch.py").write_text("", encoding="utf-8")
        _flip(new, tools)
        assert capsys.readouterr().err == ""


class TestLedgerJunctionIsConverted:
    def test_repair_turns_a_junction_era_ledger_into_a_pointer_file(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        uv = FakeUv()
        gen = gen_core.build_generation("conexus", tools=tools, run=uv, platform=WIN, now=lambda: 1_790_000_000.0, version="7.1.0")
        _flip(gen, tools)
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        os.symlink(legacy, tools / "gen-legacy-uv-tool", target_is_directory=True)  # the era's junction
        store = gen_core.FileUserPath(tmp_path / "p.txt")
        store.write("C:\\Windows", 2)
        gen_core.repair_layout(
            tools, ops=Ops(WIN), platform=WIN, store=store, uv_bin="C:\\uv", run=uv, environ={"PATH": ""},
        )
        pointer = tools / "gen-legacy-uv-tool"
        assert pointer.is_file() and not pointer.is_symlink()
        assert lc.read_link(pointer, platform=WIN) == str(legacy)


class TestListGenerationsNeverRaises448:
    def test_an_entry_that_cannot_be_traversed_is_skipped_not_raised(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        good = _gen(tools, "gen-20261101T000000Z")
        (good / lc.RECEIPT_NAME).write_text("{}", encoding="utf-8")
        bad = _gen(tools, "gen-20261102T000000Z")
        real_is_dir = Path.is_dir

        def refuse(self: Path, *a, **k):
            if self == bad:
                raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")
            return real_is_dir(self, *a, **k)

        monkeypatch.setattr(Path, "is_dir", refuse)
        assert lc.list_generations(tools=tools) == [good]


class TestReapOnPosixKeepsItsOldBehaviour:
    def test_a_dangling_generation_symlink_is_not_an_entry_and_costs_no_stderr(
        self, tools: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        os.symlink(tools / "gone", tools / "gen-dangling")
        assert gc_core._is_entry(tools / "gen-dangling", "gen-", None) is False
        assert gc_core._is_entry(tools / "gen-dangling", "gen-", WIN) is True, "a Windows pointer is one"
        assert capsys.readouterr().err == ""

    def test_the_reap_walk_never_opens_a_file_to_ask_whether_it_is_a_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tree = tmp_path / "gen-X"
        for i in range(5):
            (tree / "Lib" / f"d{i}").mkdir(parents=True)
            (tree / "Lib" / f"d{i}" / "f.py").write_text("x", encoding="utf-8")
        opened: list[str] = []
        real = lc.read_pointer
        monkeypatch.setattr(lc, "read_pointer", lambda p: (opened.append(str(p)), real(p))[1])
        gc_core._remove_all_but_lease(tree, ".nx-lease", lambda *a: None, platform=WIN)
        assert opened == [], opened
        assert [p for p in tree.rglob("*")] == []


class TestDispatchersEdges:
    def test_a_deleted_dispatcher_environment_is_rebuilt_even_though_every_launcher_exists(
        self, tools: Path,
    ) -> None:
        uv = FakeUv()
        gen = gen_core.build_generation("conexus", tools=tools, run=uv, platform=WIN, now=lambda: 1_790_000_000.0, version="7.1.0")
        gen_core.ensure_dispatchers(tools, gen, run=uv, platform=WIN)
        import shutil

        shutil.rmtree(tools / "shim")
        again = FakeUv()
        lines = gen_core.ensure_dispatchers(tools, gen, run=again, platform=WIN)
        assert any("is gone" in line for line in lines), lines
        assert (tools / "shim" / "Scripts" / "python.exe").is_file()
        assert any("--clear" in c for c in again.calls if c[:2] == ["uv", "venv"])

    def test_a_staging_directory_a_crashed_install_left_behind_is_swept(self, tools: Path) -> None:
        uv = FakeUv()
        gen = gen_core.build_generation("conexus", tools=tools, run=uv, platform=WIN, now=lambda: 1_790_000_000.0, version="7.1.0")
        old = tools / ".dispatch-wheels.4242"
        old.mkdir()
        os.utime(old, (1_000_000_000, 1_000_000_000))
        fresh = tools / ".dispatch-wheels.4343"
        fresh.mkdir()
        gen_core.ensure_dispatchers(tools, gen, run=uv, platform=WIN)
        assert not old.exists(), "older than an hour: a crash's"
        assert fresh.exists(), "a young one may belong to an install that is running now"


class TestActivateGeneration:
    def _spy(self, monkeypatch: pytest.MonkeyPatch, tools: Path, fail_launchers: bool = False) -> list[str]:
        calls: list[str] = []

        def dispatchers(*_a, **_k):
            calls.append("dispatchers")
            if fail_launchers:
                raise gen_core.GenerationError("uv is missing")
            return []

        monkeypatch.setattr(gen_core, "ensure_dispatchers", dispatchers)
        monkeypatch.setattr(gen_core, "ensure_launcher_path", lambda *_a, **_k: calls.append("path") or "result")
        monkeypatch.setattr(gen_core, "flip_current", lambda *_a, **_k: calls.append("flip"))
        return calls

    def test_the_dispatchers_and_the_path_come_before_the_flip(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch, tools)
        assert gen_core.activate_generation(tools, tools / "gen-A", platform=WIN) == "result"
        assert calls == ["dispatchers", "path", "flip"]

    def test_a_failed_dispatcher_build_means_no_flip(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch, tools, fail_launchers=True)
        with pytest.raises(gen_core.GenerationError):
            gen_core.activate_generation(tools, tools / "gen-A", platform=WIN)
        assert calls == ["dispatchers"]

    def test_a_restore_flips_first_and_a_failed_top_up_does_not_undo_it(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls = self._spy(monkeypatch, tools, fail_launchers=True)
        assert gen_core.activate_generation(tools, tools / "gen-A", platform=WIN, restoring=True) is None
        assert calls == ["flip", "dispatchers"]
        assert "current is back, but the launchers could not be topped up: uv is missing" in capsys.readouterr().err
