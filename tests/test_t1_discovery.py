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


# NO _discover_t1_endpoint (nexus-8zfwv, 2026-08-07): drove
# ``ServiceRegistry(tier="t1")``, the ``t1_addr.*`` lease format nothing
# publishes any more (T1LeasePublisher retired, deleted at ff744321) --
# and had zero callers even on develop tip (confirmed by grep against
# MERGE_HEAD). T1Database's own discovery gate never had an "addr file"
# leg reading this format either way (RDR-155 P4b already cut
# ``_init_new_discovery`` down before nexus-4lkmz narrowed it further --
# see ``nexus.db.t1.T1Database``), so this helper was exercising nothing.
#
# ``_publish_t1_session_lease`` below SURVIVES the same audit: unlike its
# sibling it has a real caller,
# ``TestT1DatabaseIsolatedLegRetired.test_isolated_hard_fails_even_with_a_live_session_lease``,
# which needs a genuinely live alternate discovery signal to prove
# isolation still outranks it (nexus-4lkmz). It already routes through
# ``ServiceRegistry.publish`` directly rather than the deleted
# ``T1LeasePublisher`` wrapper (nexus-yfh5x).
def _publish_t1_session_lease(
    config_dir, session_id, host, port, *, server_pid=4242, claude_pid=None
):
    """RDR-149 P4 test helper: publish a T1 lease. With ``session_id`` it is
    session-keyed; with ``session_id=None`` it is a transient record.
    ``claude_pid`` is stamped into the payload of EITHER kind (nexus-gff3g) for
    the ancestor-pid fallback, which serves both the cold-start window and the
    common session-id-divergence case (NX_SESSION_ID != current_session).

    nexus-yfh5x: publishes through ``ServiceRegistry.publish`` directly --
    the same call the now-deleted ``T1LeasePublisher.publish`` used to make
    internally -- since that wrapper was retired as dead production code
    (never constructed outside its own test suite)."""
    from nexus.daemon.service_registry import ServiceRegistry, mint_owner_token

    registry = ServiceRegistry(dir=Path(config_dir), tier="t1")
    scope_key = session_id or str(server_pid)
    payload = {"session_id": session_id, "server_pid": server_pid}
    if claude_pid is not None:
        payload["claude_pid"] = claude_pid
    return registry.publish(
        scope_key,
        endpoint={"host": host, "port": port, "server_pid": server_pid},
        version="1.0.0",
        owner_token=mint_owner_token(),
        payload=payload,
    )


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
# find_mcp_sibling_pids (nexus-d76vc: T1 handoff marker ancestry check)
# ─────────────────────────────────────────────────────────────────────────────


