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


def test_6a_walk_1_is_pinned_to_recorded_changesets_and_walk_2_is_a_noop() -> None:
    """The checker refuses a walk given none of --expect-recorded, --expect-new, --noop (exit 2); the commands
    the skill hands conexus must therefore carry one of them, or the hand-off fails at the fork."""
    section = SKILL.split("### 6a.")[1].split("### 6.1.")[0]
    walk1 = next(ln for ln in section.splitlines() if "--engine-log walk1.log" in ln)
    walk2 = next(ln for ln in section.splitlines() if "--engine-log walk2.log" in ln)
    # --expect-recorded (new + mark_ran), never --expect-new: this tag's staging-6 is MARK_RAN-guarded
    assert "--expect-recorded NEW" in walk1 and "--noop" not in walk1 and "--expect-new" not in walk1
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


def test_6a_names_mark_ran_and_keeps_new_and_rows_as_distinct_names() -> None:
    """A MARK_RAN-guarded changeset is recorded, not executed: the skill must say so, and the code
    block must not reuse one letter for the changesets-applied count and the table's row count."""
    section = SKILL.split("### 6a.")[1].split("### 6.1.")[0]
    assert "MARK_RAN" in section and "staging-6-drop-landing-schema" in section
    block = section.split("```bash")[1].split("```")[0]
    assert not re.search(r"--expect-(?:recorded|rows|new) N\b", block), "N is reused across two different counts"
    assert "--expect-recorded NEW" in block and "--expect-rows ROWS" in block
    assert '--min-rows "$TREE"' in block and "changelog-count" in block


def test_6a_changeset_sizing_grep_counts_a_multiline_tag_once() -> None:
    """The sizing grep must see a `<changeSet` whose attributes continue on later lines (the old
    pattern needed a space after the tag name) and must not count `<changeSetFoo` or a comment."""
    import subprocess
    section = SKILL.split("### 6a.")[1].split("### 6.1.")[0]
    m = re.search(r"grep -E '([^']*changeSet[^']*)'", section)
    assert m, "the sizing grep is not in 6a"
    diff = "\n".join([
        "+<changeSet",                       # multi-line tag: name only on its first line
        '+    id="x"',                        # an attribute continuation line is not a tag
        '+  <changeSet id="y" author="a">',   # single-line tag
        '-<changeSet id="z" author="a">',     # the removed side of an edit
        "+<changeSetFoo>",                    # not a changeSet
        "+<!-- <changeSet id=\"c\"> -->",     # a comment is not a tag start (line starts with <!--)
        "",
    ])
    out = subprocess.run(["grep", "-E", m.group(1)], input=diff, capture_output=True, text=True).stdout.splitlines()
    assert out == ["+<changeSet", '+  <changeSet id="y" author="a">', '-<changeSet id="z" author="a">'], out


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
    assertion must say it is conexus's hold-the-push line and why it is not an attestation field."""
    runbook = (REPO_ROOT / "docs" / "operations" / "ownerless-write-cutover.md").read_text()
    readme = (REPO_ROOT / "docs" / "release-arming" / "README.md").read_text()
    for name, text in (("skill", SKILL), ("runbook", " ".join(runbook.split()))):
        assert "hold-the-push line" in text, name
        assert "nothing in this repo checks it" in text, name
        assert "attestation" in text, name
    assert "hold-the-push line" in readme and "not an attestation field" in readme
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
