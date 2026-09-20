# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported close gate (RDR-215 bead nexus-q02nx.17).

``test_pre_close_verification_hook.py`` owns the decision surface and
drives both implementations through a child process. This file owns what
that harness cannot reach: the deny text as a CONTRACT, the two
false-positive fixes in the bead-id harvester, and the one limit the port
records rather than fixes.
"""
from __future__ import annotations

import json

import pytest

from nexus.hooks import pre_close_verification as gate

#: Split so this file cannot trip the gate it tests. Not decoration: the
#: verb detector reads heredoc and docstring bytes as shell structure
#: (see TestTheLimitThePortRecords below), and a literal here would make
#: any command that greps this file look like a close.
CLOSE = "bd " + "close"


class TestTheDenyTextIsACarriedContract:
    """The remedy block is duplicated NOWHERE, and that is why it needs
    pinning.

    Measured rather than repeated: before the RDR-215 port exactly one
    file on disk carried the string ``Close blocked: no
    review-completed`` -- the script. The gate CONCEPT is referenced in
    about ten documents, which is a different claim and the one the bead
    actually makes.

    Scarcity is the hazard, not ubiquity. A text living in twenty places
    cannot be quietly reworded; one living in a single place can, and
    then the documents describing the gate drift from what it says with
    nothing to disagree with them.

    RDR-215 bead nexus-q02nx.21 deleted the script: the port's own
    ``_deny_message`` output is now the single place this wording lives,
    so this class asserts against it directly. The comparison against
    the script's own bytes (never a retyped copy) that used to guard
    against silent drift served its purpose during the port; there is no
    longer a second copy to drift from.
    """

    @staticmethod
    def _rendered() -> str:
        return gate._deny_message(
            missing=["nexus-aaaaa"],
            deadline_ids=[],
            incomplete=[],
            deadline_seconds="3.5",
            seen_names={},
        )

    @pytest.mark.parametrize(
        "phrase",
        [
            "Close blocked: no review-completed marker found in T1 scratch",
            "Run the marker write as a SEPARATE tool call",
            "Run the stacked reviewers (code-review-expert + substantive-critic)",
            "The marker MUST name both reviewers; naming one (or neither) is refused",
            "the marker is reserved to the gate-owning session",
            "it is not yours to reach for",
        ],
    )
    def test_the_port_renders_the_same_phrase(self, phrase):
        assert phrase in self._rendered()

    def test_the_remedy_names_the_exact_command_an_operator_must_run(self):
        """The one line someone copies. A paraphrase here costs a
        round trip for every blocked close."""
        rendered = self._rendered()
        assert 'nx scratch put "review-completed:' in rendered
        assert "--tags" in rendered
        assert rendered.count("code-review-expert") >= 1
        assert rendered.count("substantive-critic") >= 1


class TestTheHarvesterFixes:
    """Two false-positive sources, measured before and after. Both make
    the gate demand a review marker for something that is not a bead."""

    def test_a_worktree_path_is_not_a_close_target(self):
        """Ours, created by the worktree convention: a command that
        changes directory before closing harvested the directory names."""
        ids = gate._bead_ids(f"cd /Users/x/git/nexus-wt/nexus-01 && {CLOSE} nexus-q02nx.17")
        assert ids == ["nexus-q02nx"], ids

    def test_a_peer_worktree_path_is_not_a_close_target(self):
        """The dangerous instance: this path yields a token that IS
        bead-shaped, so the refusal reads as a real gate failure."""
        ids = gate._bead_ids(f"cd /Users/x/git/nexus-wt/nexus-c3 && {CLOSE} nexus-q02nx.17")
        assert ids == ["nexus-q02nx"], ids

    def test_an_id_in_a_short_reason_flag_is_not_a_close_target(self):
        """PRE-EXISTING and unrelated to worktrees: -r is bd's real short
        form for --reason and the harvester did not know it, so any bead
        id mentioned in a close reason became a target. Taken from
        `bd close --help`, not guessed."""
        ids = gate._bead_ids(f'{CLOSE} nexus-q02nx.17 -r "supersedes nexus-zzzzz"')
        assert ids == ["nexus-q02nx"], ids

    def test_an_id_in_a_reason_file_path_is_not_a_close_target(self):
        ids = gate._bead_ids(f"{CLOSE} nexus-q02nx.17 --reason-file nexus-93.txt")
        assert ids == ["nexus-q02nx"], ids

    def test_real_targets_still_survive_both_fixes(self):
        """The fixes must not make the harvester find NOTHING — that
        routes to INDETERMINATE, which ALLOWS. Over-narrowing here is the
        failure direction that matters."""
        assert gate._bead_ids(f"{CLOSE} nexus-aaaaa nexus-bbbbb") == [
            "nexus-aaaaa", "nexus-bbbbb",
        ]


class TestTheLimitThePortRecords:
    """The body-text defect, pinned as it actually behaves rather than as
    it was first described.

    Recorded, not fixed: teaching the tokenizer about heredocs is a parser
    change, and a wrong attempt stops the gate detecting real closes,
    which fails OPEN. These tests exist so the limit is stated in
    executable form — if someone fixes it, they turn red and that is the
    signal to delete this class.
    """

    def test_a_quoted_mention_is_correctly_ignored(self):
        """Already fixed by nexus-fv65m. The port's first description of
        this defect wrongly claimed commit messages trip it; they do
        not, and this is the test that would have said so."""
        v = gate._bd_verbs(f'git commit -m "docs: {CLOSE} workflow notes"')
        assert v["has_close_or_done"] is False

    def test_an_operator_inside_heredoc_body_text_does_trip_it(self):
        """The real trigger. shlex does not model heredocs, so an && in
        the body becomes a genuine operator token, splits a segment, and
        the next segment starts with bd."""
        v = gate._bd_verbs(f"python3 - <<'PY'{chr(10)}s = 'x && {CLOSE} y'{chr(10)}PY")
        assert v["has_close_or_done"] is True, (
            "if this is now False the heredoc limit has been FIXED — "
            "delete this class and the KNOWN LIMIT note in _bd_verbs"
        )

    def test_heredoc_body_without_an_operator_is_fine(self):
        """Bounds the claim: it is the operator token that does it, not
        heredocs as such."""
        v = gate._bd_verbs(f"cat <<'EOF'{chr(10)}run {CLOSE} to finish{chr(10)}EOF")
        assert v["has_close_or_done"] is False


class TestTheEnvelopes:
    def test_allow_without_context_matches_the_scripts_shape(self):
        assert json.loads(gate._allow().stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    def test_deny_carries_both_reason_fields_and_a_system_message(self):
        """The defect the bead names, plus the half it did not. The
        create branch hand-built its JSON and omitted
        permissionDecisionReason AND systemMessage — so that deny reached
        the model while the user's transcript stayed silent."""
        parsed = json.loads(gate._deny("first line" + chr(10) + "second").stdout)
        hso = parsed["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"].startswith("first line")
        assert hso["reason"] == hso["permissionDecisionReason"]
        assert parsed["systemMessage"] == "first line"

    def test_every_no_op_path_emits_an_allow_rather_than_silence(self):
        """Ten tests failed on JSONDecodeError against an empty string
        before this: silence and an explicit allow are the same DECISION
        and different OUTPUT, and the output is the part with consumers."""
        for payload in (
            {"tool_name": "Read", "tool_input": {}},
            {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
            {"tool_name": "Bash", "tool_input": {"command": "bd ready"}},
        ):
            result = gate.run(payload)
            assert result.stdout is not None, payload
            assert json.loads(result.stdout)["hookSpecificOutput"][
                "permissionDecision"
            ] == "allow"
            assert result.exit_code == 0


class TestTheOverridePathKeepsTheBashDifferential:
    """An override stamps the two id sets DIFFERENTLY (RDR-215 nexus-q02nx.24).

    The bash split them and the first port did not::

        _stamp_ids "$COVERED_SPACE"     "passed"      "...verified at close"
        _stamp_ids "$NOT_COVERED_SPACE" "overridden"  "...no confirmed ... for: ..."
        _log_override_escape "$NOT_COVERED_SPACE"
        allow "...no confirmed review-completed coverage ... for $NOT_COVERED_SPACE ..."

    Three things key on NOT_COVERED, not on every id in the command. A
    bead that DID have a verified marker, closed in the same command as
    one that did not, must keep its true ``passed`` state; overwriting it
    with ``overridden`` writes a false record into the audit trail this
    gate exists to keep honest, and makes the escape log claim the bypass
    covered a bead that never needed it.

    No test in this file or the harness file closed more than one id
    under override, which is why the collapse survived the port.
    """

    @staticmethod
    def _drive(monkeypatch, status: dict):
        calls: list[tuple] = []
        escapes: list[list[str]] = []
        monkeypatch.setattr(gate, "_coverage", lambda ids: {"status": status})
        monkeypatch.setattr(
            gate, "_stamp_ids", lambda ids, state, reason: calls.append((list(ids), state, reason))
        )
        monkeypatch.setattr(
            gate, "_log_override_escape", lambda ids, command: escapes.append(list(ids))
        )
        monkeypatch.setattr(
            "nexus.hooks.stop_verification._read_config", lambda: {"on_close": True}
        )
        command = f"{CLOSE} " + " ".join(status)
        result = gate._run_gate(
            {}, command, {"has_create": False, "inline_override": True}
        )
        return result, calls, escapes

    def test_a_covered_id_is_stamped_passed_not_overridden(self, monkeypatch) -> None:
        _, calls, _ = self._drive(
            monkeypatch, {"nexus-cover1": "covered", "nexus-missn1": "missing"}
        )
        by_state = {state: ids for ids, state, _ in calls}
        assert "passed" in by_state, (
            f"no id was stamped `passed`; the override stamped only {sorted(by_state)}. "
            f"The bash stamped COVERED_SPACE passed on this very branch."
        )
        assert by_state["passed"] == ["nexus-cover1"]
        assert by_state["overridden"] == ["nexus-missn1"], (
            "a covered bead must not acquire an `overridden` record it did not earn"
        )

    def test_the_escape_log_names_only_the_ids_that_needed_the_bypass(
        self, monkeypatch
    ) -> None:
        _, _, escapes = self._drive(
            monkeypatch, {"nexus-cover1": "covered", "nexus-missn1": "missing"}
        )
        assert escapes == [["nexus-missn1"]], (
            f"the escape log recorded {escapes}; naming a covered id there claims "
            f"the override covered a bead that needed no covering."
        )

    def test_the_allow_text_names_only_the_uncovered_ids(self, monkeypatch) -> None:
        result, _, _ = self._drive(
            monkeypatch, {"nexus-cover1": "covered", "nexus-missn1": "missing"}
        )
        context = result.stdout or ""
        assert "nexus-missn1" in context
        assert "nexus-cover1" not in context, (
            "the allow text named a covered id as bypassed"
        )

    def test_all_four_uncovered_states_are_stamped_overridden(self, monkeypatch) -> None:
        _, calls, _ = self._drive(
            monkeypatch,
            {
                "nexus-cover1": "covered",
                "nexus-missn1": "missing",
                "nexus-uncrt1": "uncertain",
                "nexus-dedln1": "deadline",
                "nexus-incmp1": "incomplete",
            },
        )
        by_state = {state: ids for ids, state, _ in calls}
        assert by_state["passed"] == ["nexus-cover1"]
        assert sorted(by_state["overridden"]) == [
            "nexus-dedln1",
            "nexus-incmp1",
            "nexus-missn1",
            "nexus-uncrt1",
        ], "the override must absorb all four non-covered states uniformly"


class TestTheTwoFlagTablesCannotDrift:
    """The fallback path knows the same flags as the shlex path (nexus-q02nx.24).

    ``_bead_ids`` carries two tables: ``VALUE_FLAGS``, used once shlex has
    tokenized cleanly, and ``FLAG_VALUE_RE``, used on the malformed-quoting
    fallback where shlex could not isolate the value. The port expanded the
    first to add ``--reason-file`` and ``-r`` and left the second at its
    original four flags, so an unbalanced quote anywhere in the command
    sent a bead-id-shaped token inside a ``--reason-file`` path or a ``-r``
    value straight into the harvest as a required-coverage close target.

    That is the over-harvesting class this function's own history was
    built to close (nexus-cr4lp F3, nexus-fv65m), reopened on one path
    only. Nothing drove the fallback path at all, in either direction.
    """

    def test_a_reason_file_path_is_not_harvested_under_malformed_quoting(self) -> None:
        ids = gate._bead_ids(
            f'{CLOSE} nexus-q02nx.17 --reason-file nexus-93.txt "unterminated'
        )
        assert "nexus-93" not in ids, (
            f"harvested {ids}; a --reason-file PATH is not a close target, and "
            f"treating it as one demands review coverage for a bead nobody closed."
        )

    def test_a_short_reason_flag_value_is_not_harvested_under_malformed_quoting(
        self,
    ) -> None:
        ids = gate._bead_ids(f'{CLOSE} nexus-q02nx.17 -r "unterminated supersedes nexus-zzzzz')
        assert "nexus-zzzzz" not in ids, f"harvested {ids}; -r prose is not a target"

    def test_the_real_target_still_survives_both(self) -> None:
        """The fix must not blind the harvester; finding nothing ALLOWS."""
        for command in (
            f'{CLOSE} nexus-q02nx.17 --reason-file nexus-93.txt "unterminated',
            f'{CLOSE} nexus-q02nx.17 -r "unterminated supersedes nexus-zzzzz',
        ):
            assert "nexus-q02nx" in gate._bead_ids(command), command

    @pytest.mark.parametrize("flag", sorted(gate._VALUE_FLAGS))
    def test_every_value_flag_is_blanked_on_the_fallback_path(self, flag: str) -> None:
        """The structural pin: neither table may grow without the other.

        Parametrized over the module's own ``_VALUE_FLAGS`` rather than a
        list retyped here, so a seventh flag added tomorrow is covered
        the moment it is added. A hand-kept copy in the test would be a
        third constant holding the same fact -- the defect this fixes.
        """
        ids = gate._bead_ids(f'{CLOSE} nexus-q02nx.17 {flag} nexus-zzzzz "unterminated')
        assert "nexus-zzzzz" not in ids, (
            f"{flag} is in _VALUE_FLAGS but its value was still harvested on the "
            f"malformed-quoting fallback path; got {ids}"
        )
        assert "nexus-q02nx" in ids, (
            f"blanking {flag} also blinded the harvester to the real target; "
            f"finding nothing routes to INDETERMINATE, which ALLOWS"
        )
