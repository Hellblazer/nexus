# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-105 hybrid T1 discovery tests.

The single hybrid-discovery code path (RDR-105 P4) is the only T1
resolution surface. Verifies:

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

import json
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
# Periodic orphan reaper (sweep_orphan_t1_addr_files)
# ─────────────────────────────────────────────────────────────────────────────


class TestSweepOrphanT1AddrFiles:
    """RDR-105 P4 (nexus-sp84): top-level MCP startup runs this sweep
    to reap addr files left by sessions that exited ungracefully.
    Best-effort cleanup; not load-bearing.
    """

    def test_reaps_dead_pid(self, tmp_path, monkeypatch):
        import subprocess as _sub

        from nexus.session import (
            sweep_orphan_t1_addr_files,
            t1_addr_path,
            write_t1_addr,
        )

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        proc = _sub.Popen(["true"])
        proc.wait()
        write_t1_addr(proc.pid, "127.0.0.1", 1234)
        assert t1_addr_path(proc.pid).exists()

        reaped = sweep_orphan_t1_addr_files()
        assert reaped == 1
        assert not t1_addr_path(proc.pid).exists()

    def test_keeps_live_pid(self, tmp_path, monkeypatch):
        import os as _os

        from nexus.session import (
            sweep_orphan_t1_addr_files,
            t1_addr_path,
            write_t1_addr,
        )

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        own_pid = _os.getpid()
        write_t1_addr(own_pid, "127.0.0.1", 9999)

        reaped = sweep_orphan_t1_addr_files()
        assert reaped == 0
        assert t1_addr_path(own_pid).exists()

    def test_skips_malformed_suffix(self, tmp_path, monkeypatch):
        from nexus.session import sweep_orphan_t1_addr_files

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # Files whose suffix is not an integer must be skipped (not
        # reaped, not crashed). Belt-and-braces against an operator
        # leaving stray ``t1_addr.something-weird`` lying around.
        weird = tmp_path / "t1_addr.not-a-number"
        tmp_path.mkdir(parents=True, exist_ok=True)
        weird.write_text("127.0.0.1:5555\n")

        reaped = sweep_orphan_t1_addr_files()
        assert reaped == 0
        assert weird.exists()

    def test_no_op_when_config_dir_missing(self, tmp_path, monkeypatch):
        from nexus.session import sweep_orphan_t1_addr_files

        # Point NEXUS_CONFIG_DIR at a path that does not exist; sweep
        # must return 0 cleanly.
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(missing))
        assert sweep_orphan_t1_addr_files() == 0

    def test_mixed_dead_and_live(self, tmp_path, monkeypatch):
        import os as _os
        import subprocess as _sub

        from nexus.session import (
            sweep_orphan_t1_addr_files,
            t1_addr_path,
            write_t1_addr,
        )

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        own_pid = _os.getpid()
        proc = _sub.Popen(["true"])
        proc.wait()
        write_t1_addr(own_pid, "127.0.0.1", 1)
        write_t1_addr(proc.pid, "127.0.0.1", 2)

        reaped = sweep_orphan_t1_addr_files()
        assert reaped == 1
        assert t1_addr_path(own_pid).exists()
        assert not t1_addr_path(proc.pid).exists()


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

        monkeypatch.setenv("NEXUS_SKIP_T1", "1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        from nexus.db.t1 import T1Database
        db = T1Database()
        assert db.session_id  # constructor succeeded


class TestT1DatabaseFlagOnIsolationPath:
    """Path C (RDR-105 P2 / nexus-mj2o): explicit ``NX_T1_ISOLATED=1``
    or its legacy alias ``NEXUS_SKIP_T1=1`` opts into a per-process
    ``EphemeralClient``. No HTTP discovery attempted.
    """

    def test_nx_t1_isolated_uses_ephemeral(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=99999):
            from nexus.db.t1 import T1Database
            db = T1Database()

        fake_chromadb.HttpClient.assert_not_called()
        fake_chromadb.EphemeralClient.assert_called_once()
        assert db.session_id

    def test_legacy_nexus_skip_t1_alias_uses_ephemeral(
        self, tmp_path, monkeypatch
    ):
        """Per RF-4: ``NEXUS_SKIP_T1=1`` honoured for the 4.27 -> 4.28
        cycle as a deprecated alias for ``NX_T1_ISOLATED=1``."""
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")

        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=99999):
            from nexus.db.t1 import T1Database
            T1Database()

        fake_chromadb.EphemeralClient.assert_called_once()


