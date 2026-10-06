# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows readings of ``layout_core`` (RDR-224, nexus-f9bgu.47).

Every Windows branch takes an injectable ``platform`` (the seam
``nexus._winsec._is_windows`` set), so these run on macOS and Linux. Where a
real junction or ``\\\\?\\`` readlink would be needed, a seam stands in for it;
the real ones are exercised by ``tests/test_install_generation_windows.py``'s
``TestRealWindows``.

Each Windows test has a POSIX twin asserting the same call, with the default
platform, still gives the unchanged POSIX answer: the shell twins pin that
behaviour and this is the guard against the Windows branch leaking into it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install import layout_core as lc

WIN = "win32"
POSIX = "linux"


class TestExtendedPrefix:
    @pytest.mark.parametrize(
        ("raw", "plain"),
        [
            ("\\\\?\\C:\\Users\\x\\gen-1", "C:\\Users\\x\\gen-1"),
            ("\\??\\C:\\Users\\x", "C:\\Users\\x"),
            ("\\\\?\\UNC\\srv\\share\\g", "\\\\srv\\share\\g"),
            ("C:\\already\\plain", "C:\\already\\plain"),
            ("/posix/path", "/posix/path"),
            ("", ""),
        ],
    )
    def test_strips_only_the_extended_prefix(self, raw: str, plain: str) -> None:
        assert lc.strip_extended_prefix(raw) == plain


class TestVenvLayout:
    def test_windows_executables_live_in_scripts_with_exe_suffix(self, tmp_path: Path) -> None:
        assert lc.venv_bin_name(platform=WIN) == "Scripts"
        assert lc.venv_bin(tmp_path, platform=WIN) == tmp_path / "Scripts"
        assert lc.exe_name("nx", platform=WIN) == "nx.exe"
        assert lc.venv_script(tmp_path, "nx", platform=WIN) == tmp_path / "Scripts" / "nx.exe"
        assert lc.venv_python(tmp_path, platform=WIN) == tmp_path / "Scripts" / "python.exe"

    def test_posix_is_bin_without_suffix(self, tmp_path: Path) -> None:
        assert lc.venv_bin_name(platform=POSIX) == "bin"
        assert lc.exe_name("nx", platform=POSIX) == "nx"
        assert lc.venv_python(tmp_path, platform=POSIX) == tmp_path / "bin" / "python"

    def test_default_follows_the_host(self, tmp_path: Path) -> None:
        expected = "Scripts" if os.name == "nt" else "bin"
        assert lc.venv_bin(tmp_path) == tmp_path / expected


