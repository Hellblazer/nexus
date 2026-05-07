# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-105 P1 / nexus-4fek: hybrid T1 discovery spike tests.

Behind feature flag ``NX_T1_NEW_DISCOVERY=1``. Verifies:

* ``find_immediate_claude_pid`` returns the FIRST ``claude*`` ancestor
  walking up, NOT the topmost (RF-6, the load-bearing fix that prevents
  owned-mode isolation breakage).
* Address-file primitives (``write_t1_addr`` / ``read_t1_addr_for`` /
  ``unlink_t1_addr``) round-trip atomically.
* ``T1Database.__init__`` flag-gated paths: env (Path A), addr file
  (Path B), legacy fall-through.
* MCP lifespan augmentation publishes the addr file + populates
  ``_t1_state.T1_ADDR`` when flag-on; cleanup unlinks + resets.
* Dispatcher env builder honours ``share_t1`` + flag.
* End-to-end: subprocess sibling discovers a live chroma via the addr
  file (Path B) and via env (Path A).
"""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# find_immediate_claude_pid (RF-6: first not topmost)
# ─────────────────────────────────────────────────────────────────────────────


class TestFindImmediateClaudePid:
    """RF-6: returns the FIRST claude ancestor walking up, not the topmost.

    Topmost-walk (legacy ``find_claude_root_pid``) silently breaks
    owned-mode isolation: an owned ``claude -p`` subprocess MCP would
    write its addr file at the parent's claude_pid (clobbering the
    parent) and read its own discovery from the parent's addr file
    (silently sharing instead of isolating).
    """

    def test_returns_first_claude_not_topmost(self):
        """Process tree::

            test pid (1000, python)
              ppid -> 1100 (python: MCP wrapper of immediate claude)
                ppid -> 1200 (claude: IMMEDIATE)         (correct return)
                  ppid -> 1300 (python: MCP wrapper of topmost claude)
                    ppid -> 1400 (claude: TOPMOST)       (legacy returns this)
                      ppid -> 1 (init; walk stops)
        """
        from nexus.session import find_immediate_claude_pid

        ppid_map = {1000: 1100, 1100: 1200, 1200: 1300, 1300: 1400, 1400: 1}
        comm_map = {
            1000: "python",
            1100: "python",
            1200: "claude",
            1300: "python",
            1400: "claude",
        }

        def fake_ppid(pid: int) -> int | None:
            v = ppid_map.get(pid)
            return v if v and v > 1 else None

        def fake_comm(pid: int) -> str:
            return comm_map.get(pid, "")

        with patch("nexus.session._ppid_of", side_effect=fake_ppid), \
             patch("nexus.session._command_name_of", side_effect=fake_comm):
            result = find_immediate_claude_pid(start_pid=1000)
            assert result == 1200, (
                f"Expected immediate claude (1200), got {result}. "
                "Topmost-walk would return 1400; that's the bug RF-6 closes."
            )

    def test_returns_immediate_ppid_when_no_claude_in_chain(self):
        """No claude ancestor: fall back to immediate ppid (matches
        legacy behaviour for the no-claude case)."""
        from nexus.session import find_immediate_claude_pid

        ppid_map = {500: 600, 600: 700, 700: 1}
        comm_map = {500: "python", 600: "bash", 700: "init"}

        def fake_ppid(pid: int) -> int | None:
            v = ppid_map.get(pid)
            return v if v and v > 1 else None

        with patch("nexus.session._ppid_of", side_effect=fake_ppid), \
             patch("nexus.session._command_name_of",
                   side_effect=lambda pid: comm_map.get(pid, "")):
            assert find_immediate_claude_pid(start_pid=500) == 600

    def test_single_claude_ancestor(self):
        """One claude in chain: returns it."""
        from nexus.session import find_immediate_claude_pid

        ppid_map = {500: 600, 600: 700, 700: 1}
        comm_map = {500: "python", 600: "claude", 700: "bash"}

        def fake_ppid(pid: int) -> int | None:
            v = ppid_map.get(pid)
            return v if v and v > 1 else None

        with patch("nexus.session._ppid_of", side_effect=fake_ppid), \
             patch("nexus.session._command_name_of",
                   side_effect=lambda pid: comm_map.get(pid, "")):
            assert find_immediate_claude_pid(start_pid=500) == 600

    def test_match_is_case_insensitive_and_prefix(self):
        """``Claude``, ``claude-code``, ``ClaudeFoo`` all match."""
        from nexus.session import find_immediate_claude_pid

        ppid_map = {500: 600, 600: 1}
        comm_map = {500: "python", 600: "Claude-Code"}

        with patch("nexus.session._ppid_of",
                   side_effect=lambda pid: ppid_map.get(pid) if ppid_map.get(pid, 0) > 1 else None), \
             patch("nexus.session._command_name_of",
                   side_effect=lambda pid: comm_map.get(pid, "")):
            assert find_immediate_claude_pid(start_pid=500) == 600


# ─────────────────────────────────────────────────────────────────────────────
# Address-file primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestT1AddrFile:
    """Single-writer ``~/.config/nexus/t1_addr.<claude_pid>``.

    File contents: ``host:port\\n``. Atomic write via temp-then-replace.
    """

    def test_t1_addr_path_under_nexus_config(self, tmp_path, monkeypatch):
        from nexus.session import t1_addr_path

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = t1_addr_path(12345)
        assert path.name == "t1_addr.12345"
        assert path.parent == tmp_path

    def test_write_read_roundtrip(self, tmp_path, monkeypatch):
        from nexus.session import read_t1_addr_for, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        write_t1_addr(12345, "127.0.0.1", 54321)
        assert read_t1_addr_for(12345) == ("127.0.0.1", 54321)

    def test_read_missing_returns_none(self, tmp_path, monkeypatch):
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        assert read_t1_addr_for(99999) is None

    def test_unlink_idempotent(self, tmp_path, monkeypatch):
        from nexus.session import read_t1_addr_for, unlink_t1_addr, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        write_t1_addr(54321, "127.0.0.1", 11111)
        unlink_t1_addr(54321)
        assert read_t1_addr_for(54321) is None
        # Second unlink: no-op (file already gone), no exception.
        unlink_t1_addr(54321)
        assert read_t1_addr_for(54321) is None

    def test_atomic_write_replaces_existing(self, tmp_path, monkeypatch):
        from nexus.session import read_t1_addr_for, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        write_t1_addr(7, "127.0.0.1", 1000)
        write_t1_addr(7, "127.0.0.1", 2000)
        assert read_t1_addr_for(7) == ("127.0.0.1", 2000)

    def test_read_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        from nexus.session import read_t1_addr_for, t1_addr_path

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = t1_addr_path(13)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage no colon\n")
        assert read_t1_addr_for(13) is None

    def test_addr_file_permissions(self, tmp_path, monkeypatch):
        """Per the rest of the module, dir is 0o700 and file is 0o600."""
        from nexus.session import t1_addr_path, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        write_t1_addr(11, "127.0.0.1", 22)
        path = t1_addr_path(11)
        assert path.exists()
        # Permissions check (best-effort: the platform may strip group/other bits).
        mode = path.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"world/group readable: {oct(mode)}"


# ─────────────────────────────────────────────────────────────────────────────
# _t1_state minimal module
# ─────────────────────────────────────────────────────────────────────────────


class TestT1StateModule:
    def test_t1_state_module_exists(self):
        from nexus.mcp import _t1_state

        # Initial value or a previously-set tuple. Type contract only.
        assert _t1_state.T1_ADDR is None or isinstance(_t1_state.T1_ADDR, tuple)

    def test_t1_state_has_no_heavy_imports(self):
        """RF-7: the module must NOT pull FastMCP, chromadb, or nexus.corpus.

        Verified by reading the module's source directly.
        """
        from pathlib import Path

        import nexus.mcp._t1_state as mod
        src = Path(mod.__file__).read_text()
        for forbidden in ("import chromadb", "from chromadb",
                          "import fastmcp", "from fastmcp",
                          "from mcp.server", "import mcp.server",
                          "from nexus.corpus", "import nexus.corpus"):
            assert forbidden not in src, (
                f"_t1_state must stay stdlib-only; saw {forbidden!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# T1Database constructor flag-gated paths (Path A: env, Path B: file)
# ─────────────────────────────────────────────────────────────────────────────


class TestT1DatabaseFlagOnEnvPath:
    """Path A: parent MCP put NX_T1_HOST + NX_T1_PORT in subprocess env."""

    def test_env_path_uses_http_client(self, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_client = MagicMock()
        fake_chromadb.HttpClient.return_value = fake_client
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        monkeypatch.setenv("NX_T1_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "12345")
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        from nexus.db.t1 import T1Database

        T1Database()
        fake_chromadb.HttpClient.assert_called_once_with(host="127.0.0.1", port=12345)


class TestT1DatabaseFlagOnFilePath:
    """Path B: subprocess sibling reads addr file via PPID walk."""

    def test_file_path_uses_http_client(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_client = MagicMock()
        fake_chromadb.HttpClient.return_value = fake_client
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        from nexus.session import write_t1_addr
        write_t1_addr(11111, "127.0.0.1", 9999)

        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=11111):
            from nexus.db.t1 import T1Database
            T1Database()
        fake_chromadb.HttpClient.assert_called_once_with(host="127.0.0.1", port=9999)


class TestT1DatabaseFlagOffPreservesLegacyBehaviour:
    """Flag-off: legacy resolver chain runs unchanged."""

    def test_flag_off_with_skip_t1_uses_ephemeral(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.delenv("NX_T1_NEW_DISCOVERY", raising=False)
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        from nexus.db.t1 import T1Database
        db = T1Database()
        assert db.session_id  # constructor succeeded


class TestT1DatabaseFlagOnFallsThroughToLegacy:
    """When the flag is on but NEITHER env vars NOR an addr file is
    present, ``_try_new_discovery_paths`` returns False and the
    constructor falls through to the legacy resolver. P2 will replace
    this with fail-loud once the addr-file path is the canonical
    sibling discovery surface.
    """

    def test_no_env_no_file_uses_legacy_skip_t1(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        # Legacy resolver path: NEXUS_SKIP_T1=1 lets the constructor
        # construct an EphemeralClient cleanly. Without it the legacy
        # path raises T1ServerNotFoundError.
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")

        # Mock find_immediate_claude_pid so it doesn't return a real
        # ancestor whose addr file might exist somewhere.
        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=99999):
            from nexus.db.t1 import T1Database
            db = T1Database()

        # Legacy ephemeral path was taken: no HttpClient call.
        fake_chromadb.HttpClient.assert_not_called()
        fake_chromadb.EphemeralClient.assert_called_once()
        assert db.session_id


class TestT1DatabaseFlagOnPrecedence:
    """When both env and file are present, env wins (RF-5 precedence)."""

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "1111")
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        from nexus.session import write_t1_addr
        write_t1_addr(22222, "10.0.0.2", 2222)

        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=22222):
            from nexus.db.t1 import T1Database
            T1Database()
        # Should have used env-supplied 10.0.0.1:1111, not file-supplied 10.0.0.2:2222.
        fake_chromadb.HttpClient.assert_called_once_with(host="10.0.0.1", port=1111)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher env builder (share_t1 flag-gated)
# ─────────────────────────────────────────────────────────────────────────────


class TestDispatcherEnvBuilder:
    """``_build_dispatch_env`` decides whether the subprocess inherits
    ``NX_T1_HOST/PORT`` (share_t1=True + flag-on + parent T1 live) or
    falls back to the legacy ``NEXUS_SKIP_T1=1`` ephemeral path."""

    def test_legacy_path_when_flag_off(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.delenv("NX_T1_NEW_DISCOVERY", raising=False)
        env = _build_dispatch_env(share_t1=False, parent_session_id="parent-uuid")
        assert env.get("NEXUS_SKIP_T1") == "1"
        assert "NX_T1_HOST" not in env
        assert env.get("NX_SESSION_ID") == "parent-uuid"

    def test_legacy_path_when_share_t1_false(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        env = _build_dispatch_env(share_t1=False, parent_session_id=None)
        assert env.get("NEXUS_SKIP_T1") == "1"
        assert "NX_T1_HOST" not in env

    def test_share_t1_passes_env_when_flag_on(self, monkeypatch):
        from nexus.mcp import _t1_state
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        prev = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = ("127.0.0.1", 12345)
        try:
            env = _build_dispatch_env(share_t1=True, parent_session_id="parent")
        finally:
            _t1_state.T1_ADDR = prev
        assert env.get("NX_T1_HOST") == "127.0.0.1"
        assert env.get("NX_T1_PORT") == "12345"
        assert env.get("NX_T1_NEW_DISCOVERY") == "1"
        assert "NEXUS_SKIP_T1" not in env
        assert env.get("NX_SESSION_ID") == "parent"

    def test_share_t1_raises_when_t1_addr_unset(self, monkeypatch):
        from nexus.mcp import _t1_state
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")
        prev = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        try:
            with pytest.raises(RuntimeError, match="share_t1"):
                _build_dispatch_env(share_t1=True, parent_session_id=None)
        finally:
            _t1_state.T1_ADDR = prev


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan augmentation: addr-file publish + cleanup
# ─────────────────────────────────────────────────────────────────────────────


class TestLifespanAugmentation:
    """When NX_T1_NEW_DISCOVERY=1 AND we own the chroma, the lifespan
    augmentation publishes the addr file + populates ``_t1_state.T1_ADDR``."""

    def test_publish_writes_addr_and_populates_state(self, tmp_path, monkeypatch):
        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        try:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update({
                "server_port": 4242,
                "session_id": "test-uuid",
            })
            _t1_state.T1_ADDR = None
            with patch("nexus.session.find_immediate_claude_pid",
                       return_value=77777):
                mcp_core._t1_publish_addr_for_new_discovery()
            assert read_t1_addr_for(77777) == ("127.0.0.1", 4242)
            assert _t1_state.T1_ADDR == ("127.0.0.1", 4242)
            assert mcp_core._OWNED_CHROMA.get("t1_addr_claude_pid") == 77777
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr

    def test_publish_no_op_when_flag_off(self, tmp_path, monkeypatch):
        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_NEW_DISCOVERY", raising=False)

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        try:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update({"server_port": 4242})
            _t1_state.T1_ADDR = None
            mcp_core._t1_publish_addr_for_new_discovery()
            # No file written, no state change.
            assert read_t1_addr_for(77777) is None
            assert _t1_state.T1_ADDR is None
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr

    def test_cleanup_unlinks_and_resets_state(self, tmp_path, monkeypatch):
        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        try:
            write_t1_addr(33333, "127.0.0.1", 4242)
            _t1_state.T1_ADDR = ("127.0.0.1", 4242)
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update({"t1_addr_claude_pid": 33333})
            mcp_core._t1_unpublish_addr_for_new_discovery()
            assert read_t1_addr_for(33333) is None
            assert _t1_state.T1_ADDR is None
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr

    def test_publish_skips_when_chroma_reused(self, tmp_path, monkeypatch):
        """``reused=True`` means another MCP owns the chroma; we don't
        publish (and we don't delete on shutdown)."""
        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_NEW_DISCOVERY", "1")

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        try:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update({
                "server_port": 4242,
                "reused": True,
            })
            _t1_state.T1_ADDR = None
            mcp_core._t1_publish_addr_for_new_discovery()
            assert read_t1_addr_for(77777) is None
            assert _t1_state.T1_ADDR is None
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: real chroma + spike subprocess
# ─────────────────────────────────────────────────────────────────────────────


def _start_real_chroma() -> tuple[str, int, int, str]:
    """Boot a real chroma HTTP server for spike E2E tests.

    Returns ``(host, port, server_pid, tmpdir)``. The caller is
    responsible for stopping the server (``stop_t1_server``) AND
    rmtree-ing the tmpdir; ``stop_t1_server`` only signals the
    process and does not clean up the on-disk SQLite database.
    """
    from nexus.session import start_t1_server

    return start_t1_server()


@pytest.mark.integration
class TestE2EFileDiscovery:
    """Spike Phase 1 exit criterion (2): subprocess sibling discovers
    parent's chroma via the addr file (Path B).

    Runs a real chroma HTTP server. Marked integration: not part of the
    default unit suite. Run via ``uv run pytest -m integration``.
    """

    def test_sibling_subprocess_connects_via_addr_file(
        self, tmp_path, monkeypatch
    ):
        import shutil

        from nexus.session import (
            stop_t1_server,
            unlink_t1_addr,
            write_t1_addr,
        )

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        host, port, server_pid, chroma_tmpdir = _start_real_chroma()
        own_pid = os.getpid()
        try:
            write_t1_addr(own_pid, host, port)
            try:
                # Spawn a "sibling" subprocess that simulates a Bash-tool
                # invocation. find_immediate_claude_pid is monkey-pinned to
                # our PID so the test's own process plays the role of the
                # subprocess's owning Claude ancestor.
                code = (
                    "import os\n"
                    f"os.environ['NEXUS_CONFIG_DIR'] = {str(tmp_path)!r}\n"
                    "os.environ['NX_T1_NEW_DISCOVERY'] = '1'\n"
                    "os.environ.pop('NX_T1_HOST', None)\n"
                    "os.environ.pop('NX_T1_PORT', None)\n"
                    "os.environ.pop('NEXUS_SKIP_T1', None)\n"
                    "import nexus.session as session\n"
                    f"session.find_immediate_claude_pid = lambda start_pid=None: {own_pid}\n"
                    "import nexus.db.t1 as t1\n"
                    f"t1.find_immediate_claude_pid = lambda start_pid=None: {own_pid}\n"
                    "from nexus.db.t1 import T1Database\n"
                    "db = T1Database()\n"
                    "doc_id = db.put('hello from sibling', tags='spike')\n"
                    "print('OK', doc_id)\n"
                )
                proc = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert proc.returncode == 0, (
                    f"subprocess failed (rc={proc.returncode}): "
                    f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
                )
                assert proc.stdout.startswith("OK"), proc.stdout
            finally:
                unlink_t1_addr(own_pid)
        finally:
            stop_t1_server(server_pid)
            shutil.rmtree(chroma_tmpdir, ignore_errors=True)


@pytest.mark.integration
class TestE2EEnvDiscovery:
    """Spike Phase 1 exit criterion (1): MCP-dispatched subprocess
    receives ``NX_T1_HOST/PORT`` via inherited env (Path A).
    """

    def test_subprocess_connects_via_env(self, tmp_path):
        import shutil

        from nexus.session import stop_t1_server

        host, port, server_pid, chroma_tmpdir = _start_real_chroma()
        try:
            code = (
                "import os\n"
                "from nexus.db.t1 import T1Database\n"
                "db = T1Database()\n"
                "doc_id = db.put('hello from MCP-dispatched child', tags='spike')\n"
                "print('OK', doc_id)\n"
            )
            env = {
                **os.environ,
                "NX_T1_NEW_DISCOVERY": "1",
                "NX_T1_HOST": host,
                "NX_T1_PORT": str(port),
                "NEXUS_CONFIG_DIR": str(tmp_path),
            }
            env.pop("NEXUS_SKIP_T1", None)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert proc.returncode == 0, (
                f"subprocess failed (rc={proc.returncode}): "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
            assert proc.stdout.startswith("OK"), proc.stdout
        finally:
            stop_t1_server(server_pid)
            shutil.rmtree(chroma_tmpdir, ignore_errors=True)