class TestT1DatabaseFlagOnRaisesOnMisconfiguration:
    """Path D (RDR-105 P2 / nexus-mj2o): no env, no addr file, no
    isolation flag -> raise ``T1ServerNotFoundError``. Replaces P1's
    legacy fall-through.
    """

    def test_raises_when_no_source_available(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        with patch("nexus.db.t1.find_immediate_claude_pid", return_value=99999):
            from nexus.db.t1 import T1Database, T1ServerNotFoundError
            with pytest.raises(T1ServerNotFoundError, match="NX_T1"):
                T1Database()

        fake_chromadb.HttpClient.assert_not_called()
        fake_chromadb.EphemeralClient.assert_not_called()

    def test_raises_when_env_port_malformed(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "not-a-port")
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        from nexus.db.t1 import T1Database, T1ServerNotFoundError
        with pytest.raises(T1ServerNotFoundError):
            T1Database()


class TestT1DatabaseFlagOnLegacyDeleted:
    """Sanity: with the flag on, the constructor must not fall through
    to the legacy resolver chain. The legacy path is invisible in
    flag-on processes per the RDR §'Phase 2 flag-isolation contract'.
    """

    def test_flag_on_with_legacy_session_record_still_uses_new_discovery(
        self, tmp_path, monkeypatch
    ):
        """Even if a legacy session record happens to exist on disk,
        flag-on goes through the new-discovery code path."""
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        from nexus.db.t1 import T1Database
        T1Database()
        # Should hit Path A (env) directly, not the legacy resolver.
        fake_chromadb.HttpClient.assert_called_once_with(host="127.0.0.1", port=5555)





class TestT1DatabaseFlagOnPrecedence:
    """When both env and file are present, env wins (RF-5 precedence)."""

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
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
# session_id resolution chain (nexus-h8ge regression)
# ─────────────────────────────────────────────────────────────────────────────
#
# The four-branch fail-loud constructor in
# ``T1Database._init_new_discovery`` resolves the session_id used as the
# ChromaDB metadata filter. All four branches MUST follow the same chain:
#
#     ctor session_id arg
#         > NX_SESSION_ID env
#         > read_claude_session_id() (~/.config/nexus/current_session)
#         > new uuid4()
#
# The 4.27.0 ship omitted the ``read_claude_session_id()`` step in every
# branch, so two ``T1Database()`` calls in the same Claude session (the
# MCP server and a Bash-tool sibling) minted distinct UUIDs and could
# not see each other's entries via the per-entry session_id metadata
# filter. Production hooks that rely on shell ``nx scratch list``
# (subagent-start, post_compact, pre_close_verification,
# divergence-language-guard) silently saw "No scratch entries." even
# when entries existed. See bead nexus-h8ge for the live shakeout
# evidence.


_PATH_IDS = ["env", "addr_file", "isolation", "client_injection"]


def _setup_path(path_id: str, tmp_path, monkeypatch, fake_chromadb):
    """Configure env + monkeypatches so the named branch fires.

    Returns ``(extra_kwargs, expected_client_attr)`` for the
    ``T1Database`` constructor call.

    * ``env`` -- Path A (NX_T1_HOST + NX_T1_PORT).
    * ``addr_file`` -- Path B (PPID walk + addr file).
    * ``isolation`` -- Path C (NX_T1_ISOLATED=1 + no addr file).
    * ``client_injection`` -- early branch with explicit client=.
    """
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED", "NEXUS_SKIP_T1"):
        monkeypatch.delenv(var, raising=False)

    if path_id == "env":
        monkeypatch.setenv("NX_T1_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")
        return {}, "HttpClient"
    if path_id == "addr_file":
        from nexus.session import write_t1_addr
        write_t1_addr(33333, "127.0.0.1", 6666)
        monkeypatch.setattr(
            "nexus.db.t1.find_immediate_claude_pid",
            lambda start_pid=None: 33333,
        )
        return {}, "HttpClient"
    if path_id == "isolation":
        monkeypatch.setenv("NX_T1_ISOLATED", "1")
        monkeypatch.setattr(
            "nexus.db.t1.find_immediate_claude_pid",
            lambda start_pid=None: 0,
        )
        return {}, "EphemeralClient"
    if path_id == "client_injection":
        return {"client": fake_chromadb.EphemeralClient.return_value}, None
    raise ValueError(path_id)


def _write_current_session(tmp_path, sid: str) -> None:
    (tmp_path / "current_session").write_text(sid)


@pytest.fixture
def fake_chromadb(monkeypatch):
    from unittest.mock import MagicMock
    fake = MagicMock()
    fake.HttpClient.return_value = MagicMock()
    fake.EphemeralClient.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "chromadb", fake)
    return fake


