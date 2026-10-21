# SPDX-License-Identifier: AGPL-3.0-or-later
"""develop runs CI one run at a time with the newest push waiting; PRs still cancel superseded runs.

Sam, 2026-10-17: every develop commit must be covered by a finished CI run. Sam,
2026-10-18 (labrun RDR-001 F15, nexus-1wf1s): one group per develop commit put 4-6
develop runs in flight against a 10-shard cap, so they queued behind each other and
crowded out hand runs. develop is linear, so one group per ref with no cancel in
progress still covers every commit: a running develop run always finishes, and a newer
push only replaces the single pending run, which then tests every commit before it.
On every other ref a newer push cancels the run it supersedes (the 2026-07-06 rule).
Cancelling on develop is not allowed back: under continuous pushes it livelocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: The workflows that run on a push to develop and share this rule.
ONE_AT_A_TIME_ON_DEVELOP = ("ci.yml", "windows-pg-bundle-rehearsal.yml", "plugin-drift-ledger.yml")

CANCEL_OFF_DEVELOP = "${{ github.ref != 'refs/heads/develop' }}"


@pytest.mark.parametrize("name", ONE_AT_A_TIME_ON_DEVELOP)
def test_develop_runs_one_at_a_time_and_other_refs_cancel(name: str) -> None:
    concurrency = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["concurrency"]
    group = concurrency["group"]
    assert group.endswith("${{ github.ref }}"), f"{name}: one group per ref, got {group!r}"
    assert "github.sha" not in group, f"{name}: a per-commit group puts every develop push in flight at once"
    assert concurrency["cancel-in-progress"] == CANCEL_OFF_DEVELOP, (
        f"{name}: a running develop run must finish and other refs must cancel superseded runs, "
        f"got {concurrency['cancel-in-progress']!r}"
    )
