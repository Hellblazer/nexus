# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for the ``preflight`` hook verb (RDR-215 bead nexus-q02nx.21).

Carries what ``conexus/hooks/scripts/preflight.py`` used to be tested for.
That script was the pre-RDR-215 implementation, and while it was still wired
in ``hooks.json`` every assertion here ran against both copies through a dual
``impl`` fixture. ``hooks.json``'s SessionStart entry now names
``nx-hook preflight``, the script has been deleted, and this verb is the only
implementation left -- so the fixture is gone with it.

``tests/test_nx_preflight_hook.py`` keeps the other half: that the hook is
wired SECOND, above the guidance emission it counter-signals.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.hooks import preflight_verb

#: A child process, not an in-process call: these tests vary PATH per case,
#: and ``os.environ`` is process-global.
_PY_DRIVER = """
import sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import preflight_verb as _hook

result = never_fail(lambda: _hook.run(None), "preflight")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(0)
"""


def _run_preflight(env_path: str | None = None) -> tuple[int, str]:
    """Run the active implementation under *env_path* (or the current PATH
    if ``None``). Returns ``(exit_code, stdout)``.
    """
    env = os.environ.copy()
    if env_path is not None:
        env["PATH"] = env_path
    result = subprocess.run(
        [sys.executable, "-c", _PY_DRIVER],
        capture_output=True, text=True, timeout=20, env=env,
    )
    return result.returncode, result.stdout


class TestPreflightVerbExists:
    def test_module_present(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[2]
            / "src" / "nexus" / "hooks" / "preflight_verb.py"
        )
        assert module_path.exists()


class TestPreflightHealthy:
    def test_silent_when_nx_works(self) -> None:
        """On a host where ``nx --version`` works, preflight emits
        nothing. Reverting the silence-when-healthy guard would emit
        the FAILED marker on every session and break the existing
        macOS/Linux flow.
        """
        if shutil.which("nx") is None:
            pytest.skip("nx not on PATH on this host; healthy-path test n/a")
        rc, out = _run_preflight()
        assert rc == 0
        assert out == "" or out.strip() == "", (
            f"preflight must be silent when nx works; got stdout: {out[:200]!r}"
        )


class TestPreflightDegraded:
    """When nx is unreachable, the marker block must:
    - lead with ``## nx Preflight: FAILED`` so the model can match it,
    - explicitly tell the model the using-nx-skills routing is INACTIVE,
    - name the missing tool by name so the operator knows what to install,
    - exit 0 (never block the session).
    """

    def test_emits_failed_marker_when_nx_missing(self) -> None:
        if shutil.which("nx", path="/usr/bin") is not None:
            pytest.skip("nx is installed at /usr/bin on this host")
        rc, out = _run_preflight(env_path="/usr/bin")
        assert rc == 0, "preflight must always exit 0"
        assert "## nx Preflight: FAILED" in out, (
            f"FAILED marker missing from output:\n{out}"
        )
        assert "INACTIVE" in out, (
            "marker must explicitly tell the model the routing is "
            "INACTIVE so it knows to skip the skills"
        )
        assert "nx (conexus CLI)" in out
        # Per-OS install hint must be present (one of brew/apt/winget).
        assert any(
            kw in out
            for kw in ("brew install", "apt install", "winget install", "https://astral.sh/uv")
        ), f"install hint missing from FAILED marker:\n{out}"
        assert "Restart Claude Code" in out, (
            "marker must tell operator to restart Claude Code so the "
            "newly-installed tool lands on PATH"
        )


class TestPreflightVerbDoesNotReadStdin:
    """The original script never reads stdin at all; the ported verb must
    not either -- ``nexus._hook_runtime.entry.main`` already reads (or
    skips) the payload before dispatch, so a verb that reads it again would
    double-consume a stream that can only be read once.
    """

    def test_runs_with_no_stdin_attached(self) -> None:
        """A closed/empty stdin must not hang or crash the verb."""
        result = subprocess.run(
            [sys.executable, "-c", _PY_DRIVER],
            input="", capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0


class TestPreflightVerbDirect:
    """Exercises :func:`nexus.hooks.preflight_verb.run` in-process, independent
    of the subprocess harness above.
    """

    def test_ignores_payload_argument(self) -> None:
        # A non-None payload must not change behavior: this verb has no
        # stdin contract at all, unlike session-start.
        result_with_payload = preflight_verb.run({"session_id": "whatever"})
        result_without = preflight_verb.run(None)
        assert result_with_payload.stdout == result_without.stdout




class TestWindowsSkipsTheRedundantNxSpawn:
    """nexus-3cydr: on Windows the nx probe re-imported the CLI in this very
    venv (nx_argv uses sys.executable -m nexus.cli) and cost 2-7 s on a fresh
    install, cancelling the 5 s hook. The Windows arm runs here on any host."""

    @staticmethod
    def _wire(monkeypatch, which: dict[str, str | None], hook_dir: str) -> list[list[str]]:
        from nexus.hooks import preflight_verb as pv

        probed: list[list[str]] = []

        def fake_probe(name: str, args: list[str], timeout: float = 3.0):
            probed.append(list(args))
            return pv._ToolStatus(name=name, available=True, detail="probed", install_hint="")

        monkeypatch.setattr(pv, "_platform", lambda: "win32")
        monkeypatch.setattr(pv, "_which", lambda cmd: which.get(cmd))
        monkeypatch.setattr(pv, "_hook_dir", lambda: hook_dir)
        monkeypatch.setattr(pv, "_probe", fake_probe)
        return probed

    def test_nx_beside_the_hook_is_not_spawned(self, monkeypatch) -> None:
        from nexus.hooks import preflight_verb as pv

        probed = self._wire(monkeypatch, {"nx": r"C:\u\.local\bin\nx.exe"}, r"C:\u\.local\bin")
        assert pv.run(None).stdout is None
        assert all(a[0] not in (r"C:\u\.local\bin\nx.exe",) and "nexus.cli" not in a for a in probed), probed
        assert probed == [["bd", "version"]]

    def test_nx_missing_from_path_is_reported_without_a_spawn(self, monkeypatch) -> None:
        from nexus.hooks import preflight_verb as pv

        probed = self._wire(monkeypatch, {}, r"C:\u\.local\bin")
        out = pv.run(None).stdout
        assert out is not None and "## nx Preflight: FAILED" in out and "not on PATH" in out
        assert probed == [["bd", "version"]]

    def test_a_foreign_nx_on_path_is_probed_itself(self, monkeypatch) -> None:
        from nexus.hooks import preflight_verb as pv

        probed = self._wire(monkeypatch, {"nx": r"D:\other\bin\nx.exe"}, r"C:\u\.local\bin")
        assert pv.run(None).stdout is None
        assert probed[0] == [r"D:\other\bin\nx.exe", "--version"]

    def test_posix_keeps_the_nx_argv_probe(self, monkeypatch) -> None:
        from nexus.hooks import preflight_verb as pv

        probed = self._wire(monkeypatch, {"nx": "/u/.local/bin/nx"}, "/u/.local/bin")
        monkeypatch.setattr(pv, "_platform", lambda: "darwin")
        pv.run(None)
        assert probed[0][-1] == "--version" and len(probed) == 2
