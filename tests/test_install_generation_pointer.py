# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows generation pointers are FILES (nexus-f9bgu.47, RedirectionGuard).

``generation_core.flip_current`` used to make ``<tools>\\current`` and
``previous`` directory junctions with ``_winapi.CreateJunction``. Under
Windows RedirectionGuard a junction made by a non-elevated process is refused
on traversal (WinError 448), so after an upgrade every launcher that went
through ``current\\bin`` failed, and a rollback through ``previous`` failed
too. The default pointer is now a plain file holding the generation's path
(``layout_core.write_pointer``), and the launchers on the user PATH are fixed
dispatchers that read it (``tests/test_install_dispatcher.py``).

These tests run on any host: a pointer file is the same bytes everywhere, and
``platform="win32"`` selects the Windows reading. A junction-era install (what
every box that installed before this change has) is stood in for by a symlink,
which ``layout_core.is_link`` also reads as a link. The real junction, and the
kernel's refusal, are in ``tests/test_install_generation_real_windows.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc

WIN = "win32"


class Ops(gen_core.LinkOps):
    """The product's default pointer kind; only the pid probe is faked."""

    def pid_alive(self, pid: int) -> bool:
        return False


class LegacyOps(Ops):
    """What the installer was before: a directory pointer (a symlink stands in
    for the junction, which needs Windows)."""

    def create(self, target: str, link: Path) -> None:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    return root


def _gen(tools: Path, name: str) -> Path:
    gen = tools / name
    (gen / "Scripts").mkdir(parents=True)
    return gen


def _flip(gen: Path, tools: Path, ops: gen_core.LinkOps | None = None) -> None:
    gen_core.flip_current(gen, tools, ops=ops or Ops(WIN), platform=WIN)


def _names(tools: Path) -> list[str]:
    return sorted(p.name for p in tools.iterdir())


