# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx-hook mcp-connect-check`` is a silent no-op (nexus-qxyqz).

The mid-session "nx-mcp is not connected" warning it used to print was
deleted: the session-id-keyed connect marker it read cannot be made a
reliable liveness signal (a nested ``claude -p`` server, the ``nx doctor``
probe, a ``/mcp`` reconnect overlap and ``/clear``/``/resume`` each left it
reading a live server as disconnected). The verb stays REGISTERED, exiting 0
with empty stdout, because published plugins still name it in ``hooks.json``
and ``tests/e2e/hook-cli-skew`` fires every entry against every CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from nexus._hook_runtime import entry
from nexus.hooks.mcp_connect_check import run

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"


def _walk_args(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "args" and isinstance(v, list):
                found.extend(str(a) for a in v)
            else:
                found.extend(_walk_args(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_args(item))
    return found


class TestSilentNoOp:
    def test_registered_in_the_verb_table(self) -> None:
        assert entry.VERB_TABLE["mcp-connect-check"] == "nexus.hooks.mcp_connect_check"

    def test_no_output_where_it_used_to_warn(self, tmp_path: Path, monkeypatch) -> None:
        """A session that was connected and whose marker now names a dead pid
        is exactly what the verb used to warn about."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        sid = "sess-was-connected"
        (tmp_path / f"mcp_connect_marker.{sid}").write_text(json.dumps(
            {"pid": 999_999_999, "published_at": 1.0, "expires_at": 2.0}
        ))
        (tmp_path / f"mcp_connect_check_state.{sid}").write_text(
            json.dumps({"ever_connected": True, "warned_since_last_connected": False})
        )
        before = {p.name: p.read_text() for p in tmp_path.iterdir()}

        result = run({"session_id": sid})

        assert not result.stdout
        assert {p.name: p.read_text() for p in tmp_path.iterdir()} == before, (
            "the verb must neither create nor rewrite any state file"
        )

    def test_a_missing_or_empty_payload_is_fine(self) -> None:
        assert not run(None).stdout
        assert not run({}).stdout

    def test_the_prompt_hook_no_longer_pays_for_the_mcp_package(self, tmp_path: Path) -> None:
        """The per-prompt cost the verb carried: ``nexus.mcp`` (which eagerly
        imports the whole MCP server) plus structlog, on every UserPromptSubmit."""
        code = (
            "import sys\n"
            "from nexus.hooks.mcp_connect_check import run\n"
            "run({'session_id': 'x'})\n"
            "bad = [m for m in ('nexus.mcp', 'nexus.mcp.core', 'structlog') if m in sys.modules]\n"
            "print(','.join(bad))\n"
        )
        env = {**os.environ, "NEXUS_CONFIG_DIR": str(tmp_path)}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True,
        ).stdout.strip()
        assert out == ""


class TestNotRegisteredInThePlugin:
    def test_hooks_json_has_no_mcp_connect_check_entry(self) -> None:
        args = _walk_args(json.loads(HOOKS_JSON.read_text()))
        assert "mcp-connect-check" not in args
        # non-vacuity: the walker does see the other verb entries
        assert "mcp-connect-wait" in args


def _dispatch(verb: str, stdin: bytes, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(tmp_path)}
    return subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", verb],
        input=stdin, capture_output=True, env=env, timeout=60,
    )


class TestThroughTheRealEntryPoint:
    """The path a published plugin still takes: ``nx-hook mcp-connect-check``
    through ``entry.main``, on whatever stdin Claude Code hands it."""

    def test_garbage_stdin_exits_zero_with_empty_stdout(self, tmp_path: Path) -> None:
        r = _dispatch("mcp-connect-check", b"\xff\xfenot json {{", tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout == b""

    def test_empty_stdin_exits_zero_with_empty_stdout(self, tmp_path: Path) -> None:
        r = _dispatch("mcp-connect-check", b"", tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout == b""

    def test_an_unregistered_verb_would_show_the_plugin_ahead_message(
        self, tmp_path: Path,
    ) -> None:
        """Non-vacuity, and the reason the verb is kept: were it removed from
        the table, an old plugin naming it would print this on EVERY prompt."""
        r = _dispatch("no-such-verb-qxyqz", b"{}", tmp_path)
        assert r.returncode == 0
        assert b"ahead of the installed nx CLI" in r.stdout