class TestIsLink:
    def test_a_junction_is_a_link_on_windows_only(self, tmp_path: Path) -> None:
        directory = tmp_path / "d"
        directory.mkdir()
        assert lc.is_link(directory, platform=WIN, isjunction=lambda _p: True) is True
        # POSIX never asks the junction probe: a directory is a directory.
        assert lc.is_link(directory, platform=POSIX, isjunction=lambda _p: True) is False

    def test_a_symlink_is_a_link_everywhere(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.mkdir()
        link = tmp_path / "l"
        link.symlink_to(target)
        assert lc.is_link(link, platform=WIN, isjunction=lambda _p: False)
        assert lc.is_link(link, platform=POSIX)

    def test_a_plain_directory_is_not_a_link(self, tmp_path: Path) -> None:
        assert lc.is_link(tmp_path, platform=WIN, isjunction=lambda _p: False) is False

    def test_a_failing_probe_means_not_a_link(self, tmp_path: Path) -> None:
        def boom(_p):
            raise OSError("denied")

        assert lc.is_link(tmp_path, platform=WIN, isjunction=boom) is False


class TestReadLink:
    def test_windows_strips_the_junction_prefix(self) -> None:
        got = lc.read_link("X", platform=WIN, readlink=lambda _p: "\\\\?\\C:\\t\\gen-1")
        assert got == "C:\\t\\gen-1"

    def test_posix_returns_the_target_as_written(self) -> None:
        got = lc.read_link("X", platform=POSIX, readlink=lambda _p: "\\\\?\\odd")
        assert got == "\\\\?\\odd"

    def test_current_generation_reads_through_it(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen-1"
        gen.mkdir()
        (tmp_path / "current").symlink_to(gen)
        assert lc.current_generation(tools=tmp_path, platform=WIN) == gen
        assert lc.current_generation(tools=tmp_path) == gen


class TestCompareKey:
    def test_posix_is_identity(self) -> None:
        assert lc.compare_key("/A/b/", platform=POSIX) == "/A/b/"

    def test_windows_spellings_of_one_tree_share_a_key(self) -> None:
        ident = lambda s: s  # noqa: E731 -- the seam
        spellings = [
            "C:\\Users\\Sam\\tools\\gen-1",
            "c:\\users\\sam\\tools\\GEN-1",
            "\\\\?\\C:\\Users\\Sam\\tools\\gen-1",
            "C:/Users/Sam/tools/gen-1",
            "C:\\Users\\Sam\\tools\\gen-1\\",
        ]
        keys = {lc.compare_key(s, platform=WIN, realpath=ident) for s in spellings}
        assert len(keys) == 1, keys

    def test_windows_resolves_through_the_injected_realpath(self) -> None:
        real = lambda s: "C:\\real\\gen-9"  # noqa: E731
        assert lc.compare_key("C:\\link\\x", platform=WIN, realpath=real) == "c:\\real\\gen-9"

    def test_distinct_trees_keep_distinct_keys(self) -> None:
        ident = lambda s: s  # noqa: E731
        a = lc.compare_key("C:\\t\\gen-1", platform=WIN, realpath=ident)
        b = lc.compare_key("C:\\t\\gen-2", platform=WIN, realpath=ident)
        assert a != b

    def test_an_unresolvable_path_keeps_its_spelling(self) -> None:
        def boom(_s):
            raise OSError("gone")

        assert lc.compare_key("C:\\X\\Y", platform=WIN, realpath=boom) == "c:\\x\\y"


class TestSourceKind:
    @pytest.mark.parametrize(
        "spec", ["C:\\src\\nexus", "c:/src/nexus", ".\\nexus", "..\\x", "D:nexus", "dir\\sub"],
    )
    def test_backslash_and_drive_specs_are_directories_on_windows(self, spec: str) -> None:
        assert lc.source_kind(spec, platform=WIN) == "directory"

    @pytest.mark.parametrize("spec", ["conexus", "conexus[local]==7.1.0", "conexus==7"])
    def test_registry_names_stay_registry_on_windows(self, spec: str) -> None:
        assert lc.source_kind(spec, platform=WIN) == "registry"

    @pytest.mark.parametrize("spec", ["C:\\src\\nexus", ".\\nexus", "D:nexus"])
    def test_the_posix_classification_is_untouched(self, spec: str) -> None:
        # No slash, so POSIX says registry: the rule the shell twin pins.
        assert lc.source_kind(spec, platform=POSIX) == "registry"
        assert lc.source_kind(spec) == ("directory" if os.name == "nt" else "registry")

    def test_dot_and_slash_forms_are_directories_everywhere(self) -> None:
        for platform in (WIN, POSIX):
            assert lc.source_kind(".", platform=platform) == "directory"
            assert lc.source_kind("a/b", platform=platform) == "directory"


class TestUvToolRoot:
    def test_windows_is_appdata_uv_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setenv("APPDATA", "C:\\Users\\Sam\\AppData\\Roaming")
        monkeypatch.setenv("XDG_DATA_HOME", "/ignored")
        got = lc.uv_tool_root(platform=WIN)
        assert got == Path("C:\\Users\\Sam\\AppData\\Roaming") / "uv" / "tools"
        assert lc.uv_conexus_venv(platform=WIN) == got / "conexus"

    def test_uv_tool_dir_wins_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UV_TOOL_DIR", "D:\\tools")
        monkeypatch.setenv("APPDATA", "C:\\a")
        assert lc.uv_tool_root(platform=WIN) == Path("D:\\tools")

    def test_posix_default_is_unchanged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("APPDATA", "C:\\a")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert lc.uv_tool_root(platform=POSIX) == tmp_path / ".local" / "share" / "uv" / "tools"


def _generation(tmp_path: Path, names: tuple[str, ...], *, platform: str) -> Path:
    gen = tmp_path / "gen-1"
    scripts = lc.venv_bin(gen, platform=platform)
    scripts.mkdir(parents=True)
    for name in names:
        (scripts / lc.exe_name(name, platform=platform)).write_bytes(f"launcher:{name}".encode())
    return gen


class TestOwnedAndReclaimedOnWindows:
    def test_owned_names_are_the_declared_ones_that_have_a_launcher_exe(self, tmp_path: Path) -> None:
        gen = _generation(tmp_path, ("nx", "nx-mcp"), platform=WIN)
        declared = frozenset({"nx", "nx-mcp", "nx-absent", "python"})
        assert lc.owned_from_declared(declared, gen, platform=WIN) == {"nx", "nx-mcp"}

    def test_a_posix_tree_has_no_windows_launchers(self, tmp_path: Path) -> None:
        gen = _generation(tmp_path, ("nx",), platform=POSIX)
        assert lc.owned_from_declared(frozenset({"nx"}), gen, platform=WIN) == frozenset()
        assert lc.owned_from_declared(frozenset({"nx"}), gen, platform=POSIX) == {"nx"}

    def test_nothing_is_ever_reclaimed_on_windows(self, tmp_path: Path) -> None:
        """No nexus file lives in uv's bin dir on Windows (the shims are a PATH
        entry ahead of it), so a launcher uv rewrites there is not a takeover."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "nx.exe").write_bytes(b"uv wrote this")
        assert lc.reclaimed_from_owned({"nx"}, bin_dir, platform=WIN) == []

    def test_posix_still_means_symlink(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "real").write_text("x")
        (bin_dir / "taken").symlink_to(bin_dir / "real")
        assert lc.reclaimed_from_owned({"real", "taken"}, bin_dir, platform=POSIX) == ["taken"]


class TestIsStaleOnWindows:
    def test_one_tree_in_two_spellings_is_not_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = tmp_path / "gen-1"
        gen.mkdir()
        (tmp_path / "current").symlink_to(gen)
        monkeypatch.setattr(os.path, "realpath", lambda s: s)
        upper = Path(str(gen).upper())
        assert lc.is_stale(upper, tools=tmp_path, platform=WIN) is False
        assert lc.is_stale(upper, tools=tmp_path, platform=POSIX) is True

    def test_a_different_tree_is_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = tmp_path / "gen-1"
        gen.mkdir()
        (tmp_path / "current").symlink_to(gen)
        monkeypatch.setattr(os.path, "realpath", lambda s: s)
        assert lc.is_stale(tmp_path / "gen-0", tools=tmp_path, platform=WIN) is True
