# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rv2x: ``nx hook session-start`` must not hang on TTY stdin.

The bug: ``session_start_cmd`` calls ``sys.stdin.read()`` unconditionally
to parse the Claude-Code JSON payload. When stdin is a TTY with no data
(any non-Claude-Code invocation: shell pipeline, ad-hoc CLI run with no
piped input, test harness), the call blocks indefinitely. Claude Code
itself remains usable because the 10s timeout in ``conexus/hooks/hooks.json``
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


# ── _read_stdin_payload helper (nexus-d76vc) ────────────────────────────────
#
# session_start_cmd reads stdin exactly ONCE via this helper and pulls both
# session_id and source out of the same parsed dict (a stream can only be
# read once — the reason _read_stdin_session_id's old read-and-extract-one-
# field shape had to be generalized rather than called twice).


class TestReadStdinPayload:
    def test_tty_stdin_returns_none_without_reading(self):
        from nexus.commands.hook import _read_stdin_payload

        class _TtyStream(io.StringIO):
            def isatty(self) -> bool:
                return True

            def read(self, *a, **kw):
                raise AssertionError(
                    "must not call read() when isatty()=True"
                )

        assert _read_stdin_payload(_TtyStream()) is None

    def test_piped_json_returns_full_dict(self):
        from nexus.commands.hook import _read_stdin_payload

        stdin = io.StringIO('{"session_id": "abc-123", "source": "clear"}')
        assert _read_stdin_payload(stdin) == {
            "session_id": "abc-123", "source": "clear",
        }

    def test_empty_piped_stdin_returns_none(self):
        from nexus.commands.hook import _read_stdin_payload

        assert _read_stdin_payload(io.StringIO("")) is None

    def test_malformed_json_returns_none(self):
        from nexus.commands.hook import _read_stdin_payload

        assert _read_stdin_payload(io.StringIO("not json {{")) is None

    def test_non_dict_json_returns_none(self):
        from nexus.commands.hook import _read_stdin_payload

        assert _read_stdin_payload(io.StringIO("[1, 2, 3]")) is None

    def test_read_stdin_session_id_still_works_via_the_shared_helper(self):
        """Regression pin: refactoring _read_stdin_session_id to delegate
        to _read_stdin_payload must not change its observable contract."""
        from nexus.commands.hook import _read_stdin_session_id

        stdin = io.StringIO('{"session_id": "abc-123", "source": "resume"}')
        assert _read_stdin_session_id(stdin) == "abc-123"


# ── nx hook session-start CLI: source passthrough (nexus-d76vc) ────────────


class TestSessionStartCmdSourcePassthrough:
    """The CLI subcommand extracts BOTH session_id and source from the ONE
    stdin payload and forwards source to hooks.session_start — end-to-end
    from stdin JSON through to the T1 handoff marker writer.
    """

    def test_clear_source_reaches_session_start(self, monkeypatch):
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group

        runner = CliRunner()
        with patch("nexus.hooks.session_start", return_value="Nexus ready (session: s1).") as mock_start:
            result = runner.invoke(
                hook_group, ["session-start"],
                input='{"session_id": "s1", "source": "clear"}',
            )
        assert result.exit_code == 0
        mock_start.assert_called_once_with(claude_session_id="s1", source="clear")

    def test_startup_source_reaches_session_start(self, monkeypatch):
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group

        runner = CliRunner()
        with patch("nexus.hooks.session_start", return_value="Nexus ready (session: s1).") as mock_start:
            result = runner.invoke(
                hook_group, ["session-start"],
                input='{"session_id": "s1", "source": "startup"}',
            )
        assert result.exit_code == 0
        mock_start.assert_called_once_with(claude_session_id="s1", source="startup")

    def test_no_stdin_payload_passes_none_source(self, monkeypatch):
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group

        runner = CliRunner()
        with patch("nexus.hooks.session_start", return_value="Nexus ready (session: s1).") as mock_start:
            result = runner.invoke(hook_group, ["session-start"], input="")
        assert result.exit_code == 0
        mock_start.assert_called_once_with(claude_session_id=None, source=None)

    def test_end_to_end_clear_writes_a_real_marker(self, tmp_path, monkeypatch):
        """Full stack: stdin JSON -> CLI -> hooks.session_start -> a real
        marker file on disk, with a mocked ancestry/sibling resolution
        (no real process tree available under CliRunner)."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group
        from nexus.daemon.t1_handoff import read_handoff_marker

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

        runner = CliRunner()
        with (
            patch("nexus.hooks.write_claude_session_id"),
            patch("nexus.session.find_immediate_claude_pid", return_value=777),
            patch("nexus.session.find_mcp_sibling_pids", return_value=[888]),
        ):
            result = runner.invoke(
                hook_group, ["session-start"],
                input='{"session_id": "new-sess", "source": "clear"}',
            )
        assert result.exit_code == 0
        marker = read_handoff_marker(888, tmp_path)
        assert marker is not None
        assert marker.new_session_id == "new-sess"
        assert marker.claude_pid == 777


# ── nexus-h33x8.4: guidance imperative reaches the real CLI output ─────────


class TestSessionStartCmdGuidanceImperative:
    """True end-to-end: no mocking of ``hooks.session_start`` or
    ``session_start_guidance`` — pins that ``nx hook session-start`` (Tier
    B) is the channel the guidance imperative actually flows through,
    not just that ``hooks.session_start()`` includes it in isolation."""

    def test_guidance_imperative_present_with_no_plugin_root(self, monkeypatch):
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group
        from nexus.session_start_guidance import GUIDANCE_IMPERATIVE

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        runner = CliRunner()
        with _patch("nexus.hooks.write_claude_session_id"):
            result = runner.invoke(
                hook_group, ["session-start"],
                input='{"session_id": "s-h33x8-4-cli"}',
            )
        assert result.exit_code == 0
        assert "Nexus ready" in result.output
        assert GUIDANCE_IMPERATIVE in result.output

    def test_guidance_imperative_absent_when_legacy_plugin_channel_live(
        self, tmp_path, monkeypatch,
    ):
        """Interim-window behavior: an installed plugin whose own
        hooks.json still carries the legacy cat entry suppresses this
        module's emission, so the CLI does not double it up."""
        import json as _json
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from nexus.commands.hook import hook_group
        from nexus.session_start_guidance import GUIDANCE_IMPERATIVE

        plugin_root = tmp_path / "plugin"
        hooks_dir = plugin_root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(_json.dumps({
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
                                "timeout": 5,
                            },
                        ],
                    },
                ],
            },
        }))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        runner = CliRunner()
        with _patch("nexus.hooks.write_claude_session_id"):
            result = runner.invoke(
                hook_group, ["session-start"],
                input='{"session_id": "s-h33x8-4-interim"}',
            )
        assert result.exit_code == 0
        assert "Nexus ready" in result.output
        assert GUIDANCE_IMPERATIVE not in result.output
