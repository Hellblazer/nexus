# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine-release skill must not lose track of the RDR-194 D4 cloud-
count-5 delivery gate (nexus-tk070.p5a substantive-critic CRITICAL, T2
[22965]).

Same class of guard as ``tests/test_engine_release_skill_parity.py``'s
``test_the_pin_currency_gate_exists_and_is_wired_into_the_release_workflow``
(the "skill mentions the gate script it depends on, with a runnable
command, and the script actually defines the functions the skill's prose
claims it has" pattern) — a gate that only lives in one file (the skill, or
the script) and drifts from the other reintroduces exactly the eyeball-
check failure class ``check_engine_release_floor.py``'s own module
docstring cites (nexus-i5c2u): a checklist step that silently stops being
true.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"
GATE_SCRIPT = REPO_ROOT / "scripts" / "check_rdr194_cc5_delivery_gate.py"


def test_both_files_exist() -> None:
    assert SKILL.is_file(), f"skill moved: {SKILL}"
    assert GATE_SCRIPT.is_file(), f"gate script moved: {GATE_SCRIPT}"


def test_skill_names_the_gate_script_as_a_runnable_command() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "check_rdr194_cc5_delivery_gate.py" in text, (
        "engine-release skill no longer mentions the RDR-194 D4 cloud-count-5 "
        "delivery gate -- a gate the checklist does not name is a gate that "
        "does not get run (the exact nexus-i5c2u failure class this skill's "
        "own pin-currency wiring test already guards for check_engine_release_floor.py)."
    )
    # Not merely NAMED -- given as a runnable command inside a fenced block,
    # same "prose is not enough" standard test_engine_release_skill_parity.py
    # applies to --shakeout/--acquire.
    assert "uv run python scripts/check_rdr194_cc5_delivery_gate.py" in text, (
        "the gate is mentioned but not given as a runnable command in a "
        "fenced code block -- a step that degrades into prose nobody "
        "executes is the exact failure mode this pin exists to catch."
    )


def test_skill_step_is_placed_before_the_tag_push_step() -> None:
    """The gate is pre-tag by design (RDR-194 D4: cc5 must be recorded
    before the engine tag carrying taxonomy-014 is CUT, not merely before
    it deploys) -- it must appear before the tag-push step in the skill's
    own ordering, not after."""
    text = SKILL.read_text(encoding="utf-8")
    gate_pos = text.find("check_rdr194_cc5_delivery_gate.py")
    push_pos = text.find("Push the tag")
    assert gate_pos != -1 and push_pos != -1
    assert gate_pos < push_pos, (
        "the cc5 delivery gate must be documented BEFORE the tag-push step "
        "-- it is a pre-tag gate (blocks the cut), not a pre-deploy one."
    )


def test_gate_script_defines_the_functions_the_skill_implies_it_has() -> None:
    """Mirrors test_the_pin_currency_gate_exists_and_is_wired_into_the_release_workflow's
    'the gate script actually implements what is being wired in' half."""
    src = GATE_SCRIPT.read_text(encoding="utf-8")
    assert "def run_gate" in src
    assert "def file_present_at_ref" in src
    assert "def fetch_cc5_record" in src
    assert "def validate_measured_record" in src


def test_gate_script_names_its_own_target_file_and_bead() -> None:
    src = GATE_SCRIPT.read_text(encoding="utf-8")
    assert "taxonomy-014-topics-tenant-unique.xml" in src
    assert "nexus-tk070.cc5" in src
    assert "nexus-tk070.p7" in src