class TestFilePointers:
    def test_the_default_pointer_is_a_file_not_a_directory_link(self, tools: Path) -> None:
        a = _gen(tools, "gen-A")
        _flip(a, tools)
        current = tools / "current"
        assert current.is_file() and not current.is_symlink()
        assert lc.is_link(current, platform=WIN)
        assert lc.read_link(current, platform=WIN) == str(a)
        assert lc.current_generation(tools=tools, platform=WIN) == a

    def test_flip_records_the_outgoing_generation_as_previous_and_leaves_no_litter(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        _flip(b, tools)
        assert lc.read_link(tools / "current", platform=WIN) == str(b)
        assert lc.read_link(tools / "previous", platform=WIN) == str(a)
        assert _names(tools) == ["current", "gen-A", "gen-B", "previous"]

    def test_replacing_a_live_pointer_has_no_instant_where_it_is_absent(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file is replaced in one ``os.replace``. The directory pointers needed two
        renames, and a spawn through ``current`` between them did not resolve."""
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        renames: list[tuple[str, str]] = []
        real_rename = os.rename
        monkeypatch.setattr(os, "rename", lambda s, d, *k, **kw: (renames.append((str(s), str(d))), real_rename(s, d))[1])
        seen_absent: list[bool] = []
        real_replace = os.replace

        def watching_replace(src, dst, *a_, **kw):
            seen_absent.append(not os.path.lexists(dst))
            return real_replace(src, dst, *a_, **kw)

        monkeypatch.setattr(os, "replace", watching_replace)
        _flip(b, tools)
        assert not [r for r in renames if "current" in r[0]], renames
        assert seen_absent and not any(seen_absent)

    def test_rollback_is_reversible_and_stays_in_file_pointers(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)
        _flip(b, tools)
        gen_core.rollback_current(tools, ops=Ops(WIN), platform=WIN)
        assert lc.current_generation(tools=tools, platform=WIN) == a
        gen_core.rollback_current(tools, ops=Ops(WIN), platform=WIN)
        assert lc.current_generation(tools=tools, platform=WIN) == b
        assert (tools / "current").is_file() and (tools / "previous").is_file()

    def test_a_crash_between_the_new_pointer_and_the_replace_leaves_the_old_pointer(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools)

        def boom(*_a, **_k):
            raise OSError(5, "Access is denied")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(gen_core.GenerationError):
            _flip(b, tools)
        monkeypatch.undo()
        assert lc.current_generation(tools=tools, platform=WIN) == a
        assert [n for n in _names(tools) if n.startswith(".")] == [], "the unfinished pointer is removed"

    def test_stale_litter_of_a_dead_swap_is_swept(self, tools: Path) -> None:
        a = _gen(tools, "gen-A")
        _flip(a, tools)
        lc.write_pointer(tools / ".current.new.99999", str(a))
        _flip(a, tools)
        assert [n for n in _names(tools) if n.startswith(".")] == []

    def test_a_real_file_named_current_is_never_replaced(self, tools: Path) -> None:
        (tools / "current").write_text("not a pointer", encoding="utf-8")
        with pytest.raises(gen_core.GenerationError, match="not a link"):
            _flip(_gen(tools, "gen-A"), tools)
        assert (tools / "current").read_text(encoding="utf-8") == "not a pointer"

    def test_register_legacy_writes_a_file_pointer_idempotently(self, tools: Path, tmp_path: Path) -> None:
        legacy = tmp_path / "uv" / "conexus"
        legacy.mkdir(parents=True)
        pointer = gen_core.register_legacy(legacy, tools, ops=Ops(WIN), platform=WIN)
        assert pointer.is_file() and lc.read_link(pointer, platform=WIN) == str(legacy)
        before = pointer.stat().st_mtime_ns
        gen_core.register_legacy(legacy, tools, ops=Ops(WIN), platform=WIN)
        assert pointer.stat().st_mtime_ns == before


class TestJunctionEraInstalls:
    """An install made before this change has directory pointers. It must keep
    working for reads (``readlink`` does not traverse, so RedirectionGuard has
    nothing to refuse), migrate on the next flip, and roll back."""

    def _legacy_install(self, tools: Path) -> tuple[Path, Path]:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        _flip(a, tools, LegacyOps(WIN))
        _flip(b, tools, LegacyOps(WIN))
        assert (tools / "current").is_symlink() and (tools / "previous").is_symlink()
        return a, b

    def test_a_directory_pointer_still_reads_as_current_and_previous(self, tools: Path) -> None:
        a, b = self._legacy_install(tools)
        assert lc.current_generation(tools=tools, platform=WIN) == b
        assert lc.read_link(tools / "previous", platform=WIN) == str(a)

    def test_the_next_flip_turns_both_pointers_into_files(self, tools: Path) -> None:
        a, b = self._legacy_install(tools)
        c = _gen(tools, "gen-C")
        _flip(c, tools)
        for name, target in (("current", c), ("previous", b)):
            pointer = tools / name
            assert pointer.is_file() and not pointer.is_symlink(), name
            assert lc.read_link(pointer, platform=WIN) == str(target)
        assert [n for n in _names(tools) if n.startswith(".")] == []
        assert a.is_dir() and b.is_dir() and c.is_dir(), "no generation tree is touched by a pointer swap"

    def test_rollback_to_a_directory_pointer_previous_works_and_migrates_both(self, tools: Path) -> None:
        a, b = self._legacy_install(tools)
        gen_core.rollback_current(tools, ops=Ops(WIN), platform=WIN)
        assert lc.current_generation(tools=tools, platform=WIN) == a
        assert (tools / "current").is_file() and (tools / "previous").is_file()
        assert lc.read_link(tools / "previous", platform=WIN) == str(b)

    def test_a_directory_pointer_whose_target_is_gone_is_not_recorded_as_previous(self, tools: Path) -> None:
        a, b = self._legacy_install(tools)
        os.rmdir(b / "Scripts")
        os.rmdir(b)
        c = _gen(tools, "gen-C")
        _flip(c, tools)
        assert lc.read_link(tools / "current", platform=WIN) == str(c)
        assert lc.read_link(tools / "previous", platform=WIN) == str(a), "an existing previous is left as it was"

    def test_a_directory_pointer_that_cannot_be_opened_is_still_swapped(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Under RedirectionGuard ``Path.exists`` on a path through the junction raises
        OSError 448. The flip must reach the pointer with ``lstat``-class calls only."""
        a, b = self._legacy_install(tools)
        real_stat = os.stat

        def guarded(path, *args, **kwargs):
            if str(path).startswith(str(tools / "current")) or str(path).startswith(str(tools / "previous")):
                if kwargs.get("follow_symlinks", True) and not args:
                    raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", guarded)
        c = _gen(tools, "gen-C")
        _flip(c, tools)
        monkeypatch.undo()
        assert lc.read_link(tools / "current", platform=WIN) == str(c)
