# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The close-gate reconciliation backstop (nexus-dgl8g).

Sam's DECIDED note (``bd show nexus-dgl8g``): an ADVISORY check inside the
existing Stop verification hook, never blocking, that lists beads moved to
``closed`` during THIS session with no ``review-completed`` marker naming
both reviewers in this session's T1 scratch, and reports a named
undeclared-close count.

Follow-up from nexus-2b24o: the PreToolUse close gate matches enumerated
close spellings in the command TEXT, so a close whose content is off the
command line entirely (``bd batch -f``, ``bd import <file>``, a
dynamically-built ``bd sql``) is structurally invisible to it. This is the
detective backstop for exactly that gap -- it reads bd's own record of
what closed, not the command that closed it.

FOLLOW-UP 1 (sibling-session false positive): a plain time-window filter
over bd's own record fires on ANOTHER session's legitimate close too,
because this project runs several sessions against one shared bd database
at once and bd's record carries no per-close actor/session field. The fix
INTERSECTS the time-window list with THIS session's own transcript -- a
bead counts only if bd reports it closed in the window AND this session's
own transcript shows a Bash tool_use whose command would close it. This
also lets an override close (``NX_REVIEW_GATE_OVERRIDE=1``) be told apart
directly (the override is literally in the transcript's own recorded
command) instead of stating it as an unresolvable limitation.

FOLLOW-UP 2 (Stop fires every turn, not once per session): the whole
design has to be cheap on the COMMON turn, because Stop runs on every
assistant turn. Three changes: (1) the transcript scan runs BEFORE any
bd/nx spawn and is now incremental (a persisted byte offset, so each turn
reads only what was appended since the last Stop, not the whole file) --
a turn with no new close-shaped Bash command touches zero subprocesses;
(2) per-session state (offset, pending ids, and every id's TERMINAL
verdict) is memoized to a small JSON file, so a bead is verified against
bd/T1 at most once per session, ever, and every later turn renders its
warning from that cache; (3) the T1 coverage call gets its own explicit
deadline (:data:`nexus.hooks.stop_verification._STOP_COVERAGE_DEADLINE_SECONDS`)
instead of inheriting ``pre_close_verification``'s PreToolUse-sized
default.

These are in-process module tests (mirrors ``test_pre_close_verification_module.py``'s
``_coverage`` monkeypatch pattern) rather than a real T1/bd stack: bd is a
real fake binary on ``PATH`` (this module's own pattern, matching
``test_stop_verification_module.py``'s unused ``_fake_bin`` helper), and
T1 coverage is monkeypatched at ``pre_close_verification._coverage`` --
the exact reader this bead's own instructions say to reuse rather than
reimplement, so faking its OUTPUT (not the T1 scan underneath it) is the
right seam. Transcripts are written in the real Claude Code JSONL shape
(``{"type": "assistant", "message": {"content": [{"type": "tool_use",
"name": "Bash", "input": {"command": ...}}]}}``), matching
``nexus.hooks.subagent_stop_scans``'s own fixtures for the same format.

Every test in this module isolates ``NEXUS_CONFIG_DIR`` (the
``isolated_state`` autouse fixture) -- the memoization this follow-up adds
persists to a REAL file on disk keyed by session_id, under
``nexus_config_dir()`` (follow-up 3: checked against the rest of this
repo's per-session hook state, e.g. ``t1_session_lease.<session_id>`` --
NOT ``XDG_STATE_HOME``, which here is used narrowly for the RDR-184
ledger family alone), and a test that did not isolate it would read or
write a real ``~/.config/nexus/`` file and could leak state between test
runs (or between tests, since several here reuse ``session_id="s1"``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import stop_verification as hook


# --- fixtures ---------------------------------------------------------------


def _fake_bd(tmp_path: Path, closed_json: str, *, in_progress_output: str = "", counter: str | None = None) -> Path:
    """A one-file ``bd`` on PATH: ``--json`` gets *closed_json*, anything
    else (e.g. ``_beads_in_progress``'s own ``bd list --status=in_progress``
    call, which some ``run()``-level tests also trigger) gets
    *in_progress_output* and exit 0. When *counter* is given, every
    invocation appends one line to that file -- the literal, real-process
    proof (not a Python-level mock) that "no bd spawn on turns with
    nothing new" means what it says.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "bd"
    count_line = f"echo x >> {counter}\n" if counter else ""
    p.write_text(
        "#!/bin/sh\n"
        f"{count_line}"
        "case \"$*\" in\n"
        "  *--json*) cat <<'EOF'\n"
        f"{closed_json}\n"
        "EOF\n"
        "  ;;\n"
        f"  *) printf '%s' '{in_progress_output}' ;;\n"
        "esac\n"
    )
    p.chmod(0o755)
    return bin_dir


def _fake_bd_absent(tmp_path: Path) -> Path:
    """An empty bin dir: ``shutil.which('bd')`` finds nothing on it."""
    bin_dir = tmp_path / "emptybin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _bash_entry(command: str) -> dict:
    """One assistant turn's Bash ``tool_use`` block, in the real Claude
    Code transcript shape (see ``nexus.hooks.subagent_stop_scans._blocks``,
    which reads the identical ``message.content`` list)."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}}
            ]
        },
    }


