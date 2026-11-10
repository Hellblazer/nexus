"""The release skill's memory-curation step (RDR-238 Phase 3 Step 4, nexus-qo7ie.21).

It runs nx memory curate after Step 12, so the reinstalled nx carries the verb, and it never
blocks the release: a failure or a missing verb is reported and the release goes on.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "release"
    / "SKILL.md"
)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^##", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + (nxt.start() if nxt else len(text))]


def test_the_curation_step_follows_step_12_and_never_blocks() -> None:
    text = SKILL.read_text(encoding="utf-8")
    at = text.index("### 12a. ")
    assert (
        text.index("### 12. Reinstall local tool and verify")
        < at
        < text.index("## Plugin-only fast lane")
    ), "12a sits after Step 12 and before the plugin-only lane"
    step = _section(text, "### 12a. ")
    assert "nx memory curate --files --t2 --agents --cold || echo " in step, (
        "the step runs every section and tolerates a failure"
    )
    body = step.split("\n", 1)[1]  # the heading says it too; the pin holds the sentence
    assert "This step never blocks the release." in body
    assert "curation-report-" in step


def test_the_curation_step_runs_in_the_background_and_restates_the_stop() -> None:
    """Critic S5 (nexus-qo7ie.27): the run takes about 5 minutes and a foreground Bash call is cut at 10, so the step
    says to start it with run_in_background and to read the output after the notice; the 30-minute stop reads
    "if the notice has not come", and the report's stored title names --cold."""
    step = " ".join(_section(SKILL.read_text(encoding="utf-8"), "### 12a. ").split())  # line breaks are not the claim
    assert "Start the command with the Bash tool's `run_in_background`" in step
    assert "read the file after the completion notice" in step
    # the redirect wraps the whole group: appended to `cmd || echo`, it would cover only the echo
    assert ('{ nx memory curate --files --t2 --agents --cold || echo "curation step skipped (exit $?); '
            'the release goes on"; } > /tmp/curation-report.txt 2>&1') in step
    assert "Do not run it in the foreground" in step
    assert "If the notice has not come after 30 minutes, stop it" in step
    assert "never by pattern" in step
    assert "curation-report-<date>-files-t2-agents-cold" in step


def test_the_step_lists_and_the_plugin_only_lane_name_the_curation_step() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert re.search(r"11c, 12, 12a\.", text), (
        "the Steps that remain enumeration names 12a"
    )
    lane = _section(text, "## Plugin-only fast lane")
    assert lane.index("claude-restart-sessions") < lane.index(
        "After the restart, run Step 12a"
    ), "the plugin-only lane runs 12a too, after its restart"