class TestFindMcpSiblingPids:
    """The writer-side ancestry check: only nx-mcp/nx-mcp-catalog pids
    whose IMMEDIATE parent is the given claude_pid are ever returned.
    """

    def test_returns_mcp_and_catalog_children_of_claude_pid(self):
        from nexus.session import find_mcp_sibling_pids

        table = [
            (1200, 1, "claude"),
            (1300, 1200, "nx-mcp"),
            (1301, 1200, "nx-mcp-catalog"),
            (1302, 1200, "bash"),  # unrelated sibling: not an mcp command
        ]
        with patch("nexus.session._list_processes", return_value=table):
            result = find_mcp_sibling_pids(1200)
        assert sorted(result) == [1300, 1301]

    def test_excludes_pids_under_a_different_claude_pid(self):
        """Two DISTINCT claude sessions: a marker for one must never be
        computed as reachable from the other's claude_pid (MUST-HOLD
        rn3wo.1 -- a concurrent session's hook must not be able to
        re-point another session's MCP server)."""
        from nexus.session import find_mcp_sibling_pids

        table = [
            (1200, 1, "claude"),
            (1300, 1200, "nx-mcp"),
            (2200, 1, "claude"),  # a DIFFERENT top-level claude process
            (2300, 2200, "nx-mcp"),  # that session's OWN mcp server
        ]
        with patch("nexus.session._list_processes", return_value=table):
            result_for_1200 = find_mcp_sibling_pids(1200)
            result_for_2200 = find_mcp_sibling_pids(2200)
        assert result_for_1200 == [1300]
        assert result_for_2200 == [2300]
        # Foreign pid never leaks across:
        assert 2300 not in result_for_1200
        assert 1300 not in result_for_2200

    def test_no_siblings_returns_empty_list(self):
        from nexus.session import find_mcp_sibling_pids

        table = [(1200, 1, "claude"), (1300, 1200, "bash")]
        with patch("nexus.session._list_processes", return_value=table):
            assert find_mcp_sibling_pids(1200) == []

    def test_nonpositive_claude_pid_returns_empty_without_scanning(self):
        from nexus.session import find_mcp_sibling_pids

        with patch("nexus.session._list_processes") as mock_list:
            assert find_mcp_sibling_pids(0) == []
            assert find_mcp_sibling_pids(-1) == []
        mock_list.assert_not_called()

    def test_process_enumeration_failure_returns_empty_list(self):
        """``_list_processes`` already fails safe (returns []) on any OS
        error -- this pins that find_mcp_sibling_pids propagates that
        fail-safe rather than raising."""
        from nexus.session import find_mcp_sibling_pids

        with patch("nexus.session._list_processes", return_value=[]):
            assert find_mcp_sibling_pids(1200) == []

    def test_matches_full_path_comm_by_basename(self):
        """``ps -o comm=`` can report a full path on some platforms; match
        on the basename the same way ``find_immediate_claude_pid`` does."""
        from nexus.session import find_mcp_sibling_pids

        table = [
            (1200, 1, "claude"),
            (1300, 1200, "/Users/x/.local/bin/nx-mcp"),
        ]
        with patch("nexus.session._list_processes", return_value=table):
            assert find_mcp_sibling_pids(1200) == [1300]

    def test_matches_console_script_launched_via_interpreter(self):
        """THE SHAPE A REAL BOX PRODUCES, and the one every other test in
        this class missed.

        ``nx-mcp`` / ``nx-mcp-catalog`` are ``[project.scripts]`` console
        scripts: shebang wrappers the kernel executes via the interpreter.
        So the process's EXECUTABLE is python and the script name is in
        argv[1]. Matching the executable alone can never see them, which is
        why the nexus-d76vc handoff silently never fired on any real
        machine -- while every fabricated table in this class asserted
        against ``comm="nx-mcp"``, a value ``ps`` cannot report for these
        processes.
        """
        from nexus.session import find_mcp_sibling_pids

        py = "/Users/x/.local/share/uv/tools/conexus/bin/python3"
        table = [
            (1200, 1, "claude --resume 99c368fc"),
            (1300, 1200, f"{py} /Users/x/.local/bin/nx-mcp"),
            (1301, 1200, f"{py} /Users/x/.local/bin/nx-mcp-catalog"),
            (1302, 1200, f"{py} /Users/x/.local/bin/nx-other"),
        ]
        with patch("nexus.session._list_processes", return_value=table):
            assert sorted(find_mcp_sibling_pids(1200)) == [1300, 1301]

    def test_does_not_match_an_incidental_mention_in_a_later_argument(self):
        """Only the executable and the script it runs count. A child that
        merely NAMES nx-mcp in a later argument (an editor, a grep, a
        wrapper) is not an MCP server, and writing a handoff marker for it
        would target an unrelated pid."""
        from nexus.session import find_mcp_sibling_pids

        table = [
            (1200, 1, "claude"),
            (1300, 1200, "/bin/grep -r nx-mcp /Users/x/src"),
            (1301, 1200, "/usr/bin/vim /Users/x/.local/bin/nx-mcp"),
        ]
        with patch("nexus.session._list_processes", return_value=table):
            assert find_mcp_sibling_pids(1200) == []

    def test_against_the_REAL_process_table(self, tmp_path):
        """No fabricated table: spawn an actual child process named
        ``nx-mcp`` and find it through a real ``ps`` enumeration.

        This is the test that would have caught the defect. The mocked
        cases above all encode an assumption about what ``ps`` reports;
        this one asks ``ps``.
        """
        import os
        import subprocess
        import sys
        import time

        from nexus.session import find_mcp_sibling_pids

        script = tmp_path / "nx-mcp"
        script.write_text("import time; time.sleep(30)\n")

        from nexus.session import _list_processes

        me = os.getpid()

        # NON-VACUITY PRECONDITION: if this environment cannot enumerate its
        # own processes, the mechanism under test is unobservable here and a
        # failure below would say nothing about the matcher. Skip LOUDLY on
        # that specific condition only -- never on "the child was not found",
        # which is the actual assertion.
        table = _list_processes()
        if not any(pid == me for pid, _ppid, _cmd in table):
            pytest.skip(
                f"process enumeration cannot see this test's own pid ({me}); "
                f"`ps -eo pid,ppid,args=` returned {len(table)} row(s). The "
                "handoff mechanism cannot work in this environment at all, so "
                "the matcher is untestable here."
            )

        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # ps needs a moment to see a freshly forked child.
            found: list[int] = []
            for _ in range(100):
                found = find_mcp_sibling_pids(me)
                if proc.pid in found:
                    break
                time.sleep(0.1)
            # Diagnose rather than merely fail: report what ps ACTUALLY said
            # about this child. A bare "got []" cost a full CI cycle of
            # guesswork on 2026-08-10 because the message named no cause.
            if proc.pid not in found:
                rows = [
                    f"pid={p} ppid={pp} cmd={c!r}"
                    for p, pp, c in _list_processes()
                    if p == proc.pid or pp == me
                ]
                alive = proc.poll() is None
                raise AssertionError(
                    f"real ps enumeration did not find the live nx-mcp child "
                    f"{proc.pid} parented to {me}; got {found}.\n"
                    f"child still running: {alive} (exit={proc.poll()})\n"
                    f"sys.executable: {sys.executable}\n"
                    f"script: {script}\n"
                    f"ps rows for that child or parented to this test:\n  "
                    + ("\n  ".join(rows) if rows else "(none -- ps reported "
                       "no row for the child AND none parented to this test)")
                )
        finally:
            proc.kill()
            proc.wait(timeout=10)


