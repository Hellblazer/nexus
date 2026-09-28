# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported PreToolUse(Agent|Task) EXPECT writer (RDR-215 bead nexus-q02nx.10).

**Stdout is asserted empty on every path.** The hook is stdout-silent by
contract: a stray byte there is a malformed hook decision, and this is the
hook that fires on every single agent dispatch.

RDR-215 bead nexus-q02nx.21 re-declared the ``PreToolUse(Agent)`` entry to
the ``hook_agent_dispatch_expect`` mcp_tool, so the bash script no longer
runs in production; bead nexus-5l8i8 moved the wiring again, off that
mcp_tool entry onto the command tier (``nx-hook agent-dispatch-expect``,
via ``nx_hook_shim.py``) -- an mcp_tool hook's invocation depends on this
session's own MCP connection, and an outage was silently dropping the
EXPECT row this module writes. The tool stays registered for diagnosis;
it is not what fires in a real session either way. The differential class
that used to compare this module against the bash script
(``TestAgreesWithTheLiveBashScript``) is gone; every scenario it covered
has a direct assertion above (e.g.
``test_a_dispatch_writes_one_expect_row``,
``test_the_subagent_type_is_carried_verbatim_colon_included``,
``test_the_same_dispatch_id_writes_once``).

**``tool_use_id`` must be ``toolu_``-shaped (nexus-5l8i8).** Every
synthetic id fed to ``hook.run()`` below is ``toolu_``-prefixed for that
reason; a hand-invoked probe with a non-``toolu_`` id (e.g.
``"probe-dict"``) is exactly the pollution shape ``TestTheSkipPaths``
covers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.hooks import agent_dispatch_expect as hook
from nexus.hooks import expectations as exp


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("NX_ORCH_STOP_GUARD", raising=False)
    return tmp_path


def _payload(**over) -> dict:
    base = {
        "session_id": "sess-A",
        "tool_name": "Agent",
        "tool_use_id": "toolu_d1",
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
        assert rows[0][1:5] == ["EXPECT", "conexus:developer", "background", "toolu_d1"]

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
        hook.run(_payload(tool_use_id="toolu_d1"))
        hook.run(_payload(tool_use_id="toolu_d2"))
        assert len(_rows()) == 2

    def test_an_absent_dispatch_id_is_skipped(self, state):
        """Superseded by the id-shape guard (nexus-5l8i8): an empty
        tool_use_id used to write an un-deduped row on the reasoning that
        "writing twice is better than dropping a real dispatch" -- but a
        genuine PreToolUse(Agent|Task) firing always carries a real
        toolu_-shaped id (Claude Code's own hook-input contract), so an
        empty one is never a real dispatch to begin with. It is refused
        the same as any other malformed id, rather than written."""
        hook.run(_payload(tool_use_id=""))
        hook.run(_payload(tool_use_id=""))
        assert _rows() == []


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

    def test_a_probe_shaped_tool_use_id_is_skipped(self, state):
        """The regression this bead closes (nexus-5l8i8, hypothesis (b)):
        session 81d1d28b's pollution came from hand-calling this hook's own
        registered MCP tool with ``tool_use_id`` of ``"probe-dict"`` /
        ``"probe-str"``. Neither is ``toolu_``-shaped, so both are refused
        the same as any other malformed id -- closing the manual-invocation
        vector at this hook's own boundary, independent of which tier
        hooks.json wires it on."""
        for probe_id in ("probe-dict", "probe-str"):
            result = hook.run(_payload(tool_use_id=probe_id))
            assert result.stdout is None
            assert result.exit_code == 0
        assert _rows() == []

    def test_a_toolu_shaped_id_is_not_skipped(self, state):
        """Non-vacuity for the guard above: it discriminates, it does not
        just refuse everything."""
        hook.run(_payload(tool_use_id="toolu_01realShapedId"))
        assert len(_rows()) == 1

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

    def test_a_newline_in_the_dispatch_id_is_refused_outright(self, state):
        """Superseded by the id-shape guard (nexus-5l8i8): this used to be
        a SCRUB test -- the invariant was "one row, not a sanitised
        string", because \\n/\\t in the id survived as inert content
        rather than forging a second row. The shape guard makes the
        premise stronger: ``^toolu_[A-Za-z0-9]+$`` cannot match a string
        containing a newline or tab AT ALL, so this injection attempt
        never reaches the write path (or the scrub) in the first place --
        it is refused the same as any other non-``toolu_`` id, and NO row
        is written. The scrub itself is unaffected and still defends
        OTHER fields (see ``test_a_tab_in_the_subagent_type_cannot_
        reshape_the_row`` above), which are not shape-checked."""
        hook.run(_payload(tool_use_id="d1\n2026-01-01T00:00:00Z\tEXPECT\tforged\tbackground"))
        assert _rows() == [], "a shape-invalid id must be refused, not scrubbed and written"


