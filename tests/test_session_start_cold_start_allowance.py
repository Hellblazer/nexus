# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every SessionStart entry's timeout leaves room for uv's cold start (nexus-3ya9d).

Every conexus hook command is ``uv tool run --directory ${CLAUDE_PLUGIN_ROOT}
... python <script>``. On the first session after an install, uv has work to do
before the script's first line runs: it may have to fetch a Python 3.12 (the
``--python >=3.12`` request; uv-managed interpreters live outside ``UV_CACHE_DIR``,
so a seeded cache does not hold one) and it builds the tool environment once.
All of the nine SessionStart processes start together and wait on the same uv
locks, so each pays the whole of it, including the two that run no nx code.

Measured on macOS, uv 0.8.0, empty ``UV_CACHE_DIR`` and ``UV_PYTHON_INSTALL_DIR``,
seven hooks started together: 0.1 to 0.2 s when a Python already exists, 2.3 s
for every one of the seven when uv has to fetch one (47 MB, fast link), 0.1 s
warm. On Windows, in the Phase 5 gate, Claude Code cancelled the first
session's hooks at their 5, 10 and 20 s timeouts, and a warm session measured
3.1 to 5.5 s per hook against the 5 s limit (bead nexus-3ya9d).

Claude Code kills a hook at its timeout, so the timeout has to cover the cold
start AND the hook's own work. Raising it costs nothing when the machine is
warm (a hook returns when it is done) and a stuck hook still ends at its own
internal bound, so the rule is: timeout >= ``COLD_START_ALLOWANCE_S`` + the
timeout the entry had before this fix, which was the budget for its own work.
The table below holds those budgets by entry, so an entry added or removed
without a decision here fails the test instead of passing it vacuously.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import mcp_connect_wait, rdr_verb, upgrade_auto
from tests._hook_wiring import command_verb, is_plugin_launcher, launcher_script_args

pytestmark = pytest.mark.lint

HOOKS_JSON = Path(__file__).resolve().parents[1] / "conexus" / "hooks" / "hooks.json"

#: Seconds a hook may wait on uv's cold start (a Python fetch plus the one-time
#: tool environment) before its own work begins. Not measured on Windows: the
#: Mac fetch is 2.3 s and the Windows gate hosts pull 6 to 11 MB/s shared, with
#: Defender scanning every new executable. Generous on purpose, free when warm.
COLD_START_ALLOWANCE_S = 30

#: The seconds each SessionStart entry's own work was given before this fix: its
#: timeout then. Keyed by the nx-hook verb, else the script's file name.
WORK_BUDGET_S: dict[str, int] = {
    "nx_setup_notice.py": 5,
    "upgrade-auto": 30,
    "preflight": 10,
    "self-gc": 30,
    "session-start": 10,
    "session-context": 10,
    "rdr": 10,
    "version_lockstep_hook.py": 5,
    "mcp-connect-wait": 20,
}


def _key(entry: dict) -> str:
    verb = command_verb(entry)
    if verb is not None:
        return verb
    return Path(launcher_script_args(entry)[0]).name


def _session_start_entries(hooks_json: Path = HOOKS_JSON) -> list[dict]:
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    return [h for group in data["hooks"]["SessionStart"] for h in group["hooks"]]


def test_the_budget_table_is_exactly_the_session_start_entries() -> None:
    entries = _session_start_entries()
    assert all(is_plugin_launcher(e) for e in entries), (
        "a SessionStart entry is not launched through uv: this pin no longer describes it"
    )
    keys = [_key(e) for e in entries]
    assert len(keys) == len(set(keys)), f"two SessionStart entries share a key: {keys}"
    assert set(keys) == set(WORK_BUDGET_S), (
        f"hooks.json SessionStart entries {sorted(keys)} differ from the budget table "
        f"{sorted(WORK_BUDGET_S)}: decide the new entry's work budget here"
    )
    assert len(entries) >= 9, "the sweep found fewer SessionStart entries than shipped: vacuous"


def test_every_session_start_timeout_covers_the_cold_start_and_the_hooks_own_work() -> None:
    short = {
        _key(e): (e["timeout"], COLD_START_ALLOWANCE_S + WORK_BUDGET_S[_key(e)])
        for e in _session_start_entries()
        if e["timeout"] < COLD_START_ALLOWANCE_S + WORK_BUDGET_S[_key(e)]
    }
    assert not short, (
        f"SessionStart entries whose timeout is under the {COLD_START_ALLOWANCE_S} s cold-start "
        f"allowance plus their own work (entry: (timeout, needed)): {short}"
    )


def test_the_notice_and_the_lockstep_hook_which_run_no_nx_code_get_the_allowance() -> None:
    """The two scripts the Windows gate saw cancelled at 5 s start no nx code, so
    the whole of their time is uv's start."""
    by_key = {_key(e): e["timeout"] for e in _session_start_entries()}
    assert by_key["nx_setup_notice.py"] >= COLD_START_ALLOWANCE_S
    assert by_key["version_lockstep_hook.py"] >= COLD_START_ALLOWANCE_S


def test_the_barrier_timeout_clears_its_own_bound_plus_the_cold_start() -> None:
    """``mcp-connect-wait`` waits up to its bound for nx-mcp, which is itself
    started through uv; the 20 s it had left 5 s for the cold start."""
    timeout = {_key(e): e["timeout"] for e in _session_start_entries()}["mcp-connect-wait"]
    assert timeout >= COLD_START_ALLOWANCE_S + mcp_connect_wait._BOUND_SECONDS


def test_the_budget_pins_still_hold_with_the_larger_timeouts() -> None:
    """The internal budgets in rdr and upgrade-auto were pinned under the old
    timeouts; the bigger ones only widen the margin, and a bound raised past the
    work budget recorded here would eat the cold-start allowance."""
    assert rdr_verb._HOOK_BUDGET_S + 2.0 <= WORK_BUDGET_S["rdr"]
    assert upgrade_auto._SKEW_WAIT_S + 2.0 <= WORK_BUDGET_S["upgrade-auto"]