def _write_transcript(
    tmp_path: Path,
    session_start_iso: str,
    *,
    bash_commands: tuple[str, ...] = (),
    name: str = "transcript.jsonl",
) -> str:
    """A minimal transcript: a leading line with NO timestamp (matches a
    real session's first ``{"type":"mode",...}`` row), one that carries
    the session's start, and one assistant Bash ``tool_use`` entry per
    command in *bash_commands* -- this session's own recorded closes.
    """
    path = tmp_path / name
    lines = [
        json.dumps({"type": "mode", "mode": "normal", "sessionId": "s1"}),
        json.dumps({"type": "file-history-snapshot", "timestamp": session_start_iso}),
    ]
    lines.extend(json.dumps(_bash_entry(cmd)) for cmd in bash_commands)
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _append_bash_command(path: str, command: str) -> None:
    """Simulate a LATER turn: append one more Bash tool_use entry to an
    already-written transcript, so the next ``_undeclared_close_warning``
    call sees genuinely NEW content past its persisted offset."""
    with open(path, "a", encoding="utf-8") as fh:  # noqa: PTH123
        fh.write(json.dumps(_bash_entry(command)) + "\n")


def _bd_row(bead_id: str, closed_at: str) -> dict:
    return {"id": bead_id, "status": "closed", "closed_at": closed_at}


#: The bare minimum PATH a fake ``bd`` script's own ``cat``/heredoc still
#: needs to run -- real ``bd`` lives at ``/opt/homebrew/bin/bd`` on this
#: box, well outside both entries, so appending them never leaks it in.
_MINIMAL_SHELL_PATH = "/bin:/usr/bin"


@pytest.fixture
def isolated_path(monkeypatch):
    """Every test in this module controls PATH explicitly -- a stray real
    ``bd``/``nx`` reachable from the ambient dev-box PATH must never leak
    into a "bd unavailable" or "no beads closed" assertion. The fake
    ``bd`` dir is prepended (not a full replacement) so its own script
    body can still find ``sh``'s external commands (``cat``, for its
    heredoc) -- a bare fakebin-only PATH makes the fake script itself
    fail with "command not found", which reads identically to "bd
    unavailable" and silently hid every non-trivial case here.
    """
    def _set(bin_dir: Path) -> None:
        monkeypatch.setenv("PATH", f"{bin_dir}:{_MINIMAL_SHELL_PATH}")
    return _set


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test's close-gate memoization file lives under a per-test
    NEXUS_CONFIG_DIR -- see the module docstring's closing paragraph."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "nexus-config"))


# --- _session_start_dt --------------------------------------------------


class TestSessionStartDt:
    def test_reads_the_first_timestamped_line(self, tmp_path):
        transcript = _write_transcript(tmp_path, "2026-09-24T10:00:00.000Z")
        dt = hook._session_start_dt(transcript)
        assert dt is not None
        assert dt.year == 2026 and dt.month == 9 and dt.day == 24
        assert dt.hour == 10

    def test_none_for_empty_path(self):
        assert hook._session_start_dt("") is None

    def test_none_for_missing_file(self, tmp_path):
        assert hook._session_start_dt(str(tmp_path / "nope.jsonl")) is None

    def test_none_when_no_line_carries_a_timestamp(self, tmp_path):
        path = tmp_path / "notimestamp.jsonl"
        path.write_text('{"type": "mode", "mode": "normal"}\n{"type": "other"}\n')
        assert hook._session_start_dt(str(path)) is None

    def test_junk_lines_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "junk.jsonl"
        path.write_text(
            "not json at all\n"
            '["a", "list", "not", "a", "dict"]\n'
            + json.dumps({"timestamp": "2026-09-24T11:30:00Z"}) + "\n"
        )
        dt = hook._session_start_dt(str(path))
        assert dt is not None
        assert dt.hour == 11


# --- _bd_closed_since -----------------------------------------------------


