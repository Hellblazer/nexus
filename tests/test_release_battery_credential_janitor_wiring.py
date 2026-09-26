# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 3 Step 2b (nexus-wauo1.24): the credential janitor is wired
into ``tests/e2e/release-battery.sh`` as a real leg, not merely a script
that exists on disk but nothing ever runs. A leg omitted from
``define_leg`` is invisible to every release: the battery's own report only
ever lists what it defined (see ``tests/test_claude_credentials_single_source_lint.py``
for the sibling convention of pinning a shape directly against the real
file rather than a retyped copy that can drift)."""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BATTERY = REPO_ROOT / "tests" / "e2e" / "release-battery.sh"
JANITOR = REPO_ROOT / "scripts" / "credential_janitor.py"

_DEFINE_LEG_RE = re.compile(
    r"define_leg\s+janitor\s+group\s+"
    r'"CREDENTIAL JANITOR \(PASSED\|FAILED\)"\s+'
    r"python3 scripts/credential_janitor\.py\b"
)


def test_janitor_script_exists() -> None:
    assert JANITOR.is_file(), f"expected the janitor script at {JANITOR}"


def test_janitor_leg_is_defined_in_the_group_phase() -> None:
    text = BATTERY.read_text()
    assert _DEFINE_LEG_RE.search(text), (
        "expected a `define_leg janitor group \"CREDENTIAL JANITOR "
        "(PASSED|FAILED)\" python3 scripts/credential_janitor.py` line in "
        f"{BATTERY} -- a leg missing from define_leg never runs and never "
        "appears in the battery report"
    )


def test_janitor_leg_runs_before_the_alone_shakeout_leg() -> None:
    """The `alone` phase (shakeout) runs strictly after the `group` phase
    finishes (release-battery.sh's own execution order); pin that the
    janitor's define_leg line precedes shakeout's in file order too, so a
    future edit cannot accidentally reclassify it into `alone` or `serial`
    without this test noticing the reordering."""
    text = BATTERY.read_text()
    janitor_pos = text.index("define_leg janitor")
    shakeout_pos = text.index("define_leg shakeout")
    assert janitor_pos < shakeout_pos
