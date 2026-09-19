# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported PreToolUse(Agent|Task) EXPECT writer (RDR-215 bead nexus-q02nx.10).

The bash script stays live until its ``hooks.json`` entry is re-declared,
so the differential class at the bottom runs both and compares the ledger
each produces. That is the only check that catches drift while a live
session can be written by one and read by the other.

**Stdout is asserted empty on every path.** The hook is stdout-silent by
contract: a stray byte there is a malformed hook decision, and this is the
hook that fires on every single agent dispatch.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nexus.hooks import agent_dispatch_expect as hook
from nexus.hooks import expectations as exp

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "agent-dispatch-expect.sh"
)


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("NX_ORCH_STOP_GUARD", raising=False)
    return tmp_path


def _payload(**over) -> dict:
    base = {
        "session_id": "sess-A",
        "tool_name": "Agent",
        "tool_use_id": "d1",
        "tool_input": {"subagent_type": "conexus:developer", "run_in_background": True},
    }
    base.update(over)
    return base


def _rows(session: str = "sess-A") -> list[list[str]]:
    try:
        text = Path(exp.expectations_file(session)).read_text()
    except OSError:
        return []
    return [line.split("\t") for line in text.splitlines() if line]


class TestTheRowItWrites:
    def test_a_dispatch_writes_one_expect_row(self, state):
        hook.run(_payload())
        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1:5] == ["EXPECT", "conexus:developer", "background", "d1"]

    def test_the_subagent_type_is_carried_verbatim_colon_included(self, state):
        hook.run(_payload(tool_input={"subagent_type": "conexus:substantive-critic"}))
        assert _rows()[0][2] == "conexus:substantive-critic"

    def test_run_in_background_false_is_a_sync_row(self, state):
        hook.run(_payload(tool_input={"subagent_type": "t", "run_in_background": False}))
        assert _rows()[0][3] == "sync"

    @pytest.mark.parametrize("falsey", ["false", "FALSE", "0", "no", ""])
    def test_the_string_forms_of_false_are_honoured(self, state, falsey):
        """The field has arrived as a string from the harness."""
        hook.run(_payload(tool_input={"subagent_type": "t", "run_in_background": falsey}))
        assert _rows()[0][3] == "sync"

    def test_an_absent_mode_defaults_to_background(self, state):
        """A dispatch whose mode cannot be read still deserves a row, and
        background is the one that can later require a report."""
        hook.run(_payload(tool_input={"subagent_type": "t"}))
        assert _rows()[0][3] == "background"

    def test_a_missing_subagent_type_is_keyed_general_purpose(self, state):
        """nexus-a795d: the harness genuinely starts a general-purpose agent
        when the field is absent, so keying it anything else would put a row
        under a type no START will ever carry — which reads later as an
        expected-but-never-started dispatch. docs/cli-reference.md
        documents this."""
        hook.run(_payload(tool_input={}))
        assert _rows()[0][2] == "general-purpose"

    def test_a_non_dict_tool_input_is_survivable(self, state):
        hook.run(_payload(tool_input="not a dict"))
        assert _rows()[0][2] == "general-purpose"


class TestItNeverDoubleCounts:
    def test_the_same_dispatch_id_writes_once(self, state):
        """A duplicate EXPECT is not the harmless nuisance a duplicate START
        is: it inflates the credit pool and MASKS an undeclared start."""
        hook.run(_payload())
        hook.run(_payload())
        assert len(_rows()) == 1

    def test_two_distinct_dispatch_ids_both_write(self, state):
        hook.run(_payload(tool_use_id="d1"))
        hook.run(_payload(tool_use_id="d2"))
        assert len(_rows()) == 2

    def test_an_absent_dispatch_id_cannot_be_deduped_so_both_write(self, state):
        """Honest about the limit: with no tool_use_id there is nothing to
        match on, and writing twice is better than dropping a real dispatch."""
        hook.run(_payload(tool_use_id=""))
        hook.run(_payload(tool_use_id=""))
        assert len(_rows()) == 2


