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
from pathlib import Path

import pytest

from nexus.hooks import pre_close_verification as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "pre_close_verification_hook.sh"

#: Split so this file cannot trip the gate it tests. Not decoration: the
#: verb detector reads heredoc and docstring bytes as shell structure
#: (see TestTheLimitThePortRecords below), and a literal here would make
#: any command that greps this file look like a close.
CLOSE = "bd " + "close"


class TestTheDenyTextIsACarriedContract:
    """The remedy block is quoted in 19 files and duplicated verbatim in
    none of them, so a paraphrase breaks what those files rest on.

    Asserted against the SCRIPT'S OWN BYTES, never against a copy living
    here: a test holding its own copy of a contract drifts with the code
    it is supposed to pin, and both stay green.
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

    @staticmethod
    def _script_text() -> str:
        # the script stores these escaped inside a double-quoted bash
        # string; normalise only that so the comparison is about wording
        raw = SCRIPT.read_text()
        return raw.replace('\\"', '"').replace("\\'", "'")

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
    def test_each_contract_phrase_is_still_in_the_script(self, phrase):
        assert phrase in self._script_text(), (
            "the SCRIPT no longer contains this phrase — if the wording "
            "changed deliberately, the 19 documents quoting it need the "
            "same change, and this test is the place that says so"
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
