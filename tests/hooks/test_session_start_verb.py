# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.hooks.session_start_verb`` -- the ``session-start`` hook
ported onto the command-tier dispatch mechanism (RDR-215 MVV port B, bead
nexus-q02nx.5).

Two things this file must prove, per the bead:

1. ``run(payload)`` extracts ``session_id``/``source`` from an
   already-parsed payload dict exactly the way
   ``nexus.commands.hook.session_start_cmd`` does from its own stdin read
   (``TestSessionStartCmdSourcePassthrough`` in ``tests/test_hook_cli.py``),
   so the two entries -- ``nx-hook session-start`` and ``nx hook
   session-start`` -- call ``nexus.hooks.session_start`` with identical
   arguments for identical input.
2. The real, unmocked dispatch path -- ``nexus.hooks.entry`` resolving
   ``session-start`` out of its OWN ``VERB_TABLE``, not the test-only
   override -- produces byte-identical stdout to the Click verb for the
   same real JSON payload on stdin.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from nexus.hooks import entry


# -- run(): field extraction, mirroring session_start_cmd -------------------


class TestRunFieldExtraction:
    def test_extracts_session_id_and_source_and_delegates(self) -> None:
        from nexus.hooks.session_start_verb import run

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ) as mock_start:
            result = run({"session_id": "s1", "source": "clear"})
        assert result.stdout == "Nexus ready (session: s1)."
        assert result.exit_code == 0
        mock_start.assert_called_once_with(claude_session_id="s1", source="clear")

    def test_missing_payload_passes_none_for_both_fields(self) -> None:
        from nexus.hooks.session_start_verb import run

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ) as mock_start:
            run(None)
        mock_start.assert_called_once_with(claude_session_id=None, source=None)

    def test_payload_missing_both_fields_passes_none_for_both(self) -> None:
        from nexus.hooks.session_start_verb import run

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ) as mock_start:
            run({})
        mock_start.assert_called_once_with(claude_session_id=None, source=None)

    def test_non_string_fields_are_treated_as_absent(self) -> None:
        """isinstance guard, carried from session_start_cmd verbatim: a
        malformed payload (wrong JSON types) must not crash the hook or
        pass a non-string through."""
        from nexus.hooks.session_start_verb import run

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ) as mock_start:
            run({"session_id": 12345, "source": ["clear"]})
        mock_start.assert_called_once_with(claude_session_id=None, source=None)

    def test_empty_string_fields_are_treated_as_absent(self) -> None:
        from nexus.hooks.session_start_verb import run

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ) as mock_start:
            run({"session_id": "", "source": ""})
        mock_start.assert_called_once_with(claude_session_id=None, source=None)


# -- registration -------------------------------------------------------------


class TestRegisteredInTheRealVerbTable:
    def test_session_start_resolves_to_the_new_module(self) -> None:
        assert entry.VERB_TABLE["session-start"] == "nexus.hooks.session_start_verb"

    def test_session_start_is_not_a_ledger_verb(self) -> None:
        """No caller branches on this verb's exit code (RDR-215 Contracts);
        entry.main forces exit 0 for any verb not in LEDGER_VERBS."""
        assert "session-start" not in entry.LEDGER_VERBS


# -- real dispatch, in-process: entry.main() vs the Click verb --------------
#
# Calls entry.main() directly (not a subprocess) so nexus.hooks.session_start
# can be mocked identically on both sides of the comparison, isolating the
# question this test asks (do the two entries call through with the same
# arguments and render the same bytes) from mailbox-arm's real network probe
# and the stale-host ps scan, both exercised for real by the subprocess
# smoke test below.


class TestInProcessDispatchParity:
    def _run_nx_hook(self, monkeypatch, stdin_text: str) -> tuple[str, int]:
        import io

        monkeypatch.setattr(sys, "argv", ["nx-hook", "session-start"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        code = 0
        try:
            entry.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        return out.getvalue(), code

    def test_same_payload_produces_the_same_bytes_as_the_click_verb(
        self, monkeypatch,
    ) -> None:
        from click.testing import CliRunner

        from nexus.commands.hook import hook_group

        stdin_text = json.dumps({"session_id": "s1", "source": "startup"})
        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ):
            nx_hook_out, nx_hook_code = self._run_nx_hook(monkeypatch, stdin_text)
        assert nx_hook_code == 0
        assert nx_hook_out == "Nexus ready (session: s1).\n"

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s1).",
        ):
            result = CliRunner().invoke(
                hook_group, ["session-start"], input=stdin_text,
            )
        assert result.exit_code == 0
        assert result.output == nx_hook_out

    def test_no_stdin_produces_the_same_bytes_as_the_click_verb(
        self, monkeypatch,
    ) -> None:
        from click.testing import CliRunner

        from nexus.commands.hook import hook_group

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s2).",
        ) as mock_start:
            nx_hook_out, nx_hook_code = self._run_nx_hook(monkeypatch, "")
        assert nx_hook_code == 0
        mock_start.assert_called_once_with(claude_session_id=None, source=None)

        with patch(
            "nexus.hooks.session_start", return_value="Nexus ready (session: s2).",
        ):
            result = CliRunner().invoke(hook_group, ["session-start"], input="")
        assert result.output == nx_hook_out


# -- real subprocess smoke test: no mocking at all ---------------------------


_REAL_PAYLOAD = json.dumps({"session_id": "x-nexus-q02nx-5-parity", "source": "startup"})


def _spawn(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=_REAL_PAYLOAD, env=env, capture_output=True, text=True, timeout=30,
    )


def test_nx_hook_and_nx_hook_click_verb_produce_identical_bytes_end_to_end(
    tmp_path: Path, monkeypatch,
) -> None:
    """The bead's own VERIFICATION step, automated: pipe a real JSON payload
    into both entries and diff their stdout. Uses ``source: startup`` (not
    in ``_T1_HANDOFF_SOURCES``) so neither entry attempts a process-tree
    walk for a T1 handoff marker -- the one piece of real I/O in
    ``session_start()`` this test does not want to depend on. Both
    invocations share one ``NEXUS_CONFIG_DIR`` so the mailbox-arm probe's
    second call reads the first call's cache instead of re-probing.
    """
    import os

    env = dict(os.environ)
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "config")
    env.pop("NX_SESSION_ID", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)

    nx_hook_proc = _spawn([sys.executable, "-m", "nexus.hooks.entry", "session-start"], env)
    assert nx_hook_proc.returncode == 0, nx_hook_proc.stderr

    click_proc = _spawn([sys.executable, "-m", "nexus.cli", "hook", "session-start"], env)
    assert click_proc.returncode == 0, click_proc.stderr

    assert nx_hook_proc.stdout == click_proc.stdout, (
        f"nx-hook produced:\n{nx_hook_proc.stdout!r}\n\n"
        f"nx hook produced:\n{click_proc.stdout!r}"
    )
