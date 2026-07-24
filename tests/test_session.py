"""Session-id generator and Claude-session flat-file behaviours.

The legacy getsid-keyed session-file scheme (``_stable_pid``,
``session_file_path``, ``write_session_file``, ``read_session_id``)
was deleted as the RDR-105 P4 follow-up tracked by ``nexus-9nbk``.
Current callers use ``read_claude_session_id`` /
``write_claude_session_id`` against the flat
``~/.config/nexus/current_session`` file.
"""
import os
import re
import time
from pathlib import Path

import pytest

from nexus.session import generate_session_id


# ── Fix #1: T1 chroma store relocation (nexus-ycwec) ─────────────────────────


def test_generate_session_id_is_uuid4() -> None:
    sid = generate_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", sid)


def test_generate_session_id_unique() -> None:
    assert generate_session_id() != generate_session_id()


# ── write_session_record ──────────────────────────────────────────────────────







# ── find_ancestor_session ─────────────────────────────────────────────────────













# ── sweep_stale_sessions ──────────────────────────────────────────────────────













# ── UUID-keyed session records (current scheme; PID-keyed above is legacy) ──






















# ── Migration: legacy numeric-stem files swept on first new-code SessionStart




# ── nexus-99jb Layer 3: aggressive liveness-based reap ───────────────────────



































# ── RDR-094 Phase 3: sweep_orphan_tmpdirs ───────────────────────────────────


class TestT1ServerNotFoundErrorGuidance:
    """_reconnect raises T1ServerNotFoundError with actionable guidance:
    tells the user to /clear or restart the MCP server.  The in-place
    reconnect path is INTENTIONALLY unsupported per RDR-105 P4 (nexus-jnx7);
    this only improves the message, never adds reconnect logic."""

    def test_reconnect_message_mentions_clear_or_restart(self) -> None:
        """The message raised by _reconnect is actionable: it names /clear
        or MCP server restart as the recovery step (nexus-ycwec Fix #4)."""
        import unittest.mock as mock
        from nexus.db.t1 import T1Database, T1ServerNotFoundError

        db = T1Database.__new__(T1Database)
        db._dead = False
        db._session_id = "test-session"

        with pytest.raises(T1ServerNotFoundError) as exc_info:
            db._reconnect()

        text = str(exc_info.value).lower()
        # Must name the actionable recovery step
        assert "/clear" in text or "restart" in text, (
            f"_reconnect error lacks /clear or restart guidance: {text!r}"
        )

    def test_reconnect_is_idempotent_when_already_dead(self) -> None:
        """_reconnect with _dead=True is a no-op (does not double-raise)."""
        from nexus.db.t1 import T1Database

        db = T1Database.__new__(T1Database)
        db._dead = True
        db._session_id = "test-session"
        # Should return without raising
        db._reconnect()