class TestT1DatabaseFlagOnRaisesOnMisconfiguration:
    """Path D (RDR-105 P2 / nexus-mj2o, narrowed further at nexus-4lkmz):
    no env, no addr file, no client injection -> raise
    ``T1ServerNotFoundError``. Replaces P1's legacy fall-through.
    """

    def test_raises_when_no_source_available(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        fake_chromadb = MagicMock()
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
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

    def test_legacy_nexus_skip_t1_alias_removed(self, tmp_path, monkeypatch):
        """The RF-4 alias was removed at 6.5.2 (promised gone in 5.0):
        ``NEXUS_SKIP_T1=1`` alone no longer selects any path — with no
        other discovery signal the constructor fails loud."""
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


class TestT1DatabaseIsolatedLegRetired:
    """nexus-4lkmz (Hal determination 2026-07-28): the isolated
    in-process leg (``NX_T1_ISOLATED=1`` -> ``InMemoryVectorClient``,
    formerly Path C) is deleted outright — T1 is PG-only, no opt-out.
    Setting the retired var now hard-fails with a named error instead of
    constructing an in-process store, but the CHECKED-FIRST position
    nexus-svpq / GH #593 established survives: it still outranks every
    other discovery signal, including ones that would otherwise succeed.
    """

    def test_isolated_hard_fails_with_no_other_signal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_T1_HOST", raising=False)
        monkeypatch.delenv("NX_T1_PORT", raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.t1 import T1Database, T1IsolatedLegRetiredError
        with pytest.raises(T1IsolatedLegRetiredError, match="NX_T1_ISOLATED"):
            T1Database()

    def test_isolated_hard_fails_even_when_env_pair_present(self, tmp_path, monkeypatch):
        """Isolation is checked FIRST — it must still raise even when a
        (now-inert, chroma-era) env pair is present, never silently
        resolve some OTHER path instead."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "1111")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        from nexus.db.t1 import T1Database, T1IsolatedLegRetiredError
        with pytest.raises(T1IsolatedLegRetiredError):
            T1Database()

    def test_isolated_hard_fails_even_with_a_live_session_lease(self, tmp_path, monkeypatch):
        """Isolation is checked FIRST — it must still raise even when a
        published session-id lease exists that ``get_t1_database()``
        could otherwise resolve to a real ``HttpScratchStore``."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for var in ("NX_T1_HOST", "NX_T1_PORT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NX_T1_ISOLATED", "1")

        _publish_t1_session_lease(tmp_path, "sess-A", "10.0.0.2", 2222)
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")

        from nexus.db.t1 import T1IsolatedLegRetiredError, get_t1_database
        with pytest.raises(T1IsolatedLegRetiredError):
            get_t1_database()

    def test_legacy_skip_t1_alias_does_not_trigger_isolated_retirement(self, tmp_path, monkeypatch):
        """Post-removal (6.5.2): a stale ``NEXUS_SKIP_T1=1`` in the ambient
        env is INERT — it does NOT count as ``NX_T1_ISOLATED``, so with
        the retired var itself unset the constructor fails loud with the
        generic ``T1ServerNotFoundError``, not the isolated-retired one."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NEXUS_SKIP_T1", "1")

        from nexus.db.t1 import T1Database, T1ServerNotFoundError
        with pytest.raises(T1ServerNotFoundError):
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


_PATH_IDS = ["client_injection"]  # RDR-155 P4b: env/addr_file legs retired; nexus-4lkmz: isolation leg retired too


def _setup_path(path_id: str, tmp_path, monkeypatch, fake_chromadb):
    """Configure env + monkeypatches so the named branch fires.

    Returns ``(extra_kwargs, expected_client_attr)`` for the
    ``T1Database`` constructor call.

    * ``client_injection`` -- early branch with explicit client=.

    (RDR-155 P4b: the env / addr_file HttpClient discovery legs retired
    with the chroma T1 server. nexus-4lkmz: the isolation leg —
    formerly a second successful-construction path here — retired too;
    it now hard-fails, see ``TestT1DatabaseIsolatedLegRetired``.)
    """
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for var in ("NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
        monkeypatch.delenv(var, raising=False)

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
    # the client-injection path can reach the session_id assignment under
    # test (nexus-4lkmz: the isolation leg retired).
    @pytest.mark.parametrize("path_id", _PATH_IDS)
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
    """RDR-105 P2.5 / nexus-4gby: third dispatcher mode. nexus-4lkmz
    decision 1 (LOCKED 2026-08-07), blast-radius fix nexus-bjltu (2026-08-07
    review round): ``ephemeral=True`` mints the subprocess its OWN PG-backed
    T1 session (never the parent's, never the retired in-process leg) and
    injects it as ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` — no null-store
    branches — but ONLY when ``grants_tool_access=True`` (the subprocess
    was actually handed MCP tool access that could reach T1). A tool-free
    dispatch (the common case, ``grants_tool_access=False``, the default)
    never mints at all, and a mint failure on a tool-granted dispatch is
    deferred fail-loud (warning + no env injected), never fatal to the
    whole dispatch.
    """

    def test_ephemeral_mints_own_pg_backed_session_when_tool_granted(
        self, monkeypatch
    ):
        from nexus.operators.dispatch import _build_dispatch_env

        minted_calls: list[tuple[str, str]] = []

        def _fake_mint(session_id: str, *, context: str) -> dict:
            minted_calls.append((session_id, context))
            return {"session_token": "minted-tok-xyz", "expires_in_seconds": 3600}

        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", _fake_mint)
        monkeypatch.setenv("NX_T1_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_T1_PORT", "5555")
        monkeypatch.setenv("NX_T1_ISOLATED", "1")
        monkeypatch.setenv("NX_T1_SESSION", "parent-live-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "parent-session-id")

        env = _build_dispatch_env(
            ephemeral=True, parent_session_id="parent", grants_tool_access=True
        )

        assert env.get("NX_T1_SESSION") == "minted-tok-xyz"
        assert env.get("NX_T1_SESSION_ID"), "must mint a session id"
        assert env["NX_T1_SESSION_ID"] != "parent-session-id", (
            "must be the subprocess's OWN minted session, never the parent's"
        )
        assert "NX_T1_HOST" not in env
        assert "NX_T1_PORT" not in env
        assert "NX_T1_ISOLATED" not in env
        # NX_SESSION_ID (the attribution/current-session pointer) still
        # forwards the parent's conversation id -- distinct from T1's own
        # session identity above.
        assert env.get("NX_SESSION_ID") == "parent"
        assert len(minted_calls) == 1
        assert minted_calls[0][0] == env["NX_T1_SESSION_ID"]

    def test_tool_free_dispatch_never_mints(self, monkeypatch):
        """nexus-bjltu: the stateless tool-free default (grants_tool_access
        unset/False) must never mint — nothing in a tool-free subprocess
        can reach T1, so a mint here is pure dead weight and a needless
        single point of failure."""
        from nexus.operators.dispatch import _build_dispatch_env

        mint_calls: list[str] = []

        def _fake_mint(session_id: str, *, context: str) -> dict:
            mint_calls.append(session_id)
            return {"session_token": "should-never-be-used"}

        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", _fake_mint)
        monkeypatch.setenv("NX_T1_SESSION", "parent-live-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "parent-session-id")

        env = _build_dispatch_env(ephemeral=True, parent_session_id="parent")

        assert mint_calls == [], "tool-free dispatch must never mint"
        assert "NX_T1_SESSION" not in env
        assert "NX_T1_SESSION_ID" not in env

    def test_mint_failure_on_tool_granted_dispatch_does_not_raise(
        self, monkeypatch
    ):
        """nexus-bjltu CRITICAL fix: a mint failure must never kill the
        whole dispatch. It logs a warning (carrying the exception) and
        proceeds WITHOUT injecting NX_T1_SESSION/NX_T1_SESSION_ID. This is
        deferred fail-loud, not a null-store branch: no store object is
        fabricated here. (nexus-ylof9: what the subprocess's own nested
        MCP does next is NOT simply "raises T1UnavailableThisProcessError"
        — NX_SESSION_ID is still forwarded, so it usually borrows the
        parent's lease or attempts its own mint; the named error fires
        only when no session id resolves at all. See
        ``_build_dispatch_env``'s docstring for the full breakdown — out
        of scope for this env-shape assertion.)"""
        from structlog.testing import capture_logs

        from nexus.operators.dispatch import _build_dispatch_env

        def _failing_mint(session_id: str, *, context: str) -> dict:
            raise RuntimeError("T1 operator dispatch mint failed for session")

        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", _failing_mint)

        with capture_logs() as cap:
            env = _build_dispatch_env(
                ephemeral=True, parent_session_id="parent", grants_tool_access=True
            )

        assert "NX_T1_SESSION" not in env
        assert "NX_T1_SESSION_ID" not in env
        warnings = [
            e for e in cap
            if e.get("event") == "operator_dispatch_t1_mint_failed"
        ]
        assert warnings, f"must log a warning naming the mint failure: {cap}"
        assert all(e.get("log_level") == "warning" for e in warnings)
        assert "mint failed" in warnings[0].get("error", ""), (
            "the warning must carry the mint exception, not swallow it silently"
        )

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
