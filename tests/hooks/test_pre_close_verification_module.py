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
UPDATE = "bd " + "update"
BATCH = "bd " + "batch"
IMPORT = "bd " + "import"


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


class TestNexus2b24oTransitionNotVerb:
    """bd's own binary (1.0.5), probed live rather than guessed, sets the
    identical CLOSED status transition through spellings ``_bd_verbs``
    never looked for. nexus-2b24o: the detector's domain was the close
    VERB (``bd close``/``bd done``); the actual invariant is the close
    TRANSITION, and ``bd update ... --status closed`` sets it too, in
    five different CLI spellings, plus two more via ``bd batch``'s own
    grammar and one via ``bd import``'s JSONL upsert -- neither of which
    is even ``bd close``/``bd update`` by verb.

    ``TestNexus2b24oCloseTransitionSpellings`` in
    ``test_pre_close_verification_hook.py`` drives the same table through
    the full deny/allow gate; this class pins the detector in isolation.
    """

    @pytest.mark.parametrize(
        "command",
        [
            f"{UPDATE} nexus-aaaaa --status closed",
            f"{UPDATE} nexus-aaaaa --status=closed",
            f"{UPDATE} nexus-aaaaa -s closed",
            f"{UPDATE} nexus-aaaaa -s=closed",
            f"{UPDATE} nexus-aaaaa -sclosed",
        ],
    )
    def test_every_update_status_closed_spelling_is_recognized(self, command):
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert gate._bead_ids(command) == ["nexus-aaaaa"], command

    @pytest.mark.parametrize(
        "command",
        [
            f"{UPDATE} nexus-aaaaa --status open",
            f"{UPDATE} nexus-aaaaa --status in_progress",
            f"{UPDATE} nexus-aaaaa --status deferred",
            f"{UPDATE} nexus-aaaaa --priority 1",
            "bd list --status=closed",
        ],
    )
    def test_non_closing_update_forms_do_not_trigger(self, command):
        """Bounds the widening: any status OTHER than closed, and any bd
        verb other than update (`bd list` takes `--status` too, for
        filtering), must not trip it."""
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_batch_close_line_piped_from_printf_is_recognized(self):
        """`bd batch`'s own mini-grammar (`bd batch --help`): a `close
        <id>` line delivered as piped stdin text, never an argument this
        hook tokenizes."""
        command = "printf 'close nexus-aaaaa reason\\n' | " + BATCH
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-aaaaa" in gate._bead_ids(command)

    def test_batch_update_status_closed_line_is_recognized(self):
        command = "printf 'update nexus-aaaaa status=closed\\n' | " + BATCH
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-aaaaa" in gate._bead_ids(command)

    def test_batch_line_with_no_bead_id_does_not_trigger(self):
        """The batch-grammar scan is anchored on a bead-id-shaped token,
        not the bare word `close` -- a reason string alone must not trip
        it, the same property nexus-fv65m established for `bd close`."""
        command = "printf 'create task 2 \"close this out\"\\n' | " + BATCH
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_batch_verb_absent_never_triggers_the_raw_text_scan(self):
        """The scan is gated on the `batch` verb actually appearing
        (position-based, same rigor as `close`/`done`) -- text that merely
        LOOKS like a batch-close line, with no `bd batch` anywhere in the
        command, must not trigger it."""
        command = "printf 'close nexus-aaaaa reason\\n' > /tmp/ops.txt"
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_import_json_with_closed_status_is_recognized(self):
        """`bd import` upserts by id from JSONL, also read from stdin,
        never a command-line argument."""
        command = 'echo \'{"id":"nexus-aaaaa","status":"closed"}\' | ' + IMPORT + " -"
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-aaaaa" in gate._bead_ids(command)

    def test_import_json_without_closed_status_does_not_trigger(self):
        command = 'echo \'{"id":"nexus-aaaaa","status":"open"}\' | ' + IMPORT + " -"
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_import_with_no_id_field_does_not_trigger(self):
        """Anchored on BOTH the id and the closed status -- a bare
        status-closed mention with no id field must not match alone."""
        command = 'echo \'{"status":"closed"}\' | ' + IMPORT + " -"
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_import_verb_absent_never_triggers_the_raw_text_scan(self):
        command = 'echo \'{"id":"nexus-aaaaa","status":"closed"}\' > /tmp/x.jsonl'
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command


