"""The release skill's blocking upgrade rehearsal from the user's seat (nexus-f3yqd.5).

It runs after Step 3's version bump, so the gate's "both" starts resolve against the candidate,
and before Step 7 commits the release branch; a failure is a blocker, never waived.
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
    nxt = re.search(r"^### ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + (nxt.start() if nxt else len(text))]


def test_the_rehearsal_sits_after_the_version_bump_and_before_the_release_branch() -> (
    None
):
    text = SKILL.read_text(encoding="utf-8")
    at = text.index("### 6e. Upgrade rehearsal from the user's seat")
    assert (
        text.index("### 3. Bump version")
        < at
        < text.index("### 7. Commit on a release branch")
    )
    assert re.search(r"6d, 6e, 7,", text), "the Steps that remain enumeration names 6e"


def test_the_step_runs_the_gate_on_a_lab_host_and_blocks() -> None:
    step = " ".join(
        _section(SKILL.read_text(encoding="utf-8"), "### 6e. ").split()
    )  # prose wraps
    assert "lab-remote -c lab-m -- tests/e2e/upgrade-rehearsal.sh" in step
    assert "UPGRADE REHEARSAL PASSED" in step and "UPGRADE REHEARSAL FAILED" in step
    assert "A failure is a release blocker, never waived here" in step
    assert "from all three starts" in step
    assert "three previous releases" in step and "7.76.0" in step
    assert "PCI keep check" in step and "`--engine-jar PATH`" in step
    assert "DURING `nx upgrade`" in step and "second shell" in step
    assert (
        "never seeing a build in flight during `nx upgrade` is a failure".lower()
        in step.lower()
    )
    assert "MB/s" in step and "S*1024*r/MBps" in step
