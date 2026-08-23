"""The plan-audit termination contract stays wired across surfaces (nexus-ll7zm).

O(repo) meta-tests, hence the lint marker. The enforcement lives in the
wheel; these three plugin files are what tells a planner the parameters
exist at all. A caller that never learns to pass ``round_number`` gets
round-1 semantics forever, which is precisely the unterminated loop, so
guidance drifting away from the tool is not cosmetic here — it silently
restores the old behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.plans.audit_rounds import BLOCKS_PLANNING, DISCOVER_AT_IMPLEMENTATION

REPO_ROOT = Path(__file__).resolve().parent.parent

PLANNER = REPO_ROOT / "conexus" / "agents" / "strategic-planner.md"
SKILL = REPO_ROOT / "conexus" / "skills" / "plan-validation" / "SKILL.md"
COMMAND = REPO_ROOT / "conexus" / "commands" / "plan-audit.md"
LEDGER = REPO_ROOT / "conexus" / "PENDING_RELEASE.md"

pytestmark = pytest.mark.lint


def _pinned_conexus_ref() -> str | None:
    marketplace = REPO_ROOT / ".claude-plugin" / "marketplace.json"
    for plugin in json.loads(marketplace.read_text(encoding="utf-8")).get(
        "plugins", []
    ):
        if plugin.get("name") == "conexus" and isinstance(plugin.get("source"), dict):
            return plugin["source"].get("ref")
    return None


def _drifts_from_pin(path: str) -> bool | None:
    """Whether *path* differs from the pinned conexus tag (worktree state).

    ``None`` means not computable (no pin, or the tag is absent — e.g. a
    shallow clone). Diffing against the WORKING TREE, not HEAD, so an
    uncommitted plugin edit already counts as drift and the ledger
    assertions are verifiable before the commit exists.
    """
    ref = _pinned_conexus_ref()
    if not ref:
        return None
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--quiet", ref, "--", path],
        capture_output=True,
        text=True,
    )
    return diff.returncode != 0


@pytest.fixture(scope="module")
def surfaces() -> dict[str, str]:
    return {
        "strategic-planner.md": PLANNER.read_text(encoding="utf-8"),
        "plan-validation/SKILL.md": SKILL.read_text(encoding="utf-8"),
        "plan-audit.md": COMMAND.read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("param", ["round_number", "budget_rounds"])
def test_every_surface_documents_both_parameters(surfaces, param: str) -> None:
    missing = [name for name, text in surfaces.items() if param not in text]
    assert not missing, (
        f"{param} is absent from {missing}. A planner reading only that "
        f"surface never passes it, so every audit reads as round 1 and the "
        f"blocking-round cap never engages."
    )


@pytest.mark.parametrize(
    "classification", [BLOCKS_PLANNING, DISCOVER_AT_IMPLEMENTATION]
)
def test_every_surface_names_both_classifications(surfaces, classification) -> None:
    missing = [name for name, text in surfaces.items() if classification not in text]
    assert not missing, (
        f"{classification} is absent from {missing}. Unclassified guidance "
        f"leaves a reader treating every finding as blocking."
    )


def test_the_surfaces_agree_with_the_code_on_the_constant_strings() -> None:
    """Guidance strings are the code's constants, not paraphrases."""
    from nexus.plans.audit_rounds import (
        BLOCKS_PLANNING,
        DISCOVER_AT_IMPLEMENTATION,
        NOT_READY,
        READY,
        RESIDUALS_ONLY,
    )

    planner = PLANNER.read_text(encoding="utf-8")
    for verdict in (READY, NOT_READY, RESIDUALS_ONLY):
        assert verdict in planner, (
            f"verdict {verdict!r} is not explained in strategic-planner.md; a "
            f"planner meeting it for the first time will guess"
        )
    for label in (BLOCKS_PLANNING, DISCOVER_AT_IMPLEMENTATION):
        assert label in planner


def test_the_planner_states_the_after_round_two_rule() -> None:
    text = PLANNER.read_text(encoding="utf-8").lower()
    assert "evaluate the loop" in text
    assert "residual" in text


def test_the_ledger_declares_every_changed_plugin_file() -> None:
    """Plugin-surface drift must be declared — WHILE it exists.

    Conditional on live drift from the pinned tag, because a release
    clears the ledger and re-pins: an unconditional assertion here goes
    red at every release cut. Post-release there is genuinely nothing to
    declare, and that is a pass, not a masked skip — the generic
    enforcement for future drift is test_plugin_release_drift_ledger.py.
    """
    ledger = LEDGER.read_text(encoding="utf-8")
    paths = (
        "conexus/agents/strategic-planner.md",
        "conexus/skills/plan-validation/SKILL.md",
        "conexus/commands/plan-audit.md",
        "conexus/agents/architect-planner.md",
    )
    verdicts = {path: _drifts_from_pin(path) for path in paths}
    if all(v is None for v in verdicts.values()):
        pytest.skip("pinned conexus tag unavailable; drift not computable")
    undeclared = [
        path for path, drifted in verdicts.items() if drifted and path not in ledger
    ]
    assert not undeclared, (
        f"{undeclared} drift from the pinned tag but are undeclared in the ledger"
    )


def test_the_ledger_records_the_wheel_versus_plugin_split() -> None:
    """The split is the part a reader gets wrong: half of this fix is live
    on upgrade, half waits for the pinned tag.

    Keyed on the ledger still carrying the nexus-ll7zm entries — after a
    release clears them the split window is over and there is nothing to
    note.
    """
    ledger = LEDGER.read_text(encoding="utf-8")
    if "nexus-ll7zm" not in ledger:
        return
    assert "SPLIT DELIVERY" in ledger
    assert "src/nexus/plans/audit_rounds.py" in ledger
