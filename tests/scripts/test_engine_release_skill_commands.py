# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine-release skill's cut-prep commands must expand and agree with the scripts they run.

nexus-9a6io fix round: the skill once told the operator to run a command carrying a variable
that lives inside a script and expands to the empty string in the operator's shell, so the
command as written never acknowledged anything. (The published-client gate that case was about
was deleted in cleanup step 11, nexus-0r1uz; the cases that still apply to the cloud gate and
the relay text remain.)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = (REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text()


def test_step_6_1_asserts_the_live_ownerless_mode_in_both_postures() -> None:
    section = SKILL.split("### 6.1.")[1].split("### 7.")[0]
    assert "NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only" in section
    assert "NX_EXPECTED_OWNERLESS_WRITE_MODE=enforce" in section
    gate = (REPO_ROOT / "tests" / "e2e" / "cloud-client-path-gate.sh").read_text()
    assert "NX_EXPECTED_OWNERLESS_WRITE_MODE" in gate and "ownerless_write_mode" in gate


def test_the_relay_text_asks_for_the_image_smoke_with_the_production_mode() -> None:
    assert "image-smoke.sh" in SKILL
    assert "production `NX_OWNERLESS_WRITE_MODE` value" in SKILL


def test_the_size_ceilings_are_named_as_blocking_at_promotion() -> None:
    section = SKILL.split("### 5c.")[1].split("### 6.")[0]
    assert "promote_engine_release.sh" in section
    assert "BLOCKING" in section


def test_the_relay_does_not_claim_image_smoke_catches_a_valid_wrong_mode() -> None:
    """image-smoke booting with the production value catches only an INVALID value; the first
    deploy's relay must carry a /v1/status log-only assertion before the push."""
    assert "not its own default, so a mis-wired parameter fails before the push" not in SKILL
    assert "catches only an INVALID value" in SKILL
    assert "`ownerless_write_mode` must equal `log-only`" in SKILL
    runbook = (REPO_ROOT / "docs" / "operations" / "ownerless-write-cutover.md").read_text()
    assert "catches only an INVALID value" in runbook
    assert "must equal `log-only`" in runbook
    assert "must boot the image with the production value so a bad value fails before" not in runbook


def test_unverified_conexus_side_facts_are_marked_unverified_in_the_runbook() -> None:
    runbook = (REPO_ROOT / "docs" / "operations" / "ownerless-write-cutover.md").read_text()
    assert "the CloudWatch log group\n   is unverified" in runbook or "CloudWatch log group\n   is unverified" in runbook
    assert "share one egress" not in runbook
    assert "is **unverified**" in runbook


def test_the_enforce_flip_is_a_named_bead_that_follows_the_cut() -> None:
    """Critique S2: nexus-z0o2p.40 is named where a cut reader would otherwise wait on, or sign off
    without, the flip."""
    s61 = SKILL.split("### 6.1.")[1].split("### 7.")[0]
    assert "nexus-z0o2p.40" in s61 and "FOLLOWS the cut" in s61 and "NOT a Step 6.1 or Step 7 precondition" in s61
    s7 = SKILL.split("### 7.")[1].split("### 8.")[0]
    assert "nexus-z0o2p.40" in s7 and "follows the cut" in s7
    runbook = (REPO_ROOT / "docs" / "operations" / "ownerless-write-cutover.md").read_text()
    assert "nexus-z0o2p.40" in runbook and "FOLLOWS the\n   cut" in runbook


def test_the_pre_push_mode_assertion_is_named_as_conexus_owned_in_every_place_that_states_it() -> None:
    """Critique S1: no nexus code reads the mode before the push, so every document that states the
    assertion must say it is conexus's hold-the-push line."""
    runbook = (REPO_ROOT / "docs" / "operations" / "ownerless-write-cutover.md").read_text()
    for name, text in (("skill", SKILL), ("runbook", " ".join(runbook.split()))):
        assert "hold-the-push line" in text, name
        assert "nothing in this repo checks it" in text, name
    # the gate's own reader really does not know the field: the claim above is checkable
    floor = (REPO_ROOT / "scripts" / "check_engine_release_floor.py").read_text()
    assert "ownerless_write_mode" not in floor


def test_standing_instructions_that_run_the_cloud_gate_name_the_mode_variable() -> None:
    """Critique round 3 L1: run plain, the gate FAILS leg B3 on any engine that reports a mode, so
    every instruction that tells an operator to run it must say what to set."""
    for rel in ("AGENTS.md", "docs/contributing.md", "docs/runbooks/rdr-191-phase5-cloud-fk.md"):
        text = (REPO_ROOT / rel).read_text()
        assert "cloud-client-path-gate.sh" in text, rel
        for m in re.finditer(r"cloud-client-path-gate\.sh", text):
            window = text[max(0, m.start() - 700): m.end() + 900]
            if "NX_EXPECTED_OWNERLESS_WRITE_MODE" in window:
                break
        else:
            raise AssertionError(f"{rel} runs the cloud gate without naming NX_EXPECTED_OWNERLESS_WRITE_MODE nearby")
    assert "ONLY an engine from before the refusal" in SKILL.split("### 6.1.")[1].split("### 7.")[0]
