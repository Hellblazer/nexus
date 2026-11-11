"""The release skill's blocking upgrade rehearsal from the user's seat (nexus-f3yqd.5).

It runs after Step 3's version bump, so the gate's "both" starts resolve against the candidate,
and before Step 7 commits the release branch; a failure is a blocker, never waived.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _driver():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_skill", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_skill"] = module
    spec.loader.exec_module(module)
    return module


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


def test_the_step_states_the_pre_pci_ceiling_and_what_it_rests_on() -> None:
    step = _step()
    assert "`nx upgrade` from 7.76.0: 420 s" in step
    assert "209 to 211 s" in step and "460 to 662 s" in step
    assert "nexus-mjmjl" in step and "conexus 8.1.1" in step
    assert "twice the measurement" in step and "still fails" in step
    # an unset ceiling is still a hold for Sam, never a pass
    assert "Sam decides" in step and "does not go past 6e" in step


def test_the_step_is_honest_about_the_limits_and_the_basis() -> None:
    step = _step()
    assert "Sam's decision of record" in step and "(Sam 2026-11-10, halcyon)" in step
    assert "300 times its 0.4 s" in step and "a pure floor against a hang" in step
    assert "bound this fixture's size" in step
    # the figures of record: 8.0.0 12.7 s, 7.99.0 12.6 to 14.2 s, 7.76.0 209 to 211 s
    assert "12.7 s from 8.0.0" in step and "12.6 to 14.2 s from 7.99.0" in step
    assert "12 to 14 s from the other starts and 209 to 211 s from 7.76.0" in step
    assert "2.3 s" not in step and "660 s" not in step.replace("460 to 662 s", "")
    # the silence margin and the calibration host
    assert "silence limit leaves a margin of about 2" in step and "4.0 to 5.1 s" in step
    assert "420 s, calibrated on chas lab-m" in step


def test_the_step_scopes_sams_decision_to_the_seven_limits_he_accepted() -> None:
    step = _step()
    assert "Seven limits are Sam's decision of record" in step
    for gate_own in ("1800 s hang guard", "900 s PCI end wait", "50,000-row floor"):
        assert gate_own in step, gate_own
    assert "the ten probe errors" in step and "are the gate's own, not his" in step


def test_the_step_is_blocking_only_from_an_engine_pin_that_passes_the_pci_check() -> (
    None
):
    text = SKILL.read_text(encoding="utf-8")
    header = text[text.index("### 6e. ") :].splitlines()[0]
    assert "engine 0.1.187 or later" in header
    step = _step()
    assert "aspects-005-2" in step and "cannot be waived" in step
    assert "`--engine-jar PATH` is how to prove a fixed engine before its tag" in step
    # NEXUS_TARGET_RELEASE is the post-publish rerun, not a pre-tag gate
    assert "post-publish rerun, never the pre-tag gate" in step


def test_the_step_names_the_evidence_line_and_the_infrastructure_outcomes() -> None:
    step = _step()
    assert "FAILURE EVIDENCE PRESERVED: <dir>" in step
    assert "Copy those logs into the cut bead promptly" in step
    for outcome in ("`lab-remote` exit 98", "`lab-run` exit 137", "a dropped ssh"):
        assert outcome in step, outcome
    assert "rerun it" in step and "never run the script unwrapped" in step
    assert "the wrapper print its own `GATE: FAILED` and exit 1" in step


def test_every_driver_limit_is_in_the_step_text_and_sams_values_are_pinned() -> None:
    """A change to a limit in the driver goes red here until the skill and Sam's value agree."""
    ur = _driver()
    step = _step()
    limits = {
        "silence": (ur.DEFAULT_SILENCE_LIMIT_S, "prints nothing for {} s"),
        "newest-below": (
            ur.UPGRADE_CEILINGS_S["newest-below"],
            "`nx upgrade` from the newest release below the candidate: {} s",
        ),
        "last-7x": (
            ur.UPGRADE_CEILINGS_S["last-7x"],
            "`nx upgrade` from the last 7.x: {} s",
        ),
        "pre-pci": (ur.UPGRADE_CEILINGS_S["pre-pci"], "`nx upgrade` from 7.76.0: {} s"),
        "doctor": (ur.DOCTOR_CEILING_S, "`nx doctor` {} s"),
        "search": (ur.SEARCH_CEILING_S, "`nx search` {} s"),
        "package": (ur.PACKAGE_UPGRADE_CEILING_S, "package upgrade {} s"),
    }
    for name, (value, phrase) in limits.items():
        assert value is not None, name
        assert phrase.format(f"{value:g}") in step, (name, value, phrase)
    # Sam's accepted values, 2026-11-10 (relayed by halcyon)
    assert {name: value for name, (value, _) in limits.items()} == {
        "silence": 10.0,
        "newest-below": 120.0,
        "last-7x": 120.0,
        "pre-pci": 420.0,
        "doctor": 60.0,
        "search": 30.0,
        "package": 120.0,
    }


def test_the_step_tells_a_setup_failure_from_a_judged_breach_and_skips_the_fast_lane() -> (
    None
):
    step = _step()
    assert "A setup failure is not a judged breach" in step
    assert "`setup step ... failed`" in step and "A judged breach" in step
    assert "The plugin-only fast lane does not run it" in step
    assert "are the base tag's, apart from the version" in step
    assert "tree has no gate" not in step, (
        "the old reason decays: a later tag has the gate"
    )


def test_the_step_keeps_the_pci_keep_check_and_the_write_rate() -> None:
    step = _step()
    assert "PCI keep check" in step and "`--engine-jar PATH`" in step
    assert "DURING `nx upgrade`" in step and "second shell" in step
    assert (
        "never seeing a build in flight during `nx upgrade` is a failure".lower()
        in step.lower()
    )
    assert "MB/s" in step and "S*1024*r/MBps" in step


def test_the_step_matches_the_wrapper_on_what_exits_1_and_what_exits_2() -> None:
    step = _step()
    # a wheel build that fails is the wrapper's FAILED exit 1; no usable Python is its NOT RUN exit 2
    assert "or a wheel build that failed" in step
    assert (
        "no usable Python" in step
        and "each prints its own `GATE: NOT RUN` line" in step
    )
    # any non-default option counts, named by the rule and not by a partial list
    assert "some option was set to other than its default" in step
    assert "`--work`, the target, `--json-out` and `--keep-work` do not count" in step


def test_the_step_states_the_hang_guard_and_holds_a_pending_not_run_for_sams_call_once() -> (
    None
):
    step = _step()
    assert "1800 s hang guard" in step and "never makes a pass" in step
    assert step.count("Sam decides") == 1, "said once, where the pending ceiling is"
    assert "while the 7.76.0 ceiling is pending" not in step
