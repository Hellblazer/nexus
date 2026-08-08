# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the nexus-l2ku5 MCP entry-point handshake doctor check.

nexus-l2ku5: mcp 2.0.0 (2026-07-28) removed ``mcp.server.fastmcp``. The
unbounded ``mcp>=1.0`` floor let it into every fresh install for 4 days;
both ``nx-mcp`` and ``nx-mcp-catalog`` died at import with zero signal
(Claude Code swallows stderr; every test gate ran pinned to the dev venv's
uv.lock, never booting the INSTALLED entry point). ``_check_mcp_entry_points``
is the layer test that was missing: it spawns the real binary on PATH and
sends a JSON-RPC ``initialize`` request, exactly as the bug was found by
hand.

CRITICAL POLICY under test: failure to probe must NEVER render as ✓.
"""
from __future__ import annotations

import inspect
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli import main
from nexus.health import (
    _check_mcp_entry_points,
    _first_lines,
    _probe_mcp_server,
    _resolve_mcp_binary,
)


def _write_fake_binary(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_python_binary(path: Path, body: str) -> None:
    """Like :func:`_write_fake_binary` but shebangs the RUNNING
    interpreter — needed for fakes that must emit raw non-UTF8 bytes,
    which POSIX ``sh``/``printf`` cannot portably do across shells."""
    _write_fake_binary(path, f"#!{sys.executable}\n{body}")


_HEALTHY_NEXUS_RESPONSE = (
    "#!/bin/sh\n"
    "read -r line\n"
    'printf \'%s\\n\' \'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"nexus"}}}\'\n'
)

_HEALTHY_CATALOG_RESPONSE = (
    "#!/bin/sh\n"
    "read -r line\n"
    'printf \'%s\\n\' \'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"nexus-catalog"}}}\'\n'
)

# The exact failure mode nexus-l2ku5 hand-discovered: dead at import,
# ModuleNotFoundError on stderr, non-zero exit, nothing useful on stdout.
_CRASHING_MODULE_NOT_FOUND = (
    "#!/bin/sh\n"
    "read -r line\n"
    'echo "Traceback (most recent call last):" >&2\n'
    'echo "  File \\"nx-mcp\\", line 1, in <module>" >&2\n'
    "echo \"ModuleNotFoundError: No module named 'mcp.server.fastmcp'\" >&2\n"
    "exit 1\n"
)

# A crashing binary that emits invalid-UTF8 bytes on stderr (e.g. a
# mangled/binary traceback) — must decode via errors="replace", never
# raise UnicodeDecodeError out of the probe.
_CRASHING_INVALID_UTF8 = (
    "import sys\n"
    "sys.stdin.readline()\n"
    'sys.stderr.buffer.write(b"\\xff\\xfeModuleNotFoundError invalid utf8 \\xfa\\xfb\\n")\n'
    "sys.stderr.buffer.flush()\n"
    "sys.exit(1)\n"
)


class TestCheckMcpEntryPoints:
    def test_healthy_binaries_pass(self, tmp_path: Path, monkeypatch) -> None:
        _write_fake_binary(tmp_path / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(tmp_path / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(tmp_path))

        results = _check_mcp_entry_points()

        assert len(results) == 2
        for r in results:
            assert r.ok is True
            assert r.warn is False
            assert "serverInfo.name=" in r.detail

    def test_crashing_binary_is_hard_fail_never_ok(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """nexus-l2ku5's exact failure mode: dies at import with
        ModuleNotFoundError on stderr. Must be a hard ✗ (ok=False,
        warn=False, fatal=True) carrying the stderr excerpt — never ✓ and
        never a silent "could not check"."""
        _write_fake_binary(tmp_path / "nx-mcp", _CRASHING_MODULE_NOT_FOUND)
        _write_fake_binary(tmp_path / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(tmp_path))

        results = _check_mcp_entry_points()
        by_label = {r.label: r for r in results}
        nx_mcp = by_label["MCP entry point (nx-mcp)"]

        assert nx_mcp.ok is False
        assert nx_mcp.warn is False
        assert nx_mcp.fatal is True
        assert "ModuleNotFoundError" in nx_mcp.detail
        assert "No module named 'mcp.server.fastmcp'" in nx_mcp.detail
        # the healthy sibling must not be dragged down
        assert by_label["MCP entry point (nx-mcp-catalog)"].ok is True

    def test_absent_binary_is_soft_warn_never_ok(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Dev-checkout edge: no installed tool need be on PATH. Must
        degrade to ⚠ (ok=False, warn=True), never ✓."""
        # empty dir on PATH — neither binary resolves
        monkeypatch.setenv("PATH", str(tmp_path))

        results = _check_mcp_entry_points()

        assert len(results) == 2
        for r in results:
            assert r.ok is False
            assert r.warn is True
            assert r.fatal is False
            assert "not found on PATH" in r.detail
            assert r.fix_suggestions

    def test_wrong_server_name_is_hard_fail(self, tmp_path: Path, monkeypatch) -> None:
        """A binary that responds but with the wrong serverInfo.name (e.g.
        PATH resolved an unrelated binary) must fail, not pass."""
        wrong_name_script = (
            "#!/bin/sh\n"
            "read -r line\n"
            'printf \'%s\\n\' \'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"not-nexus"}}}\'\n'
        )
        _write_fake_binary(tmp_path / "nx-mcp", wrong_name_script)
        _write_fake_binary(tmp_path / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(tmp_path))

        results = _check_mcp_entry_points()
        by_label = {r.label: r for r in results}

        assert by_label["MCP entry point (nx-mcp)"].ok is False
        assert "not-nexus" in by_label["MCP entry point (nx-mcp)"].detail

    def test_wired_into_run_health_checks(self) -> None:
        """Falsification pin: deleting the run_health_checks call site
        must fail this test, not silently drop the probe from `nx
        doctor`."""
        from nexus import health  # noqa: PLC0415 — deferred to avoid import-time cost, matching codebase convention

        src = inspect.getsource(health.run_health_checks)
        assert "_check_mcp_entry_points" in src

    def test_invalid_utf8_stderr_is_hard_fail_not_unicode_decode_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Round-2 critique (SIGNIFICANT): a crashing binary emitting raw
        non-UTF8 bytes on stderr must decode via errors='replace' and
        report a hard FAIL — never let UnicodeDecodeError escape the
        check and crash `nx doctor` itself."""
        _write_fake_python_binary(tmp_path / "nx-mcp", _CRASHING_INVALID_UTF8)
        _write_fake_binary(tmp_path / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(tmp_path))

        results = _check_mcp_entry_points()
        by_label = {r.label: r for r in results}
        nx_mcp = by_label["MCP entry point (nx-mcp)"]

        assert nx_mcp.ok is False
        assert nx_mcp.fatal is True
        assert "ModuleNotFoundError" in nx_mcp.detail

    def test_unexpected_exception_probing_present_binary_is_fatal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Round-2 critique: an unexpected exception while probing a
        PRESENT binary must render as a hard FAIL (fatal=True, warn=False)
        — at least as bad as a confirmed crash, never a soft ⚠."""
        _write_fake_binary(tmp_path / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(tmp_path / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(tmp_path))

        with patch(
            "nexus.health._probe_mcp_server",
            side_effect=RuntimeError("boom"),
        ):
            results = _check_mcp_entry_points()

        for r in results:
            assert r.ok is False
            assert r.fatal is True
            assert r.warn is False
            assert "boom" in r.detail


class TestResolveMcpBinary:
    """nexus-l2ku5 critique round 2: resolution must prefer a hit outside
    this process's own sys.prefix, never silently probe the lock-pinned
    dev venv when a separately installed tool is also on PATH."""

    def test_prefers_non_own_prefix_hit_over_own_venv(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        own_venv_dir = tmp_path / "own-venv-bin"
        own_venv_dir.mkdir()
        installed_dir = tmp_path / "installed-bin"
        installed_dir.mkdir()
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(installed_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        # own_venv_dir listed FIRST on PATH — a naive `which` would pick it.
        monkeypatch.setenv("PATH", f"{own_venv_dir}{os.pathsep}{installed_dir}")
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(installed_dir / "nx-mcp")
        assert is_own_venv is False

    def test_home_scoping_prefers_own_venv_over_foreign_home_install(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """nexus-k0lk9 sibling (7.4.0 cut): the release sandbox
        (HOME=~/nexus-sandbox) ran doctor from the sandbox tool venv; the
        resolver skipped it as own-prefix and probed the REAL home's live
        install — the gate then tracked host health, not the artifact
        under test. A hit OUTSIDE the current $HOME, when our own prefix
        is home-rooted, is FOREIGN and must lose to the own-prefix hit."""
        home = tmp_path / "sandbox-home"
        own_venv_dir = home / "tool-venv-bin"
        own_venv_dir.mkdir(parents=True)
        foreign_dir = tmp_path / "real-home" / ".local" / "bin"
        foreign_dir.mkdir(parents=True)
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(foreign_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        # Foreign (host) install listed FIRST — the exact sandbox shape:
        # sandbox bin, then the inherited host PATH tail.
        monkeypatch.setenv("PATH", f"{own_venv_dir}{os.pathsep}{foreign_dir}")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(own_venv_dir / "nx-mcp")
        assert is_own_venv is True

    def test_home_scoping_keeps_l2ku5_preference_inside_home(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Dev-box shape unchanged: checkout venv AND ~/.local/bin both
        under $HOME — the separately installed tool still wins."""
        home = tmp_path / "home"
        own_venv_dir = home / "git" / "nexus" / ".venv" / "bin"
        own_venv_dir.mkdir(parents=True)
        installed_dir = home / ".local" / "bin"
        installed_dir.mkdir(parents=True)
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(installed_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        monkeypatch.setenv("PATH", f"{own_venv_dir}{os.pathsep}{installed_dir}")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(installed_dir / "nx-mcp")
        assert is_own_venv is False

    def test_home_scoping_survives_homeless_environment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """nexus-262a7 (critic Critical): Path.home() raises RuntimeError —
        not OSError — when HOME is unset with no passwd entry (K8s
        arbitrary-UID / distroless). The resolver must degrade to the
        pre-existing l2ku5 preference, never crash `nx doctor`."""
        own_venv_dir = tmp_path / "own-venv-bin"
        own_venv_dir.mkdir()
        installed_dir = tmp_path / "installed-bin"
        installed_dir.mkdir()
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(installed_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        monkeypatch.setenv("PATH", f"{own_venv_dir}{os.pathsep}{installed_dir}")
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))
        monkeypatch.setattr(
            "nexus.health.Path.home",
            classmethod(lambda cls: (_ for _ in ()).throw(
                RuntimeError("Could not determine home directory.")
            )),
        )

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(installed_dir / "nx-mcp")
        assert is_own_venv is False

    def test_home_scoping_tradeoff_outside_home_install_loses_to_own_venv(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Pins the ACCEPTED trade-off (see _resolve_mcp_binary docstring):
        home-rooted own prefix + real install ONLY outside $HOME (the
        /usr/local shape) → the own venv is probed, honestly labeled.
        Changing this behavior must be a deliberate decision, not drift."""
        home = tmp_path / "home"
        own_venv_dir = home / "checkout" / ".venv" / "bin"
        own_venv_dir.mkdir(parents=True)
        system_dir = tmp_path / "usr-local-bin"
        system_dir.mkdir()
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(system_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        monkeypatch.setenv("PATH", f"{own_venv_dir}{os.pathsep}{system_dir}")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(own_venv_dir / "nx-mcp")
        assert is_own_venv is True

    def test_home_scoping_foreign_hit_is_last_resort_not_dropped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A real binary is always probed, never skipped: when the ONLY
        PATH hit is foreign (own prefix home-rooted, no own hit on PATH),
        the foreign hit is still returned rather than None."""
        home = tmp_path / "home"
        own_prefix = home / "tool-venv"
        own_prefix.mkdir(parents=True)
        foreign_dir = tmp_path / "elsewhere" / "bin"
        foreign_dir.mkdir(parents=True)
        _write_fake_binary(foreign_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        monkeypatch.setenv("PATH", str(foreign_dir))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_prefix))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(foreign_dir / "nx-mcp")
        assert is_own_venv is False

    def test_falls_back_to_own_prefix_when_it_is_the_only_hit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A user invoking the tool venv's own `nx` directly, with only
        that venv's bin/ on PATH, must still get a REAL probe — never a
        silent skip-to-warn just because the only hit is under sys.prefix."""
        own_venv_dir = tmp_path / "own-venv-bin"
        own_venv_dir.mkdir()
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        monkeypatch.setenv("PATH", str(own_venv_dir))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        path, is_own_venv = _resolve_mcp_binary("nx-mcp")

        assert path == str(own_venv_dir / "nx-mcp")
        assert is_own_venv is True

    def test_own_prefix_hit_still_probed_and_detail_says_so(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        own_venv_dir = tmp_path / "own-venv-bin"
        own_venv_dir.mkdir()
        _write_fake_binary(own_venv_dir / "nx-mcp", _HEALTHY_NEXUS_RESPONSE)
        _write_fake_binary(own_venv_dir / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        monkeypatch.setenv("PATH", str(own_venv_dir))
        monkeypatch.setattr("nexus.health.sys.prefix", str(own_venv_dir))

        results = _check_mcp_entry_points()

        for r in results:
            assert r.ok is True  # a real binary — still a REAL probe, never skipped
            assert "probing this process's own venv" in r.detail

    def test_absent_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        path, _is_own_venv = _resolve_mcp_binary("nx-mcp")
        assert path is None


class TestFirstLines:
    def test_bounds_line_count(self) -> None:
        text = "\n".join(f"line{i}" for i in range(10))
        assert _first_lines(text, 3).count(" | ") == 2

    def test_bounds_line_length(self) -> None:
        """Round-2 critique (IMPORTANT): a single arbitrarily long line
        (no newlines) must not blow out doctor output either."""
        huge_line = "x" * 5000
        result = _first_lines(huge_line, 3)
        assert len(result) <= 200
        assert result == "x" * 200


class TestDoctorCliMcpEntryPointComposition:
    """nexus-l2ku5 critique round 2 (SIGNIFICANT): unit tests prove
    _check_mcp_entry_points fails hard in isolation; test_doctor_cmd.py's
    CLI-level tests prove the assembly line works with the probe STUBBED.
    Neither proves the two compose — a REAL crashing binary reaching a
    REAL `nx doctor` CLI invocation end to end."""

    def test_real_crashing_binary_surfaces_as_doctor_exit_1(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_dir = tmp_path / "fakebin"
        fake_dir.mkdir()
        _write_fake_binary(fake_dir / "nx-mcp", _CRASHING_MODULE_NOT_FOUND)
        _write_fake_binary(fake_dir / "nx-mcp-catalog", _HEALTHY_CATALOG_RESPONSE)
        # Prepend so real PATH resolution (not a stub) finds these first;
        # the rest of the real PATH stays intact for git/rg/bd/npx.
        monkeypatch.setenv(
            "PATH", f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        )
        # HOME-scoping (nexus-k0lk9 sibling): with the real HOME, tmp_path
        # reads as a FOREIGN install and the resolver would walk past the
        # crashing fake to the host's real ~/.local/bin nx-mcp — coupling
        # this test to the host install's health. Root the fixture's HOME
        # at tmp_path so the fake is the legitimate under-home hit.
        monkeypatch.setenv("HOME", str(tmp_path))

        mock_reg = MagicMock()
        mock_reg.all.return_value = []
        runner = CliRunner()
        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.registry.RepoRegistry", return_value=mock_reg),
            # nexus-cw262: health.py's git-hooks check now calls
            # list_repos_dual_with_catalog_roots directly; the old
            # list_repos_dual wrapper is no longer on that path.
            patch(
                "nexus.repos.list_repos_dual_with_catalog_roots",
                side_effect=lambda **_: (list(mock_reg.all()), set(), "unknown"),
            ),
            patch("nexus.config.get_credential", return_value="sk-key"),
            # Unconditional service probe (critique finding 2, test_doctor_cmd.py) — stub it green.
            patch("nexus.db.http_vector_client._get", return_value=[]),
        ):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 1
        assert "MCP entry point (nx-mcp)" in result.output
        assert "✗" in result.output
        assert "ModuleNotFoundError" in result.output


class TestProbeMcpServer:
    """Direct tests of the lower-level probe used by both the doctor
    check and (per nexus-l2ku5 half 2) the fresh-install MVV gate."""

    def test_probe_succeeds_against_healthy_binary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        binary = tmp_path / "nx-mcp"
        _write_fake_binary(binary, _HEALTHY_NEXUS_RESPONSE)

        ok, detail = _probe_mcp_server(str(binary), "nexus")

        assert ok is True
        assert "nexus" in detail

    def test_probe_falsification_crashing_binary_never_reports_ok(
        self, tmp_path: Path
    ) -> None:
        """Non-vacuity: point the probe directly at the crashing fake and
        confirm it reports ok=False. If a future edit regressed the probe
        to treat a non-zero exit (or absent JSON-RPC response) as success,
        this assertion — not an inspection of the code — is what would
        catch it."""
        binary = tmp_path / "nx-mcp"
        _write_fake_binary(binary, _CRASHING_MODULE_NOT_FOUND)

        ok, detail = _probe_mcp_server(str(binary), "nexus")

        assert ok is False
        assert "ModuleNotFoundError" in detail

    def test_probe_timeout_reports_failure(self, tmp_path: Path) -> None:
        binary = tmp_path / "nx-mcp"
        _write_fake_binary(binary, "#!/bin/sh\nsleep 5\n")

        ok, detail = _probe_mcp_server(str(binary), "nexus", timeout=0.2)

        assert ok is False
        assert "timed out" in detail

    def test_probe_missing_binary_raises_oserror_handled(self, tmp_path: Path) -> None:
        ok, detail = _probe_mcp_server(str(tmp_path / "does-not-exist"), "nexus")

        assert ok is False
        assert "failed to spawn" in detail
