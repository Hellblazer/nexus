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
import sys
from pathlib import Path

import click
import pytest

from nexus import install_layout
from nexus._install import generation_core as gen_core
from nexus.commands import self_cmd


class FakeUvAndSpy:
    """Stands in for ``uv`` and records every command, passing the rest through."""

    def __init__(self, real_run, *, fail_pip: str | None = None, tool_dir: Path | None = None) -> None:
        self.real_run = real_run
        self.calls: list[list[str]] = []
        self.fail_pip = fail_pip
        self.tool_dir = tool_dir

    def __call__(self, argv, *args, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] != "uv":
            return self.real_run(argv, *args, **kw)
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
    monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)
    monkeypatch.setattr(
        gen_core.LinkOps, "create",
        lambda self, target, link: os.symlink(target, link, target_is_directory=True),
    )
    spy = FakeUvAndSpy(subprocess.run)
    monkeypatch.setattr(gen_core.subprocess, "run", spy)
    monkeypatch.setattr(self_cmd.subprocess, "run", spy)
    return tools, bin_dir, spy


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
    monkeypatch.setattr(sys, "prefix", str(gen))


def _bash_calls(spy: FakeUvAndSpy) -> list[list[str]]:
    return [c for c in spy.calls if c and Path(c[0]).name in ("bash", "bash.exe", "sh")]


class TestGenerationSite:
    def test_builds_the_next_generation_flips_writes_shims_and_never_calls_bash(
        self, win, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools, bin_dir, spy = win
        host = _generation(tools, "20260101T000000Z", extras=["local"])
        os.symlink(str(host), tools / "current")
        _host(monkeypatch, host)
        new = self_cmd.perform_self_install(keep=3, add_extras=("voyage",))
        assert new is not None and new != host and new.parent == tools
        receipt = install_layout.read_receipt(new)
        assert receipt.extras == ["local", "voyage"]  # the receipt's extras MERGE with the request
        assert os.readlink(tools / "current") == str(new)
        assert os.readlink(tools / "previous") == str(host)
        assert (bin_dir / "nx.exe").read_bytes() == b"launcher:nx"
        assert install_layout.read_shim_record(bin_dir).keys() == {"nx", "nx-mcp"}
        assert _bash_calls(spy) == []

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
        monkeypatch.setattr(sys, "prefix", str(host).upper())
        # Not a generation under tools by exact spelling; the folded key matches.
        new = self_cmd.perform_self_install(keep=1)
        assert new is not None
        assert host.is_dir()

    def test_dry_run_describes_the_build_and_builds_nothing(
        self, win, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, bin_dir, spy = win
        host = _generation(tools, "20260101T000000Z", extras=["local"])
        _host(monkeypatch, host)
        assert self_cmd.perform_self_install(dry_run=True, version="7.2.0") is None
        out = capsys.readouterr().out
        assert "build_generation" in out and "--extras local" in out and "--version 7.2.0" in out
        assert [c for c in spy.calls if c[0] == "uv"] == []
        assert sorted(p.name for p in tools.iterdir()) == [host.name]
        assert not bin_dir.exists()

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
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "checkout" / ".venv"))
        monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: False)
        with pytest.raises(click.ClickException, match="not a generation"):
            self_cmd.perform_self_install()

    def test_extras_are_refused_off_the_generation_layout(self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
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
        monkeypatch.setattr(sys, "prefix", str(legacy))
        monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: True)
        return legacy

    def test_converges_a_legacy_uv_tree_without_bash_and_without_touching_it(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        tools, bin_dir, spy = win
        legacy = self._legacy(tmp_path, monkeypatch)
        spy.tool_dir = legacy.parent
        generation = self_cmd.perform_self_install()
        assert generation is not None and generation.parent == tools
        assert install_layout.read_receipt(generation).extras == ["local"]  # mineru dropped
        assert os.readlink(tools / "current") == str(generation)
        assert (bin_dir / "nx.exe").read_bytes() == b"launcher:nx"
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        assert (legacy / "Scripts" / "nx.exe").read_bytes() == b"uv's launcher"
        assert "converged the legacy uv-tool install" in capsys.readouterr().out
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

    def test_a_takeover_beside_an_existing_layout_refuses_with_a_clear_message(
        self, win, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        tools, _bin, _spy = win
        self._legacy(tmp_path, monkeypatch)
        host = _generation(tools, "20260101T000000Z")
        os.symlink(str(host), tools / "current")
        with pytest.raises(click.ClickException, match="not supported on Windows"):
            self_cmd.perform_self_install()
        with pytest.raises(click.ClickException, match="not supported on Windows"):
            self_cmd.repair_uv_takeover()


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
