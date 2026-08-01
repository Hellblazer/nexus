# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-105 hybrid T1 discovery tests (RDR-149 P4 lease model).

The single hybrid-discovery code path is the only T1 resolution surface.
Verifies:

* ``find_immediate_claude_pid`` returns the FIRST ``claude*`` ancestor
  walking up, NOT the topmost (RF-6). Retained for its non-T1 consumer
  (``phase_review_sentinel``); RDR-149 P4 moved T1 off pid keying.
* ``T1Database.__init__`` flag-gated paths: env (Path A), session-id
  lease (Path B), isolation (Path C), fail-loud (Path D).
* MCP lifespan Branch 3 publishes a leased registry record + populates
  ``_t1_state.T1_ADDR``; cleanup relinquishes + resets.
* The cold-start transient-window behavior (CA-3): owner + env-inheritors
  covered; a bare Bash sibling fails loud and retries.
* Dispatcher env builder honours ``share_t1`` + flag.
* End-to-end: subprocess sibling discovers a live chroma via the
  session-id lease (Path B) and via env (Path A).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _discover_t1_endpoint(config_dir, scope_key) -> tuple[str, int] | None:
    """RDR-149 P4 test helper: read the live T1 lease for ``scope_key``
    (a session-id or a transient server_pid) and return ``(host, port)``.

    Replaces the legacy ``read_t1_addr_for(claude_pid)`` probe now that T1
    rides the leased registry instead of the ``host:port`` addr file.
    """
    from nexus.daemon.service_registry import ServiceRegistry

    registry = ServiceRegistry(dir=Path(config_dir), tier="t1")
    record = registry.discover(str(scope_key))
    if record is None:
        return None
    host = record.endpoint.get("host")
    port = record.endpoint.get("port")
    if host is None or port is None:
        return None
    return host, port


def _publish_t1_session_lease(
    config_dir, session_id, host, port, *, server_pid=4242, claude_pid=None
):
    """RDR-149 P4 test helper: publish a T1 lease. With ``session_id`` it is
    session-keyed; with ``session_id=None`` it is a transient record.
    ``claude_pid`` is stamped into the payload of EITHER kind (nexus-gff3g) for
    the ancestor-pid fallback, which serves both the cold-start window and the
    common session-id-divergence case (NX_SESSION_ID != current_session)."""
    from nexus.daemon.service_registry import ServiceRegistry
    from nexus.daemon.t1_lease import T1LeasePublisher

    registry = ServiceRegistry(dir=Path(config_dir), tier="t1")
    publisher = T1LeasePublisher(
        registry=registry,
        server_pid=server_pid,
        host=host,
        port=port,
        version="1.0.0",
        session_resolver=lambda: session_id,
        claude_pid=claude_pid,
    )
    publisher.publish()
    return publisher


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


