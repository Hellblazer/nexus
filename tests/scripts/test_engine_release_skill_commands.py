# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine-release skill's cut-prep commands must expand and agree with the scripts they run.

nexus-9a6io / nexus-k9fs1 fix round: the skill once told the operator to run
``NX_EXPECTED_CLIENT_LAG="$EXPECTED_LAG_BEAD" ...``, a variable that lives inside the
script and expands to the empty string in the operator's shell, so the command as
written never acknowledged anything.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = (REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text()
GATE = (REPO_ROOT / "tests" / "e2e" / "published-client-write-gate.sh").read_text()


def _script_const(name: str) -> str:
    m = re.search(rf'^{name}="([^"]*)"$', GATE, re.MULTILINE)
    assert m, f"{name} not found in published-client-write-gate.sh"
    return m.group(1)


def test_the_ack_command_names_the_scripts_bead_literally() -> None:
    bead = _script_const("EXPECTED_LAG_BEAD")
    assert f"NX_EXPECTED_CLIENT_LAG={bead} " in SKILL
    # The unexpanded form must not be in a command line (prose may name the variable).
    assert 'NX_EXPECTED_CLIENT_LAG="$EXPECTED_LAG_BEAD"' not in SKILL


def test_the_fork_walk_handoff_carries_its_conditions() -> None:
    """Step 6a pins the script to the tag, uses plain python3, names the log source,
    and spells out schema three times, walk twice."""
    section = SKILL.split("### 6a.")[1].split("### 6.1.")[0]
    assert "schema three times, walk twice" in section
    assert "tagged commit" in section
    assert "stdlib-only" in section and "uv run python scripts/check_pitr_fork_walk.py" not in section
    assert "CloudWatch" in section
    assert "NX_DB_USER" in section and "NX_DB_ADMIN_USER" in section
    # three schema invocations, two walks
    assert section.count("check_pitr_fork_walk.py schema") == 3
    assert section.count("check_pitr_fork_walk.py walk") == 2
    assert "--save-settings" in section and section.count("--compare-settings") >= 2


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


def test_6a_walk_1_is_pinned_to_new_changesets_and_walk_2_is_a_noop() -> None:
    """The checker refuses a walk given neither --expect-new nor --noop (exit 2); the commands
    the skill hands conexus must therefore carry one of them, or the hand-off fails at the fork."""
    section = SKILL.split("### 6a.")[1].split("### 6.1.")[0]
    walk1 = next(ln for ln in section.splitlines() if "--engine-log walk1.log" in ln)
    walk2 = next(ln for ln in section.splitlines() if "--engine-log walk2.log" in ln)
    assert "--expect-new N" in walk1 and "--noop" not in walk1
    assert "--noop" in walk2
    assert "One boot per log file is enforced" in section


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


def test_the_refusal_match_is_recorded_as_measured_against_a_real_client() -> None:
    """The classifier's match was a static prediction until it was run against published 7.67.0
    (2026-10-01); the script header and the skill say which, so nobody re-reads it as a guess."""
    section = SKILL.split("### 3c.")[1].split("### 3d.")[0]
    assert "MEASURED on 2026-10-01" in section and "7.67.0" in section
    assert "MEASURED 2026-10-01 against published conexus 7.67.0" in GATE
    assert "*[Oo]wnerless*" not in GATE, "the bare-word pattern (broader than the engine's sentence) is gone"
