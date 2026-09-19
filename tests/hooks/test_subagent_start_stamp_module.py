# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported SubagentStart ledger stamp (RDR-215 bead nexus-q02nx.11).

RDR-215 bead nexus-q02nx.21 re-declared the ``SubagentStart`` entry to the
``hook_subagent_start_stamp`` mcp_tool and the bash script is deleted, so
this file drives the Python module only. The differential class that used
to compare it against bash (``TestAgreesWithTheLiveBashScript``) is gone;
every scenario it covered has a direct assertion above (e.g.
``test_a_start_row_is_written``, ``test_the_agent_type_is_verbatim_colon_included``,
``test_the_same_agent_is_stamped_once``,
``test_an_agent_id_with_dashes_and_underscores_stamps``).

The bash had one known defect the port deliberately does NOT reproduce: it
decoded with ``IFS=$'\\t' read``, and tab is IFS whitespace, so an empty
``agent_id`` collapsed and ``agent_type`` shifted into its place. Reading
the payload dict directly cannot shift anything; that guarantee is what
``TestTheFieldShiftBugClassIsGone`` pins (the bash-side proof of the
defect it used to carry alongside is gone with the script it drove).
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from nexus.hooks import expectations as exp
from nexus.hooks import subagent_start_stamp as hook


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("NX_ORCH_STOP_GUARD", raising=False)
    return tmp_path


def _payload(**over) -> dict:
    base = {"session_id": "sess-A", "agent_id": "a1", "agent_type": "conexus:developer"}
    base.update(over)
    return base


def _rows(session: str = "sess-A") -> list[list[str]]:
    try:
        text = Path(exp.expectations_file(session)).read_text()
    except OSError:
        return []
    return [line.split("\t") for line in text.splitlines() if line]


class TestTheStamp:
    def test_a_start_row_is_written(self, state):
        hook.run(_payload())
        assert _rows()[0][1:4] == ["START", "a1", "conexus:developer"]

    def test_the_agent_type_is_verbatim_colon_included(self, state):
        hook.run(_payload(agent_type="conexus:substantive-critic"))
        assert _rows()[0][3] == "conexus:substantive-critic"

    def test_the_same_agent_is_stamped_once(self, state):
        hook.run(_payload())
        hook.run(_payload())
        assert len(_rows()) == 1

    def test_two_agents_both_stamp(self, state):
        hook.run(_payload(agent_id="a1"))
        hook.run(_payload(agent_id="a2"))
        assert len(_rows()) == 2

    def test_an_agent_id_with_dashes_and_underscores_stamps(self, state):
        hook.run(_payload(agent_id="a-with-dashes_and_underscores"))
        assert _rows()[0][2] == "a-with-dashes_and_underscores"

    def test_idempotent_under_concurrent_invocation(self, state):
        """The shape that actually broke in production (nexus-3h0u6),
        ported from ``tests/hooks/test_subagent_start_stamp.py``'s bash
        version: a sequential double-call cannot reproduce a TOCTOU, since
        production wrote duplicate START rows with identical timestamps
        because the guard was a check-then-append and two registrations
        fired at the same instant. This launches the calls simultaneously
        against the SAME state dir."""
        payload = _payload()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(hook.run, payload) for _ in range(8)]
            for f in futures:
                assert f.result().exit_code == 0
        assert len(_rows()) == 1, (
            "8 concurrent stamps must compose to ONE row, got:\n" + repr(_rows())
        )


class TestTheSkipPaths:
    """Silent, exit 0, and no row. SubagentStart fires for dispatches this
    ledger does not track, so a missing field is ordinary rather than an
    anomaly worth an operator-facing line."""

    @pytest.mark.parametrize(
        "missing", ["session_id", "agent_id", "agent_type"]
    )
    def test_any_missing_field_writes_nothing(self, state, missing):
        result = hook.run(_payload(**{missing: ""}))
        assert result.stdout is None and result.exit_code == 0
        assert _rows() == []

    def test_a_path_unsafe_session_id_writes_nothing(self, state):
        assert hook.run(_payload(session_id="../../escape")).exit_code == 0

    def test_the_guard_being_off_skips(self, state, monkeypatch):
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "off")
        hook.run(_payload())
        assert _rows() == []

    def test_a_none_payload_is_survivable(self, state):
        assert hook.run(None).exit_code == 0

    @pytest.mark.parametrize(
        "payload", [None, {}, _payload(), _payload(agent_id=""), _payload(session_id="../x")]
    )
    def test_stdout_is_always_none(self, state, payload):
        assert hook.run(payload).stdout is None


class TestTheFieldShiftBugClassIsGone:
    """The port removes a real defect rather than reproducing it.

    ``IFS=$'\\t' read`` collapses an empty field because tab is IFS
    whitespace, so bash reading ``session\\t\\ttype`` shifts type into
    agent_id. Reading the payload dict cannot shift anything.
    """

    def test_an_empty_agent_id_never_becomes_the_agent_type(self, state):
        hook.run(_payload(agent_id="", agent_type="conexus:developer"))
        assert _rows() == [], "no row at all — not a row with the type in the id slot"
