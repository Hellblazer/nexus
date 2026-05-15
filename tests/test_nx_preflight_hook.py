# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hwbj (GH #619): preflight hook for SessionStart.

Contract:
- When ``nx --version`` succeeds, emit nothing (silent on healthy
  hosts; the existing SessionStart chain runs unchanged).
- When ``nx --version`` is missing or hangs, emit a loud
  "## nx Preflight: FAILED" marker so the model knows the
  using-nx-skills routing is unsafe in this session.
- Always exit 0; preflight must never block the session.
- bd missing alone is informational (bd is optional); only nx
  unreachable triggers the FAILED marker.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PREFLIGHT = (
    Path(__file__).resolve().parent.parent
    / "nx" / "hooks" / "scripts" / "preflight.py"
)


def _run_preflight(env_path: str | None = None) -> tuple[int, str]:
    """Run the preflight script under the current python with the
    given PATH (or unmodified if None). Returns (exit_code, stdout).
    """
    import os
    env = os.environ.copy()
    if env_path is not None:
        env["PATH"] = env_path
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        capture_output=True, text=True, timeout=20, env=env,
    )
    return result.returncode, result.stdout


class TestPreflightExists:
    def test_script_present_and_executable(self) -> None:
        """The hook config references this exact path; if the script
        moves or vanishes, SessionStart breaks silently in prod.
        """
        assert PREFLIGHT.exists(), (
            f"preflight.py must live at {PREFLIGHT} so the "
            f"SessionStart hook config in nx/hooks/hooks.json "
            f"can find it."
        )
        # Executable bit not strictly required (the hook runs it via
        # _run_python_hook.sh which execs python explicitly), but
        # convenient for direct invocation.
        # We don't assert chmod here (CI git-checkout may strip the
        # bit on Windows runners).


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
            f"preflight must be silent when nx works; got stdout: "
            f"{out[:200]!r}"
        )


class TestPreflightDegraded:
    """When nx is unreachable, the marker block must:
    - lead with ``## nx Preflight: FAILED`` so the model can match it,
    - explicitly tell the model the using-nx-skills routing is
      INACTIVE,
    - name the missing tool by name so the operator knows what to
      install,
    - exit 0 (never block the session).
    """

    def test_emits_failed_marker_when_nx_missing(self, tmp_path: Path) -> None:
        rc, out = _run_preflight(env_path="/nonexistent")
        # /nonexistent has no python either, but the hook is run by
        # _run_python_hook.sh in prod, which finds python via its own
        # PATH probe. Here we use sys.executable directly so python
        # resolution is independent of the env PATH.
        # Use a path that has python but not nx:
        # ``/usr/bin`` typically has python3 (POSIX) but no nx
        # unless the user installed it system-wide.
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
            kw in out for kw in ("brew install", "apt install", "winget install", "https://astral.sh/uv")
        ), f"install hint missing from FAILED marker:\n{out}"
        assert "Restart Claude Code" in out, (
            "marker must tell operator to restart Claude Code so the "
            "newly-installed tool lands on PATH"
        )


class TestHookConfigWiresPreflightEarly:
    """Preflight must run AFTER the ``nx upgrade --auto`` self-
    upgrade (test_phase5_integration.TestHooksJson asserts that
    upgrade is the first hook, since stale-conexus version
    handling is a hard prereq for everything else) but BEFORE the
    cat-of-using-nx-skills step. Position 2 keeps the FAILED
    marker (if any) above the 600-line capability dump and the
    routing skill so the model sees the counter-signal first.
    """

    def test_hook_config_references_preflight_second(self) -> None:
        cfg = (
            Path(__file__).resolve().parent.parent
            / "nx" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        # Flatten all SessionStart matcher groups: y0nb prepended an orb_bridge
        # entry at index 0; the originals (nx upgrade, preflight, using-nx-skills)
        # live in subsequent matcher groups.
        sessionstart = [
            h for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]
            if "orb_bridge_" not in h["command"]
        ]
        # Position 0 is `nx upgrade --auto` (existing contract per
        # tests/test_phase5_integration.py::TestHooksJson::
        # test_upgrade_auto_is_first_session_start_hook).
        assert "nx upgrade --auto" in sessionstart[0]["command"]
        # Position 1 must be the preflight so the FAILED marker
        # lands above the capability dump and the using-nx-skills
        # routing.
        assert "preflight.py" in sessionstart[1]["command"], (
            f"preflight must be the SECOND SessionStart hook (right "
            f"after `nx upgrade --auto`). Got hook[1]: "
            f"{sessionstart[1]['command']!r}"
        )

    def test_hook_config_preflight_runs_before_using_nx_skills_cat(self) -> None:
        """The cat of using-nx-skills SKILL.md must come AFTER the
        preflight so the FAILED counter-signal appears above the
        routing it counters.
        """
        cfg = (
            Path(__file__).resolve().parent.parent
            / "nx" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        # Flatten all SessionStart matcher groups: y0nb prepended an orb_bridge
        # entry at index 0; the originals (nx upgrade, preflight, using-nx-skills)
        # live in subsequent matcher groups.
        sessionstart = [
            h for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]
            if "orb_bridge_" not in h["command"]
        ]
        cmds = [h["command"] for h in sessionstart]
        preflight_idx = next(
            (i for i, c in enumerate(cmds) if "preflight.py" in c), -1,
        )
        skill_idx = next(
            (i for i, c in enumerate(cmds) if "using-nx-skills" in c), -1,
        )
        assert preflight_idx >= 0, "preflight hook missing"
        assert skill_idx >= 0, "using-nx-skills cat hook missing"
        assert preflight_idx < skill_idx, (
            f"preflight (index {preflight_idx}) must run before "
            f"using-nx-skills cat (index {skill_idx}); otherwise "
            f"the FAILED marker lands AFTER the routing it "
            f"counters and the model sees the routing first."
        )

    def test_hook_config_still_cats_using_nx_skills(self) -> None:
        """The using-nx-skills routing is still injected; the
        preflight FAILED marker is a counter-signal, not a
        replacement. Test guards against accidental removal of the
        cat hook.
        """
        cfg = (
            Path(__file__).resolve().parent.parent
            / "nx" / "hooks" / "hooks.json"
        )
        data = json.loads(cfg.read_text())
        # Flatten all SessionStart matcher groups: y0nb prepended an orb_bridge
        # entry at index 0; the originals (nx upgrade, preflight, using-nx-skills)
        # live in subsequent matcher groups.
        sessionstart = [
            h for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]
            if "orb_bridge_" not in h["command"]
        ]
        commands = " ".join(h["command"] for h in sessionstart)
        assert "using-nx-skills" in commands
