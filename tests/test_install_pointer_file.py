# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pointer FILE: what ``<tools>\\current`` and ``previous`` are on Windows
(nexus-f9bgu.47, RedirectionGuard).

Windows RedirectionGuard refuses to traverse a directory junction that a
non-elevated process made (WinError 448), and a user without Developer Mode
cannot make a symlink (WinError 1314), so the Windows generation pointers are
plain files holding the generation's path. A plain file is not a reparse point,
so nothing about it can be untrusted. These tests need no Windows: a pointer
file is the same bytes on every host, and ``platform="win32"`` selects the
Windows reading.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install import layout_core as lc

WIN = "win32"


class TestPointerFile:
    def test_a_pointer_file_is_a_link_on_the_windows_reading_and_reads_back(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen-1"
        gen.mkdir()
        link = tmp_path / "current"
        lc.write_pointer(link, str(gen))
        assert lc.is_link(link, platform=WIN)
        assert lc.read_link(link, platform=WIN) == str(gen)

    def test_the_pointer_holds_a_magic_line_then_the_path_and_nothing_else(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        lc.write_pointer(link, "C:\\tools\\gen-1")
        assert link.read_text(encoding="utf-8") == "nx-pointer 1\nC:\\tools\\gen-1\n"

    def test_an_ordinary_file_is_not_a_link(self, tmp_path: Path) -> None:
        plain = tmp_path / "current"
        plain.write_text("hello\n", encoding="utf-8")
        assert not lc.is_link(plain, platform=WIN)
        with pytest.raises(OSError):
            lc.read_link(plain, platform=WIN)

    def test_a_directory_is_not_a_link(self, tmp_path: Path) -> None:
        assert not lc.is_link(tmp_path, platform=WIN)

    def test_a_pointer_file_is_not_a_link_on_the_posix_reading(self, tmp_path: Path) -> None:
        """POSIX pointers are symlinks and stay so; a stray file there is a file."""
        link = tmp_path / "current"
        lc.write_pointer(link, str(tmp_path))
        assert not lc.is_link(link, platform="linux")

    def test_a_truncated_or_headerless_pointer_is_not_a_link(self, tmp_path: Path) -> None:
        for body in (b"", b"nx-pointer 1", b"nx-pointer 1\n", b"nx-pointer 2\nC:\\x\n", b"\xff\xfe\x00"):
            link = tmp_path / "current"
            link.write_bytes(body)
            assert not lc.is_link(link, platform=WIN), body
            link.unlink()

    def test_a_relative_target_is_refused_on_write(self, tmp_path: Path) -> None:
        with pytest.raises(lc.LayoutError):
            lc.write_pointer(tmp_path / "current", "gen-1")

    def test_a_newline_in_the_target_is_refused_on_write(self, tmp_path: Path) -> None:
        with pytest.raises(lc.LayoutError):
            lc.write_pointer(tmp_path / "current", "/x\n/y")

    def test_remove_link_unlinks_a_pointer_file_and_refuses_an_ordinary_one(self, tmp_path: Path) -> None:
        link = tmp_path / "previous"
        lc.write_pointer(link, str(tmp_path))
        lc.remove_link(link)
        assert not os.path.lexists(link)
        plain = tmp_path / "plain"
        plain.write_text("x", encoding="utf-8")
        with pytest.raises(lc.LayoutError):
            lc.remove_link(plain)
        assert plain.exists()

    def test_current_generation_reads_a_pointer_file(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        gen = tools / "gen-20261128T000000Z"
        gen.mkdir(parents=True)
        lc.write_pointer(lc.current_link(tools=tools), str(gen))
        assert lc.current_generation(tools=tools, platform=WIN) == gen

    def test_a_relative_pointer_target_is_refused_on_read(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "current").write_text("nx-pointer 1\ngen-1\n", encoding="utf-8")
        with pytest.raises(lc.LayoutError):
            lc.current_generation(tools=tools, platform=WIN)

    def test_a_junction_is_still_read_without_being_traversed(self, tmp_path: Path) -> None:
        """A junction-era install keeps working for reads: ``readlink`` does not
        traverse, so RedirectionGuard has nothing to refuse. (The seam stands in
        for ``os.readlink`` on a junction.)"""
        link = tmp_path / "previous"
        link.mkdir()
        got = lc.read_link(
            link, platform=WIN, readlink=lambda _p: "\\\\?\\C:\\tools\\gen-old",
        )
        assert got == "C:\\tools\\gen-old"

    def test_is_link_survives_winerror_448_from_the_probe(self, tmp_path: Path) -> None:
        """RedirectionGuard raises OSError 448 where a probe would have said False."""
        def refuse(_p):
            raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")

        assert lc.is_link(tmp_path / "x", platform=WIN, isjunction=refuse) is False
