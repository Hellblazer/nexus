# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""plugin-release.yml wiring coverage (RDR-197 P2a, nexus-a2wmi.7).

A workflow with no wiring test is a guard nobody proves is wired -- the
same lesson tests/test_plugin_release_drift_ledger.py::TestTheCIWiringItself
already encodes for plugin-drift-ledger.yml (see that class's own
docstring: the 2026-07-25 incident where three "mechanized" guards were
protecting nothing because the workflow that was supposed to run them
never did). These tests copy that class's shape verbatim -- one YAML
parse, then cheap structural assertions -- for the second workflow in the
same channel.

TWO DISTINCT THINGS ARE BEING PROVEN HERE:
  1. plugin-release.yml itself is wired correctly (fires on the right
     tags, runs the right battery, has a concurrency group, publishes
     nothing).
  2. release.yml -- a workflow this file does NOT own or modify -- cannot
     ALSO fire on an anchored plugin tag and publish a wheel from a plugin
     cut. Its trigger is `v*`; an anchored tag is `plugin-v*`, which does
     not match. This is pinned here because a future edit to release.yml's
     trigger is exactly the kind of change nobody would think to run
     against a plugin-cut wheel-surface question.
"""
from __future__ import annotations

import fnmatch
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plugin-release.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

#: The minimal battery named in the bead (RDR-197 step 4), as substrings
#: expected to appear somewhere in the workflow's step text. The
#: wheel-surface proof lives INSIDE test_plugin_release_drift_ledger.py
#: (RDR Technical Design: "beside the drift-ledger tests, reusing
#: SURFACE_BY_PLUGIN"), so running that file IS running the proof -- it is
#: not a separate battery target.
BATTERY_TARGETS = (
    "test_plugin_release_drift_ledger.py",
    "test_plugin_structure.py",
    "tests/hooks/",
    "-m lint",
)

#: Any of these appearing in a step's `uses:` or `run:` text means this
#: "verify-only" workflow stopped being verify-only. RDR-197 Technical
#: Design: "publishes nothing to PyPI, no wheel, no `.mcpb`".
PUBLISH_MARKERS = (
    "pypa/gh-action-pypi-publish",
    "uv publish",
    "twine",
    "gh release",
    "softprops/action-gh-release",
    "mcpb pack",
)

pytestmark = pytest.mark.lint


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    assert WORKFLOW.exists(), (
        "the plugin-release workflow is gone. Without it a plugin-v* tag "
        "push verifies nothing -- exactly the 'guard believed live but "
        "actually inert' shape the drift-ledger channel already burned a "
        "postmortem on."
    )
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_section(wf: dict) -> dict:
    # PyYAML (1.1-flavored safe_load) parses the bare `on:` mapping key as
    # the boolean True, not the string "on" -- the same gotcha the
    # drift-ledger wiring tests already work around.
    return wf[True] if True in wf else wf["on"]


def _steps(wf: dict) -> list[dict]:
    jobs = wf["jobs"]
    assert len(jobs) == 1, (
        f"expected exactly one job in plugin-release.yml, found "
        f"{list(jobs)} -- update this test if a second job is deliberate"
    )
    (job,) = jobs.values()
    return job["steps"]


def _all_step_text(steps: list[dict]) -> str:
    """Every `uses:` and `run:` string across all steps, concatenated."""
    parts: list[str] = []
    for step in steps:
        uses = step.get("uses")
        if uses:
            parts.append(str(uses))
        run = step.get("run")
        if run:
            parts.append(str(run))
    return "\n".join(parts)


def test_the_workflow_file_exists() -> None:
    """Not folded into the other tests: this is the one whose failure
    message must not itself depend on the workflow existing to parse."""
    assert WORKFLOW.exists()


def test_the_tag_filter_is_plugin_v_star() -> None:
    wf = _workflow()
    tags = _on_section(wf)["push"]["tags"]
    assert tags == ["plugin-v*"], (
        f"the tag trigger changed to {tags!r}. This workflow must fire on "
        "exactly the anchored plugin-tag shape, never the client v* shape "
        "release.yml already owns -- firing on both would run this "
        "verify-only battery twice, or worse, let a plugin tag dodge it."
    )


def test_the_tag_filter_does_not_match_a_client_tag() -> None:
    """Non-vacuity check on the assertion above: prove `plugin-v*` really
    discriminates, rather than trusting the literal string comparison."""
    wf = _workflow()
    tags = _on_section(wf)["push"]["tags"]
    assert not any(fnmatch.fnmatchcase("v7.15.0", pat) for pat in tags), (
        "the plugin-release trigger matches a plain client tag -- it would "
        "fire (and, worse, be trusted to have verified) a normal PyPI "
        "release cut."
    )
    assert any(
        fnmatch.fnmatchcase("plugin-v7.15.0-1", pat) for pat in tags
    ), "the plugin-release trigger no longer matches its own anchored tag shape"


@pytest.mark.parametrize("target", BATTERY_TARGETS)
def test_the_workflow_names_every_battery_target(target: str) -> None:
    steps = _steps(_workflow())
    text = _all_step_text(steps)
    assert target in text, (
        f"the minimal battery no longer runs {target!r} (RDR-197 step 4: "
        "the wheel-surface proof, tests/test_plugin_structure.py, "
        "tests/test_plugin_release_drift_ledger.py, tests/hooks/, and the "
        "-m lint bucket). A dropped battery target is a plugin cut that "
        "ships unverified."
    )


def test_the_workflow_has_a_concurrency_group() -> None:
    wf = _workflow()
    concurrency = wf.get("concurrency")
    assert concurrency and concurrency.get("group"), (
        "the concurrency group is gone -- superseded runs on a re-tagged "
        "or re-pushed cut would stack instead of cancelling (CI cost "
        "discipline, AGENTS.md)."
    )
    assert concurrency.get("cancel-in-progress") is True, (
        "cancel-in-progress must be true: a stale verify run for a tag "
        "that no longer exists must not keep burning runner minutes."
    )


def test_the_workflow_steps_contain_no_publish_action() -> None:
    """The load-bearing assertion (bead acceptance criterion): proven by
    scanning the workflow's own steps, not by reading the file and
    trusting the header comment."""
    steps = _steps(_workflow())
    text = _all_step_text(steps).lower()
    offenders = [marker for marker in PUBLISH_MARKERS if marker in text]
    assert not offenders, (
        f"plugin-release.yml contains publish action(s): {offenders}. "
        "This workflow is VERIFY-ONLY (RDR-197 Technical Design): a "
        "plugin cut publishes nothing to PyPI, no wheel, no .mcpb, no "
        "GitHub release asset. A publish step here means a plugin tag "
        "push now ships a wheel nobody reviewed as one."
    )


def test_the_workflow_does_not_use_fetch_depth_zero_or_fetch_tags() -> None:
    """RDR-197 step 3 / the bead's own hard constraint: this workflow
    fetches exactly the base client tag, never the whole history. A
    regression back to `fetch-depth: 0` or `fetch-tags: true` would still
    "work" but silently reintroduce the cost this channel's shape was
    chosen to avoid (see plugin-drift-ledger.yml's COST header)."""
    steps = _steps(_workflow())
    for step in steps:
        with_block = step.get("with") or {}
        assert with_block.get("fetch-depth") != 0, (
            "a step now uses fetch-depth: 0 -- this workflow's shape is "
            "deliberately depth-1 plus an explicit single-tag fetch."
        )
        assert with_block.get("fetch-tags") is not True, (
            "a step now uses fetch-tags: true, which release.yml's own "
            "checkout comment records as delivering nothing at depth 1."
        )


def test_the_workflow_fetches_the_base_client_tag_explicitly() -> None:
    steps = _steps(_workflow())
    text = _all_step_text(steps)
    assert "git fetch" in text and "refs/tags/" in text, (
        "the explicit base-client-tag fetch is gone -- the wheel-surface "
        "proof's range base and the drift-ledger contract's anchored-"
        "window checks both need it resolvable locally."
    )


def _release_workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    assert RELEASE_WORKFLOW.exists(), "release.yml is gone; nothing to pin against"
    return yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def test_release_workflow_cannot_fire_on_an_anchored_plugin_tag() -> None:
    """RDR-197 step 8. release.yml's trigger is `v*`; a plugin cut's tag is
    `plugin-v{version}-{n}`. `v*` must not match that ref, or a future
    trigger edit to release.yml could silently publish a wheel from a
    plugin-only cut -- exactly the coupling this whole channel exists to
    avoid (Decision Rationale: "the plugin surface and the wheel are
    different products")."""
    wf = _release_workflow()
    tags = _on_section(wf)["push"]["tags"]
    anchored_example = "plugin-v9.9.9-1"
    matches = [pat for pat in tags if fnmatch.fnmatchcase(anchored_example, pat)]
    assert not matches, (
        f"release.yml's tag trigger {tags!r} matches an anchored plugin "
        f"tag ({anchored_example!r}) via pattern(s) {matches}. A plugin-"
        "only cut would now also fire the PyPI publish workflow."
    )


# ── The cut-range ledger assert (nexus-a2wmi.9 follow-up) ─────────────────
#
# This step shipped as an inline substring-match loop over the whole
# ledger and failed EVERY cut: the ledger's permanent header prose names
# `.claude-plugin/marketplace.json`, and every cut touches that file by
# construction (moving source.ref IS the cut). It had zero coverage, and
# the channel has never been cut, so it shipped having never executed.
#
# A `run:` block is executed by nothing in this suite -- structural
# assertions about step TEXT are not execution. So the decision moved into
# scripts/check_cut_ledger_clean.py where a test can drive it, and these
# tests pin both the decision and the wiring.

from check_cut_ledger_clean import stale_ledger_offenders  # noqa: E402

#: The ledger's real header shape. Line 3 is the prose that broke the
#: original step -- it names the one path every cut touches.
_LEDGER_HEADER = """# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree.

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed below.
"""

#: A real entry: a bullet block carrying a path-shaped backtick span.
_LEDGER_ENTRY = """
- `conexus/skills/orchestration/SKILL.md` — dispatch contract reworded.
  bead: nexus-abcde
"""


def test_header_prose_naming_marketplace_json_is_not_an_offender() -> None:
    """THE REGRESSION. Every cut touches marketplace.json; the header names
    it; the whole-file scan therefore flagged every cut forever."""
    assert ".claude-plugin/marketplace.json" in _LEDGER_HEADER, (
        "fixture no longer reproduces the condition -- the header must name "
        "the path for this test to mean anything"
    )
    offenders = stale_ledger_offenders(
        [".claude-plugin/marketplace.json"], _LEDGER_HEADER
    )
    assert offenders == [], (
        "header contract prose is not a ledger entry; flagging it makes every "
        "cut impossible"
    )


def test_a_real_entry_naming_a_shipped_path_is_still_an_offender() -> None:
    """The belt must still catch what it was built to catch."""
    offenders = stale_ledger_offenders(
        ["conexus/skills/orchestration/SKILL.md"], _LEDGER_HEADER + _LEDGER_ENTRY
    )
    assert offenders == ["conexus/skills/orchestration/SKILL.md"]


def test_a_path_no_entry_names_is_clean() -> None:
    offenders = stale_ledger_offenders(
        ["conexus/CHANGELOG.md"], _LEDGER_HEADER + _LEDGER_ENTRY
    )
    assert offenders == []


def test_the_live_ledger_does_not_flag_a_marketplace_only_cut() -> None:
    """Against the REAL ledger in the tree, not a fixture.

    A fixture can drift away from the file it models; this one cannot.
    """
    ledger = (REPO_ROOT / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")
    assert stale_ledger_offenders([".claude-plugin/marketplace.json"], ledger) == []


def test_the_workflow_step_delegates_to_the_tested_script() -> None:
    """Pins the wiring: the decision must not drift back into the YAML.

    An inline scan here is untestable by construction, which is how the
    original defect reached a shipped release.
    """
    steps = _steps(_workflow())
    ledger_steps = [
        s for s in steps if "PENDING_RELEASE" in (s.get("name") or "")
    ]
    assert len(ledger_steps) == 1, "expected exactly one ledger-assert step"
    run = ledger_steps[0].get("run") or ""
    assert "check_cut_ledger_clean.py" in run, (
        "the ledger assert must call the tested script"
    )
    assert "grep" not in run, (
        "the ledger decision must not be reimplemented inline -- an inline "
        "scan is executed by no test and was wrong for the whole of 7.16.0"
    )
