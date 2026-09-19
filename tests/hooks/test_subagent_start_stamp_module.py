# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported SubagentStart ledger stamp (RDR-215 bead nexus-q02nx.11).

The bash script stays wired until beads .21/.22 re-declare it, so the
differential class runs both and compares the resulting ledger.

One case is deliberately NOT expected to match: an empty middle field.
The bash decodes with ``IFS=$'\\t' read``, and tab is IFS whitespace, so
an empty ``agent_id`` collapses and ``agent_type`` shifts into its place.
``agent-dispatch-expect.sh``'s own header says this script "is currently
benign only by luck". The port cannot reproduce that because it reads the
payload dict directly, and it should not try.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nexus.hooks import expectations as exp
from nexus.hooks import subagent_start_stamp as hook

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "subagent-start-stamp.sh"
)


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

    def test_bash_really_does_shift_the_field(self, state):
        """Pins the defect the port is NOT reproducing, so the claim above
        is evidence rather than assertion. If bash is ever fixed, this
        turns red and the docstrings should stop claiming a divergence."""
        payload = {"session_id": "theirs", "agent_id": "", "agent_type": "conexus:developer"}
        subprocess.run(
            ["bash", str(_SCRIPT)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]},
        )
        rows = _rows("theirs")
        if rows:
            assert rows[0][2] == "conexus:developer", (
                "bash is expected to shift agent_type into the agent_id slot; "
                "if it no longer does, the port's divergence note is stale"
            )


class TestAgreesWithTheLiveBashScript:
    def _bash(self, payload: dict, state_home: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "XDG_STATE_HOME": state_home},
        )

    @pytest.mark.parametrize(
        "payload,label",
        [
            (_payload(), "ordinary stamp"),
            (_payload(agent_type="conexus:critic"), "colon-qualified type"),
            (_payload(session_id=""), "no session id"),
            (_payload(agent_type=""), "no agent type"),
            (_payload(agent_id="a-with-dashes_and_underscores"), "id charset"),
        ],
    )
    def test_the_ledger_matches(self, state, payload, label):
        mine = dict(payload, session_id=payload["session_id"] and "mine")
        theirs = dict(payload, session_id=payload["session_id"] and "theirs")
        hook.run(mine)
        proc = self._bash(theirs, os.environ["XDG_STATE_HOME"])
        assert proc.returncode == 0, f"{label}: bash must always exit 0"
        assert proc.stdout == "", f"{label}: bash is stdout-silent"
        assert [r[1:] for r in _rows("mine")] == [r[1:] for r in _rows("theirs")], (
            f"{label}: the ledger drifted from bash"
        )

    def test_bash_also_stamps_at_most_once(self, state):
        payload = _payload(session_id="theirs")
        self._bash(payload, os.environ["XDG_STATE_HOME"])
        self._bash(payload, os.environ["XDG_STATE_HOME"])
        assert len(_rows("theirs")) == 1
