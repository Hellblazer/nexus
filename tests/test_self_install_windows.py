# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx self install`` on native Windows (RDR-224, nexus-f9bgu.47).

The command must never call ``bash`` there (on Windows ``bash`` is the WSL
launcher): it calls ``generation_core`` directly. These tests drive the Windows
branches on any host by patching ``self_cmd._is_windows``, standing a fake
``uv`` in for the real one (every other command passes through), and making
``LinkOps.create`` a symlink where Windows makes a junction. They cover both
sites the issue names: the generation site (build next, flip, shims, reap with
rule (d)) and the legacy uv-tool site (extras from ``uv-receipt.toml``, build,
flip, shims, ledger registration).
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import click
import pytest

from nexus import install_layout
from nexus._install import generation_core as gen_core
from nexus.commands import self_cmd
from tests._module_seam import setattr_in


class FakeUvAndSpy:
    """Stands in for ``uv`` and records every command, passing the rest through."""

    def __init__(
        self, real_run, *, fail_pip: str | None = None, tool_dir: Path | None = None,
        uv_bin: Path | None = None,
    ) -> None:
        self.uv_bin = uv_bin
        self.real_run = real_run
        self.calls: list[list[str]] = []
        self.fail_pip = fail_pip
        self.tool_dir = tool_dir

    def __call__(self, argv, *args, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] != "uv":
            return self.real_run(argv, *args, **kw)
        if argv[1:4] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.uv_bin}\n", "")
        if argv[1:3] == ["tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.tool_dir}\n", "")
        if argv[1] == "venv":
            gen = Path(argv[-1])
            (gen / "Scripts").mkdir(parents=True, exist_ok=True)
            (gen / "pyvenv.cfg").write_text("home = C:\\py\\cpython-3.12\nversion = 3.12.8\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["pip", "install"]:
            if self.fail_pip is not None:
                return subprocess.CompletedProcess(argv, 1, "", self.fail_pip)
            python = Path(argv[argv.index("--python") + 1])
            python.write_text("#!/bin/sh\nprintf '%s\\n' nx nx-mcp\n")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            (python.parent / "nx.exe").write_bytes(b"launcher:nx")
            (python.parent / "nx-mcp.exe").write_bytes(b"launcher:nx-mcp")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected uv command {argv}")


@pytest.fixture
def win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(self_cmd, "_is_windows", lambda platform=None: True)
    setattr_in(monkeypatch, "nexus._install.layout_core", "os.path.realpath", lambda s, **_kw: s)
    monkeypatch.setattr(
        gen_core.LinkOps, "create",
        lambda self, target, link: os.symlink(target, link, target_is_directory=True),
    )
    uv_bin = tmp_path / "uvbin"
    uv_bin.mkdir()
    store = tmp_path / "userpath.txt"
    monkeypatch.setenv("NX_USER_PATH_STORE", str(store))
    monkeypatch.setenv("PATH", "C:\\Windows")
    spy = FakeUvAndSpy(subprocess.run, uv_bin=uv_bin)
    setattr_in(monkeypatch, gen_core, "subprocess.run", spy)
    setattr_in(monkeypatch, self_cmd, "subprocess.run", spy)
    return tools, uv_bin, spy


def _generation(tools: Path, stamp: str, *, extras: list[str] | None = None) -> Path:
    extras = extras or []
    gen = tools / f"gen-{stamp}"
    (gen / "Scripts").mkdir(parents=True)
    (gen / "pyvenv.cfg").write_text("home = C:\\py\nversion = 3.12.8\n")
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    (gen / "nexus-install.json").write_text(install_layout.Receipt(
        version="7.1.0", spec=spec, source_kind="registry", source="conexus",
        python="3.12.8", base_interpreter="C:\\py", created_at="2026-10-01T00:00:00Z",
        extras=extras,
    ).to_json())
    os.utime(gen, (1_000_000_000, 1_000_000_000))
    return gen


def _host(monkeypatch: pytest.MonkeyPatch, gen: Path) -> None:
    setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus._install.generation_core"), "sys.prefix", str(gen))


def _bash_calls(spy: FakeUvAndSpy) -> list[list[str]]:
    return [c for c in spy.calls if c and Path(c[0]).name in ("bash", "bash.exe", "sh")]


class TestGenerationSite:
    def test_builds_the_next_generation_flips_puts_current_bin_on_the_path_and_never_calls_bash(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, uv_bin, spy = win
        host = _generation(tools, "20260101T000000Z", extras=["local"])
        os.symlink(str(host), tools / "current")
        _host(monkeypatch, host)
        new = self_cmd.perform_self_install(keep=3, add_extras=("voyage",))
        assert new is not None and new != host and new.parent == tools
        receipt = install_layout.read_receipt(new)
        assert receipt.extras == ["local", "voyage"]  # the receipt's extras MERGE with the request
        assert os.readlink(tools / "current") == str(new)
        assert os.readlink(tools / "previous") == str(host)
        # The shim is a PATH entry: the new generation's own launchers, and the
        # user PATH naming <tools>\current\bin. Nothing is written into uv's bin dir.
        assert sorted(p.name for p in (new / "bin").iterdir()) == ["nx-mcp.exe", "nx.exe"]
        user_path = (tmp_path / "userpath.txt").read_text().strip().split(";")
        assert user_path[0] == str(tools / "current" / "bin")
        assert os.environ["PATH"].split(";")[0] == str(tools / "current" / "bin")
        assert list(uv_bin.iterdir()) == []
        out = capsys.readouterr().out
        assert "added" in out and "restart" in out.lower() and "Claude Code" in out
        assert _bash_calls(spy) == []

    def test_a_second_install_does_not_rewrite_the_path_or_repeat_the_notice(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, _uv_bin, _spy = win
        host = _generation(tools, "20260101T000000Z")
        os.symlink(str(host), tools / "current")
        _host(monkeypatch, host)
        self_cmd.perform_self_install(keep=3)
        capsys.readouterr()
        store = tmp_path / "userpath.txt"
        before = store.stat().st_mtime_ns
        self_cmd.perform_self_install(keep=3)
        assert store.stat().st_mtime_ns == before
        assert "added" not in capsys.readouterr().out

    def test_the_hosting_generation_is_never_reaped_even_outside_keep(
        self, win, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools, _bin, _spy = win
        host = _generation(tools, "20260101T000000Z")
        others = [_generation(tools, f"2026010{n}T000000Z") for n in range(2, 6)]
        _host(monkeypatch, host)
        new = self_cmd.perform_self_install(keep=1)
        assert host.is_dir(), "rule (d): the generation hosting the installer survives"
        assert new is not None and new.is_dir()
        assert not any(g.exists() for g in others), "unprotected, unheld generations are reaped"

    def test_rule_d_holds_when_sys_prefix_is_spelt_differently(
        self, win, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools, _bin, _spy = win
        host = _generation(tools, "20260101T000000Z")
        for n in range(2, 5):
            _generation(tools, f"2026010{n}T000000Z")
        upper = str(host).upper()
        if not Path(upper).is_dir():
            pytest.skip("case-sensitive filesystem: the upper-cased sys.prefix does not exist here "
                        "(rule (d)'s folded comparison is covered directly in tests/test_install_gc_windows.py)")
        setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus._install.generation_core"), "sys.prefix", upper)
        # Not a generation under tools by exact spelling; the folded key matches.
        new = self_cmd.perform_self_install(keep=1)
        assert new is not None
        assert host.is_dir()

    def test_dry_run_describes_the_build_and_builds_nothing(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, _uv_bin, spy = win
        host = _generation(tools, "20260101T000000Z", extras=["local"])
        _host(monkeypatch, host)
        assert self_cmd.perform_self_install(dry_run=True, version="7.2.0") is None
        out = capsys.readouterr().out
        assert "build_generation" in out and "--extras local" in out and "--version 7.2.0" in out
        assert [c for c in spy.calls if c[0] == "uv"] == []
        assert sorted(p.name for p in tools.iterdir()) == [host.name]
        assert not (tmp_path / "userpath.txt").exists()

    def test_a_failed_build_names_the_index_and_flips_nothing(
        self, win, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools, _bin, spy = win
        host = _generation(tools, "20260101T000000Z")
        _host(monkeypatch, host)
        spy.fail_pip = "error: Failed to fetch: `https://pypi.example/simple/x/`\n  dns error: failed to lookup address"
        with pytest.raises(click.ClickException) as raised:
            self_cmd.perform_self_install()
        message = raised.value.message
        assert "generation build failed" in message
        assert "uv could not reach the package index" in message
        assert "The install you have was not changed." in message
        assert not os.path.lexists(tools / "current")
        assert sorted(p.name for p in tools.iterdir()) == [host.name], "an unfinished tree is removed"

    def test_a_dev_checkout_still_refuses(self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus._install.generation_core"), "sys.prefix", str(tmp_path / "checkout" / ".venv"))
        monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: False)
        with pytest.raises(click.ClickException, match="not a generation"):
            self_cmd.perform_self_install()

    def test_extras_are_refused_off_the_generation_layout(self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus._install.generation_core"), "sys.prefix", str(tmp_path / "elsewhere"))
        with pytest.raises(click.ClickException, match="--extras applies to a generation install"):
            self_cmd.perform_self_install(add_extras=("local",))


class TestLegacySite:
    def _legacy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "uvtools"
        legacy = root / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        (legacy / "Scripts" / "nx.exe").write_bytes(b"uv's launcher")
        (legacy / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conexus", extras = ["local", "mineru"] }]\n'
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(root))
        setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus._install.generation_core"), "sys.prefix", str(legacy))
        monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: True)
        return legacy

    def test_converges_a_legacy_uv_tree_without_bash_and_without_touching_it(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, uv_bin, spy = win
        legacy = self._legacy(tmp_path, monkeypatch)
        spy.tool_dir = legacy.parent
        generation = self_cmd.perform_self_install()
        assert generation is not None and generation.parent == tools
        assert install_layout.read_receipt(generation).extras == ["local"]  # mineru dropped
        assert os.readlink(tools / "current") == str(generation)
        assert (generation / "bin" / "nx.exe").read_bytes().endswith(b"launcher:nx")
        entry = str(tools / "current" / "bin")
        assert (tmp_path / "userpath.txt").read_text().strip().split(";")[0] == entry
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        assert (legacy / "Scripts" / "nx.exe").read_bytes() == b"uv's launcher"
        assert list(uv_bin.iterdir()) == [], "nothing is written into uv's bin dir"
        out = capsys.readouterr().out
        assert "converged the legacy uv-tool install" in out
        assert "added" in out and "restart" in out.lower()
        assert _bash_calls(spy) == []

    def test_dry_run_converges_nothing(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, _bin, spy = win
        self._legacy(tmp_path, monkeypatch)
        assert self_cmd.perform_self_install(dry_run=True, version="7.2.0") is None
        assert "migrate_legacy --source conexus --version 7.2.0" in capsys.readouterr().out
        assert list(tools.iterdir()) == []
        assert [c for c in spy.calls if c[0] == "uv"] == []

    def test_a_legacy_site_with_no_legacy_tree_is_a_loud_contradiction(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, _bin, spy = win
        legacy = self._legacy(tmp_path, monkeypatch)
        spy.tool_dir = tmp_path / "nowhere"
        with pytest.raises(click.ClickException, match="found no legacy tree"):
            self_cmd.perform_self_install()
        assert legacy.is_dir() and list(tools.iterdir()) == []

    def test_a_half_migrated_box_is_finished_by_the_next_install_not_refused_or_rebuilt(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """What a real box looked like after the first implementation failed
        writing a shim: a generation built and `current` flipped, the legacy
        launcher still the one running, no PATH entry, no ledger junction."""
        tools, _uv_bin, spy = win
        legacy = self._legacy(tmp_path, monkeypatch)
        host = _generation(tools, "20260101T000000Z")
        for name in ("nx", "nx-mcp"):
            (host / "Scripts" / f"{name}.exe").write_bytes(f"#!{host}\nlauncher:{name}".encode())
        python = host / "Scripts" / "python.exe"
        python.write_text("#!/bin/sh\nprintf '%s\\n' nx nx-mcp\n")
        python.chmod(0o755)
        os.symlink(str(host), tools / "current")
        gens_before = sorted(p.name for p in tools.iterdir())

        assert self_cmd.perform_self_install() is None
        out = capsys.readouterr().out
        assert "restart" in out.lower()
        assert (host / "bin" / "nx.exe").is_file()
        assert (tmp_path / "userpath.txt").read_text().strip().split(";")[0] == str(tools / "current" / "bin")
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        assert [c for c in spy.calls if c[0] == "uv" and "pip" in c] == [], "no other generation was built"
        assert set(sorted(p.name for p in tools.iterdir())) - set(gens_before) == {"gen-legacy-uv-tool"}

        # and again: nothing left to do, nothing printed
        assert self_cmd.perform_self_install() is None
        assert capsys.readouterr().out == ""

    def test_repair_on_windows_no_longer_refuses(self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        tools, _uv_bin, _spy = win
        assert self_cmd.repair_uv_takeover() == []  # no layout: nothing to repair
        host = _generation(tools, "20260101T000000Z")
        os.symlink(str(host), tools / "current")
        lines = self_cmd.repair_uv_takeover(dry_run=True)
        assert any("user PATH" in line for line in lines)


class TestRegisterAndGc:
    def test_registers_the_legacy_tree_after_the_reap_on_the_generation_path(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, _bin, _spy = win
        host = _generation(tools, "20260101T000000Z")
        _host(monkeypatch, host)
        root = tmp_path / "uvtools"
        (root / "conexus" / "Scripts").mkdir(parents=True)
        monkeypatch.setenv("UV_TOOL_DIR", str(root))
        self_cmd.perform_self_install(keep=3)
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(root / "conexus")
        assert (root / "conexus").is_dir(), "registered this pass, reaped by a later one"

    def test_no_legacy_tree_registers_nothing(self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        tools, _bin, _spy = win
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "empty"))
        assert self_cmd._register_legacy_tree_if_present(self_cmd.packaged_install_dir(), tools) is None
        assert not os.path.lexists(tools / "gen-legacy-uv-tool")

    def test_self_gc_reaps_through_generation_core(
        self, win, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools, _bin, spy = win
        gens = [_generation(tools, f"2026010{n}T000000Z") for n in range(1, 6)]
        os.symlink(str(gens[-1]), tools / "current")
        _host(monkeypatch, gens[-1])
        lines = self_cmd.perform_self_gc(keep=2)
        assert lines is not None
        assert sum(line.startswith("reaped") for line in lines) == 3, lines
        assert [g.exists() for g in gens] == [False, False, False, True, True]
        assert _bash_calls(spy) == []

    def _gc_bed(self, win, monkeypatch, tmp_path):
        tools, uv_bin, spy = win
        gens = [_generation(tools, f"2026010{n}T000000Z") for n in range(1, 3)]
        os.symlink(str(gens[-1]), tools / "current")
        (gens[-1] / "bin").mkdir()
        (gens[-1] / "bin" / "nx.exe").write_bytes(b"new")
        python = gens[-1] / "Scripts" / "python.exe"  # answers "what do you declare"
        python.write_text("#!/bin/sh\nprintf '%s\\n' nx\n")
        python.chmod(0o755)
        _host(monkeypatch, gens[-1])
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uvtools"))  # no legacy tree there
        return tools, uv_bin, gens

    def test_gc_removes_uvs_old_launchers_once_the_legacy_tree_is_gone(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, uv_bin, gens = self._gc_bed(win, monkeypatch, tmp_path)
        (uv_bin / "nx.exe").write_bytes(b"uv's")
        (uv_bin / "uv.exe").write_bytes(b"uv itself")
        lines = self_cmd.perform_self_gc(keep=3)
        assert f"removed {uv_bin / 'nx.exe'}" in lines
        assert [p.name for p in uv_bin.iterdir()] == ["uv.exe"]
        assert all(g.exists() for g in gens)

    def test_gc_names_the_receipts_launchers_before_the_reap_deletes_it(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, uv_bin, gens = self._gc_bed(win, monkeypatch, tmp_path)
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        (legacy / "pyvenv.cfg").write_text("home = C:\\py\n")
        (legacy / "uv-receipt.toml").write_text(
            'entrypoints = [\n  { name = "nx-hook", install-path = "x", from = "conexus" },\n]\n'
        )
        os.symlink(str(legacy), tools / "gen-legacy-uv-tool")
        (uv_bin / "nx-hook.exe").write_bytes(b"uv's")
        (gens[-1] / "bin" / "nx-hook.exe").write_bytes(b"new")
        lines = self_cmd.perform_self_gc(keep=3)
        assert not legacy.exists(), lines
        assert f"removed {uv_bin / 'nx-hook.exe'}" in lines

    def test_gc_keeps_a_launcher_while_the_legacy_tree_still_exists(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, uv_bin, gens = self._gc_bed(win, monkeypatch, tmp_path)
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        (uv_bin / "nx.exe").write_bytes(b"uv's")
        lines = self_cmd.perform_self_gc(keep=3, dry_run=True)
        assert (uv_bin / "nx.exe").exists()
        assert not any("nx.exe" in line for line in lines)

    def test_a_box_with_no_layout_is_silent(self, win) -> None:
        assert self_cmd.perform_self_gc() is None


def test_posix_still_builds_a_bash_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Windows branch is gated: with the real ``_is_windows`` on a POSIX host
    the build request is the bash argv the POSIX path always used."""
    if os.name == "nt":
        pytest.skip("POSIX argv")
    receipt = install_layout.Receipt(
        version="7.1.0", spec="conexus", source_kind="registry", source="conexus",
        python="3.12", base_interpreter="/usr/bin", created_at="2026-10-01T00:00:00Z",
    )
    request = self_cmd._build_request(Path("/pkg/_install"), receipt, version=None)
    assert request[:2] == ["bash", "/pkg/_install/install_generation.sh"]
    assert not isinstance(request, self_cmd._WindowsBuild)