class TestT1DatabaseSessionIdResolution:
    """nexus-h8ge: session_id MUST follow the same four-step chain in
    every branch (Path A/B/C + client-injection).

    The chain is:
        ctor arg > NX_SESSION_ID env > read_claude_session_id() > uuid4()

    Pre-fix the ``read_claude_session_id()`` step was missing from every
    branch, so two ``T1Database()`` calls in the same Claude session
    minted distinct UUIDs and could not see each other's entries via
    the per-entry session_id metadata filter.
    """

    @pytest.mark.parametrize("path_id", _PATH_IDS)
    def test_explicit_arg_wins(self, path_id, tmp_path, monkeypatch, fake_chromadb):
        kwargs, _ = _setup_path(path_id, tmp_path, monkeypatch, fake_chromadb)
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        _write_current_session(tmp_path, "from-file")

        from nexus.db.t1 import T1Database
        db = T1Database(session_id="from-arg", **kwargs)
        assert db.session_id == "from-arg"

    @pytest.mark.parametrize("path_id", _PATH_IDS)
    def test_env_wins_over_current_session_file(
        self, path_id, tmp_path, monkeypatch, fake_chromadb
    ):
        kwargs, _ = _setup_path(path_id, tmp_path, monkeypatch, fake_chromadb)
        monkeypatch.setenv("NX_SESSION_ID", "from-env")
        _write_current_session(tmp_path, "from-file")

        from nexus.db.t1 import T1Database
        db = T1Database(**kwargs)
        assert db.session_id == "from-env"

    @pytest.mark.parametrize("path_id", _PATH_IDS)
    def test_current_session_file_wins_over_uuid_fallback(
        self, path_id, tmp_path, monkeypatch, fake_chromadb
    ):
        """Regression: the missing fallback step.

        With env unset and current_session populated, every branch must
        resolve to the file's contents -- not mint a fresh UUID. This
        is the load-bearing invariant for cross-process T1 visibility:
        the MCP server and a Bash-tool sibling both find the same
        Claude session via the on-disk pointer and converge on its
        UUID, so each side's session_id metadata filter sees the
        other's entries.
        """
        kwargs, _ = _setup_path(path_id, tmp_path, monkeypatch, fake_chromadb)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        _write_current_session(tmp_path, "canonical-claude-uuid")

        from nexus.db.t1 import T1Database
        db = T1Database(**kwargs)
        assert db.session_id == "canonical-claude-uuid"

    @pytest.mark.parametrize("path_id", _PATH_IDS)
    def test_uuid_fallback_when_nothing_set(
        self, path_id, tmp_path, monkeypatch, fake_chromadb
    ):
        """Truly anonymous CLI (no env, no file) still gets a fresh
        UUID -- preserves the pre-fix behaviour for callers running
        outside any Claude session, e.g. ad-hoc scripting against an
        explicit ephemeral.
        """
        kwargs, _ = _setup_path(path_id, tmp_path, monkeypatch, fake_chromadb)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No current_session file written.

        from nexus.db.t1 import T1Database
        db = T1Database(**kwargs)
        # uuid4() strings are 36 chars with four hyphens.
        assert len(db.session_id) == 36
        assert db.session_id.count("-") == 4


