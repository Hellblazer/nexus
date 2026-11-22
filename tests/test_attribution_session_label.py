# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 attribution labels a write with the active session (nexus-d8yul).

``resolve_attribution`` goes through ``nexus.session.resolve_session_id``:
``NX_SESSION_ID``, then ``CLAUDE_CODE_SESSION_ID``, then the session in this
process's identity token file (RDR-253 Phase 2 retired the machine-wide
``current_session`` file, which concurrent Claude windows overwrote). The
token-file tier is stubbed per test through ``_token_file_session_id``; the
real read of a token file is tested in tests/test_session_resolve_session_id.py.
"""
from __future__ import annotations

import pytest

import nexus.session as session_mod
from nexus.db.t2._attribution import resolve_attribution


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "NX_AGENT"):
        monkeypatch.delenv(name, raising=False)


def _token_file_names(monkeypatch: pytest.MonkeyPatch, sid: str | None) -> None:
    monkeypatch.setattr(session_mod, "_token_file_session_id", lambda: sid)


def test_claude_code_session_id_beats_the_token_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _token_file_names(monkeypatch, "token-file-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "this-windows-session")

    assert resolve_attribution(None, None) == (None, "this-windows-session")


def test_nx_session_id_beats_claude_code_session_id_and_the_token_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docstring's promised NX_SESSION_ID step is real."""
    _token_file_names(monkeypatch, "token-file-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "this-windows-session")
    monkeypatch.setenv("NX_SESSION_ID", "forced-by-nx")

    assert resolve_attribution(None, None) == (None, "forced-by-nx")


def test_a_process_with_no_session_env_labels_with_its_token_file_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _token_file_names(monkeypatch, "token-file-session")

    assert resolve_attribution(None, None) == (None, "token-file-session")


def test_no_env_and_no_token_file_session_is_none() -> None:
    assert resolve_attribution(None, None) == (None, None)


def test_explicit_session_is_never_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient")

    assert resolve_attribution("a", "explicit") == ("a", "explicit")


def test_a_stale_current_session_file_is_never_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retired machine-wide file, even present with a session id, labels
    nothing: no resolver tier reads it."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "current_session").write_text("last-writer-from-file")

    assert resolve_attribution(None, None) == (None, None)