class TestNexus2b24oRound2Scoping:
    """Round 2 of nexus-2b24o: code-review-expert + substantive-critic
    both returned on commit 5ba250e92. Two SHIP-BLOCKERS (false positives
    that regressed nexus-fv65m's own quoted-mention protection, one level
    removed) plus three "also fix" items, addressed together.

    SHIP-BLOCKER: round 1's batch/import raw-text scan ran over the WHOLE
    ``cmd`` once the verb appeared ANYWHERE, so a close-shaped substring
    sitting in an unrelated &&-joined command, or in a --reason/-m value
    of a DIFFERENT command, false-positived. Fixed by scoping the scan to
    the prior PIPE STAGE(S) of the SAME strong-boundary-delimited shell
    segment -- see :func:`gate._pipeline_segments` and the two regexes'
    own docstrings.
    """

    @pytest.mark.parametrize(
        "command",
        [
            f'echo "close nexus-99999: fixed bug" && {BATCH} --help',
            f'{UPDATE} nexus-11111 --reason "will close nexus-99999 later" && {BATCH} --help',
        ],
    )
    def test_close_shaped_text_in_an_unrelated_segment_does_not_trigger(self, command):
        """The exact two reproductions from the round-2 code review. Both
        must fail against 5ba250e92 (has_close_or_done True there) and
        pass here."""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, (
            f"false positive: {command!r} -> has_close_or_done=True. "
            f"The batch scan read text outside bd batch's own pipeline."
        )

    def test_the_genuine_batch_close_still_survives_the_scoping_fix(self):
        """The scoping fix must not blind the scan to a REAL close --
        over-narrowing here is the failure direction that matters, same
        doctrine as the bead-id harvester fixes above."""
        command = "printf 'close nexus-aaaaa reason\\n' | " + BATCH
        assert gate._bd_verbs(command)["has_close_or_done"] is True, command

    # -- Item 4: batch/import content off the command line is now VISIBLE,
    # not silently allowed. --------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            f"{BATCH} -f file.txt",
            f"{BATCH} < file.txt",
            f"{IMPORT} path/to.jsonl",
        ],
    )
    def test_content_off_the_command_line_is_indeterminate_not_silent(self, command):
        """`bd batch -f <file>`, a bare redirect, and `bd import <file>`
        all carry their close-shaped content (if any) somewhere this hook
        cannot read without spawning a process. Round 1's docstring
        claimed this degraded to the module's INDETERMINATE-allow; it did
        not -- has_close_or_done stayed False AND has_indeterminate_source
        did not exist, so `run()` took the top-of-function bare `_allow()`
        with zero message. Now it is a real, distinct signal."""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is True, (
            f"{command!r} carries content this hook cannot read, but "
            f"has_indeterminate_source is False -- back to a silent allow."
        )

    def test_an_opaque_shell_variable_feeding_batch_is_indeterminate(self):
        """The substantive-critic's own example: a variable populated by
        an earlier command substitution. The LITERAL text ("$OPS") proves
        nothing about what bd actually receives -- correctly neither a
        confirmed close nor a confirmed non-close."""
        command = 'OPS=$(cat f); echo "$OPS" | ' + BATCH
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is True, command

    def test_a_fully_visible_non_close_batch_call_stays_silent(self):
        """Bounds item 4: content that IS visible and definitively is NOT
        a close (no variable, no close-shaped line) must stay a clean,
        silent allow -- indeterminate is for content this hook cannot
        read, not a blanket noise tax on every batch/import call."""
        command = "printf 'create task 2 \"new feature\"\\n' | " + BATCH
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is False, command

    def test_indeterminate_source_message_names_the_reason_and_never_denies(
        self, monkeypatch
    ) -> None:
        """The message `_run_gate` emits for the indeterminate-only path
        must be visible (not the bare pre-round-2 `_allow()`) and must
        NEVER call `_bead_ids` -- doing so would re-harvest whatever
        unrelated bead id sits in a sibling segment, reopening the
        ship-blocker one call away."""
        monkeypatch.setattr(
            "nexus.hooks.stop_verification._read_config", lambda: {"on_close": True}
        )
        called: list[str] = []
        monkeypatch.setattr(
            gate, "_bead_ids", lambda cmd: called.append(cmd) or []
        )
        command = f'echo "close nexus-99999" && {BATCH} -f ops.txt'
        verbs = gate._bd_verbs(command)
        result = gate._run_gate({}, command, verbs)
        parsed = json.loads(result.stdout)
        hso = parsed["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "INDETERMINATE" in (hso.get("additionalContext") or "")
        assert called == [], (
            "_bead_ids was called on the indeterminate-only path -- this "
            "re-opens the ship-blocker via the id harvester's own breadth"
        )

    # -- Item 5: `bd sql` is a fourth close transition. -------------------

    @pytest.mark.parametrize(
        "command",
        [
            "bd sql \"UPDATE issues SET status='closed' WHERE id='nexus-aaaaa'\"",
            'bd sql \'UPDATE issues SET status="closed" WHERE id="nexus-aaaaa"\'',
            "bd sql \"UPDATE issues SET priority=1, status='closed' WHERE id='nexus-aaaaa'\"",
        ],
    )
    def test_bd_sql_confirmed_close_is_recognized(self, command):
        """`bd sql --help`: 'Execute a raw SQL query... Useful for...
        working around bugs in higher-level commands.' A real bd 1.0.5
        subcommand, unmentioned by round 1 despite closing a bead with no
        close/done/update/batch/import verb anywhere in the command."""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-aaaaa" in gate._bead_ids(command)

    def test_bd_sql_write_to_a_different_status_is_definitively_not_a_close(self):
        command = "bd sql \"UPDATE issues SET status='open' WHERE id='nexus-aaaaa'\""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is False, command

    def test_bd_sql_select_is_not_a_close(self):
        command = "bd sql \"SELECT * FROM issues WHERE status='closed'\""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is False, command

    def test_bd_sql_unparseable_status_value_is_indeterminate(self):
        """A bind parameter, expression, or subquery for the status value
        cannot be read literally -- neither confirmed close nor confirmed
        non-close."""
        command = "bd sql \"UPDATE issues SET status=@newval WHERE id='nexus-aaaaa'\""
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is False, command
        assert v["has_indeterminate_source"] is True, command


class TestNexus2b24oRound3ShellBoundaries:
    """Round 3 of nexus-2b24o (substantive-critic on commit 8853ee707):
    two PRE-EXISTING silent bypasses in the shared segment splitter, not
    caused by round 1 or 2 but walked through by a literal close all the
    same:

    * a bare NEWLINE between two commands. The verb-position check only
      ever looks at position 0 of a segment (``rest[0] == 'bd'``); with no
      newline boundary, ``echo hi\\nbd close nexus-x`` tokenized as ONE
      segment starting with ``echo``, so the real close at token position
      2 was invisible -- a full silent allow, not even the INDETERMINATE
      message.
    * ``|&`` (bash's stdout+stderr pipe), invisible to both boundary
      regexes -- same failure shape, one operator this file never knew
      about.

    Fixed in the SHARED boundary finder (:func:`gate.iter_shell_boundaries`)
    so both this module and ``phase_review_close_gate`` inherit it -- see
    that module's own tests for its half.
    """

    def test_bare_newline_between_commands_is_a_boundary(self):
        command = "echo hi" + chr(10) + "bd close nexus-99999"
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-99999" in gate._bead_ids(command)

    def test_stdout_stderr_pipe_is_a_boundary(self):
        command = "echo foo |& bd close nexus-99999"
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-99999" in gate._bead_ids(command)

    def test_a_genuine_close_on_line_three_is_caught(self):
        """The multi-line shape named directly: several unrelated lines,
        then a real close."""
        command = chr(10).join(["echo one", "echo two", "bd close nexus-line3"])
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-line3" in gate._bead_ids(command)

    def test_a_heredoc_body_containing_close_shaped_text_still_does_not_trigger(self):
        """The newline boundary must NOT reach inside a heredoc's body --
        a heredoc's multi-line construct is syntactically ONE command from
        the shell's perspective, and its body is DATA fed to the
        preceding command, never executed. Without this exclusion, adding
        `\\n` as a boundary would turn every heredoc line into its own
        fake "segment" and this exact case would become a false
        positive."""
        command = "cat <<'EOF'" + chr(10) + "bd close nexus-hdoc1" + chr(10) + "EOF"
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_the_two_pinned_heredoc_limit_tests_are_unaffected(self):
        """Non-regression against `TestTheLimitThePortRecords` below: the
        operator-inside-heredoc-body trip and the no-operator-heredoc
        no-op must both still hold with `\\n` now a boundary too."""
        v_operator = gate._bd_verbs(
            "python3 - <<'PY'" + chr(10) + "s = 'x && " + CLOSE + " y'" + chr(10) + "PY"
        )
        assert v_operator["has_close_or_done"] is True

        v_no_operator = gate._bd_verbs(
            "cat <<'EOF'" + chr(10) + "run " + CLOSE + " to finish" + chr(10) + "EOF"
        )
        assert v_no_operator["has_close_or_done"] is False


class TestNexus2b24oRound4QuoteAwareBoundaries:
    """Round 4 of nexus-2b24o (substantive-critic on commit 47f635dcd):
    round 3's new bare-newline boundary is not QUOTE-aware -- only
    heredoc bodies were protected. A multi-line ``--reason``/``-m`` VALUE
    is a normal, common shape (a multi-paragraph close reason), and its
    embedded newlines are literal quoted text, not command boundaries:

        bd update nexus-1 --reason "line one
        bd close nexus-x
        line three"

    tokenized the SECOND line as its own segment starting with ``bd``,
    a full false positive. Fixed by :func:`gate._quoted_spans`, wired
    into :func:`gate.iter_shell_boundaries` -- see that function's own
    docstring for the two independent protections (quotes protect EVERY
    boundary type; heredoc bodies protect only the newline).
    """

    _REASON_MULTILINE = (
        'line one' + chr(10) + 'bd close nexus-x' + chr(10) + 'line three'
    )

    @pytest.mark.parametrize(
        "command",
        [
            f'{UPDATE} nexus-1 --reason "{_REASON_MULTILINE}"',
            f'{UPDATE} nexus-1 -m "{_REASON_MULTILINE}"',
            f"{UPDATE} nexus-1 --reason '{_REASON_MULTILINE}'",
        ],
    )
    def test_a_close_shaped_line_inside_a_quoted_multiline_value_does_not_trigger(
        self, command
    ):
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_operator_inside_a_quoted_value_also_does_not_trigger(self):
        """Quoting protects EVERY boundary type, not only the newline --
        a literal `&&` inside a quoted --reason value is equally not a
        real shell boundary."""
        command = (
            f'{UPDATE} nexus-1 --reason "supersedes nexus-y && ' + CLOSE + ' nexus-fake"'
        )
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_command_substitution_nesting_inside_double_quotes_does_not_confuse_the_scanner(
        self,
    ):
        """`"$(echo "nested")"` -- a double-quoted string containing its
        own nested double quotes via $(...) -- must not make the scanner
        think the outer quote closes early and then misread the rest."""
        command = (
            f'{UPDATE} nexus-1 --reason "output: $(echo "nested ' + CLOSE + ' nexus-fake")"'
        )
        assert gate._bd_verbs(command)["has_close_or_done"] is False, command

    def test_a_genuine_close_after_a_closed_multiline_quoted_value_is_still_caught(self):
        """Bounds the fix: protection ends at the REAL closing quote. A
        close on its own line AFTER the quoted string properly closes
        must still be caught."""
        command = (
            f'{UPDATE} nexus-1 --reason "{self._REASON_MULTILINE}"'
            + chr(10) + CLOSE + " nexus-z"
        )
        v = gate._bd_verbs(command)
        assert v["has_close_or_done"] is True, command
        assert "nexus-z" in gate._bead_ids(command)

    def test_an_unterminated_quote_does_not_crash(self):
        """Defined posture, not a crash: an unterminated quote is simply
        NOT protected (see `_quoted_spans`'s own docstring for why under-
        protecting here is the safe direction, matching
        `TestMalformedQuotingNeverBypasses`'s existing, accepted
        behavior for a malformed --reason value)."""
        command = f'{CLOSE} nexus-abc12 --reason="unterminated'
        gate._bd_verbs(command)  # must not raise

    def test_the_operator_inside_heredoc_body_limit_survives_quote_awareness(self):
        """The exact regression this fix's first draft introduced: a
        heredoc body containing a Python string literal (`'x && bd close
        y'`) was newly (and wrongly) read as a REAL single-quoted span,
        hiding the `&&` the heredoc known-limit test requires to still
        split. Heredoc bodies are excluded from quote-scanning entirely
        (`_quoted_spans`'s `skip_spans` parameter)."""
        command = (
            "python3 - <<'PY'" + chr(10) + "s = 'x && " + CLOSE + " y'" + chr(10) + "PY"
        )
        assert gate._bd_verbs(command)["has_close_or_done"] is True, command


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
            {}, command,
            {
                "has_create": False,
                "has_close_or_done": True,
                "has_indeterminate_source": False,
                "inline_override": True,
            },
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
