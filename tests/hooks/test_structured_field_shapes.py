# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A contractually-object payload field, read the same way on both tiers (nexus-17i1n).

``tool_input`` reaches a hook by two routes. On the command tier it is a
real dict, lifted straight out of the harness's stdin JSON. On the tool
tier it comes through a ``hooks.json`` ``input`` map, and the tool's
parameter is typed ``Any``, so a caller can hand it the JSON *text* just
as readily as the object.

Three ported hooks read it with a bare ``isinstance(x, dict)`` and fell
through to a default when it was not one. None crashed. Each produced a
plausible wrong answer instead: the close gate read the whole JSON blob
as the command and found no ``bd`` verb in it, so it allowed the close;
``agent_dispatch_expect`` recorded every dispatch as ``general-purpose``,
which the RDR-184 ledger reads as a phantom credit plus an undeclared
start; ``divergence_language_guard`` scanned nothing.

These tests pin the coercion and, for each of the three hooks, pin that
the two shapes now produce the SAME decision — which is the property
that was actually missing, not the coercion itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus._hook_runtime._io import structured_field
from nexus.hooks import agent_dispatch_expect as dispatch_mod
from nexus.hooks import pre_close_verification as mod
from nexus.hooks import stop_verification as stop_mod
from nexus.hooks.pre_close_verification import _bd_verbs, run


@pytest.fixture
def unmarked_t1(monkeypatch):
    """Arm the close gate: T1 reachable, and no bead carries a marker.

    Three separate things make the gate allow in a bare unit test, and
    all three have to be off the table before an ``allow`` can be read
    as being about the payload shape: the ``on_close`` config is not
    enabled, T1 is unreachable for a bogus session id, and an env
    override may be set from the ambient environment. Each is a real
    fail-open path with its own reason to exist; none is what these
    tests are about.

    Stubbing is the right isolation rather than a shortcut. The claim
    here is that the COMMAND is extracted from each payload shape; the
    marker lookup and the config are separate mechanisms with their own
    tests. Without this the gate allows for reasons unrelated to shape
    and the assertion passes against the defect — which is exactly what
    an earlier draft of these tests did.
    """
    def _all_missing(bead_ids: list[str]) -> dict:
        # Keys mirror the real _coverage return exactly; a stub missing
        # one raises inside the deny path, which is a fail that reads as
        # the gate being broken rather than the stub being wrong.
        return {
            "t1_reachable": True,
            "status": {bead_id: "missing" for bead_id in bead_ids},
            "deadline_seconds": 3.5,
            "seen_names": [],
        }

    monkeypatch.setattr(mod, "_coverage", _all_missing)
    monkeypatch.setattr(stop_mod, "_read_config", lambda: {"on_close": True})
    monkeypatch.delenv("NX_REVIEW_GATE_OVERRIDE", raising=False)
    return _all_missing


class TestStructuredField:
    def test_a_dict_passes_through_unchanged(self) -> None:
        payload = {"tool_input": {"command": "bd close nexus-abcde"}}
        assert structured_field(payload, "tool_input") == {"command": "bd close nexus-abcde"}

    def test_json_text_holding_an_object_is_parsed(self) -> None:
        inner = {"command": "bd close nexus-abcde", "description": "close it"}
        payload = {"tool_input": json.dumps(inner)}
        assert structured_field(payload, "tool_input") == inner

    @pytest.mark.parametrize("value", [None, ""])
    def test_absent_and_empty_are_an_empty_mapping(self, value: object) -> None:
        assert structured_field({"tool_input": value}, "tool_input") == {}

    def test_a_missing_key_is_an_empty_mapping(self) -> None:
        assert structured_field({}, "tool_input") == {}

    @pytest.mark.parametrize(
        "value",
        [
            "bd close nexus-abcde",  # a bare string, not JSON at all
            "[1, 2, 3]",  # JSON, but an array
            "42",  # JSON, but a scalar
            ["a", "b"],  # already a list
            17,
        ],
    )
    def test_a_shape_it_cannot_read_is_empty_rather_than_an_exception(self, value: object) -> None:
        """Fails open, always. A hook must never take down the event it observes."""
        assert structured_field({"tool_input": value}, "tool_input") == {}