@pytest.mark.integration
class TestE2ESessionIdSharedAcrossProcesses:
    """nexus-h8ge: two processes in the same Claude session must
    converge on the canonical session_id via the on-disk pointer.

    Boots a real chroma HTTP server. Spawns two subprocesses, each
    with NEITHER ``NX_SESSION_ID`` nor explicit ctor arg set, both
    pointed at the same ``NEXUS_CONFIG_DIR`` containing a populated
    ``current_session`` file and an addr file naming the chroma. The
    test asserts (a) both ``T1Database().session_id`` resolve to the
    same canonical UUID, and (b) a put from process A is visible from
    a list in process B.

    This is the missing invariant test that 4.27.0 shipped without.
    Pre-fix this test fails because each subprocess mints its own
    UUID and the per-entry session_id metadata filter isolates them.
    """

    def test_two_subprocesses_share_session_id_via_current_session_file(
        self, tmp_path
    ):
        import shutil

        from nexus.session import (
            stop_t1_server,
            unlink_t1_addr,
            write_claude_session_id,
            write_t1_addr,
        )

        host, port, server_pid, chroma_tmpdir = _start_real_chroma()
        own_pid = os.getpid()
        canonical = "11111111-2222-3333-4444-555555555555"
        try:
            # Populate NEXUS_CONFIG_DIR with: current_session pointer +
            # an addr file naming the chroma. NX_SESSION_ID stays UNSET
            # in both subprocesses' env -- they must read the file.
            env_overlay = {"NEXUS_CONFIG_DIR": str(tmp_path)}
            for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_SESSION_ID",
                        "NX_T1_ISOLATED", "NEXUS_SKIP_T1"):
                env_overlay[var] = ""  # blank-out below

            base_env = {
                k: v for k, v in os.environ.items()
                if k not in {"NX_T1_HOST", "NX_T1_PORT", "NX_SESSION_ID",
                             "NX_T1_ISOLATED", "NEXUS_SKIP_T1"}
            }
            base_env["NEXUS_CONFIG_DIR"] = str(tmp_path)

            import nexus.session as _sess
            real_dir_at_import = _sess._nexus_config_dir_at_import
            try:
                # Force the helpers in this test process to use tmp_path.
                _sess._nexus_config_dir_at_import = (  # type: ignore[attr-defined]
                    lambda: tmp_path
                )
                write_claude_session_id(canonical)
                write_t1_addr(own_pid, host, port)
            finally:
                _sess._nexus_config_dir_at_import = real_dir_at_import

            try:
                child_code = (
                    "import os, sys, json\n"
                    "from nexus.db.t1 import T1Database\n"
                    "import nexus.session as _sess\n"
                    "import nexus.db.t1 as _t1\n"
                    f"_sess.find_immediate_claude_pid = lambda start_pid=None: {own_pid}\n"
                    f"_t1.find_immediate_claude_pid = lambda start_pid=None: {own_pid}\n"
                    "action = sys.argv[1]\n"
                    "db = T1Database()\n"
                    "if action == 'put':\n"
                    "    eid = db.put('hello-from-A', tags='shakeout')\n"
                    "    print(json.dumps({'session_id': db.session_id, 'entry_id': eid}))\n"
                    "elif action == 'list':\n"
                    "    items = [e['content'] for e in db.list_entries()]\n"
                    "    print(json.dumps({'session_id': db.session_id, 'items': items}))\n"
                )

                proc_a = subprocess.run(
                    [sys.executable, "-c", child_code, "put"],
                    capture_output=True, text=True, timeout=30, env=base_env,
                )
                assert proc_a.returncode == 0, (
                    f"put-subprocess failed (rc={proc_a.returncode}): "
                    f"stdout={proc_a.stdout!r} stderr={proc_a.stderr!r}"
                )
                a_result = json.loads(proc_a.stdout.strip())

                proc_b = subprocess.run(
                    [sys.executable, "-c", child_code, "list"],
                    capture_output=True, text=True, timeout=30, env=base_env,
                )
                assert proc_b.returncode == 0, (
                    f"list-subprocess failed (rc={proc_b.returncode}): "
                    f"stdout={proc_b.stdout!r} stderr={proc_b.stderr!r}"
                )
                b_result = json.loads(proc_b.stdout.strip())

                # (a) both processes converge on the canonical UUID.
                assert a_result["session_id"] == canonical, a_result
                assert b_result["session_id"] == canonical, b_result
                # (b) put-from-A is visible from list-in-B.
                assert "hello-from-A" in b_result["items"], b_result
            finally:
                unlink_t1_addr(own_pid)
        finally:
            stop_t1_server(server_pid)
            shutil.rmtree(chroma_tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher env builder (share_t1 flag-gated)
# ─────────────────────────────────────────────────────────────────────────────


class TestDispatcherEnvBuilder:
    """``_build_dispatch_env`` decides whether the subprocess inherits
    ``NX_T1_HOST/PORT`` (share_t1=True + flag-on + parent T1 live) or
    falls back to the legacy ``NEXUS_SKIP_T1=1`` ephemeral path."""



    def test_share_t1_passes_env_when_flag_on(self, monkeypatch):
        from nexus.mcp import _t1_state
        from nexus.operators.dispatch import _build_dispatch_env

        prev = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = ("127.0.0.1", 12345)
        try:
            env = _build_dispatch_env(share_t1=True, parent_session_id="parent")
        finally:
            _t1_state.T1_ADDR = prev
        assert env.get("NX_T1_HOST") == "127.0.0.1"
        assert env.get("NX_T1_PORT") == "12345"
        assert "NEXUS_SKIP_T1" not in env
        assert env.get("NX_SESSION_ID") == "parent"

    def test_share_t1_raises_when_t1_addr_unset(self, monkeypatch):
        from nexus.mcp import _t1_state
        from nexus.operators.dispatch import _build_dispatch_env

        prev = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        try:
            with pytest.raises(RuntimeError, match="share_t1"):
                _build_dispatch_env(share_t1=True, parent_session_id=None)
        finally:
            _t1_state.T1_ADDR = prev




class TestDispatcherEphemeralMode:
    """RDR-105 P2.5 / nexus-4gby: third dispatcher mode. ``ephemeral=True``
    sets ``NX_T1_ISOLATED=1`` and strips any inherited host/port; the
    receiving subprocess opens a per-process ``EphemeralClient``.
    """

    def test_ephemeral_sets_isolated_when_flag_on(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")
        env = _build_dispatch_env(ephemeral=True, parent_session_id="parent")
        assert env.get("NX_T1_ISOLATED") == "1"
        assert "NX_T1_HOST" not in env
        assert "NX_T1_PORT" not in env
        assert "NEXUS_SKIP_T1" not in env  # don't leak the deprecated alias
        assert env.get("NX_SESSION_ID") == "parent"



    def test_share_and_ephemeral_mutually_exclusive(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        with pytest.raises(ValueError, match="mutually exclusive"):
            _build_dispatch_env(share_t1=True, ephemeral=True)


class TestDispatcherOwnedMode:
    """Default mode (neither share_t1 nor ephemeral). Subprocess gets
    its own T1 session; parent's NX_T1_HOST/PORT/ISOLATED are stripped
    so the subprocess MCP spawns its own chroma."""

    def test_owned_strips_parent_t1_env(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")
        env = _build_dispatch_env(share_t1=False, ephemeral=False)
        assert "NX_T1_HOST" not in env
        assert "NX_T1_PORT" not in env
        assert "NX_T1_ISOLATED" not in env
        assert "NEXUS_SKIP_T1" not in env


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan: 3-branch new-discovery generator + addr-file publish/cleanup
# ─────────────────────────────────────────────────────────────────────────────


class TestLifespanNewDiscoveryGenerator:
    """RDR-105 P2.4 / nexus-zlus: when flag-on the lifespan dispatches
    to ``_t1_chroma_lifespan_new_discovery`` (a 3-branch
    asynccontextmanager). Branches 1 and 2 do not spawn; Branch 3
    spawns + writes addr file + populates ``_t1_state``.
    """

    def test_branch1_inherited_env_does_not_spawn(self, monkeypatch):
        """``NX_T1_HOST`` + ``NX_T1_PORT`` present -> no spawn, no file."""
        import asyncio

        from nexus.mcp import core as mcp_core

        monkeypatch.setenv("NX_T1_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")

        called = {"start": 0, "write": 0}

        def fake_start():
            called["start"] += 1
            return ("127.0.0.1", 1, 1, "/tmp/x")

        def fake_write(*args, **kwargs):
            called["write"] += 1

        with patch("nexus.session.start_t1_server", side_effect=fake_start), \
             patch("nexus.session.write_t1_addr", side_effect=fake_write):
            async def _run():
                async with mcp_core._t1_chroma_lifespan(None):
                    pass
            asyncio.run(_run())

        assert called["start"] == 0
        assert called["write"] == 0

    def test_branch2_isolated_does_not_spawn(self, monkeypatch):
        """``NX_T1_ISOLATED=1`` -> no spawn, no file."""
        import asyncio

        from nexus.mcp import core as mcp_core

        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        called = {"start": 0, "write": 0}

        with patch("nexus.session.start_t1_server",
                   side_effect=lambda: called.update(start=called["start"] + 1) or ("h", 1, 1, "/t")), \
             patch("nexus.session.write_t1_addr",
                   side_effect=lambda *a, **k: called.update(write=called["write"] + 1)):
            async def _run():
                async with mcp_core._t1_chroma_lifespan(None):
                    pass
            asyncio.run(_run())

        assert called["start"] == 0
        assert called["write"] == 0

    def test_branch3_top_level_spawns_and_publishes(self, tmp_path, monkeypatch):
        """No env, no isolation -> spawn chroma + write addr file +
        populate ``_t1_state.T1_ADDR``. Cleanup unlinks file + resets
        the variable."""
        import asyncio

        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        prev_addr = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        try:
            calls = {"stop": 0}

            def fake_stop(_pid):
                calls["stop"] += 1

            with patch("nexus.session.start_t1_server",
                       return_value=("127.0.0.1", 33333, 99999, str(tmp_path / "chroma_tmpdir"))), \
                 patch("nexus.session.stop_t1_server", side_effect=fake_stop), \
                 patch("nexus.session.find_immediate_claude_pid", return_value=44444):
                async def _run():
                    async with mcp_core._t1_chroma_lifespan(None):
                        # During the body: addr file present + state set
                        assert read_t1_addr_for(44444) == ("127.0.0.1", 33333)
                        assert _t1_state.T1_ADDR == ("127.0.0.1", 33333)
                asyncio.run(_run())

            # After body: cleanup.
            assert read_t1_addr_for(44444) is None
            assert _t1_state.T1_ADDR is None
            assert calls["stop"] == 1
        finally:
            _t1_state.T1_ADDR = prev_addr

    def test_branch3_no_claude_pid_skips_publish_but_keeps_chroma(
        self, tmp_path, monkeypatch
    ):
        """When ``find_immediate_claude_pid`` returns 0, the lifespan
        emits a warning, skips the addr-file write, leaves
        ``_t1_state.T1_ADDR`` unset, and still tears chroma down on
        exit. Documents the unusual-process-parentage edge."""
        import asyncio

        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        prev_addr = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        calls = {"stop": 0}
        try:
            with patch("nexus.session.start_t1_server",
                       return_value=("127.0.0.1", 33333, 99999, str(tmp_path / "chroma_tmpdir"))), \
                 patch("nexus.session.stop_t1_server",
                       side_effect=lambda _p: calls.update(stop=calls["stop"] + 1)), \
                 patch("nexus.session.find_immediate_claude_pid", return_value=0):
                async def _run():
                    async with mcp_core._t1_chroma_lifespan(None):
                        # No addr file, no T1_ADDR, but chroma is running.
                        assert _t1_state.T1_ADDR is None
                        # No file at any pid (we mocked walker to 0).
                        for pid in (0, 1, 100, 99999):
                            assert read_t1_addr_for(pid) is None
                asyncio.run(_run())

            assert calls["stop"] == 1
            assert _t1_state.T1_ADDR is None
        finally:
            _t1_state.T1_ADDR = prev_addr

    def test_sigterm_path_cleans_up_via_owned_chroma(self, tmp_path, monkeypatch):
        """Stdio SIGTERM scenario: lifespan body has populated
        ``_OWNED_CHROMA``; ``_t1_chroma_shutdown`` runs (signal
        handler / atexit) and dispatches to the new-discovery impl
        instead of the legacy one. Idempotent with the lifespan's
        own finally."""
        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        prev_inflight = mcp_core._SHUTDOWN_IN_FLIGHT
        try:
            # Simulate a populated lifespan-body state.
            write_t1_addr(88888, "127.0.0.1", 7777)
            _t1_state.T1_ADDR = ("127.0.0.1", 7777)
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update({
                "server_pid": 12345,
                "tmpdir": str(tmp_path / "chroma_tmpdir"),
                "t1_addr_claude_pid": 88888,
            })
            mcp_core._SHUTDOWN_IN_FLIGHT = False

            calls = {"stop": 0}
            with patch("nexus.session.stop_t1_server",
                       side_effect=lambda _p: calls.update(stop=calls["stop"] + 1)):
                # SIGTERM-equivalent: atexit / signal handler entry.
                mcp_core._t1_chroma_shutdown()

            # Cleanup ran: addr file gone, state reset, chroma stopped.
            assert read_t1_addr_for(88888) is None
            assert _t1_state.T1_ADDR is None
            assert calls["stop"] == 1
            assert not mcp_core._OWNED_CHROMA
            # _SHUTDOWN_IN_FLIGHT set so a second call short-circuits.
            assert mcp_core._SHUTDOWN_IN_FLIGHT is True

            # Second call is a no-op.
            with patch("nexus.session.stop_t1_server",
                       side_effect=lambda _p: calls.update(stop=calls["stop"] + 1)):
                mcp_core._t1_chroma_shutdown()
            assert calls["stop"] == 1  # unchanged
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr
            mcp_core._SHUTDOWN_IN_FLIGHT = prev_inflight

    def test_branch3_chroma_reaped_when_publish_raises(self, tmp_path, monkeypatch):
        """If ``write_t1_addr`` raises, the lifespan's finally still
        reaps chroma (no orphan process). Validates the spawn-then-
        try/finally layout."""
        import asyncio

        from nexus.mcp import _t1_state, core as mcp_core

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        prev_owned = dict(mcp_core._OWNED_CHROMA)
        prev_addr = _t1_state.T1_ADDR
        try:
            calls = {"stop": 0}

            def boom(*args, **kwargs):
                raise OSError("simulated disk-full at addr-file write")

            with patch("nexus.session.start_t1_server",
                       return_value=("127.0.0.1", 4242, 1234, str(tmp_path / "chroma_tmpdir"))), \
                 patch("nexus.session.stop_t1_server",
                       side_effect=lambda _p: calls.update(stop=calls["stop"] + 1)), \
                 patch("nexus.session.find_immediate_claude_pid", return_value=11), \
                 patch("nexus.session.write_t1_addr", side_effect=boom):
                async def _run():
                    async with mcp_core._t1_chroma_lifespan(None):
                        pass

                with pytest.raises(OSError, match="simulated disk-full"):
                    asyncio.run(_run())

            # Chroma was reaped despite the failure.
            assert calls["stop"] == 1
            # State reset.
            assert _t1_state.T1_ADDR is None
            assert not mcp_core._OWNED_CHROMA
        finally:
            mcp_core._OWNED_CHROMA.clear()
            mcp_core._OWNED_CHROMA.update(prev_owned)
            _t1_state.T1_ADDR = prev_addr

    def test_owned_respawn_does_not_clobber_parent_file(self, tmp_path, monkeypatch):
        """RDR-105 RF-6 owned-mode invariant at the lifespan layer.

        Scenario: a top-level Claude (claude_pid=100) is running; its
        MCP wrote ``t1_addr.100``. An owned ``claude -p`` subprocess
        starts (its own claude_pid=200). The owned MCP's lifespan
        spawns its own chroma and writes ``t1_addr.200``.

        Invariant: the owned MCP MUST NOT touch ``t1_addr.100``. If
        ``find_immediate_claude_pid`` accidentally returned 100 (the
        topmost-walk bug RF-6 closes), the owned MCP would clobber
        the parent's file with its own chroma's address.

        This test simulates the scenario and locks the contract at the
        lifespan layer; companion to the unit test on
        ``find_immediate_claude_pid`` itself.
        """
        import asyncio

        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for, write_t1_addr

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        # Pre-existing parent's addr file.
        write_t1_addr(100, "127.0.0.1", 11111)
        parent_before = read_t1_addr_for(100)
        assert parent_before == ("127.0.0.1", 11111)

        prev_addr = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        try:
            with patch("nexus.session.start_t1_server",
                       return_value=("127.0.0.1", 22222, 99999, str(tmp_path / "owned_chroma_tmpdir"))), \
                 patch("nexus.session.stop_t1_server", side_effect=lambda _p: None), \
                 patch("nexus.session.find_immediate_claude_pid", return_value=200), \
                 patch("nexus.session.sweep_orphan_t1_addr_files", return_value=0):
                # The orphan-reaper sweep on lifespan entry would
                # reap the test's fake parent file (pid 100 is not
                # a live process). Patch it off so the RF-6
                # invariant under test is exercised cleanly.
                async def _run():
                    async with mcp_core._t1_chroma_lifespan(None):
                        # Owned MCP wrote its OWN file at claude_pid=200.
                        assert read_t1_addr_for(200) == ("127.0.0.1", 22222)
                        # Parent's file at claude_pid=100 is UNCHANGED.
                        assert read_t1_addr_for(100) == ("127.0.0.1", 11111)
                asyncio.run(_run())

            # After cleanup: owned's file unlinked, parent's still intact.
            assert read_t1_addr_for(200) is None
            assert read_t1_addr_for(100) == ("127.0.0.1", 11111)
        finally:
            _t1_state.T1_ADDR = prev_addr

    def test_branch3_cleanup_runs_on_body_exception(self, tmp_path, monkeypatch):
        """Lifespan must unlink the addr file even when the wrapped
        body raises. ``async finally`` is the relevant primitive."""
        import asyncio

        from nexus.mcp import _t1_state, core as mcp_core
        from nexus.session import read_t1_addr_for

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)

        prev_addr = _t1_state.T1_ADDR
        _t1_state.T1_ADDR = None
        try:
            with patch("nexus.session.start_t1_server",
                       return_value=("127.0.0.1", 33333, 99999, str(tmp_path / "chroma_tmpdir"))), \
                 patch("nexus.session.stop_t1_server", side_effect=lambda _p: None), \
                 patch("nexus.session.find_immediate_claude_pid", return_value=55555):
                async def _run():
                    async with mcp_core._t1_chroma_lifespan(None):
                        raise RuntimeError("body error")

                with pytest.raises(RuntimeError, match="body error"):
                    asyncio.run(_run())

            assert read_t1_addr_for(55555) is None
            assert _t1_state.T1_ADDR is None
        finally:
            _t1_state.T1_ADDR = prev_addr


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan augmentation: addr-file publish + cleanup
# ─────────────────────────────────────────────────────────────────────────────





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


# ─────────────────────────────────────────────────────────────────────────────
# RDR-105 P3 (nexus-xf5r): default-on production behaviour + RF-3 stress
# ─────────────────────────────────────────────────────────────────────────────





@pytest.mark.integration
class TestE2EParallelStress:
    """RDR-105 P3.2 / nexus-1q88: 10-parallel ``claude -p`` shared
    stress test (RF-3 verification).

    Boots a real chroma HTTP server, spawns 10 concurrent Python
    subprocesses each acting as a shared-T1 child (NX_T1_HOST /
    NX_T1_PORT inherited), each writing N entries via
    ``T1Database.put``. Verifies every subprocess exits cleanly and
    chromadb's ``MAX_CONCURRENT_WRITES = 10`` ceiling queues rather
    than drops under load.

    RF-3: the new architecture does not increase chroma load relative
    to the pre-RDR-105 baseline; only the discovery mechanism changed.
    This test makes the empirical claim concrete.
    """

    def test_ten_parallel_shared_subprocesses(self, tmp_path):
        import shutil

        from nexus.session import stop_t1_server

        host, port, server_pid, chroma_tmpdir = _start_real_chroma()
        try:
            n_workers = 10
            entries_per_worker = 5
            child_code = (
                "import os, sys\n"
                "from nexus.db.t1 import T1Database\n"
                "db = T1Database()\n"
                "tag = sys.argv[1]\n"
                f"for i in range({entries_per_worker}):\n"
                "    db.put(f'{tag}-entry-{i}', tags=tag)\n"
                "print('OK', db.session_id)\n"
            )
            base_env = {
                **os.environ,
                "NX_T1_HOST": host,
                "NX_T1_PORT": str(port),
                "NEXUS_CONFIG_DIR": str(tmp_path),
            }
            base_env.pop("NEXUS_SKIP_T1", None)

            procs = []
            for i in range(n_workers):
                # Distinct NX_SESSION_ID per worker so each gets its
                # own session_id-scoped scratch view, matching real
                # share_t1=True dispatch where every subprocess has
                # its own conversation UUID.
                env_i = {**base_env, "NX_SESSION_ID": f"worker-{i:02d}"}
                p = subprocess.Popen(
                    [sys.executable, "-c", child_code, f"worker-{i:02d}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env_i,
                )
                procs.append(p)

            # Wait for all workers; collect rc + output for diagnostics.
            results = []
            for p in procs:
                stdout, stderr = p.communicate(timeout=60)
                results.append((p.returncode, stdout.decode(), stderr.decode()))

            # Every worker exited cleanly.
            for i, (rc, out, err) in enumerate(results):
                assert rc == 0, (
                    f"worker {i} exited rc={rc}: stdout={out!r} stderr={err!r}"
                )
                assert out.startswith("OK"), out
        finally:
            stop_t1_server(server_pid)
            shutil.rmtree(chroma_tmpdir, ignore_errors=True)
