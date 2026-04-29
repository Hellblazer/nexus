# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rv2x: ``nx hook session-start`` must not hang on TTY stdin.

The bug: ``session_start_cmd`` calls ``sys.stdin.read()`` unconditionally
to parse the Claude-Code JSON payload. When stdin is a TTY with no data
(any non-Claude-Code invocation: shell pipeline, ad-hoc CLI run with no
piped input, test harness), the call blocks indefinitely. Claude Code
itself remains usable because the 10s timeout in ``nx/hooks/hooks.json``
bounds the SessionStart entry, but the CLI surface is broken for any
out-of-band invocation.

Repro that surfaced the bug::

    nx hook session-start | wc -lc      # hangs forever; ^C to escape
    echo '{}' | nx hook session-start    # returns immediately (correct)

The fix: factor the stdin-payload read into a helper that is testable
without going through Click's CliRunner (which substitutes its own
StringIO for sys.stdin and so cannot reproduce the TTY-blocked case).
The helper checks ``isatty()`` first; if True, it returns ``None``
without reading.
"""
from __future__ import annotations

import io


# ── _read_stdin_session_id helper ──────────────────────────────────────────


class TestReadStdinSessionId:
    """The helper extracted from ``session_start_cmd``.

    Tests pass an explicit stdin so the TTY-vs-pipe behaviour can be
    exercised directly. CliRunner-based tests can't reproduce the bug
    because Click substitutes its own StringIO for sys.stdin.
    """

    def test_tty_stdin_returns_none_without_reading(self):
        """When stdin.isatty() is True, the helper returns None and
        does NOT call .read() (which would block forever on a real TTY).
        """
        from nexus.commands.hook import _read_stdin_session_id

        class _TtyStream(io.StringIO):
            def isatty(self) -> bool:
                return True

            def read(self, *a, **kw):
                raise AssertionError(
                    "must not call read() when isatty()=True"
                )

        result = _read_stdin_session_id(_TtyStream())
        assert result is None

    def test_piped_json_with_session_id_returns_id(self):
        """Pipe carrying valid JSON with session_id returns the id."""
        from nexus.commands.hook import _read_stdin_session_id

        stdin = io.StringIO('{"session_id": "abc-123"}')
        # StringIO.isatty() returns False by default — matches a real pipe.
        assert _read_stdin_session_id(stdin) == "abc-123"

    def test_piped_json_without_session_id_returns_none(self):
        from nexus.commands.hook import _read_stdin_session_id

        stdin = io.StringIO("{}")
        assert _read_stdin_session_id(stdin) is None

    def test_empty_piped_stdin_returns_none(self):
        """Closed/empty pipe returns None (read() returns '', JSON
        decode fails, helper swallows and returns None).
        """
        from nexus.commands.hook import _read_stdin_session_id

        stdin = io.StringIO("")
        assert _read_stdin_session_id(stdin) is None

    def test_malformed_json_returns_none(self):
        """Malformed JSON is logged-and-swallowed; the helper does not
        raise to the CLI surface.
        """
        from nexus.commands.hook import _read_stdin_session_id

        stdin = io.StringIO("not json {{")
        assert _read_stdin_session_id(stdin) is None

    def test_unexpected_read_exception_returns_none(self):
        """If stdin.read() raises (closed file, OS error), the helper
        returns None instead of crashing the hook.
        """
        from nexus.commands.hook import _read_stdin_session_id

        class _BrokenStdin(io.StringIO):
            def isatty(self) -> bool:
                return False

            def read(self, *a, **kw):
                raise OSError("simulated read failure")

        result = _read_stdin_session_id(_BrokenStdin())
        assert result is None
