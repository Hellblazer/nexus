# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release skill's "Plugin-only fast lane" section (nexus-rbmja.6).

The section is the only place a reader learns how to cut a plugin-only release,
so it must exist, name the machinery by real paths, and not name the retired
anchored channel (nexus-rbmja retires it). Design of record: T2
nexus/design-plugin-only-fast-lane.md.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".claude" / "skills" / "release" / "SKILL.md"
HEADING = "Plugin-only fast lane"

REQUIRED_TOKENS = (
    "--plugin-only",
    "check_plugin_only_eligibility.py",
    "plugin-delivery-proof.sh",
    "cherry-pick -x",
    "--develop-ref",
)
RETIRED_TOKENS = (
    "cut_plugin_release",
    "plugin_cut_back_merge",
    "plugin-release/",
    "plugin-v",
    "plugin channel cut",
)
_PATH_RE = re.compile(r"\b((?:scripts|tests/e2e)/[A-Za-z0-9_./-]*[A-Za-z0-9_])")


def _section() -> str:
    """Lines from the "Plugin-only fast lane" heading to the next heading of
    the same or a higher level (fenced code blocks may hold `#` comments, so
    fences are tracked)."""
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    start = level = None
    fenced = False
    body: list[str] = []
    for line in lines:
        if line.startswith("```"):
            fenced = not fenced
        heading = None if fenced else re.match(r"^(#{1,6}) (.*)$", line)
        if start is None:
            if heading and heading.group(2).startswith(HEADING):
                start, level = True, len(heading.group(1))
            continue
        if heading and len(heading.group(1)) <= level:
            break
        body.append(line)
    assert start, f'{SKILL}: no heading "{HEADING}"'
    return "\n".join(body)


def test_section_exists_and_is_not_trivial() -> None:
    assert len(_section().splitlines()) > 30


def test_every_named_script_and_e2e_path_exists() -> None:
    paths = sorted(set(_PATH_RE.findall(_section())))
    assert paths, "the section names no scripts/ or tests/e2e/ path at all"
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    assert not missing, f"section names paths absent from the tree: {missing}"


def test_section_names_the_machinery() -> None:
    section = _section()
    absent = [t for t in REQUIRED_TOKENS if t not in section]
    assert not absent, f"section does not mention: {absent}"


def _fenced_lines(section: str) -> list[str]:
    """Every line inside a fenced block of the section, in order."""
    out: list[str] = []
    fenced = False
    for line in section.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            out.append(line)
    return out


def _flat(section: str) -> str:
    """The section with whitespace runs collapsed, so a pin survives re-wrapping."""
    return re.sub(r"\s+", " ", section)


def test_section_states_the_decisions_of_record() -> None:
    flat = _flat(_section())
    # the CHANGELOG engine pairing; the accepted risk of the skipped legs; the
    # unit suite as a hand step.
    for needle in ("Pairs with engine-service-v", "accepted risk", "unit suite"):
        assert needle in flat or needle in flat.lower(), f"section lacks: {needle}"


def test_section_branches_off_the_tag_not_develop() -> None:
    commands = _fenced_lines(_section())
    assert any(
        "worktree add" in c and "-b release/vX.Y.N vX.Y.Z" in c for c in commands
    ), "no command branches release/vX.Y.N off the base tag vX.Y.Z"


def test_eligibility_verdict_and_non_passes_are_stated() -> None:
    flat = _flat(_section())
    assert "PLUGIN-ONLY ELIGIBILITY PASSED" in flat
    # exit 1 / exit 2 of the gate and the UNVERIFIED proof verdict are not passes
    assert flat.count("is not a pass") + flat.count("Neither is a pass") >= 2


def test_the_releaser_is_human() -> None:
    flat = _flat(_section())
    assert "The releaser is human" in flat
    assert '"cut the release"' in flat


def test_engine_floor_check_precedes_the_pr_and_closes_the_lane() -> None:
    section = _section()
    commands = _fenced_lines(section)
    floor = "uv run python scripts/check_engine_release_floor.py"
    assert any(c.split("#")[0].strip() == floor for c in commands), (
        "the floor check is not a command of the lane, in its bare form"
    )
    assert "--paired-deploy-auto" in _flat(section), "the section does not say why it runs the bare form"
    # the lane is open only while the newest engine tag is the base tag's pin, and a red closes it
    flat = _flat(section)
    assert "newest published `engine-service-v*` tag is the one the base tag pins" in flat
    assert "closes the lane" in flat
    # "before the PR": it sits in the gate block, which comes before the Publish item that opens the PR
    assert section.index(floor) < section.index("**6. Publish.**")


def test_base_tag_machinery_is_checked_not_named_by_version() -> None:
    section = _section()
    commands = _fenced_lines(section)
    for path in (
        "scripts/check_plugin_only_eligibility.py",
        "tests/e2e/plugin-delivery-proof.sh",
    ):
        assert f"git cat-file -e vX.Y.Z:{path}" in commands
    assert any(
        c.startswith("git show vX.Y.Z:tests/e2e/release-battery.sh") and "--plugin-only" in c
        for c in commands
    )
    assert not re.search(r"engine-service-v\d", section), "a literal engine tag is named as a rule"


def test_publish_uses_the_prebuilt_branch_and_rechecks_the_pr_head() -> None:
    flat = _flat(_section())
    assert "skip Step 7's branch-creation and pre-merge lines" in flat
    assert "re-run the eligibility script on the exact PR head" in flat


def test_step_11c_stays_in_the_lane() -> None:
    section = _section()
    flat = _flat(section)
    assert "replaces Step 11c" not in flat
    assert "run Step 11c exactly as written" in flat
    # the delivery proof runs in addition; the published-bytes legs are named in the
    # lane's own prose because the proof does not replace them
    assert "fresh-install-mvv.sh --published" in flat
    assert "NEXUS_TARGET_RELEASE" in flat
    assert any(
        c.startswith("tests/e2e/plugin-delivery-proof.sh --prev") for c in _fenced_lines(section)
    )


def test_back_merge_and_cherry_pick_mechanics_are_given() -> None:
    section = _section()
    commands = _fenced_lines(section)
    flat = _flat(section)
    assert any("checkout --ours" in c for c in commands)
    assert "git cherry-pick --continue" in commands
    assert "git cherry-pick --abort" in flat and "depends on a commit you skipped" in flat
    assert "not trivially clean" in flat
    assert "develop's `## [Unreleased]` first" in flat
    assert "deleted from it" in flat


def test_unit_suite_goes_through_the_watched_runner_and_evidence_is_filed() -> None:
    section = _section()
    commands = _fenced_lines(section)
    assert any(
        c.startswith("lab-remote") and "scripts/watched_pytest.py" in c for c in commands
    ), "the unit suite is not run through watched_pytest.py under lab-remote"
    flat = _flat(section)
    assert "Steps 0 (beyond the floor check" in flat and "6d do not apply" in flat
    assert "nexus-rbmja.13" in flat
    assert "in-session auto-update" in flat


def test_section_does_not_name_the_retired_channel() -> None:
    section = _section()
    present = [t for t in RETIRED_TOKENS if t in section]
    assert not present, f"section names the retired channel: {present}"


def test_skill_outside_the_section_does_not_point_at_the_retired_channel() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "plugin channel cut" not in text
    assert "anchored" not in text.lower()
