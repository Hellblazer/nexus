# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 attribution labels a write with the active session (nexus-d8yul).

``resolve_attribution`` used to read ``NX_SESSION_ID`` -> nothing -> the
machine-wide ``current_session`` file (its docstring promised an
``NX_SESSION_ID`` step that ``read_claude_session_id`` does not have), and it
ignored ``CLAUDE_CODE_SESSION_ID``. The file belongs to whichever Claude
session wrote it last, so concurrent Claude windows were mislabelled. It now
goes through ``resolve_active_session_id``. These tests use a real file in a
tmp config dir, not a patched reader.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus.session as session_mod
from nexus.db.t2._attribution import resolve_attribution


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("NX_AGENT", raising=False)
    return tmp_path


def _write_file(config_dir: Path, sid: str) -> None:
    (config_dir / "current_session").write_text(sid)


def test_claude_code_session_id_beats_the_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_file(config_dir, "last-writer-from-file")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "this-windows-session")

    assert resolve_attribution(None, None) == (None, "this-windows-session")


def test_nx_session_id_beats_claude_code_session_id_and_the_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docstring's promised NX_SESSION_ID step is real."""
    _write_file(config_dir, "last-writer-from-file")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "this-windows-session")
    monkeypatch.setenv("NX_SESSION_ID", "forced-by-nx")

    assert resolve_attribution(None, None) == (None, "forced-by-nx")


def test_bare_cli_with_no_markers_still_falls_back_to_the_file(
    config_dir: Path,
) -> None:
    _write_file(config_dir, "last-writer-from-file")

    assert resolve_attribution(None, None) == (None, "last-writer-from-file")


def test_no_markers_and_no_file_is_none(config_dir: Path) -> None:
    assert resolve_attribution(None, None) == (None, None)


def test_explicit_session_is_never_overridden(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient")

    assert resolve_attribution("a", "explicit") == ("a", "explicit")


def _non_claude_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process a Cursor/Codex/Gemini-hosted ``nx-mcp``: marked as
    an MCP server, no Claude Code marker, no session env."""
    for name in (
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID", "NX_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    session_mod.mark_mcp_server_process()  # reset by the autouse conftest flag fixture


def test_a_gated_mcp_server_labels_nothing_instead_of_borrowing_the_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composes with the REAL xs7fp gate: an MCP server Claude Code did not
    start, with no session env, has no claim on the file, so the label is
    ``None`` (stored NULL), not the last Claude session's id. Fails if the
    gate is removed from ``resolve_active_session_id`` or if
    ``resolve_attribution`` goes back to reading the file directly."""
    _write_file(config_dir, "last-writer-from-file")
    _non_claude_mcp_server(monkeypatch)

    assert resolve_attribution(None, None) == (None, None)


def test_the_same_server_started_by_claude_code_keeps_the_file_label(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the test above: the only change is the Claude
    Code marker, and the file is read again, so the gate is what declined."""
    _write_file(config_dir, "last-writer-from-file")
    _non_claude_mcp_server(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")

    assert resolve_attribution(None, None) == (None, "last-writer-from-file")
