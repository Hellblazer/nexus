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


def test_section_states_the_decisions_of_record() -> None:
    section = _section()
    # base tag must already carry the machinery; the CHANGELOG engine pairing;
    # the accepted risk of the skipped legs; the unit suite as a hand step.
    for needle in ("engine-service-v", "engine-service-v0.1.165", "accepted risk", "unit suite"):
        assert needle in section.lower(), f"section lacks: {needle}"


def test_section_does_not_name_the_retired_channel() -> None:
    section = _section()
    present = [t for t in RETIRED_TOKENS if t in section]
    assert not present, f"section names the retired channel: {present}"


def test_skill_outside_the_section_does_not_point_at_the_retired_channel() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "plugin channel cut" not in text
    assert "anchored" not in text.lower()
