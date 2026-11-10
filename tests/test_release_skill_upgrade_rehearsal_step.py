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


def _step() -> str:
    return " ".join(
        _section(SKILL.read_text(encoding="utf-8"), "### 6e. ").split()
    )  # prose wraps


def test_the_step_runs_the_gate_on_a_lab_host_in_the_background() -> None:
    step = _step()
    assert "lab-remote -c lab-m -- tests/e2e/upgrade-rehearsal.sh" in step
    assert "run_in_background" in step and "outruns a 10-minute foreground call" in step
    assert "two or three previous releases" in step and "7.76.0" in step
    assert "wall time" in step.lower() and "50 to 75 minutes" in step
    assert "about 13 minutes" in step and "about 20 minutes" in step
    assert "900 s" in step


def test_the_step_names_the_gate_line_and_the_exit_codes() -> None:
    step = _step()
    assert (
        "UPGRADE REHEARSAL GATE: <word>" in step
        and "the only one the wrapper reads" in step
    )
    for word in ("`PASSED`", "`FAILED`", "`NOT RUN`", "`NOT THE RELEASE GATE`"):
        assert word in step, word
    assert "exit 0" in step and "exit 1" in step and "exit 2" in step
    assert "Only exit 0 clears 6e" in step
    assert "A FAILED is a release blocker, never waived here" in step
    assert "NOT RUN and NOT THE RELEASE GATE are not passes" in step


def test_the_step_states_the_pending_ceiling_and_who_owns_and_decides_it() -> None:
    step = _step()
    assert "**PENDING**" in step
    assert "nexus-f3yqd.5 owns the number" in step
    assert "nexus-mjmjl" in step and "conexus 8.1.1" in step
    assert "no guessed value stands in" in step
    assert "Sam decides" in step and "does not go past 6e" in step
    # the measured figures the pending ceiling rests on
    assert "460 to 662 s" in step and "211 s" in step


def test_the_step_is_honest_about_the_limits_and_the_basis() -> None:
    step = _step()
    assert "our chosen reading of Sam's" in step and "not a number he gave" in step
    assert "300 times its 0.4 s" in step and "floors against a hang" in step
    assert "bound this fixture's size" in step


def test_the_step_tells_a_setup_failure_from_a_judged_breach_and_skips_the_fast_lane() -> (
    None
):
    step = _step()
    assert "A setup failure is not a judged breach" in step
    assert "`setup step ... failed`" in step and "A judged breach" in step
    assert "The plugin-only fast lane does not run it" in step
    assert "no gate" in step


def test_the_step_keeps_the_pci_keep_check_and_the_write_rate() -> None:
    step = _step()
    assert "PCI keep check" in step and "`--engine-jar PATH`" in step
    assert "DURING `nx upgrade`" in step and "second shell" in step
    assert (
        "never seeing a build in flight during `nx upgrade` is a failure".lower()
        in step.lower()
    )
    assert "MB/s" in step and "S*1024*r/MBps" in step
