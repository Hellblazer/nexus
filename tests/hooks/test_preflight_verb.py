# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for the ``preflight`` hook verb (RDR-215 bead nexus-q02nx.21).

Ports ``conexus/hooks/scripts/preflight.py``'s coverage
(``tests/test_nx_preflight_hook.py``) onto :func:`nexus.hooks.preflight_verb.run`,
driving both implementations per assertion via the dual ``impl`` fixture
pattern used in ``tests/hooks/test_post_compact_hook.py``: the original
script stays wired in ``conexus/hooks/hooks.json`` until bead nexus-q02nx.21/.22
re-declares that SessionStart entry, so every assertion here is a
differential against what production still runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.hooks import preflight_verb

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "preflight.py"
)

#: Which implementation :func:`_run_preflight` drives, set per-test by `impl`.
_IMPL = "script"


@pytest.fixture(params=["script", "python"], autouse=True)
def impl(request):
    """Run every assertion against BOTH implementations.

    Drop the "script" param once bead nexus-q02nx.21/.22 re-points the
    ``hooks.json`` SessionStart entry at the ``nx-hook preflight`` verb and
    the plugin script is deleted.
    """
    global _IMPL
    _IMPL = request.param
    yield request.param
    _IMPL = "script"


#: A child process, not an in-process call: these tests vary PATH per case,
#: and ``os.environ`` is process-global.
_PY_DRIVER = """
import sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import preflight_verb as _hook

result = never_fail(lambda: _hook.run(None), "preflight")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""


def _run_preflight(env_path: str | None = None) -> tuple[int, str]:
    """Run the active implementation under *env_path* (or the current PATH
    if ``None``). Returns ``(exit_code, stdout)``.
    """
    env = os.environ.copy()
    if env_path is not None:
        env["PATH"] = env_path
    argv = (
        [sys.executable, "-c", _PY_DRIVER]
        if _IMPL == "python"
        else [sys.executable, str(SCRIPT)]
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=20, env=env,
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
        """A closed/empty stdin must not hang or crash either implementation."""
        argv = (
            [sys.executable, "-c", _PY_DRIVER]
            if _IMPL == "python"
            else [sys.executable, str(SCRIPT)]
        )
        result = subprocess.run(
            argv, input="", capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0


class TestPreflightVerbDirect:
    """Exercises :func:`nexus.hooks.preflight_verb.run` in-process, independent
    of the dual-drive subprocess harness above.
    """

    def test_ignores_payload_argument(self) -> None:
        # A non-None payload must not change behavior: this verb has no
        # stdin contract at all, unlike session-start.
        result_with_payload = preflight_verb.run({"session_id": "whatever"})
        result_without = preflight_verb.run(None)
        assert result_with_payload.stdout == result_without.stdout

    def test_exit_code_always_zero(self) -> None:
        result = preflight_verb.run(None)
        assert result.exit_code == 0


class TestInstallHintParity:
    """The two copies of the install hint cannot drift apart while both exist.

    ``_install_hint`` is the one string a user reads when ``nx`` is missing,
    so it is the one string that has to be right, and it is written out twice
    -- here and in the plugin script this verb ports. The assertions above are
    keyword-loose by design ("brew install", "winget install"), which is
    exactly the shape that would let one copy gain ``--python 3.12``
    (nexus-sa187) while the other kept telling a user on a 3.14 distro to run
    a command that cannot resolve. This pins them equal instead.

    It goes away with the script: ``hooks.json``'s SessionStart entry already
    names the ``nx-hook preflight`` verb, so nothing wires the plugin copy any
    more, and this module's own opening docstring records deletion as that
    port's remaining half.
    """

    @staticmethod
    def _script_module():
        import importlib.util

        spec = importlib.util.spec_from_file_location("_preflight_script", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolves a class's module through sys.modules while the
        # decorator runs, so the script's own _ToolStatus cannot be built from
        # an unregistered module.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    @pytest.mark.parametrize("tool", ["nx", "bd"])
    def test_hints_are_identical(self, platform: str, tool: str, monkeypatch) -> None:
        script = self._script_module()
        monkeypatch.setattr(sys, "platform", platform)
        script_hint = script._install_hint(tool)
        verb_hint = preflight_verb._install_hint(tool)
        assert script_hint, f"{tool}/{platform}: the script copy has no hint at all"
        assert script_hint == verb_hint, (
            f"{tool}/{platform}: the plugin script says {script_hint!r}, the "
            f"nx-hook verb says {verb_hint!r}. One copy was edited and the "
            "other was not."
        )