class TestBdClosedSince:
    def test_filters_to_the_session_window(self, tmp_path, isolated_path):
        """A bead closed BEFORE the session started is ignored; one closed
        after is reported. Covers the bead's "close before the session
        window (ignored)" scenario at the reader level."""
        rows = json.dumps([
            _bd_row("nexus-before", "2026-09-24T09:00:00Z"),
            _bd_row("nexus-after", "2026-09-24T10:30:00Z"),
        ])
        isolated_path(_fake_bd(tmp_path, rows))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        ids = hook._bd_closed_since(session_start)
        assert ids == ["nexus-after"]

    def test_none_when_bd_absent(self, tmp_path, isolated_path):
        isolated_path(_fake_bd_absent(tmp_path))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) is None

    def test_none_on_unparseable_output(self, tmp_path, isolated_path):
        isolated_path(_fake_bd(tmp_path, "not json"))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) is None

    def test_empty_list_when_bd_answers_with_nothing_closed(self, tmp_path, isolated_path):
        isolated_path(_fake_bd(tmp_path, "[]"))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) == []


# --- _scan_transcript_tail --------------------------------------------------


class TestScanTranscriptTail:
    """Reuses ``pre_close_verification``'s own close-spelling detector
    (``_bd_verbs``/``_bead_ids``) against each Bash command found strictly
    after the given byte offset, rather than re-implementing the
    spellings or re-scanning the whole file every call."""

    def test_finds_a_bd_close_and_its_id_from_offset_zero(self, tmp_path):
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=("bd close nexus-x --reason done",),
        )
        result = hook._scan_transcript_tail(transcript, 0)
        assert result is not None
        declared, offset = result
        assert declared == {"nexus-x": False}
        assert offset == len(open(transcript, "rb").read())  # noqa: PTH123, SIM115 — test-only, immediate read

    def test_a_second_call_from_the_returned_offset_sees_only_new_content(self, tmp_path):
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=("bd close nexus-x",),
        )
        declared1, offset1 = hook._scan_transcript_tail(transcript, 0)
        assert declared1 == {"nexus-x": False}

        _append_bash_command(transcript, "bd close nexus-y")
        declared2, offset2 = hook._scan_transcript_tail(transcript, offset1)
        assert declared2 == {"nexus-y": False}  # NOT nexus-x again
        assert offset2 > offset1

    def test_finds_the_inline_override(self, tmp_path):
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=("NX_REVIEW_GATE_OVERRIDE=1 bd close nexus-z --reason override",),
        )
        declared, _ = hook._scan_transcript_tail(transcript, 0)
        assert declared == {"nexus-z": True}

    def test_non_close_bd_commands_are_ignored(self, tmp_path):
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=("bd show nexus-w", "bd list --status open"),
        )
        declared, _ = hook._scan_transcript_tail(transcript, 0)
        assert declared == {}

    def test_a_quoted_mention_does_not_count_as_a_close(self, tmp_path):
        """Reusing ``_bd_verbs`` inherits its own quoted-mention exclusion
        (nexus-fv65m) for free -- proof this is a real reuse, not a
        reimplementation that merely resembles it."""
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=('git commit -m "docs: bd close nexus-q notes"',),
        )
        declared, _ = hook._scan_transcript_tail(transcript, 0)
        assert declared == {}

    def test_none_for_missing_file(self, tmp_path):
        assert hook._scan_transcript_tail(str(tmp_path / "nope.jsonl"), 0) is None

    def test_offset_at_eof_returns_nothing_new(self, tmp_path):
        transcript = _write_transcript(
            tmp_path, "2026-09-24T10:00:00Z",
            bash_commands=("bd close nexus-x",),
        )
        size = len(open(transcript, "rb").read())  # noqa: PTH123, SIM115 — test-only
        declared, offset = hook._scan_transcript_tail(transcript, size)
        assert declared == {}
        assert offset == size

    def test_a_half_written_trailing_line_is_not_advanced_past(self, tmp_path):
        """nexus-dgl8g follow-up 3 (coupled to the fingerprint fix):
        Claude Code can fire Stop between writing a JSONL entry's bytes
        and its trailing newline. The old ``fh.tell()``-after-the-loop
        offset advanced past that half-written line even though it was
        never parsed, so the REST of the same line, written moments
        later, was never re-read -- an entry split across a Stop
        boundary went permanently missing."""
        transcript = tmp_path / "t.jsonl"
        full_line = json.dumps(_bash_entry("bd close nexus-partial"))
        half = len(full_line) // 2
        transcript.write_text(full_line[:half])  # no trailing newline: incomplete

        declared, offset = hook._scan_transcript_tail(str(transcript), 0)
        assert declared == {}
        assert offset == 0, "nothing complete yet -- must not advance at all"

        with open(transcript, "a") as f:  # noqa: PTH123
            f.write(full_line[half:] + "\n")  # complete the SAME line

        declared2, offset2 = hook._scan_transcript_tail(str(transcript), offset)
        assert declared2 == {"nexus-partial": False}
        assert offset2 == len(full_line) + 1

    def test_a_complete_line_before_a_partial_one_still_advances_to_it(self, tmp_path):
        """The partial-line guard must only hold back the LAST line, not
        regress the whole scan: a complete line followed by a partial one
        still reports the complete line and its own correct offset."""
        transcript = tmp_path / "t.jsonl"
        complete_line = json.dumps(_bash_entry("bd close nexus-full"))
        partial_line = json.dumps(_bash_entry("bd close nexus-half"))
        half = len(partial_line) // 2
        transcript.write_text(complete_line + "\n" + partial_line[:half])

        declared, offset = hook._scan_transcript_tail(str(transcript), 0)
        assert declared == {"nexus-full": False}
        assert offset == len(complete_line) + 1


