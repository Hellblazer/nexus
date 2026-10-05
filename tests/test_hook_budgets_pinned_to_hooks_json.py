# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two SessionStart hooks' internal time budgets stay under the timeouts
``conexus/hooks/hooks.json`` gives them (nexus-wozn6).

Claude Code cancels a hook at its ``timeout`` and prints nothing for it.
Both hooks bound themselves with a constant in their own module, and nothing
tied those constants to the number hooks.json declares, so editing either
side alone would silently re-open the cancel the fix closed:

* ``nx-hook rdr`` runs every network leg inside ``_HOOK_BUDGET_S`` and must
  leave room before its timeout for interpreter start, imports and the write.
* ``nx-hook upgrade-auto`` waits ``_SKEW_WAIT_S`` on a detached child and
  returns; that wait must be far inside its timeout.

hooks.json timeout changes are out of scope for the fix that added this
file, so a change there that breaks the relation fails here, naming both
numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import rdr_verb, upgrade_auto

pytestmark = pytest.mark.lint

HOOKS_JSON = Path(__file__).resolve().parents[1] / "conexus" / "hooks" / "hooks.json"

#: Seconds the rdr hook needs beyond its budget: interpreter start (~0.01 s),
#: the verb's imports (~0.013 s), the structlog sink (0.06-0.3 s) and the
#: stdout write. Measured well under a second; two seconds is stated so a
#: budget raised to "just under the timeout" fails.
_START_MARGIN_S = 2.0


def _session_start_timeout(verb: str) -> float:
    """The ``timeout`` of the one SessionStart ``nx-hook <verb>`` entry."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    found = [
        h["timeout"]
        for entry in data["hooks"]["SessionStart"]
        for h in entry["hooks"]
        if h.get("command") == "nx-hook" and h.get("args") == [verb]
    ]
    assert len(found) == 1, (
        f"expected exactly one SessionStart `nx-hook {verb}` entry in hooks.json, "
        f"found {len(found)}: the pin below would be vacuous"
    )
    return float(found[0])


def test_rdr_budget_plus_start_margin_is_under_the_rdr_hook_timeout() -> None:
    timeout = _session_start_timeout("rdr")
    assert rdr_verb._HOOK_BUDGET_S + _START_MARGIN_S < timeout, (
        f"rdr_verb._HOOK_BUDGET_S={rdr_verb._HOOK_BUDGET_S} + start margin "
        f"{_START_MARGIN_S} must be under the hooks.json rdr timeout {timeout}"
    )


def test_every_rdr_leg_cap_fits_inside_the_budget() -> None:
    """A leg cap above the budget is dead weight at best; the T2 cap is the
    budget itself, so raising one raises the other."""
    assert rdr_verb._T2_DEADLINE_S <= rdr_verb._HOOK_BUDGET_S
    assert rdr_verb._CATALOG_DEADLINE_S <= rdr_verb._HOOK_BUDGET_S
    assert rdr_verb._T3_DEADLINE_S <= rdr_verb._HOOK_BUDGET_S


def test_upgrade_auto_skew_wait_is_under_the_upgrade_auto_timeout() -> None:
    timeout = _session_start_timeout("upgrade-auto")
    assert upgrade_auto._SKEW_WAIT_S < timeout, (
        f"upgrade_auto._SKEW_WAIT_S={upgrade_auto._SKEW_WAIT_S} must be under "
        f"the hooks.json upgrade-auto timeout {timeout}"
    )
