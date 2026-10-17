# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every push to develop gets a CI run that finishes; PRs still cancel superseded runs.

Sam, 2026-10-17: with several sessions pushing, a develop run was cancelled by the
next push and many commits never got a finished run. `cancel-in-progress: false`
alone does not help, because GitHub keeps one pending run per concurrency group
and a third push still cancels the second's queued run. So the group is unique
per commit on develop and per ref elsewhere, and cancel-in-progress stays true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: The workflows that run on a push to develop and used to cancel each other.
PER_COMMIT_ON_DEVELOP = ("ci.yml", "windows-pg-bundle-rehearsal.yml", "plugin-drift-ledger.yml")

DEVELOP_KEY = "github.ref == 'refs/heads/develop' && github.sha || github.ref"


@pytest.mark.parametrize("name", PER_COMMIT_ON_DEVELOP)
def test_a_develop_push_gets_its_own_concurrency_group(name: str) -> None:
    concurrency = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["concurrency"]
    group = concurrency["group"]
    assert DEVELOP_KEY in group, f"{name}: develop pushes must be grouped per commit, got {group!r}"
    assert concurrency["cancel-in-progress"] is True, f"{name}: PRs must still cancel superseded runs"