# --- close-gate memoization state -------------------------------------------


class TestCloseGateState:
    def test_round_trips(self, tmp_path):
        state = {
            "transcript_path": "/tmp/t.jsonl",
            "fingerprint": {"dev": 1, "ino": 2, "head_hash": "abc123"},
            "offset": 42,
            "pending": {"nexus-a": True},
            "resolved": {"nexus-b": "clean"},
        }
        hook._write_close_gate_state("s1", state)
        assert hook._read_close_gate_state("s1") == state

    def test_missing_state_file_is_empty(self):
        assert hook._read_close_gate_state("s-never-seen") == hook._empty_close_gate_state()

    def test_path_unsafe_session_id_reads_as_empty_and_write_is_a_noop(self, tmp_path):
        assert hook._read_close_gate_state("../escape") == hook._empty_close_gate_state()
        hook._write_close_gate_state("../escape", {"offset": 1, "pending": {}, "resolved": {}})
        # Nothing to assert on disk (there is no valid path) -- the point
        # is that this does not raise.

    def test_corrupt_state_file_reads_as_empty(self, tmp_path):
        path = hook._close_gate_state_path("s1")
        path.write_text("not json")
        assert hook._read_close_gate_state("s1") == hook._empty_close_gate_state()

    def test_a_malformed_fingerprint_reads_as_none_not_a_crash(self, tmp_path):
        """A hand-edited or half-written state file must fail open on the
        fingerprint field specifically, not just on the whole file."""
        path = hook._close_gate_state_path("s1")
        path.write_text(json.dumps({
            "transcript_path": "/tmp/t.jsonl",
            "fingerprint": {"dev": 1},  # missing ino/head_hash
            "offset": 5,
            "pending": {},
            "resolved": {},
        }))
        state = hook._read_close_gate_state("s1")
        assert state["fingerprint"] is None
        assert state["offset"] == 5  # the rest of the file is still trusted


