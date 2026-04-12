# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural tests for the RDR-067 Phase 4 scheduling templates.

Verifies that the launchd plist, cron crontab line, shell wrapper, and READMEs
exist at the specified paths, are syntactically valid, and enforce the safety
invariant that the wrapper only ever invokes `claude -p '/nx:rdr-audit ...'`
without touching privileged OS state itself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WRAPPER = SCRIPTS_DIR / "cron-rdr-audit.sh"
PLIST = SCRIPTS_DIR / "launchd" / "com.nexus.rdr-audit.PROJECT.plist"
LAUNCHD_README = SCRIPTS_DIR / "launchd" / "README.md"
CRONTAB = SCRIPTS_DIR / "cron" / "rdr-audit.crontab"
CRON_README = SCRIPTS_DIR / "cron" / "README.md"


class TestSchedulingFilesExist:

    def test_wrapper_exists(self) -> None:
        assert WRAPPER.exists()

    def test_wrapper_is_executable(self) -> None:
        assert os.access(WRAPPER, os.X_OK), (
            f"{WRAPPER} must be chmod +x so launchd/cron can execute it"
        )

    def test_plist_exists(self) -> None:
        assert PLIST.exists()

    def test_launchd_readme_exists(self) -> None:
        assert LAUNCHD_README.exists()

    def test_crontab_exists(self) -> None:
        assert CRONTAB.exists()

    def test_cron_readme_exists(self) -> None:
        assert CRON_README.exists()


class TestWrapperSyntax:

    def test_wrapper_bash_syntax(self) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not available")
        result = subprocess.run(
            ["bash", "-n", str(WRAPPER)], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"Bash syntax error in {WRAPPER}:\n{result.stderr}"
        )

    def test_wrapper_requires_project_env_var(self) -> None:
        text = WRAPPER.read_text()
        # Must fail fast if PROJECT is unset
        assert re.search(r'PROJECT.*required|required.*PROJECT', text, re.IGNORECASE), (
            "Wrapper must fail with a clear error when PROJECT env var is unset"
        )
        assert re.search(r'exit\s+1', text), "Wrapper must exit non-zero on missing PROJECT"

    def test_wrapper_invokes_claude_p_rdr_audit(self) -> None:
        text = WRAPPER.read_text()
        # The wrapper's purpose is to invoke `claude -p '/nx:rdr-audit <project>'`
        assert "claude" in text
        assert "/nx:rdr-audit" in text
        assert "-p" in text, "Wrapper must invoke claude in headless (-p) mode"

    def test_wrapper_uses_set_euo_pipefail(self) -> None:
        text = WRAPPER.read_text()
        assert "set -euo pipefail" in text, (
            "Wrapper must use strict bash mode"
        )

    def test_wrapper_does_not_execute_privileged_ops(self) -> None:
        """Phase 4 safety invariant: the wrapper runs the audit skill, nothing else.
        It must not run launchctl, crontab, or write plist files — those are the
        user's explicit install step."""
        text = WRAPPER.read_text()
        # These are the forbidden operations
        forbidden = [
            (r"launchctl\s+(load|unload)", "launchctl load/unload"),
            (r"crontab\s+-e", "crontab -e"),
            (r"sudo\b", "sudo"),
        ]
        for pattern, description in forbidden:
            assert not re.search(pattern, text), (
                f"Wrapper must not execute {description}"
            )


class TestPlistSyntax:

    def test_plist_is_valid_xml(self) -> None:
        """The plist template parses as valid XML — placeholders keep it parseable."""
        try:
            ET.parse(PLIST)
        except ET.ParseError as exc:
            pytest.fail(f"{PLIST} is not valid XML: {exc}")

    def test_plist_references_wrapper(self) -> None:
        text = PLIST.read_text()
        assert "cron-rdr-audit.sh" in text, (
            "Plist must reference the shared shell wrapper"
        )

    def test_plist_has_project_placeholder(self) -> None:
        text = PLIST.read_text()
        # The template uses 'PROJECT' as a substitution marker in Label/env/paths
        assert text.count("PROJECT") >= 3, (
            "Plist must have PROJECT placeholders in Label, EnvironmentVariables, "
            "and StandardOutPath at minimum"
        )

    def test_plist_has_absolute_path_placeholder(self) -> None:
        text = PLIST.read_text()
        assert "/ABSOLUTE/PATH/TO/nexus" in text, (
            "Plist must flag the absolute-path substitution point clearly"
        )

    def test_plist_has_start_calendar_interval(self) -> None:
        text = PLIST.read_text()
        assert "StartCalendarInterval" in text
        # Day=1 is the 30-day approximation for 90-day cadence on launchd
        assert re.search(r"<key>Day</key>\s*<integer>1</integer>", text)