class TestTheThreeHooksAgreeAcrossShapes:
    """The property the original code lacked: same payload, same answer.

    Each hook gets its own test rather than a shared parametrization,
    because what counts as "the same answer" differs per hook — a
    verdict for the close gate, a ledger row for the dispatch recorder,
    a resolved path for the divergence guard — and collapsing them onto
    one comparison is what would make the check generic enough to stop
    meaning anything.
    """

    def test_close_gate_sees_the_bd_verb_through_json_text(self, unmarked_t1) -> None:
        command = "bd close nexus-99xyz --reason probe"

        # The mechanism, at its narrowest: _bd_verbs never saw the verb
        # inside JSON quoting, and no verb meant no gate.
        assert _bd_verbs(command)["has_close_or_done"] is True
        assert _bd_verbs(json.dumps({"command": command}))["has_close_or_done"] is False

        as_dict = run(
            {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": command}}
        )
        as_text = run(
            {"session_id": "s", "tool_name": "Bash", "tool_input": json.dumps({"command": command})}
        )
        # Assert the verdict by VALUE, not just that the two agree. An
        # earlier draft compared them only to each other and passed
        # against the unfixed code, because with no reachable T1 both
        # shapes allow and equality holds for the wrong reason — the
        # check's domain has to contain the thing claimed.
        assert _decision(as_dict) == "deny", "the fixture did not arm the gate"
        assert _decision(as_text) == "deny", (
            "the close gate allows when tool_input arrives as its own JSON "
            "text — the 7.55.0 defect"
        )

    def test_a_bare_command_string_is_still_read_as_the_command(self, unmarked_t1) -> None:
        """The deliberate direct-caller path, kept. Only JSON text was the bug."""
        command = "bd close nexus-99xyz --reason probe"
        bare = run({"session_id": "s", "tool_name": "Bash", "tool_input": command})
        assert _decision(bare) == "deny"

    def test_the_gate_still_fails_open_when_t1_cannot_be_reached(self) -> None:
        """No fixture here, deliberately: this is the unarmed case.

        It is also what made the two tests above vacuous before the
        fixture existed, so it is worth holding as a fact rather than
        leaving as an accident — the gate allows rather than blocking
        every close when it cannot check.
        """
        command = "bd close nexus-99xyz --reason probe"
        assert (
            _decision(run({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": command}}))
            == "allow"
        )

    def test_dispatch_expect_records_the_same_subagent_type_either_way(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "observe")

        inner = {"subagent_type": "conexus:code-review-expert", "run_in_background": True}

        dispatch_mod.run({"session_id": "shape-a", "tool_name": "Agent", "tool_use_id": "u1", "tool_input": inner})
        dispatch_mod.run(
            {
                "session_id": "shape-b",
                "tool_name": "Agent",
                "tool_use_id": "u1",
                "tool_input": json.dumps(inner),
            }
        )

        rows_a = _ledger_rows(tmp_path, "shape-a")
        rows_b = _ledger_rows(tmp_path, "shape-b")
        assert rows_a and rows_b, f"no ledger rows written: a={rows_a!r} b={rows_b!r}"
        assert "conexus:code-review-expert" in rows_a
        assert "conexus:code-review-expert" in rows_b, (
            "the JSON-text shape recorded a different subagent type — this is the "
            "phantom-credit defect: a wrong type is worse than a missing row"
        )

    def test_divergence_guard_resolves_the_same_file_path_either_way(self) -> None:
        path = "/x/docs/rdr/post-mortem/pm-001.md"
        inner = {"file_path": path}
        # By value on both sides. Comparing the two to each other alone
        # would hold when both resolve to "", which is precisely the
        # unfixed behaviour for the JSON-text shape.
        assert structured_field({"tool_input": inner}, "tool_input").get("file_path") == path
        assert (
            structured_field({"tool_input": json.dumps(inner)}, "tool_input").get("file_path")
            == path
        )


def _decision(result) -> str:
    """The permissionDecision out of a hook's envelope, or '' if it said nothing."""
    if not result.stdout:
        return ""
    try:
        return str(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])
    except Exception:  # noqa: BLE001 — any unparseable stdout means "said nothing"
        return ""


def _ledger_rows(state_home, session_id: str) -> str:
    d = Path(state_home) / "nexus" / "orchestration"
    hits = list(d.glob(f"{session_id}*"))
    return "\n".join(p.read_text() for p in hits if p.is_file())