class TestTranscriptFingerprint:
    def test_same_file_same_fingerprint(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("line one\n")
        fp1 = hook._transcript_fingerprint(str(path))
        fp2 = hook._transcript_fingerprint(str(path))
        assert fp1 is not None
        assert fp1 == fp2

    def test_replacement_file_same_path_different_fingerprint(self, tmp_path):
        """A NEW file written to the SAME path (rotation, /resume onto a
        different saved state) gets a new inode -- dev/ino differ even
        though the path is identical."""
        path = tmp_path / "t.jsonl"
        path.write_text("original content\n")
        fp1 = hook._transcript_fingerprint(str(path))

        path.unlink()
        path.write_text("entirely different content\n")
        fp2 = hook._transcript_fingerprint(str(path))

        assert fp1 is not None and fp2 is not None
        assert fp1 != fp2

    def test_in_place_append_keeps_dev_ino_but_head_hash_is_stable(self, tmp_path):
        """An ordinary append (the common case, every idle-then-growing
        Stop turn) must NOT look like a replacement: dev/ino AND the
        first-256-bytes hash all stay the same, since the head of the
        file did not change. The initial content must already EXCEED the
        fingerprint's head-byte window (:data:`hook._FINGERPRINT_HEAD_BYTES`)
        for this to hold -- appending to a file SMALLER than that window
        changes the hash too, since the "head" is still the whole file;
        that shape is real transcripts (which cross the window almost
        immediately) rather than this test's synthetic content."""
        path = tmp_path / "t.jsonl"
        path.write_text("x" * (hook._FINGERPRINT_HEAD_BYTES + 100) + "\n")
        fp1 = hook._transcript_fingerprint(str(path))
        with open(path, "a") as f:  # noqa: PTH123
            f.write("line two\n")
        fp2 = hook._transcript_fingerprint(str(path))
        assert fp1 == fp2

    def test_none_for_missing_file(self, tmp_path):
        assert hook._transcript_fingerprint(str(tmp_path / "nope.jsonl")) is None


# --- _undeclared_close_warning (the integrated backstop) ------------------


class TestUndeclaredCloseWarning:
    """Each test corresponds to one of the bead's named coverage
    scenarios."""

    def _payload(
        self, tmp_path, session_start_iso: str = "2026-09-24T10:00:00Z",
        *, bash_commands: tuple[str, ...] = (),
    ) -> dict:
        return {
            "session_id": "s1",
            "transcript_path": _write_transcript(tmp_path, session_start_iso, bash_commands=bash_commands),
        }

    def test_no_session_id_is_silent(self, tmp_path):
        assert hook._undeclared_close_warning({"transcript_path": "irrelevant"}) == ""

    def test_no_transcript_path_is_silent(self):
        """No window to check against is not evidence of anything amiss --
        matches this file's other missing-signal branches."""
        assert hook._undeclared_close_warning({"session_id": "s1"}) == ""

    def test_guard_mode_off_disables_the_whole_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "off")
        payload = self._payload(tmp_path)
        assert hook._undeclared_close_warning(payload) == ""

    def test_no_close_commands_means_zero_subprocesses(self, tmp_path, isolated_path):
        """THE CHEAP-TRIGGER-FIRST CONTRACT: a transcript with no
        close-shaped Bash command at all never even looks at bd -- there
        is no fake bd on PATH here, so any spawn attempt would surface as
        a crash or a wrong answer, not a silent pass."""
        isolated_path(_fake_bd_absent(tmp_path))
        payload = self._payload(tmp_path)  # no bash_commands
        assert hook._undeclared_close_warning(payload) == ""

    def test_bd_unavailable_reports_cannot_check(self, tmp_path, isolated_path):
        isolated_path(_fake_bd_absent(tmp_path))
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-x",))
        warning = hook._undeclared_close_warning(payload)
        assert "could not check" in warning
        assert "WARNING" in warning

    def test_close_not_confirmed_by_bd_is_silent(self, tmp_path, isolated_path):
        """The transcript declares a close, but bd's window answers empty
        (the command may have failed, or bd has not caught up) -- resolved
        clean, not reported."""
        isolated_path(_fake_bd(tmp_path, "[]"))
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-x",))
        assert hook._undeclared_close_warning(payload) == ""

    def test_close_before_session_window_is_ignored_end_to_end(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-old", "2026-09-24T09:00:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        # If this were consulted it would report "missing" -- proving the
        # window filter, not the coverage check, is what kept it out.
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-old",))
        assert hook._undeclared_close_warning(payload) == ""

    def test_sibling_closed_bead_in_window_is_not_reported(self, tmp_path, isolated_path, monkeypatch):
        """THE HEADLINE FIX (follow-up 1): bd reports a bead closed inside
        this session's time window, but THIS session's own transcript
        never names it -- a sibling session (sharing the same bd
        database) closed it correctly. Must not be reported, even though
        the coverage stub below would flag it as missing if it were ever
        consulted."""
        rows = json.dumps([_bd_row("nexus-sibling", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        consulted = []
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: consulted.append(list(ids)) or {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        # This session's own transcript closes a DIFFERENT bead -- proves
        # the exclusion is identity-scoped, not "transcript has no closes
        # at all".
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-mine",))
        assert hook._undeclared_close_warning(payload) == ""
        assert consulted == []  # never even reached the T1 coverage check

    def test_covered_bead_with_both_reviewer_marker_is_not_reported(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-good", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-good": "covered"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-good",))
        assert hook._undeclared_close_warning(payload) == ""

    def test_this_sessions_close_with_no_marker_is_reported(self, tmp_path, isolated_path, monkeypatch):
        """THIS session's own transcript shows the close AND bd confirms it
        landed in the window AND T1 has no marker -- genuinely undeclared."""
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-bad": "missing"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-bad --reason done",))
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-bad" in warning
        assert "1 bead" in warning
        assert "closed under" not in warning  # not the override category

    def test_partial_marker_incomplete_is_reported_as_undeclared(self, tmp_path, isolated_path, monkeypatch):
        """nexus-e3mak: a marker naming only one reviewer does not count as
        coverage -- ``_coverage`` already encodes this as ``incomplete``,
        and this backstop must not treat it as clean."""
        rows = json.dumps([_bd_row("nexus-half", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-half": "incomplete"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-half",))
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-half" in warning

    def test_this_sessions_override_close_is_its_own_category(self, tmp_path, isolated_path):
        """An override close is now IDENTIFIED, not merely disclaimed: the
        inline ``NX_REVIEW_GATE_OVERRIDE=1`` sits on this session's own
        recorded closing command, so it is reported as "closed under
        override" and never reaches the T1 coverage check at all (real
        ``_coverage`` is left UNPATCHED here -- proof it is genuinely
        never called, not merely mocked to look clean)."""
        rows = json.dumps([_bd_row("nexus-override", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        payload = self._payload(
            tmp_path,
            bash_commands=("NX_REVIEW_GATE_OVERRIDE=1 bd close nexus-override --reason evidence",),
        )
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-override" in warning
        assert "override" in warning.lower()
        assert "NOTE:" in warning
        assert "no review-completed marker naming both reviewers" not in warning
        assert "cannot be told apart" not in warning  # the old caveat is gone

    def test_t1_unreachable_reports_cannot_check_not_undeclared(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-unk", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": False, "status": {"nexus-unk": "uncertain"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-unk",))
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-unk" in warning
        assert "could not verify" in warning
        assert "no review-completed marker naming both reviewers" not in warning

    def test_deadline_status_reported_as_cannot_check_not_undeclared(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-slow", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-slow": "deadline"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-slow",))
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-slow" in warning
        assert "could not verify" in warning
        assert "no review-completed marker naming both reviewers" not in warning

    def test_passes_session_id_and_deadline_through_to_coverage_reader(self, tmp_path, isolated_path, monkeypatch):
        """Reuses ``pre_close_verification._coverage``'s reader against
        THIS session's own T1 scope, and Stop's OWN explicit deadline --
        not ``NX_CLOSE_GATE_DEADLINE_SECONDS``'s PreToolUse-sized default."""
        rows = json.dumps([_bd_row("nexus-x", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        seen = {}

        def _fake_coverage(ids, session_id="", deadline_seconds=None):
            seen["session_id"] = session_id
            seen["deadline_seconds"] = deadline_seconds
            return {"t1_reachable": True, "status": {b: "covered" for b in ids}}

        monkeypatch.setattr("nexus.hooks.pre_close_verification._coverage", _fake_coverage)
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-x",))
        hook._undeclared_close_warning(payload)
        assert seen["session_id"] == "s1"
        assert seen["deadline_seconds"] == hook._STOP_COVERAGE_DEADLINE_SECONDS

    def test_unreadable_transcript_falls_back_to_the_unscoped_list(self, tmp_path, isolated_path, monkeypatch):
        """The transcript is readable enough to anchor the session's start
        (stubbed directly here) but genuinely missing by the time
        ``os.path.getsize`` tries to stat it -- the real, non-stubbed
        code path hits a plain missing file. Falls back to the UNSCOPED
        bd time-window list with an honest caveat, rather than silently
        reporting nothing (a missing transcript is not evidence every
        close was legitimate either)."""
        rows = json.dumps([_bd_row("nexus-fallback", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-fallback": "missing"}
            },
        )
        monkeypatch.setattr(
            hook, "_session_start_dt",
            lambda transcript_path: hook._exp._parse_iso("2026-09-24T10:00:00Z"),
        )
        payload = {"session_id": "s1", "transcript_path": str(tmp_path / "does-not-exist.jsonl")}
        warning = hook._undeclared_close_warning(payload)
        assert "could not scope" in warning
        assert "nexus-fallback" in warning

    # --- memoization: the follow-up 2 contract -----------------------------

    def test_a_resolved_bead_is_never_reverified_but_stays_reported(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        calls = []
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: calls.append(list(ids)) or {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-bad",))

        first = hook._undeclared_close_warning(payload)
        second = hook._undeclared_close_warning(payload)  # same payload, no transcript growth

        assert "nexus-bad" in first
        assert "nexus-bad" in second  # still SHOWN
        assert calls == [["nexus-bad"]]  # but VERIFIED exactly once

    def test_no_bd_spawn_on_a_turn_with_nothing_new(self, tmp_path, isolated_path):
        """Coordinator's explicit ask: call ``_undeclared_close_warning``
        repeatedly and assert no bd spawn on turns with nothing new --
        proven against a REAL fake bd binary that counts its own
        invocations (not a Python-level mock), so this is evidence about
        the actual subprocess boundary."""
        counter = tmp_path / "bd_invocations"
        rows = json.dumps([_bd_row("nexus-once", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows, counter=str(counter)))
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-once",))

        hook._undeclared_close_warning(payload)  # turn 1: new close -> bd spawns
        first_count = counter.read_text().count("x") if counter.exists() else 0
        assert first_count == 1

        hook._undeclared_close_warning(payload)  # turn 2: nothing new
        hook._undeclared_close_warning(payload)  # turn 3: nothing new
        second_count = counter.read_text().count("x") if counter.exists() else 0
        assert second_count == 1  # unchanged -- zero additional bd spawns

    def test_a_later_turns_new_close_triggers_exactly_one_more_bd_call(self, tmp_path, isolated_path):
        counter = tmp_path / "bd_invocations"
        rows = json.dumps([
            _bd_row("nexus-one", "2026-09-24T10:15:00Z"),
            _bd_row("nexus-two", "2026-09-24T10:45:00Z"),
        ])
        isolated_path(_fake_bd(tmp_path, rows, counter=str(counter)))
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-one",))

        hook._undeclared_close_warning(payload)
        assert counter.read_text().count("x") == 1

        hook._undeclared_close_warning(payload)  # still nothing new
        assert counter.read_text().count("x") == 1

        _append_bash_command(payload["transcript_path"], "bd close nexus-two")
        warning = hook._undeclared_close_warning(payload)
        assert counter.read_text().count("x") == 2  # exactly one more spawn
        assert "nexus-two" in warning or warning == ""  # bd's own real status decides; no crash either way

    def test_memoization_survives_a_fresh_read_of_persisted_state(self, tmp_path, isolated_path, monkeypatch):
        """State written by one call is read back correctly by a later,
        independent call (simulating the NEXT Stop invocation, a separate
        hook process in production) -- not merely held in a Python
        variable across two calls in the same test process."""
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-bad": "missing"}
            },
        )
        payload = self._payload(tmp_path, bash_commands=("bd close nexus-bad",))
        hook._undeclared_close_warning(payload)

        state = hook._read_close_gate_state("s1")
        assert state["resolved"] == {"nexus-bad": "undeclared"}
        assert state["pending"] == {}

    # --- SHIP-BLOCKER: transcript replacement/shrink (T2 nexus/verification-nexus-dgl8g-close-gate-backstop-46139ed9d-shrink-defect) ---

    def test_transcript_shrink_repro_is_detected_not_silently_dark(self, tmp_path, isolated_path, monkeypatch):
        """The exact live repro: a transcript starts past 5000 bytes with
        nothing to report, then gets REPLACED (same path) by a much
        smaller file carrying an undeclared close. Before the fingerprint
        fix, `size > offset` stayed false forever once the persisted
        offset (~5000+) exceeded the replacement's size, and three
        subsequent calls all returned '' -- reproduced here as the same
        shape (an old file, then a short replacement, then three calls)."""
        rows = json.dumps([_bd_row("nexus-abc123", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )

        transcript_path = tmp_path / "shrink.jsonl"
        lines = [
            json.dumps({"type": "mode", "mode": "normal"}),
            json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
        ]
        padding_line = json.dumps({"type": "user", "message": {"content": "x" * 200}})
        while sum(len(entry) + 1 for entry in lines) < 5001:
            lines.append(padding_line)
        transcript_path.write_text("\n".join(lines) + "\n")
        assert transcript_path.stat().st_size > 5000

        payload = {"session_id": "s1", "transcript_path": str(transcript_path)}
        assert hook._undeclared_close_warning(payload) == ""
        assert hook._read_close_gate_state("s1")["offset"] > 5000

        replacement = "\n".join([
            json.dumps({"type": "mode", "mode": "normal"}),
            json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
            json.dumps(_bash_entry("bd close nexus-abc123 --reason done")),
        ]) + "\n"
        transcript_path.write_text(replacement)
        assert transcript_path.stat().st_size < 5001

        warnings = [hook._undeclared_close_warning(payload) for _ in range(3)]
        assert any("nexus-abc123" in w for w in warnings), (
            f"the replacement file's undeclared close must be detected; got {warnings!r}"
        )

    def test_content_swap_without_shrinking_is_caught_by_fingerprint(self, tmp_path, isolated_path, monkeypatch):
        """Belt-and-suspenders proof for the OTHER trigger: a same-path
        replacement that is NOT smaller than the persisted offset (so
        `size < offset` never fires) must still be caught -- by the
        dev/ino/head-hash fingerprint disagreeing, independent of size."""
        rows = json.dumps([_bd_row("nexus-swap", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        transcript_path = tmp_path / "swap.jsonl"
        original = "\n".join([
            json.dumps({"type": "mode", "mode": "normal", "marker": "ORIGINAL"}),
            json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
        ]) + "\n"
        transcript_path.write_text(original)
        payload = {"session_id": "s1", "transcript_path": str(transcript_path)}
        assert hook._undeclared_close_warning(payload) == ""
        offset_after_first = hook._read_close_gate_state("s1")["offset"]

        replacement = "\n".join([
            json.dumps({"type": "mode", "mode": "normal", "marker": "REPLACED"}),
            json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
            json.dumps(_bash_entry("bd close nexus-swap --reason done")),
        ]) + "\n"
        # Pad, if needed, so the replacement is not smaller than the old
        # offset -- isolates the fingerprint path from the size<offset one.
        while len(replacement.encode("utf-8")) <= offset_after_first:
            replacement += json.dumps({"type": "user", "pad": "x" * 50}) + "\n"
        transcript_path.write_text(replacement)
        assert len(replacement.encode("utf-8")) > offset_after_first

        warning = hook._undeclared_close_warning(payload)
        assert "nexus-swap" in warning

    def test_transcript_path_change_also_resets_and_detects(self, tmp_path, isolated_path, monkeypatch):
        """A /resume onto a DIFFERENT transcript file (a new path, same
        stable session_id) must reset and rescan too, not just a
        same-path replacement."""
        rows = json.dumps([_bd_row("nexus-newpath", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        first_payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(tmp_path, "2026-09-24T10:00:00Z", name="first.jsonl"),
        }
        assert hook._undeclared_close_warning(first_payload) == ""

        second_payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(
                tmp_path, "2026-09-24T10:00:00Z",
                bash_commands=("bd close nexus-newpath",), name="second.jsonl",
            ),
        }
        warning = hook._undeclared_close_warning(second_payload)
        assert "nexus-newpath" in warning

    def test_already_resolved_id_is_not_reverified_or_reported_twice_across_a_reset(
        self, tmp_path, isolated_path, monkeypatch
    ):
        """A reset drops `pending` but KEEPS `resolved` -- an id already
        verified before the transcript was replaced must not be
        re-verified (no second bd/T1 round trip) and must not appear
        twice."""
        rows = json.dumps([
            _bd_row("nexus-old", "2026-09-24T10:15:00Z"),
            _bd_row("nexus-fresh", "2026-09-24T10:45:00Z"),
        ])
        isolated_path(_fake_bd(tmp_path, rows))
        calls = []
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: calls.append(sorted(ids)) or {
                "t1_reachable": True, "status": {b: "missing" for b in ids}
            },
        )
        transcript_path = tmp_path / "reset.jsonl"
        transcript_path.write_text(
            "\n".join([
                json.dumps({"type": "mode", "mode": "normal"}),
                json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
                json.dumps(_bash_entry("bd close nexus-old")),
            ]) + "\n"
        )
        payload = {"session_id": "s1", "transcript_path": str(transcript_path)}
        first = hook._undeclared_close_warning(payload)
        assert "nexus-old" in first
        assert calls == [["nexus-old"]]

        # Replace (same path) with a file that does NOT mention nexus-old
        # at all, but does declare a genuinely new close -- content
        # differs early enough to change the head-hash fingerprint
        # regardless of the exact byte-size relationship.
        transcript_path.write_text(
            "\n".join([
                json.dumps({"type": "mode", "mode": "normal"}),
                json.dumps({"type": "file-history-snapshot", "timestamp": "2026-09-24T10:00:00Z"}),
                json.dumps(_bash_entry("bd close nexus-fresh")),
            ]) + "\n"
        )
        second = hook._undeclared_close_warning(payload)
        assert "nexus-old" in second  # still reported, from cache
        assert "nexus-fresh" in second  # the new one, freshly verified
        assert calls == [["nexus-old"], ["nexus-fresh"]]  # nexus-old never re-verified


# --- run() integration: still only ever approves ---------------------------


class TestRunIntegration:
    def test_run_includes_the_backstop_warning_in_the_reason(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-bad": "missing"}
            },
        )
        # on_stop stays default (unset -> not True), so only the RDR-184-
        # family warnings (reconcile + this backstop) can appear.
        monkeypatch.setattr(hook, "_read_config", lambda: {})
        payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(
                tmp_path, "2026-09-24T10:00:00Z", bash_commands=("bd close nexus-bad",)
            ),
        }
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert "nexus-bad" in parsed.get("reason", "")

    def test_run_never_denies_even_with_undeclared_closes(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="", deadline_seconds=None: {
                "t1_reachable": True, "status": {"nexus-bad": "missing"}
            },
        )
        monkeypatch.setattr(hook, "_read_config", lambda: {"on_stop": True})
        payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(
                tmp_path, "2026-09-24T10:00:00Z", bash_commands=("bd close nexus-bad",)
            ),
        }
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert result.exit_code == 0