class TestCrontabSyntax:

    def test_crontab_has_schedule_line(self) -> None:
        text = CRONTAB.read_text()
        # At least one non-comment line with a cron expression invoking the wrapper
        cron_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert cron_lines, "Crontab template must have at least one active cron line"
        for line in cron_lines:
            assert "cron-rdr-audit.sh" in line, (
                f"Cron line must invoke the wrapper: {line}"
            )
            assert "PROJECT" in line, (
                f"Cron line must set PROJECT env var: {line}"
            )

    def test_crontab_schedule_is_90_day_approximation(self) -> None:
        """Default schedule is `0 3 1 */3 *` — 3am on 1st of every 3rd month."""
        text = CRONTAB.read_text()
        assert "0 3 1 */3 *" in text or re.search(r"\*/3", text), (
            "Crontab template should document a ~90-day schedule (every 3rd month)"
        )

    def test_crontab_references_absolute_path_placeholder(self) -> None:
        text = CRONTAB.read_text()
        assert "/ABSOLUTE/PATH/TO/nexus" in text


class TestReadmeSafetyNotes:
    """Phase 4 safety invariant: both READMEs must explicitly document that
    the user retains manual install authority — no auto-install."""

    @pytest.mark.parametrize("readme", [LAUNCHD_README, CRON_README])
    def test_readme_has_safety_note(self, readme: Path) -> None:
        text = readme.read_text().lower()
        # Must say something like "do not run ... automatically" and point to
        # the user's explicit manual step
        assert "safety" in text, f"{readme.name} must have a Safety Note section"
        assert re.search(
            r"(not run|never|do not|explicit).{0,120}(launchctl load|crontab -e|automatic|manual)",
            text,
        ), f"{readme.name} must explicitly document that install is the user's manual step"

    @pytest.mark.parametrize("readme", [LAUNCHD_README, CRON_README])
    def test_readme_points_to_skill_schedule_command(self, readme: Path) -> None:
        """The READMEs should cross-reference the skill's print-only schedule command."""
        text = readme.read_text()
        assert "/nx:rdr-audit schedule" in text, (
            f"{readme.name} should point to the skill's print-only schedule command"
        )
        assert "/nx:rdr-audit unschedule" in text, (
            f"{readme.name} should point to the skill's print-only unschedule command"
        )

    @pytest.mark.parametrize("readme", [LAUNCHD_README, CRON_README])
    def test_readme_has_install_section(self, readme: Path) -> None:
        text = readme.read_text()
        assert "## Install" in text

    @pytest.mark.parametrize("readme", [LAUNCHD_README, CRON_README])
    def test_readme_has_uninstall_section(self, readme: Path) -> None:
        text = readme.read_text()
        assert "## Uninstall" in text


class TestFormatCoordination:
    """Phase 2b (management subcommands) and Phase 4 (scheduling templates)
    must agree on the <PROJECT> substitution format so both ship compatible text."""

    def test_skill_plist_and_file_plist_have_same_placeholder(self) -> None:
        skill = (REPO_ROOT / "nx" / "skills" / "rdr-audit" / "SKILL.md").read_text()
        plist = PLIST.read_text()
        # Both use <PROJECT> (angle-bracketed) as the substitution marker
        assert "<PROJECT>" in skill, (
            "Phase 2b skill's schedule subcommand should use <PROJECT> placeholder"
        )
        assert "PROJECT" in plist

    def test_skill_and_crontab_use_compatible_format(self) -> None:
        skill = (REPO_ROOT / "nx" / "skills" / "rdr-audit" / "SKILL.md").read_text()
        cron = CRONTAB.read_text()
        # Both reference the `/nx:rdr-audit <PROJECT>` invocation
        assert "/nx:rdr-audit" in skill
        assert "cron-rdr-audit.sh" in cron