class TestT1DatabaseFlagOffPreservesLegacyBehaviour:
    """Flag-off: legacy resolver chain runs unchanged."""

    def test_isolated_env_uses_ephemeral_before_discovery(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NX_T1_ISOLATED", "1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        from nexus.db.t1 import T1Database
        db = T1Database()
        assert db.session_id  # constructor succeeded


class TestT1DatabaseFlagOnIsolationPath:
    """Path C (RDR-105 P2 / nexus-mj2o): explicit ``NX_T1_ISOLATED=1``
    opts into the process-scoped ``InMemoryVectorClient`` (RDR-155 P4b
    P0a — previously a per-process ``EphemeralClient``). No HTTP
    discovery attempted. (The legacy ``NEXUS_SKIP_T1`` alias was removed
    at 6.5.2.)
    """

    def test_nx_t1_isolated_uses_inmemory_store(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.inmemory_vector_store import InMemoryVectorClient
        from nexus.db.t1 import T1Database
        db = T1Database()

        fake_chromadb.HttpClient.assert_not_called()
        fake_chromadb.EphemeralClient.assert_not_called()
        assert isinstance(db._client, InMemoryVectorClient)
        assert db.session_id

    def test_isolated_leg_shares_one_client_per_process(self, tmp_path, monkeypatch):
        """Chroma-parity contract (RDR-155 P4b P0a): the legacy
        EphemeralClient leg shared backing state per process (the
        SharedSystemClient settings-hash cache), and the scratch CLI
        journey (put in one invocation, flag/unflag in the next, same
        process) depends on it. Two constructions must see one store."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.t1 import T1Database
        db1 = T1Database()
        db2 = T1Database()
        assert db1._client is db2._client

    def test_legacy_nexus_skip_t1_alias_removed(
        self, tmp_path, monkeypatch
    ):
        """The RF-4 alias was removed at 6.5.2 (promised gone in 5.0):
        ``NEXUS_SKIP_T1=1`` alone no longer selects the ephemeral path —
        with no other discovery signal the constructor fails loud."""
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.EphemeralClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")

        from nexus.db.t1 import T1Database, T1ServerNotFoundError
        with pytest.raises(T1ServerNotFoundError):
            T1Database()

        fake_chromadb.EphemeralClient.assert_not_called()


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
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)
        # No session-id resolves, so the session-id lease path (Path B) is
        # skipped and the constructor fails loud (RDR-149 P4).
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

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
        monkeypatch.delenv("NX_T1_ISOLATED", raising=False)

        from nexus.db.t1 import T1Database, T1ServerNotFoundError
        with pytest.raises(T1ServerNotFoundError):
            T1Database()


class TestT1DatabaseIsolatedOverridesDiscovery:
    """nexus-svpq / GH #593: ``NX_T1_ISOLATED=1`` is an explicit operator
    opt-in and must outrank both env-pair (Path A) and addr-file (Path B)
    auto-discovery. Pre-fix, Path C only fired when Paths A and B both
    missed, so an operator setting the flag from inside a Claude session
    silently wrote into the live MCP-owned T1 instead of a sealed
    in-process store.
    """

    def test_isolated_wins_over_env_pair(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "1111")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.inmemory_vector_store import InMemoryVectorClient
        from nexus.db.t1 import T1Database
        db = T1Database()

        assert isinstance(db._client, InMemoryVectorClient)
        fake_chromadb.HttpClient.assert_not_called()

    def test_isolated_wins_over_addr_file(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        # A session-id lease exists, but isolation (Path C) outranks it.
        _publish_t1_session_lease(tmp_path, "sess-A", "10.0.0.2", 2222)
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")

        from nexus.db.inmemory_vector_store import InMemoryVectorClient
        from nexus.db.t1 import T1Database
        db = T1Database()

        assert isinstance(db._client, InMemoryVectorClient)
        fake_chromadb.HttpClient.assert_not_called()

    def test_isolated_wins_over_env_pair_and_addr_file(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        fake_chromadb.HttpClient.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "1111")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        # Both an env pair and a session-id lease exist; isolation outranks both.
        _publish_t1_session_lease(tmp_path, "sess-A", "10.0.0.3", 3333)
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")

        from nexus.db.inmemory_vector_store import InMemoryVectorClient
        from nexus.db.t1 import T1Database
        db = T1Database()

        assert isinstance(db._client, InMemoryVectorClient)
        fake_chromadb.HttpClient.assert_not_called()

    def test_legacy_skip_t1_alias_no_longer_activates_isolation(self, tmp_path, monkeypatch):
        """Post-removal (6.5.2): a stale ``NEXUS_SKIP_T1=1`` in the ambient
        env is INERT — it does NOT activate the isolation branch, so with
        no NX_T1_ISOLATED the two-branch constructor fails loud (RDR-155
        P4b: the discovery legs the alias used to be inert AGAINST are
        gone; inert now means "does not count as opt-in")."""
        import pytest as _pytest

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")

        from nexus.db.t1 import T1Database, T1ServerNotFoundError
        with _pytest.raises(T1ServerNotFoundError):
            T1Database()


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


_PATH_IDS = ["isolation", "client_injection"]  # RDR-155 P4b: env/addr_file discovery legs retired


def _setup_path(path_id: str, tmp_path, monkeypatch, fake_chromadb):
    """Configure env + monkeypatches so the named branch fires.

    Returns ``(extra_kwargs, expected_client_attr)`` for the
    ``T1Database`` constructor call.

    * ``isolation`` -- Path C (NX_T1_ISOLATED=1).
    * ``client_injection`` -- early branch with explicit client=.

    (RDR-155 P4b: the env / addr_file HttpClient discovery legs retired
    with the chroma T1 server.)
    """
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
        monkeypatch.delenv(var, raising=False)

    if path_id == "isolation":
        monkeypatch.setenv("NX_T1_ISOLATED", "1")
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

    # RDR-149 P4: the ``addr_file`` (session-id lease) path is excluded here.
    # With no session-id resolving, Path B cannot discover a lease, so only
    # the env / isolation / client-injection paths can reach the session_id
    # assignment under test.
    @pytest.mark.parametrize("path_id", ["isolation", "client_injection"])
    def test_unknown_fallback_when_nothing_set(
        self, path_id, tmp_path, monkeypatch, fake_chromadb
    ):
        """Truly anonymous CLI (no env, no file) attributes its T1
        writes to the canonical ``"unknown"`` sentinel.

        Pre-issue-#594 the fallback was ``uuid4()`` -- a per-process
        random string that made T1 writes impossible to correlate with
        the audit log when the on-disk pointer was missing, the exact
        failure mode PR #590 was supposed to close. Issue #594 /
        nexus-9e9a unifies the chain through
        ``nexus.session.resolve_active_session_id`` and uses
        ``"unknown"`` as the per-row last-resort sentinel, so the
        T1 chunk store and the tier-write audit log agree on
        attribution: rows under ``"unknown"`` are exactly the rows
        from processes that did not bind to a Claude session.
        """
        kwargs, _ = _setup_path(path_id, tmp_path, monkeypatch, fake_chromadb)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # No current_session file written.

        from nexus.db.t1 import T1Database
        db = T1Database(**kwargs)
        assert db.session_id == "unknown"


class TestDispatcherEnvBuilder:
    """RDR-155 P4b: ``share_t1=True`` is retired with the chroma T1
    server — it must raise unconditionally, never silently fall back."""

    def test_share_t1_retired_raises(self, monkeypatch):
        from nexus.operators.dispatch import _build_dispatch_env

        with pytest.raises(RuntimeError, match="share_t1"):
            _build_dispatch_env(share_t1=True, parent_session_id="parent")




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
        env = _build_dispatch_env(share_t1=False, ephemeral=False)
        assert "NX_T1_HOST" not in env
        assert "NX_T1_PORT" not in env
        assert "NX_T1_ISOLATED" not in env