class TestTheSkipPaths:
    """Every skip returns cleanly and writes NO row. The bash exits 0
    unconditionally on all of them."""

    def test_a_non_dispatch_tool_is_skipped(self, state):
        assert hook.run(_payload(tool_name="Bash")).stdout is None
        assert _rows() == []

    def test_an_empty_session_id_is_skipped(self, state):
        hook.run(_payload(session_id=""))
        assert _rows() == []

    def test_a_path_unsafe_session_id_is_skipped(self, state):
        """expectations_file refuses it; the hook must not propagate that."""
        result = hook.run(_payload(session_id="../../escape"))
        assert result.stdout is None
        assert result.exit_code == 0

    def test_the_guard_being_off_skips_everything(self, state, monkeypatch):
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "off")
        hook.run(_payload())
        assert _rows() == []

    @pytest.mark.parametrize("mode", ["observe", "block"])
    def test_both_live_guard_modes_write(self, state, monkeypatch, mode):
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", mode)
        hook.run(_payload())
        assert len(_rows()) == 1

    def test_a_none_payload_is_survivable(self, state):
        assert hook.run(None).exit_code == 0
        assert _rows() == []


class TestStdoutIsSilentOnEveryPath:
    """This hook fires on EVERY agent dispatch, so a stray stdout byte is
    both a malformed decision and a very frequent one."""

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            _payload(),
            _payload(tool_name="Bash"),
            _payload(session_id=""),
            _payload(session_id="../../escape"),
            _payload(tool_input={}),
            _payload(tool_input="junk"),
        ],
    )
    def test_stdout_is_always_none(self, state, payload):
        assert hook.run(payload).stdout is None

    def test_the_result_never_carries_a_nonzero_exit(self, state):
        for payload in (None, {}, _payload(), _payload(tool_name="Bash")):
            assert hook.run(payload).exit_code == 0


class TestFieldScrubbing:
    def test_a_tab_in_the_subagent_type_cannot_reshape_the_row(self, state):
        """A tab would split a TSV field and silently reshape every later
        read of this ledger."""
        hook.run(_payload(tool_input={"subagent_type": "bad\ttype"}))
        rows = _rows()
        assert rows == [] or "\t" not in rows[0][2]

    def test_a_newline_in_the_dispatch_id_cannot_forge_a_row(self, state):
        """The invariant is ONE ROW, not a sanitised string.

        The scrub turns \n and \t into spaces, so the injected text
        survives as inert content inside field 5 rather than becoming a
        second row — verified byte-identical to bash, which scrubs the
        same way. An earlier version of this test asserted the word
        "forged" was absent, which is a property neither implementation
        has and which would have failed the port for matching bash.
        """
        hook.run(_payload(tool_use_id="d1\n2026-01-01T00:00:00Z\tEXPECT\tforged\tbackground"))
        rows = _rows()
        assert len(rows) == 1, "a newline must not be able to append a second row"
        assert "\n" not in rows[0][4] and "\t" not in rows[0][4]
        assert rows[0][1] == "EXPECT" and rows[0][2] == "conexus:developer"


class TestAgreesWithTheLiveBashScript:
    """The script is still wired in hooks.json, so the two must agree."""

    def _bash(self, payload: dict, state_home: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "XDG_STATE_HOME": state_home},
        )

    @pytest.mark.parametrize(
        "payload,label",
        [
            (_payload(), "ordinary background dispatch"),
            (_payload(tool_input={"subagent_type": "t", "run_in_background": False}), "sync"),
            (_payload(tool_input={}), "missing subagent_type"),
            (_payload(tool_name="Bash"), "not a dispatch tool"),
            (_payload(session_id=""), "empty session id"),
            (_payload(tool_use_id=""), "no dispatch id"),
            (_payload(tool_input={"subagent_type": "conexus:critic"}), "colon-qualified"),
        ],
    )
    def test_the_ledger_matches(self, state, payload, label):
        mine = dict(payload, session_id=(payload.get("session_id") and "mine") or "")
        theirs = dict(payload, session_id=(payload.get("session_id") and "theirs") or "")
        hook.run(mine)
        proc = self._bash(theirs, os.environ["XDG_STATE_HOME"])
        assert proc.returncode == 0, f"{label}: bash must always exit 0"
        assert proc.stdout == "", f"{label}: bash must be stdout-silent"
        mine_rows = [r[1:] for r in _rows("mine")]
        theirs_rows = [r[1:] for r in _rows("theirs")]
        assert mine_rows == theirs_rows, f"{label}: the ledger drifted from bash"

    def test_bash_also_declines_to_double_count(self, state):
        """The property that matters most, checked on both sides."""
        payload = _payload(session_id="theirs")
        self._bash(payload, os.environ["XDG_STATE_HOME"])
        self._bash(payload, os.environ["XDG_STATE_HOME"])
        assert len(_rows("theirs")) == 1
